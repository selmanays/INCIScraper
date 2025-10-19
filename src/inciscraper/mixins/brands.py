"""Brand scraping helpers."""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Dict, List, Tuple

from ..constants import PROGRESS_LOG_INTERVAL, REQUEST_SLEEP
from ..parser import extract_text, parse_html

LOGGER = logging.getLogger(__name__)


class BrandScraperMixin:
    """Mixin exposing brand collection behaviour."""

    conn: sqlite3.Connection

    def scrape_brands(
        self,
        *,
        reset_offset: bool = False,
        max_pages: int | None = None,
        max_brands: int | None = None,
    ) -> None:
        """Collect brand listings and persist them to the database."""

        if reset_offset:
            self._set_metadata("brands_next_offset", "1")
        start_offset = int(self._get_metadata("brands_next_offset", "1"))
        if start_offset > 1:
            LOGGER.info("Resuming brand collection from offset %s", start_offset)
        total_offsets_known = int(self._get_metadata("brands_total_offsets", "0") or 0)
        existing_brand_total = (
            self.conn.execute("SELECT COUNT(*) FROM brands").fetchone()[0]
        )
        if max_brands is not None and existing_brand_total >= max_brands:
            LOGGER.info(
                "Brand limit (%s) already satisfied – skipping brand scraping",
                max_brands,
            )
            return
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
                planned_pages,
                total_offsets_known,
            )
        else:
            LOGGER.info("Brand workload: offset unknown – scraping until no more pages")
        offset = start_offset
        processed_pages = 0
        final_total = total_offsets_known
        while True:
            if max_pages is not None and processed_pages >= max_pages:
                break
            current_url = self._append_offset(self.base_url + "/brands", offset)
            LOGGER.debug("Fetching brand listing page %s", current_url)
            html = self._fetch_html(current_url)
            if html is None:
                LOGGER.warning("Unable to download brand listing %s", current_url)
                break
            brands = self._parse_brand_list(html)
            if not brands:
                LOGGER.debug("No more brands found on %s", current_url)
                break
            for name, url in brands:
                inserted = self._insert_brand(name, url)
                if (
                    max_brands is not None
                    and inserted
                    and (self.conn.execute("SELECT COUNT(*) FROM brands").fetchone()[0])
                    >= max_brands
                ):
                    LOGGER.info("Reached brand limit (%s) – stopping", max_brands)
                    final_total = max(final_total, offset)
                    break
            processed_pages += 1
            if processed_pages % PROGRESS_LOG_INTERVAL == 0:
                self._log_progress("Brand page", processed_pages, planned_pages)
            offset += 1
            final_total = max(final_total, offset - 1)
            time.sleep(REQUEST_SLEEP)
        self._set_metadata("brands_next_offset", str(offset))
        if final_total:
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

    def _parse_brand_list(self, html: str) -> List[Tuple[str, str]]:
        """Extract brand names and URLs from a listing page."""

        root = parse_html(html)
        brands: List[Tuple[str, str]] = []
        nodes = root.find_all(class_="brandlist__item")
        if not nodes:
            # Fallback for the updated brand list markup that uses direct anchor
            # elements with the ``simpletextlistitem`` class.
            for anchor in root.find_all(tag="a", class_="simpletextlistitem"):
                href = anchor.get("href")
                name = extract_text(anchor)
                if not href or not name:
                    continue
                brands.append((name, self._absolute_url(href)))
            return brands

        for node in nodes:
            anchor = node.find(tag="a")
            if not anchor:
                continue
            href = anchor.get("href")
            name = extract_text(anchor)
            if not href or not name:
                continue
            brands.append((name, self._absolute_url(href)))
        return brands

    def _insert_brand(self, name: str, url: str) -> bool:
        """Insert or update a brand record."""

        now = self._current_timestamp()
        row = self.conn.execute(
            "SELECT id, name, last_updated_at FROM brands WHERE url = ?",
            (url,),
        ).fetchone()
        if row is None:
            while True:
                brand_id = self._generate_id()
                try:
                    self.conn.execute(
                        """
                        INSERT INTO brands (id, name, url, products_scraped, last_checked_at, last_updated_at)
                        VALUES (?, ?, ?, 0, ?, ?)
                        """,
                        (brand_id, name, url, now, now),
                    )
                except sqlite3.IntegrityError as exc:
                    if "brands.id" in str(exc):
                        continue
                    raise
                return True
        updates: Dict[str, str] = {"last_checked_at": now}
        changed = False
        if row["name"] != name:
            updates["name"] = name
            changed = True
        if changed or not row["last_updated_at"]:
            updates["last_updated_at"] = now
        if updates:
            assignments = ", ".join(f"{column} = ?" for column in updates)
            params = list(updates.values()) + [row["id"]]
            self.conn.execute(
                f"UPDATE brands SET {assignments} WHERE id = ?",
                params,
            )
        return False

