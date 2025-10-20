"""Product listing scraping mixin for INCIScraper."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Tuple

from bs4 import BeautifulSoup

from inciscraper.constants import PROGRESS_LOG_INTERVAL
from inciscraper.mixins.database import DatabaseMixin
from inciscraper.mixins.monitoring import MonitoringMixin
from inciscraper.mixins.network import NetworkMixin

LOGGER = logging.getLogger(__name__)


class ProductScraperMixin(DatabaseMixin, NetworkMixin, MonitoringMixin):
    """Mixin for scraping product listings from brand pages."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_workers = kwargs.get('max_workers', 1)

    def scrape_products(self, *, rescan_all: bool = False) -> None:
        """Scrape product listings from all brand pages."""
        
        if rescan_all:
            cursor = self.conn.execute(
                "SELECT id, name, url, products_scraped FROM brands ORDER BY id",
            )
        else:
            cursor = self.conn.execute(
                "SELECT id, name, url, products_scraped FROM brands WHERE products_scraped = 0 ORDER BY id",
            )
        
        pending_brands = cursor.fetchall()
        if not pending_brands:
            LOGGER.info("No brands require product scraping – skipping stage")
            return
        
        total_brands = len(pending_brands)
        LOGGER.info("Product workload: %s brand(s) awaiting scraping", total_brands)
        
        # Create progress bar for brand processing
        self.create_progress_bar("brands", total_brands, "Processing brands")
        processed = 0
        # Use parallel processing for multiple workers
        if self.max_workers > 1:
            # Use parallel processing for multiple workers
            processed = self._scrape_brands_parallel(
                pending_brands,
                max_products_per_brand=None,
                total_brands=total_brands,
            )
        else:
            # Use sequential processing for single worker
            for brand in pending_brands:
                self._process_single_brand(brand, max_products_per_brand=None)
                processed += 1
                self.update_progress("brands", 1)
                if processed % PROGRESS_LOG_INTERVAL == 0 or processed == total_brands:
                    self._log_progress("Brand", processed, total_brands)

        self.close_progress_bar("brands")
        LOGGER.info("Product scraping completed")

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
        start_offset = int(self._get_metadata_thread_safe(resume_key, "1"))
        
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
                brand_name,
                max_products_per_brand,
                start_offset,
                connection=thread_conn
            )
            
            # Update brand status
            if completed:
                thread_conn.execute(
                    "UPDATE brands SET products_scraped = 1 WHERE id = ?",
                    (brand_id,)
                )
                thread_conn.commit()
                thread_conn.close()
                
                # Clean up resume metadata
                self._delete_metadata(resume_key)
                
                if products_found == 0:
                    LOGGER.info(
                        "Brand %s has no products – marking as empty",
                        brand_name,
                    )
                    self._set_metadata(f"brand_empty_products:{brand_id}", "1")
                else:
                    self._delete_metadata(f"brand_empty_products:{brand_id}")
            else:
                self._set_metadata_thread_safe(resume_key, str(next_offset))
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
        except Exception as e:
            LOGGER.error(f"Error processing brand {brand_name}: {e}")
            raise

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
                try:
                    future.result()  # This will raise any exception that occurred
                    processed += 1
                    self.update_progress("brands", 1)
                    
                    if processed % PROGRESS_LOG_INTERVAL == 0 or processed == total_brands:
                        self._log_progress("Brand", processed, total_brands)
                        
                except Exception as e:
                    brand = future_to_brand[future]
                    LOGGER.error(f"Error processing brand {brand['name']}: {e}")
                    processed += 1
                    self.update_progress("brands", 1)
        
        return processed

    def _collect_products_for_brand(
        self,
        brand_id: str,
        brand_url: str,
        brand_name: str,
        max_products_per_brand: Optional[int] = None,
        start_offset: int = 1,
        connection=None
    ) -> Tuple[int, bool, int]:
        """Collect all products for a specific brand."""
        
        products_found = 0
        offset = start_offset
        
        while True:
            # Construct URL for this page
            page_url = f"{brand_url}?offset={offset}"
            
            # Fetch brand page
            response = self._fetch(page_url)
            if not response:
                LOGGER.warning(
                    "Failed to fetch brand page %s (offset %s)",
                    brand_name,
                    offset,
                )
                return products_found, False, offset
            
            soup = BeautifulSoup(response.content, "html.parser")
            
            # Extract products from this page
            products = self._extract_products_from_page(soup, brand_id)
            
            if not products:
                # No more products found
                break
            
            # Store products in database
            for product in products:
                self._insert_product(product, connection=connection)
                products_found += 1
                
                # Check if we've reached the limit
                if max_products_per_brand and products_found >= max_products_per_brand:
                    return products_found, True, offset + 1
            
            # Move to next page
            offset += 1
            
            # Check if there are more pages
            if not self._has_more_pages(soup):
                break
        
        return products_found, True, offset

    def _extract_products_from_page(self, soup: BeautifulSoup, brand_id: str) -> List[dict]:
        """Extract product information from a brand page."""
        
        products = []
        
        # Look for product links
        product_links = soup.select('a[href*="/products/"]')
        
        for link in product_links:
            product_name = link.get_text(strip=True)
            product_url = link.get('href')
            
            if product_url and product_url.startswith('/'):
                product_url = self.base_url + product_url
            
            if product_name and product_url:
                product_id = self._generate_id()
                now = self._current_timestamp()
                
                products.append({
                    'id': product_id,
                    'brand_id': brand_id,
                    'name': product_name,
                    'url': product_url,
                    'last_checked_at': now,
                    'last_updated_at': now
                })
        
        return products

    def _has_more_pages(self, soup: BeautifulSoup) -> bool:
        """Check if there are more pages of products."""
        
        # Look for pagination indicators
        next_link = soup.select_one('a[href*="offset="]')
        return next_link is not None

    def _insert_product(self, product: dict, connection=None) -> None:
        """Insert a product into the database."""
        
        conn = connection or self.conn
        
        conn.execute(
            """
            INSERT OR IGNORE INTO products (id, brand_id, name, url, last_checked_at, last_updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                product['id'],
                product['brand_id'],
                product['name'],
                product['url'],
                product['last_checked_at'],
                product['last_updated_at']
            ),
        )
        conn.commit()

    def _count_products_for_brand(self, brand_id: str, connection=None) -> int:
        """Count products for a specific brand."""
        
        conn = connection or self.conn
        
        cursor = conn.execute(
            "SELECT COUNT(*) FROM products WHERE brand_id = ?",
            (brand_id,)
        )
        return cursor.fetchone()[0]

    def _log_progress(self, stage: str, processed: int, total: int) -> None:
        """Log progress for a specific stage."""
        
        percentage = (processed / total) * 100 if total > 0 else 0
        LOGGER.info(
            "%s progress: %s/%s (%.1f%%)",
            stage, processed, total, percentage
        )