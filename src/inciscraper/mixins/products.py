"""Product listing scraping helpers."""

from __future__ import annotations

import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

from ..constants import PROGRESS_LOG_INTERVAL, REQUEST_SLEEP
from ..parser import extract_text, parse_html

LOGGER = logging.getLogger(__name__)


class ProductScraperMixin:
    """Mixin implementing product list scraping."""

    conn: sqlite3.Connection
    max_workers: int

    def scrape_products(
        self,
        *,
        max_brands: int | None = None,
        max_products_per_brand: int | None = None,
        rescan_all: bool = False,
    ) -> None:
        """Discover products for each brand pending product scraping."""

        self._reset_brand_completion_flags_if_products_empty()
        self._retry_incomplete_brand_products()
        if rescan_all:
            cursor = self.conn.execute(
                "SELECT id, name, url FROM brands ORDER BY id",
            )
        else:
            cursor = self.conn.execute(
                "SELECT id, name, url FROM brands WHERE products_scraped = 0 ORDER BY id",
            )
        pending_brands = cursor.fetchall()
        if max_brands is not None:
            pending_brands = pending_brands[:max_brands]
        total_brands = len(pending_brands)
        if total_brands == 0:
            LOGGER.info("No brands require product scraping – skipping stage")
            return
        if rescan_all:
            LOGGER.info("Product workload: revalidating %s brand(s)", total_brands)
        else:
            LOGGER.info("Product workload: %s brand(s) awaiting scraping", total_brands)
        
        # Create progress bar for brand processing
        self.create_progress_bar("brands", total_brands, "Processing brands")
        processed = 0
        # Temporarily disable parallel processing due to SQLite thread safety issues
        # TODO: Implement proper thread-safe database operations
        if False:  # self.max_workers > 1:
            # Use parallel processing for multiple workers
            processed = self._scrape_brands_parallel(
                pending_brands,
                max_products_per_brand=max_products_per_brand,
                total_brands=total_brands,
            )
        else:
            # Use sequential processing for single worker
            for brand in pending_brands:
                brand_id = brand["id"]
                brand_url = brand["url"]
                resume_key = f"brand_products_next_offset:{brand_id}"
                if rescan_all:
                    start_offset = 1
                    self._delete_metadata(resume_key)
                else:
                    start_offset = int(self._get_metadata(resume_key, "1"))
                if start_offset > 1:
                    LOGGER.info(
                        "Resuming product collection for brand %s from offset %s",
                        brand["name"],
                        start_offset,
                    )
                LOGGER.info("Collecting products for brand %s (%s)", brand["name"], brand_url)
                products_found, completed, next_offset = self._collect_products_for_brand(
                    brand_id,
                    brand_url,
                    start_offset=start_offset,
                    max_products=max_products_per_brand,
                    connection=self.conn,
                )
                self.conn.commit()
                if completed:
                    self.conn.execute(
                        "UPDATE brands SET products_scraped = 1 WHERE id = ?",
                        (brand_id,),
                    )
                    self.conn.commit()
                self._delete_metadata(resume_key)
                product_total = self._count_products_for_brand(brand_id, connection=self.conn)
                if product_total == 0:
                    LOGGER.warning(
                        "Brand %s marked complete but no products recorded – flagging for review",
                        brand["name"],
                    )
                    self._set_metadata(f"brand_empty_products:{brand_id}", "1")
                else:
                    self._delete_metadata(f"brand_empty_products:{brand_id}")
            else:
                self._set_metadata(resume_key, str(next_offset))
                LOGGER.info(
                    "Product collection for brand %s interrupted – will retry from offset %s",
                    brand["name"],
                    next_offset,
                )
                status = "complete" if completed else "incomplete"
                LOGGER.info(
                    "Finished brand %s – %s products recorded (%s)",
                    brand["name"],
                    products_found,
                    status,
                )
                processed += 1
                self.update_progress("brands", 1)
                if processed % PROGRESS_LOG_INTERVAL == 0 or processed == total_brands:
                    self._log_progress("Brand", processed, total_brands)

    def _scrape_brands_parallel(
        self,
        brands: List[Tuple],
        *,
        max_products_per_brand: Optional[int] = None,
        total_brands: int,
    ) -> int:
        """Process brands in parallel using ThreadPoolExecutor."""
        
        processed = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Submit all brand processing tasks
            future_to_brand = {}
            for brand in brands:
                future = executor.submit(
                    self._process_single_brand,
                    brand,
                    max_products_per_brand
                )
                future_to_brand[future] = brand
            
            # Process completed tasks as they finish
            for future in as_completed(future_to_brand):
                brand = future_to_brand[future]
                try:
                    result = future.result()
                    processed += 1
                    self.update_progress("brands", 1)
                    if processed % PROGRESS_LOG_INTERVAL == 0 or processed == total_brands:
                        self._log_progress("Brand", processed, total_brands)
                except Exception as exc:
                    LOGGER.error("Brand %s generated an exception: %s", brand["name"], exc)
        
        return processed

    def _process_single_brand(
        self,
        brand: Tuple,
        max_products_per_brand: Optional[int] = None,
    ) -> None:
        """Process a single brand (for parallel execution)."""
        
        brand_id = brand["id"]
        brand_url = brand["url"]
        brand_name = brand["name"]
        
        LOGGER.info("Processing brand %s", brand_name)
        
        # Check for resume point
        resume_key = f"brand_products_next_offset:{brand_id}"
        start_offset = int(self._get_metadata(resume_key, "1"))
        
        if start_offset > 1:
            LOGGER.info(
                "Resuming product collection for brand %s from offset %s",
                brand_name,
                start_offset,
            )
        
        try:
            # Create thread-safe connection first
            thread_conn = self._get_thread_safe_connection()
            
            products_found, completed, next_offset = self._collect_products_for_brand(
                brand_id,
                brand_url,
                start_offset=start_offset,
                max_products=max_products_per_brand,
                connection=thread_conn,
            )
            
            # Use a separate connection for thread safety
            thread_conn = self._get_thread_safe_connection()
            
            if completed:
                thread_conn.execute(
                    "UPDATE brands SET products_scraped = 1 WHERE id = ?",
                    (brand_id,),
                )
                thread_conn.commit()
                self._delete_metadata(resume_key)
                product_total = thread_conn.execute(
                    "SELECT COUNT(*) FROM products WHERE brand_id = ?", (brand_id,)
                ).fetchone()[0]
                
                if product_total == 0:
                    LOGGER.warning(
                        "Brand %s marked complete but no products recorded – flagging for review",
                        brand_name,
                    )
                    self._set_metadata(f"brand_empty_products:{brand_id}", "1")
                else:
                    self._delete_metadata(f"brand_empty_products:{brand_id}")
            else:
                self._set_metadata(resume_key, str(next_offset))
                LOGGER.info(
                    "Product collection for brand %s interrupted – will retry from offset %s",
                    brand_name,
                    next_offset,
                )
            
            status = "complete" if completed else "incomplete"
            LOGGER.info(
                "Finished brand %s – %s products recorded (%s)",
                brand_name,
                products_found,
                status,
            )
            
            thread_conn.close()
            
        except Exception as exc:
            LOGGER.error("Error processing brand %s: %s", brand_name, exc)
            raise

    def _collect_products_for_brand(
        self,
        brand_id: str,
        brand_url: str,
        *,
        start_offset: int = 1,
        max_products: Optional[int] = None,
        connection: Optional[sqlite3.Connection] = None,
    ) -> Tuple[int, bool, int]:
        """Walk through paginated product listings for a brand."""

        # Use provided connection or fall back to main connection
        conn = connection or self.conn
        
        offset = start_offset
        total = 0
        existing_total = 0
        if max_products is not None:
            existing_total = self._count_products_for_brand(brand_id, connection=conn)
            if existing_total >= max_products:
                LOGGER.debug(
                    "Brand %s already has %s product(s) – skipping due to limit",
                    brand_url,
                    existing_total,
                )
                return 0, True, offset
        fallback_attempted = False
        while True:
            page_url = self._append_offset(brand_url, offset)
            current_url = page_url
            LOGGER.debug("Fetching product listing page %s", current_url)
            html = self._fetch_html(current_url)
            if (
                html is None
                and offset == start_offset == 1
                and not fallback_attempted
                and page_url != brand_url
            ):
                if "?offset=" not in brand_url:
                    LOGGER.debug(
                        "First attempt for %s failed – retrying without offset",
                        brand_url,
                    )
                    fallback_attempted = True
                    current_url = brand_url
                    html = self._fetch_html(current_url)
            if html is None:
                LOGGER.warning("Unable to download product listing %s", current_url)
                return total, False, offset
            products = self._parse_product_list(html)
            if (
                not products
                and offset == start_offset == 1
                and not fallback_attempted
                and page_url != brand_url
            ):
                LOGGER.debug(
                    "First attempt for %s returned no products – retrying without offset",
                    brand_url,
                )
                fallback_attempted = True
                html = self._fetch_html(brand_url)
                if html is None:
                    LOGGER.warning("Unable to download fallback product listing %s", brand_url)
                    return total, False, offset
                products = self._parse_product_list(html)
            if not products:
                LOGGER.debug("No more products found on %s", page_url)
                return total, True, offset
            for name, url in products:
                inserted = self._insert_product(brand_id, name, url, connection=conn)
                if inserted:
                    total += 1
                if (
                    max_products is not None
                    and existing_total + total >= max_products
                ):
                    LOGGER.debug(
                        "Reached product limit (%s) for brand %s",
                        max_products,
                        brand_url,
                    )
                    return total, True, offset
            offset += 1
            time.sleep(REQUEST_SLEEP)

    def _retry_incomplete_brand_products(self) -> None:
        """Requeue brands that were marked complete without stored products."""

        cursor = self.conn.execute(
            """
            SELECT b.id, b.name
            FROM brands b
            LEFT JOIN (
                SELECT brand_id, COUNT(*) AS product_count
                FROM products
                GROUP BY brand_id
            ) p ON p.brand_id = b.id
            WHERE b.products_scraped = 1
              AND IFNULL(p.product_count, 0) = 0
            ORDER BY b.id
            """
        )
        for row in cursor.fetchall():
            marker_key = f"brand_empty_products:{row['id']}"
            if self._get_metadata(marker_key) == "1":
                continue
            LOGGER.info(
                "Brand %s previously marked complete but has no products – scheduling retry",
                row["name"],
            )
            self.conn.execute(
                "UPDATE brands SET products_scraped = 0 WHERE id = ?",
                (row["id"],),
            )
        self.conn.commit()

    def _count_products_for_brand(self, brand_id: str, *, connection: Optional[sqlite3.Connection] = None) -> int:
        """Return how many products have been stored for the brand."""

        conn = connection or self.conn
        cursor = conn.execute(
            "SELECT COUNT(*) FROM products WHERE brand_id = ?",
            (brand_id,),
        )
        return cursor.fetchone()[0]

    def _parse_product_list(self, html: str) -> List[Tuple[str, str]]:
        """Extract product names and URLs from a listing page."""

        root = parse_html(html)
        anchors = []
        for class_name in ("productlist__item", "product-card", "product__item"):
            anchors.extend(root.find_all(tag="a", class_=class_name))
        if not anchors:
            anchors = [
                node
                for node in root.find_all(tag="a")
                if node.get("href", "").startswith("/products/")
            ]
        seen = set()
        products: List[Tuple[str, str]] = []
        for anchor in anchors:
            href = anchor.get("href")
            name = extract_text(anchor)
            if not href or not name:
                continue
            absolute = self._absolute_url(href)
            if absolute in seen:
                continue
            seen.add(absolute)
            products.append((name, absolute))
        return products

    def _insert_product(self, brand_id: str, name: str, url: str, *, connection: Optional[sqlite3.Connection] = None) -> bool:
        """Persist a product, updating its name if it already exists."""

        conn = connection or self.conn
        now = self._current_timestamp()
        row = conn.execute(
            "SELECT id, brand_id, name, last_updated_at FROM products WHERE url = ?",
            (url,),
        ).fetchone()
        if row is None:
            while True:
                product_id = self._generate_id()
                try:
                    conn.execute(
                        """
                        INSERT INTO products (
                            id, brand_id, name, url, details_scraped, last_checked_at, last_updated_at
                        ) VALUES (?, ?, ?, ?, 0, ?, ?)
                        """,
                        (product_id, brand_id, name, url, now, now),
                    )
                except sqlite3.IntegrityError as exc:
                    if "products.id" in str(exc):
                        continue
                    raise
                return True
        updates: Dict[str, object] = {"last_checked_at": now}
        changed = False
        if row["name"] != name:
            updates["name"] = name
            changed = True
        if row["brand_id"] != brand_id:
            updates["brand_id"] = brand_id
            changed = True
        if changed or not row["last_updated_at"]:
            updates["last_updated_at"] = now
        if updates:
            assignments = ", ".join(f"{column} = ?" for column in updates)
            params = list(updates.values()) + [row["id"]]
            conn.execute(
                f"UPDATE products SET {assignments} WHERE id = ?",
                params,
            )
        return False

