"""High level scraping logic for collecting data from INCIDecoder.

The scraper follows three sequential stages that mirror the data hierarchy on
INCIDecoder:

1. **Brands** – iterate over the paginated brand list and persist every brand
   name and URL in the ``brands`` table.
2. **Products** – for each brand, walk through the paginated product listing and
   store every product in the ``products`` table.
3. **Product details** – visit each product page, capture the structured
   information (description, ingredients, highlights, etc.), download the lead
   image and make sure ingredient level data is stored in the ``ingredients``
   table.  The many-to-many relationship between products and ingredients is
   represented in ``product_ingredients``.

Each stage is idempotent: the scraper relies on ``UNIQUE`` constraints in the
SQLite schema and a set of boolean status flags so that interrupted runs can be
resumed safely.
"""
from __future__ import annotations

import http.client
import json
import logging
import os
import re
import socket
import sqlite3
import ssl
import time
from io import BytesIO
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib import error, parse, request

try:
    from PIL import Image, ImageFile
except ModuleNotFoundError:  # pragma: no cover - optional dependency safeguard
    Image = None  # type: ignore[assignment]
    ImageFile = None  # type: ignore[assignment]

from .parser import Node, extract_text, parse_html

LOGGER = logging.getLogger(__name__)

BASE_URL = "https://incidecoder.com"
USER_AGENT = "INCIScraper/1.0 (+https://incidecoder.com)"
DEFAULT_TIMEOUT = 30
REQUEST_SLEEP = 0.5  # polite delay between HTTP requests
PROGRESS_LOG_INTERVAL = 10

EXPECTED_SCHEMA: Dict[str, Set[str]] = {
    "brands": {"id", "name", "url", "products_scraped"},
    "products": {
        "id",
        "brand_id",
        "name",
        "url",
        "description",
        "image_path",
        "ingredient_ids_json",
        "ingredient_functions_json",
        "highlights_json",
        "discontinued",
        "replacement_product_url",
        "details_scraped",
    },
    "ingredients": {
        "id",
        "name",
        "url",
        "rating_tag",
        "also_called",
        "function_ids_json",
        "irritancy",
        "comedogenicity",
        "details_text",
        "cosing_all_functions",
        "cosing_description",
        "cosing_cas",
        "cosing_ec",
        "cosing_chemical_name",
        "cosing_restrictions",
    },
    "ingredient_functions": {
        "id",
        "name",
        "url",
        "description",
    },
    "product_ingredients": {
        "product_id",
        "ingredient_id",
        "tooltip_text",
        "tooltip_ingredient_link",
    },
    "metadata": {"key", "value"},
}


@dataclass
class IngredientReference:
    name: str
    url: str
    tooltip_text: Optional[str]
    tooltip_ingredient_link: Optional[str]
    ingredient_id: Optional[int] = None


@dataclass
class IngredientFunction:
    ingredient_name: str
    ingredient_page: Optional[str]
    what_it_does: List[str]
    function_links: List[str]


@dataclass
class HighlightEntry:
    function_name: Optional[str]
    function_link: Optional[str]
    ingredient_name: Optional[str]
    ingredient_page: Optional[str]


@dataclass
class ProductHighlights:
    hashtags: List[str]
    key_ingredients: List[HighlightEntry]
    other_ingredients: List[HighlightEntry]


@dataclass
class ProductDetails:
    name: str
    description: str
    image_url: Optional[str]
    ingredients: List[IngredientReference]
    ingredient_functions: List[IngredientFunction]
    highlights: ProductHighlights
    discontinued: bool
    replacement_product_url: Optional[str]


@dataclass
class IngredientDetails:
    name: str
    url: str
    rating_tag: str
    also_called: str
    functions: List["IngredientFunctionInfo"]
    irritancy: str
    comedogenicity: str
    details_text: str
    cosing_all_functions: str
    cosing_description: str
    cosing_cas: str
    cosing_ec: str
    cosing_chemical_name: str
    cosing_restrictions: str


@dataclass
class IngredientFunctionInfo:
    name: str
    url: Optional[str]
    description: str


