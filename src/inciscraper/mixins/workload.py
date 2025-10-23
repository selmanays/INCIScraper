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
        LOGGER.debug(message)  # DEBUG level - kullanıcıya gösterilmemeli

    def get_workload_summary(self) -> Dict[str, Optional[int]]:
        """Return a snapshot summarising remaining scraping work."""

        total_brands = self.conn.execute("SELECT COUNT(*) FROM brands").fetchone()[0]
        pending_brands = self.conn.execute(
            "SELECT COUNT(*) FROM brands WHERE products_scraped = 0",
        ).fetchone()[0]
        total_products = self.conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        pending_products = self.conn.execute(
            "SELECT COUNT(*) FROM products WHERE details_scraped = 0",
        ).fetchone()[0]
        next_offset_raw = self._get_metadata("brands_next_offset", "1") or "1"
        total_offsets_raw = self._get_metadata("brands_total_offsets", "0") or "0"
        try:
            next_offset = max(int(next_offset_raw), 1)
        except (TypeError, ValueError):
            next_offset = 1
        try:
            total_offsets = max(int(total_offsets_raw), 0)
        except (TypeError, ValueError):
            total_offsets = 0
        if total_offsets:
            remaining = total_offsets - (next_offset - 1)
            brand_pages_remaining: Optional[int] = max(remaining, 0)
        else:
            brand_pages_remaining = None
        summary = {
            "brands_total": int(total_brands),
            "brands_pending": int(pending_brands),
            "brands_pending_products": int(pending_brands),
            "brand_pages_remaining": brand_pages_remaining,
            "products_total": int(total_products),
            "products_pending_details": int(pending_products),
        }
        return summary

