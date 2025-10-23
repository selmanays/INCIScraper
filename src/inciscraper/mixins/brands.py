"""Brand scraping helpers."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Dict, List, Optional, Tuple

from ..constants import PROGRESS_LOG_INTERVAL
from ..parser import extract_text, parse_html

LOGGER = logging.getLogger(__name__)


class BrandScraperMixin:
    """Mixin exposing brand collection behaviour."""

    conn: sqlite3.Connection

    def _discover_total_brand_pages(self) -> int:
        """Discover total number of brand pagination pages using exponential backoff + binary search.
        
        Strategy:
        1. Exponential Phase: Jump to 1, 2, 4, 8, 16, 32... until empty page found
        2. Binary Search Phase: Use binary search between lower and upper bounds
        
        This approach is significantly more efficient than linear search:
        - For 200 pages: ~8 exponential + ~8 binary = ~16 requests (vs ~58 with old method)
        - For 500 pages: ~9 exponential + ~9 binary = ~18 requests (vs ~118 with old method)
        
        Returns:
            Total number of pagination pages
        """
        import random
        
        def save_state(stage: str, lower: int, upper: int, next_page: int, checks: int) -> None:
            state = {
                "stage": stage,
                "lower": lower,
                "upper": upper,
                "next": max(next_page, 1),
                "checks": checks,
            }
            self._set_metadata("progress_brands_discovery_state", json.dumps(state))
        
        def clear_state() -> None:
            self._delete_metadata("progress_brands_discovery_state")
        
        # Set discovery flag for UI (frontend will show "Hesaplanıyor..." message)
        self._set_metadata("brands_discovering_pages", "1")
        
        resume_state = self._get_metadata("progress_brands_discovery_state")
        lower_bound = 1
        upper_bound = 1
        page_num = 1
        stage = "exponential"
        checks_made = 0
        
        if resume_state:
            try:
                state_data = json.loads(resume_state)
                stage = state_data.get("stage", "exponential")
                lower_bound = max(int(state_data.get("lower", 1)), 1)
                upper_bound = max(int(state_data.get("upper", lower_bound)), lower_bound)
                page_num = max(int(state_data.get("next", lower_bound)), 1)
                checks_made = max(int(state_data.get("checks", 0)), 0)
                LOGGER.info(
                    "Brand discovery resumed at page %s (stage: %s, lower=%s, upper=%s)",
                    page_num,
                    stage,
                    lower_bound,
                    upper_bound if upper_bound > 0 else "?",
                )
            except (ValueError, json.JSONDecodeError) as exc:
                LOGGER.warning("Failed to restore discovery state: %s", exc)
                stage = "exponential"
                lower_bound = 1
                upper_bound = 1
                page_num = 1
                checks_made = 0
                self._delete_metadata("progress_brands_discovery_state")
        else:
            LOGGER.info("Brand discovery started")
        
        # Ensure metadata reflects discovery state
        self._set_metadata("progress_brands_total_pages", "?")
        
        # Phase 1: Exponential backoff to find upper bound
        if stage == "exponential":
            while True:
                if self._should_stop_scraping():
                    LOGGER.info("Discovery interrupted by user")
                    if lower_bound > 0:
                        self._set_metadata("progress_brands_total_pages", str(lower_bound))
                        self._set_metadata("brands_total_offsets", str(lower_bound))
                    save_state("exponential", lower_bound, upper_bound, page_num, checks_made)
                    self._set_metadata("brands_discovering_pages", "0")
                    return lower_bound
                
                url = self._append_offset(self.base_url + "/brands", page_num)
                
                # Update progress metadata for UI
                self._set_metadata("progress_brands_current_page", str(page_num))
                LOGGER.info("Checking brand discovery page %s", page_num)
                
                html = self._fetch_html(url)
                checks_made += 1
                
                if html is None:
                    LOGGER.warning("Failed to fetch %s, using last known page %s", url, lower_bound)
                    upper_bound = page_num
                    save_state("binary", lower_bound, upper_bound, max(lower_bound + 1, (lower_bound + upper_bound) // 2), checks_made)
                    stage = "binary"
                    break
                
                brands = self._parse_brand_list(html)
                
                if brands:
                    lower_bound = page_num
                    LOGGER.debug("Page %s has content (%s brands)", page_num, len(brands))
                    
                    # Prepare for next exponential step
                    if page_num == 1:
                        page_num = 2
                    else:
                        page_num *= 2
                    
                    save_state("exponential", lower_bound, upper_bound, page_num, checks_made)
                    
                    time.sleep(random.uniform(0.5, 2.0))
                else:
                    upper_bound = page_num
                    LOGGER.debug("Empty page found at %s", page_num)
                    LOGGER.debug("Bounds established: [%s, %s]", lower_bound, upper_bound)
                    save_state("binary", lower_bound, upper_bound, max(lower_bound + 1, (lower_bound + upper_bound) // 2), checks_made)
                    stage = "binary"
                    break
        
        # If lower and upper are adjacent, we found the answer
        if upper_bound - lower_bound <= 1:
            LOGGER.info("Total brand pagination pages discovered: %s (checked %s pages)", lower_bound, checks_made)
            self._set_metadata("progress_brands_total_pages", str(lower_bound))
            self._set_metadata("brands_total_offsets", str(lower_bound))
            self._set_metadata("brands_discovering_pages", "0")
            clear_state()
            return lower_bound
        
        # Phase 2: Binary search between lower_bound and upper_bound
        while upper_bound - lower_bound > 1:
            if self._should_stop_scraping():
                LOGGER.info("Discovery interrupted by user")
                if lower_bound > 0:
                    self._set_metadata("progress_brands_total_pages", str(lower_bound))
                    self._set_metadata("brands_total_offsets", str(lower_bound))
                save_state("binary", lower_bound, upper_bound, max(lower_bound + 1, (lower_bound + upper_bound) // 2), checks_made)
                self._set_metadata("brands_discovering_pages", "0")
                return lower_bound
            
            mid = (lower_bound + upper_bound) // 2
            if mid <= lower_bound:
                mid = lower_bound + 1
            if mid >= upper_bound:
                mid = upper_bound - 1
            mid = max(mid, 1)
            
            url = self._append_offset(self.base_url + "/brands", mid)
            
            self._set_metadata("progress_brands_current_page", str(mid))
            LOGGER.info("Checking brand discovery page %s", mid)
            
            html = self._fetch_html(url)
            checks_made += 1
            
            if html is None:
                LOGGER.warning("Failed to fetch page %s, narrowing to lower half", mid)
                upper_bound = mid
            else:
                brands = self._parse_brand_list(html)
                if brands:
                    lower_bound = mid
                    LOGGER.debug("Page %s has content (%s brands), searching upper half", mid, len(brands))
                else:
                    upper_bound = mid
                    LOGGER.debug("Page %s is empty, searching lower half", mid)
            
            next_mid = (lower_bound + upper_bound) // 2
            if next_mid <= lower_bound:
                next_mid = lower_bound + 1
            save_state("binary", lower_bound, upper_bound, next_mid, checks_made)
            
            time.sleep(random.uniform(0.5, 2.0))
        
        # The answer is lower_bound (last page with content)
        LOGGER.info("Total brand pagination pages discovered: %s (checked %s pages)", lower_bound, checks_made)
        
        self._set_metadata("progress_brands_total_pages", str(lower_bound))
        self._set_metadata("brands_total_offsets", str(lower_bound))
        self._set_metadata("brands_discovering_pages", "0")
        clear_state()
        
        return lower_bound

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
            self._set_metadata("brands_total_offsets", "0")
            
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
        
        # Discover total pages if not known
        if not total_offsets_known or reset_offset:
            total_offsets_known = self._discover_total_brand_pages()
            self._set_metadata("brands_total_offsets", str(total_offsets_known))
            
        planned_pages = max(total_offsets_known - start_offset + 1, 0)
        if planned_pages <= 0 and not reset_offset:
            LOGGER.info(
                "Brand workload already complete (total offsets: %s)",
                total_offsets_known,
            )
            return
            
        LOGGER.debug(
            "Brand workload: %s/%s page offsets remaining",
            planned_pages,
            total_offsets_known,
        )
        offset = start_offset
        processed_pages = 0
        final_total = total_offsets_known
        while True:
            # Check if user requested pause/stop
            if self._should_stop_scraping():
                LOGGER.info("Scraping paused by user after %s pages", processed_pages)
                # Discovery bayrağını temizle (indeterminate progress bar'ı durdur)
                self._set_metadata("brands_discovering_pages", "0")
                break
                
            if max_pages is not None and processed_pages >= max_pages:
                break
            
            current_url = self._append_offset(self.base_url + "/brands", offset)
            
            # Log current brand page being scraped
            LOGGER.info("Currently scraping brand page %s/%s", offset, total_offsets_known)
            
            html = self._fetch_html(current_url)
            if html is None:
                LOGGER.warning("Unable to download brand listing %s", current_url)
                break
            brands = self._parse_brand_list(html)
            if not brands:
                LOGGER.debug("No more brands found on %s", current_url)
                break
            limit_reached = False
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
                    limit_reached = True
                    break
            processed_pages += 1
            
            # Update progress metadata AFTER page is successfully processed
            self._set_metadata("progress_brands_current_page", str(offset))
            self._set_metadata("progress_brands_total_pages", str(total_offsets_known))
            if processed_pages % PROGRESS_LOG_INTERVAL == 0:
                self._log_progress("Brand page", processed_pages, planned_pages)
            if limit_reached:
                break
            offset += 1
            final_total = max(final_total, offset - 1)
            self._adaptive_sleep()
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
            
            # Log completion message once at the end
            LOGGER.info("All brands scraping completed.")

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
                self.conn.commit()
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
            self.conn.commit()
        return False