class INCIScraper:
    """Main entry point that orchestrates all scraping steps."""

    def __init__(
        self,
        *,
        db_path: str = "incidecoder.db",
        image_dir: str | os.PathLike[str] = "images",
        base_url: str = BASE_URL,
        request_timeout: int = DEFAULT_TIMEOUT,
        alternate_base_urls: Optional[Iterable[str]] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = request_timeout
        self.image_dir = Path(image_dir)
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_db()
        self._function_description_cache: Dict[str, str] = {}
        self._host_failover: Dict[str, str] = {}
        self._host_ip_overrides: Dict[str, str] = {}
        self._host_alternatives = self._build_host_alternatives(
            self.base_url, alternate_base_urls or []
        )
        self._ssl_context = ssl.create_default_context()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def run(self) -> None:
        """Execute the full scraping pipeline."""

        LOGGER.info("Starting brand collection")
        self.scrape_brands()
        LOGGER.info("Starting product collection")
        self.scrape_products()
        LOGGER.info("Starting product detail collection")
        self.scrape_product_details()

    def resume_incomplete_metadata(self) -> None:
        """Complete any partial work recorded in the metadata table."""

        resumed_anything = False

        if self._metadata_has_incomplete_brands():
            LOGGER.info(
                "Metadata indicates interrupted brand collection – resuming before continuing",
            )
            self.scrape_brands()
            resumed_anything = True

        pending_product_resumes = self._count_metadata_with_prefix(
            "brand_products_next_offset:"
        )
        if pending_product_resumes:
            LOGGER.info(
                "Metadata indicates interrupted product collection for %s brand(s) – resuming before continuing",
                pending_product_resumes,
            )
            self.scrape_products()
            resumed_anything = True

        if resumed_anything:
            LOGGER.info("Metadata recovery completed")

    def scrape_brands(
        self,
        *,
        reset_offset: bool = False,
        max_pages: int | None = None,
    ) -> None:
        if reset_offset:
            self._set_metadata("brands_next_offset", "1")
        start_offset = int(self._get_metadata("brands_next_offset", "1"))
        if start_offset > 1:
            LOGGER.info("Resuming brand collection from offset %s", start_offset)
        total_offsets_known = int(self._get_metadata("brands_total_offsets", "0") or 0)
        planned_pages = 0
        if total_offsets_known:
            planned_pages = max(total_offsets_known - start_offset + 1, 0)
            if planned_pages <= 0 and not reset_offset:
                LOGGER.info(
                    "Brand workload already complete according to metadata (total offsets: %s)",
                    total_offsets_known,
                )
                return
            LOGGER.info(
                "Brand workload: %s/%s page offsets remaining",
                planned_pages if planned_pages > 0 else 0,
                total_offsets_known,
            )
        else:
            LOGGER.info(
                "Brand workload: unknown total page count – metadata not yet populated"
            )
        self._set_metadata("brands_complete", "0")
        offset = start_offset
        processed_pages = 0
        estimated_total_offsets = total_offsets_known if total_offsets_known else 0
        completed_normally = False
        while True:
            page_url = f"{self.base_url}/brands?offset={offset}"
            LOGGER.info("Fetching brand page %s", page_url)
            html = self._fetch_html(page_url)
            if html is None:
                LOGGER.warning("Unable to download brand page %s", page_url)
                break
            brands = self._parse_brand_list(html)
            if not brands:
                LOGGER.info("No brands found on page %s – stopping", page_url)
                self._set_metadata("brands_complete", "1")
                self._set_metadata("brands_next_offset", "1")
                completed_normally = True
                break
            new_entries = 0
            for name, url in brands:
                new_entries += self._insert_brand(name, url)
            LOGGER.info("Stored %s brands from %s", new_entries, page_url)
            self.conn.commit()
            self._set_metadata("brands_next_offset", str(offset + 1))
            processed_pages += 1
            estimated_total_offsets = max(estimated_total_offsets, offset)
            if processed_pages % PROGRESS_LOG_INTERVAL == 0:
                total_for_log = planned_pages if planned_pages else 0
                if total_for_log and processed_pages > total_for_log:
                    total_for_log = processed_pages
                extra = f"last offset={offset}"
                if not total_offsets_known:
                    extra = f"{extra}; total unknown"
                self._log_progress(
                    "Brand page",
                    processed_pages,
                    total_for_log,
                    extra=extra,
                )
            offset += 1
            if max_pages is not None and processed_pages >= max_pages:
                LOGGER.info(
                    "Reached max pages limit (%s) – stopping brand scraping early",
                    max_pages,
                )
                break
            time.sleep(REQUEST_SLEEP)
        final_total = max(estimated_total_offsets, offset - 1)
        if completed_normally:
            self._set_metadata("brands_total_offsets", str(final_total))
        else:
            self._set_metadata("brands_total_offsets", "0")
        if processed_pages:
            total_for_log = planned_pages if planned_pages else 0
            if not total_for_log and final_total >= start_offset:
                total_for_log = final_total - start_offset + 1
            if total_for_log and processed_pages > total_for_log:
                total_for_log = processed_pages
            extra = f"last offset={offset - 1}"
            if not total_offsets_known:
                extra = f"{extra}; total unknown"
            self._log_progress(
                "Brand page",
                processed_pages,
                total_for_log,
                extra=extra,
            )

    def scrape_products(self) -> None:
        self._retry_incomplete_brand_products()
        cursor = self.conn.execute(
            "SELECT id, name, url FROM brands WHERE products_scraped = 0 ORDER BY id"
        )
        pending_brands = cursor.fetchall()
        total_brands = len(pending_brands)
        if total_brands == 0:
            LOGGER.info("No brands require product scraping – skipping stage")
            return
        LOGGER.info("Product workload: %s brand(s) awaiting scraping", total_brands)
        processed = 0
        for brand in pending_brands:
            brand_id = brand["id"]
            brand_url = brand["url"]
            resume_key = f"brand_products_next_offset:{brand_id}"
            start_offset = int(self._get_metadata(resume_key, "1"))
            if start_offset > 1:
                LOGGER.info(
                    "Resuming product collection for brand %s from offset %s",
                    brand["name"],
                    start_offset,
                )
            LOGGER.info("Collecting products for brand %s (%s)", brand["name"], brand_url)
            products_found, completed, next_offset = self._collect_products_for_brand(
                brand_id, brand_url, start_offset=start_offset
            )
            self.conn.commit()
            if completed:
                self.conn.execute(
                    "UPDATE brands SET products_scraped = 1 WHERE id = ?",
                    (brand_id,),
                )
                self.conn.commit()
                self._delete_metadata(resume_key)
                product_total = self._count_products_for_brand(brand_id)
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
            if processed % PROGRESS_LOG_INTERVAL == 0 or processed == total_brands:
                self._log_progress("Brand", processed, total_brands)

    def scrape_product_details(self) -> None:
        cursor = self.conn.execute(
            "SELECT id, brand_id, name, url FROM products WHERE details_scraped = 0 ORDER BY id"
        )
        pending_products = cursor.fetchall()
        total_products = len(pending_products)
        if total_products == 0:
            LOGGER.info("No products require detail scraping – skipping stage")
            return
        LOGGER.info("Detail workload: %s product(s) awaiting scraping", total_products)
        processed = 0
        for product in pending_products:
            LOGGER.info("Fetching product details for %s", product["url"])
            html = self._fetch_html(product["url"])
            if html is None:
                LOGGER.warning("Skipping product %s due to download error", product["url"])
                continue
            details = self._parse_product_page(html)
            if not details:
                LOGGER.warning("Could not parse product page %s", product["url"])
                continue
            image_path = self._download_product_image(details.image_url, details.name, product["id"])
            self._store_product_details(product["id"], details, image_path)
            self.conn.execute(
                "UPDATE products SET details_scraped = 1 WHERE id = ?",
                (product["id"],),
            )
            self.conn.commit()
            LOGGER.info("Stored product details for %s", details.name)
            processed += 1
            if processed % PROGRESS_LOG_INTERVAL == 0 or processed == total_products:
                self._log_progress("Product", processed, total_products)

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------
    # Database initialisation
    # ------------------------------------------------------------------
    def _init_db(self) -> None:
        cursor = self.conn.cursor()
        self._enforce_schema()
        cursor.executescript(
            """
            CREATE TABLE IF NOT EXISTS brands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                products_scraped INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brand_id INTEGER NOT NULL REFERENCES brands(id),
                name TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                description TEXT,
                image_path TEXT,
                ingredient_ids_json TEXT,
                ingredient_functions_json TEXT,
                highlights_json TEXT,
                discontinued INTEGER NOT NULL DEFAULT 0,
                replacement_product_url TEXT,
                details_scraped INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS ingredients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                rating_tag TEXT,
                also_called TEXT,
                function_ids_json TEXT,
                irritancy TEXT,
                comedogenicity TEXT,
                details_text TEXT,
                cosing_all_functions TEXT,
                cosing_description TEXT,
                cosing_cas TEXT,
                cosing_ec TEXT,
                cosing_chemical_name TEXT,
                cosing_restrictions TEXT
            );

            CREATE TABLE IF NOT EXISTS ingredient_functions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT UNIQUE,
                description TEXT
            );

            CREATE TABLE IF NOT EXISTS product_ingredients (
                product_id INTEGER NOT NULL REFERENCES products(id),
                ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
                tooltip_text TEXT,
                tooltip_ingredient_link TEXT,
                PRIMARY KEY (product_id, ingredient_id)
            );

            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # Metadata helpers
    # ------------------------------------------------------------------
    def _get_metadata(self, key: str, default: Optional[str] = None) -> Optional[str]:
        cursor = self.conn.execute("SELECT value FROM metadata WHERE key = ?", (key,))
        row = cursor.fetchone()
        if row is None:
            return default
        return row["value"]

    def _set_metadata(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO metadata (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self.conn.commit()

    def _delete_metadata(self, key: str) -> None:
        self.conn.execute("DELETE FROM metadata WHERE key = ?", (key,))
        self.conn.commit()

    def _count_metadata_with_prefix(self, prefix: str) -> int:
        cursor = self.conn.execute(
            "SELECT COUNT(*) AS total FROM metadata WHERE key LIKE ?",
            (f"{prefix}%",),
        )
        row = cursor.fetchone()
        return int(row["total"]) if row else 0

    def _metadata_has_incomplete_brands(self) -> bool:
        if self._get_metadata("brands_complete") == "0":
            return True
        next_offset = self._get_metadata("brands_next_offset")
        if next_offset and next_offset not in {"", "1"}:
            return True
        return False

    def _enforce_schema(self) -> None:
        cursor = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        existing_tables = {row["name"] for row in cursor.fetchall()}
        dropped_tables: Set[str] = set()
        for table in existing_tables:
            if table.startswith("sqlite_"):
                continue
            if table not in EXPECTED_SCHEMA:
                LOGGER.info("Dropping unexpected table: %s", table)
                self.conn.execute(f"DROP TABLE IF EXISTS {table}")
                dropped_tables.add(table)
        for table, expected_columns in EXPECTED_SCHEMA.items():
            cursor = self.conn.execute(f"PRAGMA table_info({table})")
            rows = cursor.fetchall()
            if not rows:
                continue
            actual_columns = {row["name"] for row in rows}
            if actual_columns != expected_columns:
                LOGGER.info(
                    "Recreating table %s due to schema mismatch (expected: %s, found: %s)",
                    table,
                    sorted(expected_columns),
                    sorted(actual_columns),
                )
                self.conn.execute(f"DROP TABLE IF EXISTS {table}")
                dropped_tables.add(table)
        self.conn.commit()
        if dropped_tables:
            cursor = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            remaining_tables = {row["name"] for row in cursor.fetchall()}
            self._reset_progress_after_schema_changes(dropped_tables, remaining_tables)

    def _reset_progress_after_schema_changes(
        self, dropped_tables: Set[str], remaining_tables: Set[str]
    ) -> None:
        metadata_available = "metadata" in remaining_tables
        brands_available = "brands" in remaining_tables
        products_available = "products" in remaining_tables

        if "products" in dropped_tables:
            if brands_available:
                LOGGER.info(
                    "Resetting products_scraped flags after products table rebuild",
                )
                self.conn.execute("UPDATE brands SET products_scraped = 0")
            if metadata_available:
                LOGGER.info(
                    "Clearing product progress metadata after products table rebuild",
                )
                self.conn.execute(
                    "DELETE FROM metadata WHERE key LIKE 'brand_products_next_offset:%'"
                )
                self.conn.execute(
                    "DELETE FROM metadata WHERE key LIKE 'brand_empty_products:%'"
                )

        detail_tables = {
            "ingredients",
            "product_ingredients",
            "ingredient_functions",
        }
        if detail_tables & dropped_tables and products_available:
            LOGGER.info(
                "Resetting product detail flags after ingredient table rebuild",
            )
            self.conn.execute("UPDATE products SET details_scraped = 0")

        if dropped_tables:
            self.conn.commit()

    # ------------------------------------------------------------------
    # Workload inspection helpers
    # ------------------------------------------------------------------
    def has_brand_work(self) -> bool:
        return self._get_metadata("brands_complete") != "1"

    def has_product_work(self) -> bool:
        cursor = self.conn.execute(
            "SELECT 1 FROM brands WHERE products_scraped = 0 LIMIT 1"
        )
        return cursor.fetchone() is not None

    def has_product_detail_work(self) -> bool:
        cursor = self.conn.execute(
            "SELECT 1 FROM products WHERE details_scraped = 0 LIMIT 1"
        )
        return cursor.fetchone() is not None

    @staticmethod
    def _log_progress(stage: str, processed: int, total: int, *, extra: str | None = None) -> None:
        if total > 0:
            percent = (processed / total) * 100
            message = f"{stage} progress: {processed}/{total} ({percent:.1f}%)"
        else:
            message = f"{stage} progress: processed {processed} item(s)"
        if extra:
            message = f"{message} – {extra}"
        LOGGER.info(message)

    def get_workload_summary(self) -> Dict[str, Optional[int]]:
        brand_count = self.conn.execute("SELECT COUNT(*) FROM brands").fetchone()[0]
        product_count = self.conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        brands_pending_products = self.conn.execute(
            "SELECT COUNT(*) FROM brands WHERE products_scraped = 0"
        ).fetchone()[0]
        products_pending_details = self.conn.execute(
            "SELECT COUNT(*) FROM products WHERE details_scraped = 0"
        ).fetchone()[0]
        start_offset = int(self._get_metadata("brands_next_offset", "1"))
        total_offsets_known = int(self._get_metadata("brands_total_offsets", "0") or 0)
        brand_pages_remaining: Optional[int]
        if total_offsets_known:
            brand_pages_remaining = max(total_offsets_known - start_offset + 1, 0)
        elif self._get_metadata("brands_complete") == "1":
            brand_pages_remaining = 0
        else:
            brand_pages_remaining = None
        return {
            "brand_pages_remaining": brand_pages_remaining,
            "brands_pending_products": brands_pending_products,
            "products_pending_details": products_pending_details,
            "brands_total": brand_count,
            "products_total": product_count,
        }

    # ------------------------------------------------------------------
    # Brand scraping helpers
    # ------------------------------------------------------------------
    def _parse_brand_list(self, html: str) -> List[Tuple[str, str]]:
        root = parse_html(html)
        anchors: List[Node] = []
        for class_name in ("brand__item", "brand-card", "brandlist__item"):
            anchors.extend(root.find_all(tag="a", class_=class_name))
        if not anchors:
            anchors = [
                node
                for node in root.find_all(tag="a")
                if node.get("href", "").startswith("/brands/")
                and "?offset=" not in node.get("href", "")
            ]
        seen = set()
        brands: List[Tuple[str, str]] = []
        for anchor in anchors:
            href = anchor.get("href")
            name = extract_text(anchor)
            if not href or not name:
                continue
            absolute = self._absolute_url(href)
            if absolute in seen:
                continue
            seen.add(absolute)
            brands.append((name, absolute))
        return brands

    def _insert_brand(self, name: str, url: str) -> int:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO brands (name, url) VALUES (?, ?)",
            (name, url),
        )
        if cur.rowcount == 0:
            # Update the brand name in case it changed on the website
            self.conn.execute("UPDATE brands SET name = ? WHERE url = ?", (name, url))
            return 0
        return 1

    def _collect_products_for_brand(
        self, brand_id: int, brand_url: str, *, start_offset: int = 1
    ) -> Tuple[int, bool, int]:
        offset = start_offset
        total = 0
        fallback_attempted = False
        while True:
            page_url = self._append_offset(brand_url, offset)
            current_url = page_url
            LOGGER.debug("Fetching product listing page %s", current_url)
            html = self._fetch_html(current_url)
            if html is None and offset == start_offset == 1 and not fallback_attempted:
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
                and "?offset=" not in brand_url
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
                total += self._insert_product(brand_id, name, url)
            offset += 1
            time.sleep(REQUEST_SLEEP)

    def _retry_incomplete_brand_products(self) -> None:
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

    def _count_products_for_brand(self, brand_id: int) -> int:
        cursor = self.conn.execute(
            "SELECT COUNT(*) FROM products WHERE brand_id = ?",
            (brand_id,),
        )
        return cursor.fetchone()[0]

    def _parse_product_list(self, html: str) -> List[Tuple[str, str]]:
        root = parse_html(html)
        anchors: List[Node] = []
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

    def _insert_product(self, brand_id: int, name: str, url: str) -> int:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO products (brand_id, name, url) VALUES (?, ?, ?)",
            (brand_id, name, url),
        )
        if cur.rowcount == 0:
            self.conn.execute("UPDATE products SET name = ? WHERE url = ?", (name, url))
            return 0
        return 1

    # ------------------------------------------------------------------
    # Product detail parsing
    # ------------------------------------------------------------------
    def _parse_product_page(self, html: str) -> Optional[ProductDetails]:
        root = parse_html(html)
        product_block = root.find(class_="detailpage") or root
        name_node = product_block.find(id_="product-title") or root.find(id_="product-title")
        description_node = product_block.find(id_="product-details") or root.find(id_="product-details")
        if not name_node:
            return None
        name = extract_text(name_node)
        description = extract_text(description_node) if description_node else ""
        image_url = self._extract_product_image(product_block)
        tooltip_map = self._build_tooltip_index(root)
        ingredients = self._extract_ingredients(product_block, tooltip_map)
        ingredient_functions = self._extract_ingredient_functions(root)
        highlights = self._extract_highlights(root)
        discontinued = False
        replacement_url = None
        alert_node = root.find(class_="topalert")
        if alert_node:
            alert_text = extract_text(alert_node).lower()
            if "discontinued" in alert_text:
                discontinued = True
                replacement_anchor = alert_node.find(
                    tag="a",
                    predicate=lambda n: n.get("href", "").startswith("/products/"),
                )
                if replacement_anchor and replacement_anchor.get("href"):
                    replacement_url = self._absolute_url(replacement_anchor.get("href"))
        return ProductDetails(
            name=name,
            description=description,
            image_url=image_url,
            ingredients=ingredients,
            ingredient_functions=ingredient_functions,
            highlights=highlights,
            discontinued=discontinued,
            replacement_product_url=replacement_url,
        )

    def _extract_product_image(self, product_block: Node) -> Optional[str]:
        for node in product_block.find_all(tag="img"):
            src = node.get("data-src") or node.get("src")
            if not src:
                continue
            absolute = self._absolute_url(src)
            return absolute
        return None

    def _build_tooltip_index(self, root: Node) -> Dict[str, Node]:
        index: Dict[str, Node] = {}
        for node in root.find_all(class_="tooltip_templates"):
            tooltip_id = node.get("id")
            if tooltip_id:
                index[tooltip_id] = node
        return index

    def _extract_ingredients(
        self,
        product_block: Node,
        tooltip_map: Dict[str, Node],
    ) -> List[IngredientReference]:
        container = product_block.find(id_="ingredlist-short")
        if not container:
            container = product_block.find(class_="ingredlist-short")
        if not container:
            return []
        ingredients: List[IngredientReference] = []
        for anchor in container.find_all(tag="a"):
            if "ingred-link" not in anchor.classes():
                continue
            href = anchor.get("href")
            name = extract_text(anchor)
            if not href or not name:
                continue
            tooltip_text = None
            tooltip_link = None
            tooltip_id = None
            tooltip_span = self._find_tooltip_anchor(anchor)
            if tooltip_span:
                tooltip_id_attr = tooltip_span.get("data-tooltip-content")
                if tooltip_id_attr:
                    tooltip_id = tooltip_id_attr.lstrip("#")
            if tooltip_id and tooltip_id in tooltip_map:
                tooltip_node = tooltip_map[tooltip_id]
                tooltip_text = extract_text(tooltip_node)
                link_node = tooltip_node.find(
                    tag="a",
                    predicate=lambda n: n.get("href", "").startswith("/ingredients/")
                )
                if link_node and link_node.get("href"):
                    tooltip_link = self._absolute_url(link_node.get("href"))
            ingredients.append(
                IngredientReference(
                    name=name,
                    url=self._absolute_url(href),
                    tooltip_text=tooltip_text,
                    tooltip_ingredient_link=tooltip_link,
                )
            )
        return ingredients

    def _find_tooltip_anchor(self, node: Node) -> Optional[Node]:
        current = node.parent
        while current is not None:
            tooltip = current.find(class_="info-circle-ingred-short")
            if tooltip:
                return tooltip
            if current.tag == "li" or current.tag == "div":
                break
            current = current.parent
        return node.parent.find(class_="info-circle-ingred-short") if node.parent else None

    def _extract_ingredient_functions(self, root: Node) -> List[IngredientFunction]:
        section = root.find(id_="ingredlist-table-section")
        if not section:
            return []
        rows: List[IngredientFunction] = []
        for tr in section.find_all(tag="tr"):
            cells = [child for child in tr.children if isinstance(child, Node) and child.tag == "td"]
            if len(cells) < 2:
                continue
            ingred_cell, function_cell = cells[:2]
            ingred_anchor = ingred_cell.find(
                tag="a",
                predicate=lambda n: n.get("href", "").startswith("/ingredients/")
            )
            if not ingred_anchor:
                continue
            ingredient_name = extract_text(ingred_anchor)
            ingredient_page = self._absolute_url(ingred_anchor.get("href")) if ingred_anchor.get("href") else None
            what_it_does: List[str] = []
            function_links: List[str] = []
            for anchor in function_cell.find_all(tag="a"):
                if "ingred-function-link" not in anchor.classes():
                    continue
                text = extract_text(anchor)
                href = anchor.get("href")
                if text:
                    what_it_does.append(text)
                if href:
                    function_links.append(self._absolute_url(href))
            rows.append(
                IngredientFunction(
                    ingredient_name=ingredient_name,
                    ingredient_page=ingredient_page,
                    what_it_does=what_it_does,
                    function_links=function_links,
                )
            )
        return rows

    def _extract_highlights(self, root: Node) -> ProductHighlights:
        section = root.find(id_="ingredlist-highlights-section")
        hashtags: List[str] = []
        key_entries: List[HighlightEntry] = []
        other_entries: List[HighlightEntry] = []
        if section:
            for node in section.find_all(tag="span"):
                if node.has_class("hashtag"):
                    text = extract_text(node)
                    if text:
                        hashtags.append(text)
            for block in section.find_all(tag="div"):
                if not block.has_class("ingredlist-by-function-block"):
                    continue
                heading = block.find(tag="h3")
                heading_text = extract_text(heading).lower() if heading else ""
                target_list: Optional[List[HighlightEntry]] = None
                if "key ingredients" in heading_text:
                    target_list = key_entries
                elif "other ingredients" in heading_text:
                    target_list = other_entries
                if target_list is None:
                    continue
                for span in block.find_all(tag="span"):
                    ingred_anchor = span.find(
                        tag="a",
                        predicate=lambda n: "ingred-link" in n.classes(),
                    )
                    if not ingred_anchor:
                        continue
                    func_anchor = span.find(
                        tag="a",
                        predicate=lambda n: "func-link" in n.classes(),
                    )
                    target_list.append(
                        HighlightEntry(
                            function_name=extract_text(func_anchor) if func_anchor else None,
                            function_link=self._absolute_url(func_anchor.get("href"))
                            if func_anchor and func_anchor.get("href")
                            else None,
                            ingredient_name=extract_text(ingred_anchor),
                            ingredient_page=self._absolute_url(ingred_anchor.get("href"))
                            if ingred_anchor.get("href")
                            else None,
                        )
                    )
        return ProductHighlights(
            hashtags=hashtags,
            key_ingredients=key_entries,
            other_ingredients=other_entries,
        )

    # ------------------------------------------------------------------
    # Product detail persistence helpers
    # ------------------------------------------------------------------
    def _store_product_details(
        self,
        product_id: int,
        details: ProductDetails,
        image_path: Optional[str],
    ) -> None:
        self.conn.execute(
            "DELETE FROM product_ingredients WHERE product_id = ?",
            (product_id,),
        )
        ingredient_ids: List[int] = []
        for ingredient in details.ingredients:
            ingredient_id = self._ensure_ingredient(ingredient)
            ingredient.ingredient_id = ingredient_id
            ingredient_ids.append(ingredient_id)
            self.conn.execute(
                """
                INSERT OR REPLACE INTO product_ingredients
                (product_id, ingredient_id, tooltip_text, tooltip_ingredient_link)
                VALUES (?, ?, ?, ?)
                """,
                (
                    product_id,
                    ingredient_id,
                    ingredient.tooltip_text,
                    ingredient.tooltip_ingredient_link,
                ),
            )
        self.conn.execute(
            """
            UPDATE products
            SET name = ?, description = ?, image_path = ?,
                ingredient_ids_json = ?, ingredient_functions_json = ?,
                highlights_json = ?, discontinued = ?,
                replacement_product_url = ?
            WHERE id = ?
            """,
            (
                details.name,
                details.description,
                image_path,
                json.dumps(ingredient_ids, ensure_ascii=False),
                json.dumps(
                    [
                        {
                            "ingredient_name": row.ingredient_name,
                            "ingredient_page": row.ingredient_page,
                            "what_it_does": row.what_it_does,
                            "function_links": row.function_links,
                        }
                        for row in details.ingredient_functions
                    ],
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "hashtags": details.highlights.hashtags,
                        "key_ingredients": [
                            {
                                "function_name": entry.function_name,
                                "function_link": entry.function_link,
                                "ingredient_name": entry.ingredient_name,
                                "ingredient_page": entry.ingredient_page,
                            }
                            for entry in details.highlights.key_ingredients
                        ],
                        "other_ingredients": [
                            {
                                "function_name": entry.function_name,
                                "function_link": entry.function_link,
                                "ingredient_name": entry.ingredient_name,
                                "ingredient_page": entry.ingredient_page,
                            }
                            for entry in details.highlights.other_ingredients
                        ],
                    },
                    ensure_ascii=False,
                ),
                1 if details.discontinued else 0,
                details.replacement_product_url,
                product_id,
            ),
        )

    def _ensure_ingredient(self, ingredient: IngredientReference) -> int:
        row = self.conn.execute(
            "SELECT id FROM ingredients WHERE url = ?",
            (ingredient.url,),
        ).fetchone()
        if row:
            return row["id"]
        try:
            details = self._scrape_ingredient_page(ingredient.url)
        except RuntimeError:
            LOGGER.exception("Failed to scrape ingredient %s", ingredient.url)
            self.conn.execute(
                "INSERT OR IGNORE INTO ingredients (name, url) VALUES (?, ?)",
                (ingredient.name, ingredient.url),
            )
        else:
            ingredient_id = self._store_ingredient_details(details)
            return ingredient_id
        row = self.conn.execute(
            "SELECT id FROM ingredients WHERE url = ?",
            (ingredient.url,),
        ).fetchone()
        if row:
            return row["id"]
        raise RuntimeError(f"Unable to store ingredient {ingredient.url}")

    # ------------------------------------------------------------------
    # Ingredient scraping & persistence
    # ------------------------------------------------------------------
    def _scrape_ingredient_page(self, url: str) -> IngredientDetails:
        LOGGER.info("Fetching ingredient details %s", url)
        html = self._fetch_html(url)
        if html is None:
            raise RuntimeError(f"Unable to download ingredient page {url}")
        return self._parse_ingredient_page(html, url)

    def _parse_ingredient_page(self, html: str, url: str) -> IngredientDetails:
        root = parse_html(html)
        name_node = root.find(tag="h1", class_="klavikab") or root.find(tag="h1")
        rating_node = root.find(class_="ourtake")
        name = extract_text(name_node)
        rating_tag = extract_text(rating_node)
        label_map = self._build_label_map(root)
        also_called_node = label_map.get("also-called-like-this")
        what_it_does_nodes = label_map.get("what-it-does")
        irritancy_node = label_map.get("irritancy")
        comedogenicity_node = label_map.get("comedogenicity")
        functions = self._parse_ingredient_functions(what_it_does_nodes)
        cosing = self._parse_cosing_section(root)
        details_text = self._parse_details_text(root)
        return IngredientDetails(
            name=name,
            url=url,
            rating_tag=rating_tag,
            also_called=extract_text(also_called_node) if also_called_node else "",
            functions=functions,
            irritancy=self._extract_label_text(irritancy_node),
            comedogenicity=self._extract_label_text(comedogenicity_node),
            details_text=details_text,
            cosing_all_functions=cosing["all_functions"],
            cosing_description=cosing["description"],
            cosing_cas=cosing["cas_number"],
            cosing_ec=cosing["ec_number"],
            cosing_chemical_name=cosing["chemical_iupac_name"],
            cosing_restrictions=cosing["cosmetic_restrictions"],
        )

    def _build_label_map(self, root: Node) -> Dict[str, Node]:
        label_map: Dict[str, Node] = {}
        for label in root.find_all(class_="label"):
            text = extract_text(label).lower().strip(":")
            key = text.replace(" ", "-")
            value_node = self._find_value_node(label)
            if value_node:
                label_map[key] = value_node
        return label_map

    def _find_value_node(self, label_node: Node) -> Optional[Node]:
        parent = label_node.parent
        if not parent:
            return None
        for item in parent.content:
            if isinstance(item, Node) and item.has_class("value"):
                return item
        for sibling in label_node.next_siblings():
            if sibling.has_class("value"):
                return sibling
        return None

    def _extract_label_text(self, node: Optional[Node]) -> str:
        if not node:
            return ""
        text = extract_text(node)
        return self._normalize_whitespace(text)

    def _parse_ingredient_functions(
        self, node: Optional[Node]
    ) -> List[IngredientFunctionInfo]:
        if not node:
            return []
        functions: List[IngredientFunctionInfo] = []
        anchors = node.find_all(tag="a")
        if anchors:
            for anchor in anchors:
                name = self._normalize_whitespace(extract_text(anchor))
                href = anchor.get("href")
                url = self._absolute_url(href) if href else None
                description = self._fetch_function_description(url) if url else ""
                if name or url:
                    functions.append(
                        IngredientFunctionInfo(name=name, url=url, description=description)
                    )
        else:
            raw_text = self._normalize_whitespace(extract_text(node))
            if raw_text:
                for part in re.split(r",\s*", raw_text):
                    if part:
                        functions.append(
                            IngredientFunctionInfo(name=part, url=None, description="")
                        )
        return functions

    def _parse_cosing_section(self, root: Node) -> Dict[str, str]:
        section = root.find(id_="cosing-data")
        empty = {
            "all_functions": "",
            "description": "",
            "cas_number": "",
            "ec_number": "",
            "chemical_iupac_name": "",
            "cosmetic_restrictions": "",
        }
        if not section:
            return empty
        key_map = {
            "all functions": "all_functions",
            "description": "description",
            "cas #": "cas_number",
            "ec #": "ec_number",
            "chemical/iupac name": "chemical_iupac_name",
            "cosmetic restrictions": "cosmetic_restrictions",
        }
        values: Dict[str, str] = dict(empty)
        for bold in section.find_all(tag="b"):
            label = extract_text(bold).strip().lower().strip(":")
            key = key_map.get(label)
            if not key:
                continue
            text_parts: List[str] = []
            for item in self._iter_next_content(bold):
                if isinstance(item, Node) and item.tag == "b":
                    break
                if isinstance(item, Node):
                    text_parts.append(extract_text(item))
                elif isinstance(item, str):
                    text_parts.append(item)
            raw_value = " ".join(part.strip() for part in text_parts if part)
            cleaned = self._normalize_whitespace(raw_value.replace("|", " "))
            values[key] = cleaned
        return values

    def _iter_next_content(self, node: Node) -> Iterable:
        parent = node.parent
        if not parent:
            return []
        seen = False
        for item in parent.content:
            if item is node:
                seen = True
                continue
            if not seen:
                continue
            yield item

    def _normalize_whitespace(self, value: str) -> str:
        value = value.strip()
        value = re.sub(r"\s+", " ", value)
        return value

    def _fetch_function_description(self, url: Optional[str]) -> str:
        if not url:
            return ""
        if url in self._function_description_cache:
            return self._function_description_cache[url]
        html = self._fetch_html(url)
        if not html:
            self._function_description_cache[url] = ""
            return ""
        root = parse_html(html)
        content = root.find(id_="content") or root.find(class_="content")
        if content:
            description = self._normalize_whitespace(extract_text(content))
        else:
            description = ""
        self._function_description_cache[url] = description
        return description

    def _parse_details_text(self, root: Node) -> str:
        section = root.find(id_="showmore-section-details") or root.find(id_="details")
        if not section:
            return ""
        content_node = section.find(class_="content") or section
        paragraphs: List[str] = []
        for paragraph in content_node.find_all(tag="p"):
            text = self._normalize_whitespace(extract_text(paragraph))
            if text:
                paragraphs.append(text)
        if not paragraphs:
            text = self._normalize_whitespace(extract_text(content_node))
            return text
        return "\n\n".join(paragraphs)

    def _store_ingredient_details(self, details: IngredientDetails) -> int:
        function_ids: List[int] = []
        for function in details.functions:
            function_id = self._ensure_ingredient_function(function)
            if function_id is not None:
                function_ids.append(function_id)
        self.conn.execute(
            """
            INSERT INTO ingredients (
                name, url, rating_tag, also_called, function_ids_json,
                irritancy, comedogenicity, details_text, cosing_all_functions,
                cosing_description, cosing_cas, cosing_ec, cosing_chemical_name,
                cosing_restrictions
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                name = excluded.name,
                rating_tag = excluded.rating_tag,
                also_called = excluded.also_called,
                function_ids_json = excluded.function_ids_json,
                irritancy = excluded.irritancy,
                comedogenicity = excluded.comedogenicity,
                details_text = excluded.details_text,
                cosing_all_functions = excluded.cosing_all_functions,
                cosing_description = excluded.cosing_description,
                cosing_cas = excluded.cosing_cas,
                cosing_ec = excluded.cosing_ec,
                cosing_chemical_name = excluded.cosing_chemical_name,
                cosing_restrictions = excluded.cosing_restrictions
            """,
            (
                details.name,
                details.url,
                details.rating_tag,
                details.also_called,
                json.dumps(function_ids, ensure_ascii=False),
                details.irritancy,
                details.comedogenicity,
                details.details_text,
                details.cosing_all_functions,
                details.cosing_description,
                details.cosing_cas,
                details.cosing_ec,
                details.cosing_chemical_name,
                details.cosing_restrictions,
            ),
        )
        row = self.conn.execute(
            "SELECT id FROM ingredients WHERE url = ?",
            (details.url,),
        ).fetchone()
        if not row:
            raise RuntimeError(f"Unable to store ingredient {details.url}")
        return row["id"]

    def _ensure_ingredient_function(self, info: IngredientFunctionInfo) -> Optional[int]:
        name = info.name.strip()
        url = info.url
        description = info.description.strip()
        if not name and not url:
            return None
        row = None
        if url:
            row = self.conn.execute(
                "SELECT id, name, description FROM ingredient_functions WHERE url = ?",
                (url,),
            ).fetchone()
        if row is None:
            row = self.conn.execute(
                """
                SELECT id, name, description
                FROM ingredient_functions
                WHERE url IS NULL AND name = ?
                """,
                (name,),
            ).fetchone()
        if row:
            updates: Dict[str, str] = {}
            if name and name != row["name"]:
                updates["name"] = name
            if description and description != (row["description"] or ""):
                updates["description"] = description
            if updates:
                assignments = ", ".join(f"{col} = ?" for col in updates)
                params = list(updates.values()) + [row["id"]]
                self.conn.execute(
                    f"UPDATE ingredient_functions SET {assignments} WHERE id = ?",
                    params,
                )
            return row["id"]
        cur = self.conn.execute(
            """
            INSERT INTO ingredient_functions (name, url, description)
            VALUES (?, ?, ?)
            """,
            (name, url, description),
        )
        return cur.lastrowid

    # ------------------------------------------------------------------
    # Networking helpers
    # ------------------------------------------------------------------
    def _fetch_html(self, url: str, *, attempts: int = 3) -> Optional[str]:
        data = self._fetch(url, attempts=attempts)
        if data is None:
            return None
        return data.decode("utf-8", errors="replace")

    def _fetch(self, url: str, *, attempts: int = 3) -> Optional[bytes]:
        delay = REQUEST_SLEEP
        original_parts = parse.urlsplit(url)
        current_url = self._apply_host_override(url)
        canonical_host = original_parts.hostname
        alternative_hosts = list(self._host_alternatives.get(canonical_host or "", []))
        alt_index = 0

        if canonical_host and canonical_host in self._host_ip_overrides:
            data = self._fetch_via_direct_ip(original_parts, self._host_ip_overrides[canonical_host])
            if data is not None:
                return data

        for attempt in range(1, attempts + 1):
            LOGGER.debug("Downloading %s (attempt %s/%s)", current_url, attempt, attempts)
            req = request.Request(current_url, headers={"User-Agent": USER_AGENT})
            try:
                with request.urlopen(req, timeout=self.timeout) as response:
                    if (
                        canonical_host
                        and canonical_host not in self._host_failover
                        and parse.urlsplit(current_url).hostname != canonical_host
                    ):
                        # Persist the working host so future requests do not repeat the
                        # failing DNS lookup.
                        working_host = parse.urlsplit(current_url).hostname
                        if working_host:
                            self._host_failover[canonical_host] = working_host
                    return response.read()
            except error.URLError as exc:  # pragma: no cover - network errors are hard to simulate
                root_cause = getattr(exc, "reason", None)
                if (
                    canonical_host
                    and isinstance(root_cause, socket.gaierror)
                    and alt_index < len(alternative_hosts)
                ):
                    next_host = alternative_hosts[alt_index]
                    alt_index += 1
                    replacement = self._replace_host(original_parts, next_host)
                    if replacement:
                        LOGGER.warning(
                            "DNS resolution failed for %s – retrying with alternate host %s",
                            current_url,
                            next_host,
                        )
                        self._host_failover[canonical_host] = next_host
                        current_url = replacement
                        time.sleep(delay)
                        delay *= 2
                        continue

                if canonical_host and isinstance(root_cause, socket.gaierror):
                    resolved_ip = self._resolve_host_via_doh(canonical_host)
                    if resolved_ip:
                        LOGGER.warning(
                            "DNS resolution failed for %s – attempting direct IP connection via %s",
                            canonical_host,
                            resolved_ip,
                        )
                        data = self._fetch_via_direct_ip(original_parts, resolved_ip)
                        if data is not None:
                            self._host_ip_overrides[canonical_host] = resolved_ip
                            return data

                if attempt == attempts:
                    LOGGER.error("Failed to download %s: %s", current_url, exc)
                    return None
                LOGGER.warning(
                    "Attempt %s to download %s failed (%s) – retrying",
                    attempt,
                    current_url,
                    exc,
                )
                time.sleep(delay)
                delay *= 2
                current_url = self._apply_host_override(url)

        return None

    def _fetch_via_direct_ip(
        self, parts: parse.SplitResult, ip_address: str
    ) -> Optional[bytes]:
        if parts.scheme != "https":
            return None

        hostname = parts.hostname
        if not hostname:
            return None

        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"

        connection = _DirectHTTPSConnection(
            ip_address,
            server_hostname=hostname,
            timeout=self.timeout,
            context=self._ssl_context,
        )
        try:
            headers = {
                "Host": hostname,
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
                "Connection": "close",
            }
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            if 200 <= response.status < 300:
                return response.read()
            LOGGER.warning(
                "Direct IP request to %s for %s returned HTTP %s",
                ip_address,
                parts.geturl(),
                response.status,
            )
        except (OSError, http.client.HTTPException):
            LOGGER.warning(
                "Direct IP request to %s for %s failed",
                ip_address,
                parts.geturl(),
                exc_info=True,
            )
        finally:
            connection.close()

        return None

    def _apply_host_override(self, url: str) -> str:
        parts = parse.urlsplit(url)
        host = parts.hostname
        if not host:
            return url
        override = self._host_failover.get(host)
        if not override or override == host:
            return url
        replacement = self._replace_host(parts, override)
        return replacement or url

    def _resolve_host_via_doh(self, hostname: str) -> Optional[str]:
        resolver_endpoint = os.environ.get(
            "INCISCRAPER_DOH_ENDPOINT", "https://dns.google/resolve"
        )
        query_params = parse.urlencode({"name": hostname, "type": "A"})
        doh_url = f"{resolver_endpoint}?{query_params}"
        payload = self._download_doh_payload(doh_url)
        if payload is None:
            LOGGER.warning(
                "Failed to resolve %s via DNS-over-HTTPS endpoint %s",
                hostname,
                resolver_endpoint,
            )
            return None

        answers = payload.get("Answer")
        if not answers:
            return None

        for answer in answers:
            if answer.get("type") == 1:
                ip_address = answer.get("data")
                if ip_address:
                    return ip_address
        return None

    def _download_doh_payload(self, doh_url: str) -> Optional[Dict[str, object]]:
        req = request.Request(
            doh_url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/dns-json",
            },
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.URLError as exc:
            root_cause = getattr(exc, "reason", None)
            if isinstance(root_cause, socket.gaierror):
                payload = self._download_doh_payload_via_ip(doh_url)
                if payload is not None:
                    return payload
            LOGGER.debug("Standard DNS lookup for DoH endpoint failed: %s", exc, exc_info=True)
        except Exception:
            LOGGER.debug("Unexpected error querying DoH endpoint", exc_info=True)
        return None

    def _download_doh_payload_via_ip(
        self, doh_url: str
    ) -> Optional[Dict[str, object]]:
        parsed = parse.urlsplit(doh_url)
        hostname = parsed.hostname
        if not hostname:
            return None
        ip_address = self._doh_ip_override().get(hostname)
        if not ip_address:
            return None

        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        connection = _DirectHTTPSConnection(
            ip_address,
            server_hostname=hostname,
            timeout=self.timeout,
            context=self._ssl_context,
        )
        try:
            headers = {
                "Host": hostname,
                "User-Agent": USER_AGENT,
                "Accept": "application/dns-json",
                "Connection": "close",
            }
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            if 200 <= response.status < 300:
                return json.loads(response.read().decode("utf-8"))
            LOGGER.debug(
                "Direct IP DoH request to %s for %s returned HTTP %s",
                ip_address,
                hostname,
                response.status,
            )
        except (OSError, http.client.HTTPException, json.JSONDecodeError):
            LOGGER.debug(
                "Direct IP DoH request to %s for %s failed",
                ip_address,
                hostname,
                exc_info=True,
            )
        finally:
            connection.close()

        return None

    @staticmethod
    def _doh_ip_override() -> Dict[str, str]:
        return {
            "dns.google": "8.8.8.8",
            "dns.google.com": "8.8.8.8",
            "cloudflare-dns.com": "1.1.1.1",
        }

    def _build_host_alternatives(
        self, base_url: str, alternate_base_urls: Iterable[str]
    ) -> Dict[str, List[str]]:
        hosts: List[str] = []

        def _ensure_host(value: Optional[str]) -> None:
            if value and value not in hosts:
                hosts.append(value)

        base_host = parse.urlsplit(base_url).hostname
        _ensure_host(base_host)
        for candidate in alternate_base_urls:
            parsed_host = parse.urlsplit(candidate.rstrip("/")).hostname
            _ensure_host(parsed_host)

        # Ensure common "www" variations are available as fallbacks in both directions.
        for existing in list(hosts):
            if existing.startswith("www."):
                _ensure_host(existing[4:])
            else:
                _ensure_host(f"www.{existing}")

        alternatives: Dict[str, List[str]] = {}
        for host in hosts:
            alt_candidates: List[str] = [h for h in hosts if h != host]

            unique_alts: List[str] = []
            for alt in alt_candidates:
                if alt and alt != host and alt not in unique_alts:
                    unique_alts.append(alt)
            if unique_alts:
                alternatives[host] = unique_alts

        return alternatives

    def _replace_host(self, parts: parse.SplitResult, new_host: str) -> Optional[str]:
        if parts.hostname is None:
            return None
        if parts.username or parts.password:
            return None

        netloc = new_host
        if parts.port:
            netloc = f"{netloc}:{parts.port}"

        return parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))

    def _download_product_image(
        self,
        image_url: Optional[str],
        product_name: str,
        product_id: int,
    ) -> Optional[str]:
        if not image_url:
            return None
        data = self._fetch(image_url)
        if data is None:
            return None
        suffix = self._guess_extension(image_url)
        optimized_data, suffix = self._compress_image(data, suffix)
        product_dir = self.image_dir / str(product_id)
        product_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{self._slugify(product_name)}{suffix}"
        path = product_dir / filename
        path.write_bytes(optimized_data)
        return str(path)

    def _compress_image(self, data: bytes, original_suffix: str) -> Tuple[bytes, str]:
        if Image is None:
            return data, original_suffix

        if ImageFile is not None:
            ImageFile.LOAD_TRUNCATED_IMAGES = True

        try:
            with Image.open(BytesIO(data)) as image:
                image.load()

                # Normalise palette images to avoid surprises during conversion.
                if image.mode == "P":
                    if "transparency" in image.info:
                        image = image.convert("RGBA")
                    else:
                        image = image.convert("RGB")

                # Attempt a lossless WebP compression first.
                buffer = BytesIO()
                try:
                    save_kwargs = {"format": "WEBP", "lossless": True, "method": 6}
                    if image.mode not in {"RGB", "RGBA", "L", "LA"}:
                        image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
                    image.save(buffer, **save_kwargs)
                    return buffer.getvalue(), ".webp"
                except (OSError, ValueError):
                    # Fall back to the original format with the best optimisation Pillow offers.
                    buffer = BytesIO()
                    target_format = image.format or self._extension_to_format(original_suffix)
                    save_kwargs = {"optimize": True}
                    if target_format == "JPEG":
                        save_kwargs.update({"quality": 95, "progressive": True})
                    image.save(buffer, format=target_format, **save_kwargs)
                    return buffer.getvalue(), f".{target_format.lower()}"
        except OSError:
            LOGGER.warning("Failed to process product image, storing original bytes", exc_info=True)
            return data, original_suffix

        return data, original_suffix

    def _extension_to_format(self, suffix: str) -> str:
        suffix = suffix.lower().lstrip(".")
        if suffix in {"jpg", "jpeg"}:
            return "JPEG"
        if suffix == "png":
            return "PNG"
        if suffix == "gif":
            return "GIF"
        if suffix == "webp":
            return "WEBP"
        return "PNG"

    def _guess_extension(self, url: str) -> str:
        parsed = parse.urlparse(url)
        _, ext = os.path.splitext(parsed.path)
        return ext if ext else ".jpg"

    # ------------------------------------------------------------------
    # Misc helpers
    # ------------------------------------------------------------------
    def _absolute_url(self, href: str) -> str:
        if href.startswith("http://") or href.startswith("https://"):
            return href
        return f"{self.base_url}{href}" if href.startswith("/") else href

    def _append_offset(self, base_url: str, offset: int) -> str:
        if "?" in base_url:
            return f"{base_url}&offset={offset}"
        return f"{base_url}?offset={offset}"

    def _slugify(self, value: str) -> str:
        value = value.lower()
        value = re.sub(r"[^a-z0-9]+", "-", value)
        value = value.strip("-")
        return value or "product"


class _DirectHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that allows overriding the SNI hostname for TLS."""

    def __init__(
        self,
        host: str,
        *,
        server_hostname: str,
        timeout: Optional[float],
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(host, timeout=timeout, context=context)
        self._server_hostname = server_hostname

    def connect(self) -> None:  # pragma: no cover - exercised via network operations
        conn = socket.create_connection(
            (self.host, self.port), self.timeout, self.source_address
        )
        try:
            if self._tunnel_host:
                self.sock = conn
                self._tunnel()
                conn = self.sock  # type: ignore[assignment]
            self.sock = self.context.wrap_socket(conn, server_hostname=self._server_hostname)
        except Exception:
            conn.close()
            raise


__all__ = ["INCIScraper"]
