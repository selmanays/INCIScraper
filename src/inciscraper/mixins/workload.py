"""Helpers that summarise remaining scraping work."""

from __future__ import annotations

import logging
import sqlite3
from typing import Dict, Optional

LOGGER = logging.getLogger(__name__)


class WorkloadMixin:
    """Common workload inspection and logging helpers."""

    conn: sqlite3.Connection

    def has_brand_work(self) -> bool:
        """Return ``True`` when brand scraping still has pending work."""

        return self._get_metadata("brands_complete") != "1"

    def has_product_work(self) -> bool:
        """Return ``True`` when there are brands awaiting product scraping."""

        cursor = self.conn.execute(
            "SELECT 1 FROM brands WHERE products_scraped = 0 LIMIT 1",
        )
        return cursor.fetchone() is not None

    def has_product_detail_work(self) -> bool:
        """Return ``True`` when product detail scraping is still required."""

        cursor = self.conn.execute(
            "SELECT 1 FROM products WHERE details_scraped = 0 LIMIT 1",
        )
        return cursor.fetchone() is not None

    @staticmethod
    def _log_progress(
        stage: str,
        processed: int,
        total: int,
        *,
        extra: str | None = None,
    ) -> None:
        """Log a progress message for long running stages."""

        if total > 0:
            percent = (processed / total) * 100
            message = f"{stage} progress: {processed}/{total} ({percent:.1f}%)"
        else:
            message = f"{stage} progress: processed {processed} item(s)"
        if extra:
            message = f"{message} – {extra}"
        LOGGER.info(message)

    def get_workload_summary(self) -> Dict[str, Optional[int]]:
        """Return a snapshot summarising remaining scraping work."""

        total_brands = self.conn.execute("SELECT COUNT(*) FROM brands").fetchone()[0]
        pending_brands = self.conn.execute(
            "SELECT COUNT(*) FROM brands WHERE products_scraped = 0",
        ).fetchone()[0]
        pending_products = self.conn.execute(
            "SELECT COUNT(*) FROM products WHERE details_scraped = 0",
        ).fetchone()[0]
        summary = {
            "brands_total": int(total_brands),
            "brands_pending": int(pending_brands),
            "products_pending_details": int(pending_products),
        }
        return summary

