"""High level scraping logic for collecting data from INCIDecoder."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sqlite3
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable, Optional

from .constants import BASE_URL, DEFAULT_TIMEOUT
from .lru_cache import LRUCache
from .mixins import (
    AsyncNetworkMixin,
    BatchProcessorMixin,
    BrandScraperMixin,
    DatabaseMixin,
    DetailScraperMixin,
    MonitoringMixin,
    NetworkMixin,
    ProductScraperMixin,
    UtilityMixin,
    WorkloadMixin,
)

LOGGER = logging.getLogger(__name__)


class INCIScraper(
    UtilityMixin,
    NetworkMixin,
    AsyncNetworkMixin,
    DetailScraperMixin,
    ProductScraperMixin,
    BrandScraperMixin,
    DatabaseMixin,
    WorkloadMixin,
    MonitoringMixin,
    BatchProcessorMixin,
):
    """Main entry point that orchestrates all scraping steps."""

    def __init__(
        self,
        *,
        db_path: str = "data/incidecoder.db",
        image_dir: str | os.PathLike[str] = "data/images",
        base_url: str = BASE_URL,
        request_timeout: int = DEFAULT_TIMEOUT,
        alternate_base_urls: Optional[Iterable[str]] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = request_timeout
        self.image_dir = Path(image_dir)
        self.image_dir.mkdir(parents=True, exist_ok=True)
        db_path_obj = Path(db_path)
        db_path_obj.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path_obj)
        self.conn.row_factory = sqlite3.Row
        self._host_failover: dict[str, str] = {}
        self._host_ip_overrides: dict[str, str] = {}
        self._host_alternatives = self._build_host_alternatives(
            self.base_url, alternate_base_urls or []
        )
        self._ssl_context = ssl.create_default_context()
        self._cosing_playwright = None
        self._cosing_browser = None
        self._cosing_context = None
        self._cosing_page = None
        self._cosing_playwright_failed = False
        self._cosing_record_cache = LRUCache(max_size=10000)  # LRU cache for CosIng records
        
        # Adaptive sleep tracking
        self._request_success_count = 0
        self._request_error_count = 0
        self._current_sleep_time = 0.5  # Start with default REQUEST_SLEEP
        self._min_sleep_time = 0.1  # Minimum sleep time
        self._max_sleep_time = 2.0  # Maximum sleep time
        
        # Initialize monitoring
        MonitoringMixin.__init__(self)
        
        # Initialize batch processor
        BatchProcessorMixin.__init__(self)
        
        # Initialize async network
        AsyncNetworkMixin.__init__(self)
        
        # Initialize thread pool for image processing
        self._image_thread_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="image_processing")
        
        self._init_db()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def run(self) -> None:
        """Execute the full scraping pipeline."""

        self._start_monitoring()
        
        LOGGER.info("Starting brand collection")
        self._start_stage("brands")
        self.scrape_brands()
        self._end_stage()
        
        LOGGER.info("Starting product collection")
        self._start_stage("products")
        self.scrape_products()
        self._end_stage()
        
        LOGGER.info("Starting product detail collection")
        self._start_stage("details")
        self.scrape_product_details()
        self._end_stage()
        
        self._log_performance_summary()

    def generate_sample_dataset(
        self,
        *,
        brand_count: int = 3,
        products_per_brand: int | None = None,
    ) -> None:
        """Create a compact dataset used for smoke testing the scraper.
        
        Args:
            brand_count: Number of brands to scrape (default: 3)
            products_per_brand: Max products per brand. If None, scrapes ALL products for each brand.
        """

        LOGGER.info(
            "Preparing clean database state before generating sample dataset",
        )
        self.conn.executescript(
            """
            DELETE FROM products;
            DELETE FROM ingredients;
            DELETE FROM functions;
            DELETE FROM brands;
            DELETE FROM metadata;
            """
        )
        self.conn.commit()
        
        if products_per_brand is None:
            LOGGER.info(
                "Collecting %s brand(s) and ALL their products for sample dataset",
                brand_count,
            )
        else:
            LOGGER.info(
                "Collecting %s brand(s) and %s product(s) per brand for sample dataset",
                brand_count,
                products_per_brand,
            )
            
        self.scrape_brands(reset_offset=True, max_brands=brand_count)
        self._set_metadata("brands_complete", "1")
        self._set_metadata("brands_next_offset", "1")
        if brand_count:
            self._set_metadata("brands_total_offsets", str(brand_count))
        self.scrape_products(max_products_per_brand=products_per_brand)
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

    def _should_stop_scraping(self) -> bool:
        """Check if user requested to pause/stop scraping via UI state file."""
        state_file = Path("data/scraper_state.json")
        if not state_file.exists():
            return False
        
        try:
            with open(state_file, 'r') as f:
                state = json.load(f)
                # If status is not "running", user requested pause/stop
                return state.get("status") != "running"
        except Exception as e:
            LOGGER.debug("Error reading state file: %s", e)
            return False

    def close(self) -> None:
        """Close the underlying SQLite connection and release Playwright resources."""

        self._shutdown_cosing_resources()
        
        # Shutdown thread pool
        if hasattr(self, '_image_thread_pool'):
            self._image_thread_pool.shutdown(wait=True)
        
        # Close async session
        if hasattr(self, '_close_async_session'):
            try:
                loop = asyncio.get_event_loop()
                loop.run_until_complete(self._close_async_session())
            except RuntimeError:
                # No event loop, create one temporarily
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(self._close_async_session())
                loop.close()
        
        self.conn.close()

    # ------------------------------------------------------------------
    # Helper utilities
    # ------------------------------------------------------------------
    @staticmethod
    def _generate_id() -> str:
        """Return a random identifier suitable for primary keys."""

        return secrets.token_hex(16)
    
    def _adaptive_sleep(self) -> None:
        """Sleep with adaptive timing based on request success/error rates."""
        time.sleep(self._current_sleep_time)
    
    def _record_request_success(self) -> None:
        """Record a successful request and adjust sleep time."""
        self._request_success_count += 1
        # Gradually decrease sleep time on success (but not below minimum)
        if self._current_sleep_time > self._min_sleep_time:
            self._current_sleep_time = max(
                self._min_sleep_time,
                self._current_sleep_time * 0.95  # 5% reduction
            )
    
    def _record_request_error(self) -> None:
        """Record a failed request and adjust sleep time."""
        self._request_error_count += 1
        # Increase sleep time on error (but not above maximum)
        self._current_sleep_time = min(
            self._max_sleep_time,
            self._current_sleep_time * 1.5  # 50% increase
        )
    
    def _get_adaptive_sleep_stats(self) -> dict:
        """Get adaptive sleep statistics."""
        total_requests = self._request_success_count + self._request_error_count
        success_rate = self._request_success_count / total_requests if total_requests > 0 else 0.0
        return {
            'success_count': self._request_success_count,
            'error_count': self._request_error_count,
            'success_rate': success_rate,
            'current_sleep_time': self._current_sleep_time,
            'min_sleep_time': self._min_sleep_time,
            'max_sleep_time': self._max_sleep_time,
        }
    
    def _download_image_parallel(self, image_url: str, product_name: str, product_id: str) -> Optional[str]:
        """Download image in a separate thread."""
        if not hasattr(self, '_image_thread_pool'):
            # Fallback to synchronous download
            return self._download_product_image(image_url, product_name, product_id)
        
        future = self._image_thread_pool.submit(
            self._download_product_image, image_url, product_name, product_id
        )
        return future.result()


__all__ = ["INCIScraper"]

