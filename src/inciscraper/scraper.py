"""High level scraping logic for collecting data from INCIDecoder."""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
import ssl
from pathlib import Path
from typing import Iterable, Optional

from .constants import BASE_URL, DEFAULT_TIMEOUT
from .mixins import (
    BrandScraperMixin,
    DatabaseMixin,
    DetailScraperMixin,
    NetworkMixin,
    ProductScraperMixin,
    UtilityMixin,
    WorkloadMixin,
)

LOGGER = logging.getLogger(__name__)


class INCIScraper(
    UtilityMixin,
    NetworkMixin,
    DetailScraperMixin,
    ProductScraperMixin,
    BrandScraperMixin,
    DatabaseMixin,
    WorkloadMixin,
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
        self._init_db()

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

    def generate_sample_dataset(
        self,
        *,
        brand_count: int = 3,
        products_per_brand: int = 1,
    ) -> None:
        """Create a compact dataset used for smoke testing the scraper."""

        LOGGER.info(
            "Preparing clean database state before generating sample dataset",
        )
        self.conn.executescript(
            """
            DELETE FROM products;
            DELETE FROM ingredients;
            DELETE FROM functions;
            DELETE FROM frees;
            DELETE FROM brands;
            DELETE FROM metadata;
            """
        )
        self.conn.commit()
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

    def close(self) -> None:
        """Close the underlying SQLite connection and release Playwright resources."""

        self._shutdown_cosing_resources()
        self.conn.close()

    # ------------------------------------------------------------------
    # Helper utilities
    # ------------------------------------------------------------------
    @staticmethod
    def _generate_id() -> str:
        """Return a random identifier suitable for primary keys."""

        return secrets.token_hex(16)


__all__ = ["INCIScraper"]

