"""Product detail scraping, ingredient persistence and CosIng helpers."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import unicodedata
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib import parse

try:  # pragma: no cover - optional dependency safeguard
    from playwright.sync_api import (
        Error as PlaywrightError,
        TimeoutError as PlaywrightTimeoutError,
        sync_playwright,
    )
except ModuleNotFoundError:  # pragma: no cover - fallback when Playwright missing
    PlaywrightError = Exception  # type: ignore[assignment]
    PlaywrightTimeoutError = TimeoutError  # type: ignore[assignment]
    sync_playwright = None  # type: ignore[assignment]

from ..constants import COSING_BASE_URL, PROGRESS_LOG_INTERVAL
from ..models import (
    CosIngRecord,
    FreeTag,
    HighlightEntry,
    IngredientDetails,
    IngredientFunction,
    IngredientFunctionInfo,
    IngredientReference,
    ProductDetails,
    ProductHighlights,
)
from ..parser import Node, extract_text, parse_html

LOGGER = logging.getLogger(__name__)


class DetailScraperMixin:
    """Handle product details, ingredient parsing and CosIng integration."""

    conn: sqlite3.Connection
    _cosing_playwright: Optional[Any]
    _cosing_browser: Optional[Any]
    _cosing_context: Optional[Any]
    _cosing_page: Optional[Any]
    _cosing_playwright_failed: bool

    def scrape_product_details(self, *, rescan_all: bool = False) -> None:
        """Download and persist detailed information for each product."""

        if rescan_all:
            cursor = self.conn.execute(
                "SELECT id, brand_id, name, url, details_scraped FROM products ORDER BY id",
            )
        else:
            cursor = self.conn.execute(
                "SELECT id, brand_id, name, url, details_scraped FROM products WHERE details_scraped = 0 ORDER BY id",
            )
        pending_products = cursor.fetchall()
        total_products = len(pending_products)
        if total_products == 0:
            LOGGER.info("No products require detail scraping – skipping stage")
            return
        if rescan_all:
            LOGGER.info("Detail workload: revalidating %s product(s)", total_products)
        else:
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
            image_path = self._download_product_image(
                details.image_url,
                details.name,
                product["id"],
            )
            self._store_product_details(product["id"], details, image_path)
            if not product["details_scraped"]:
                self.conn.execute(
                    "UPDATE products SET details_scraped = 1 WHERE id = ?",
                    (product["id"],),
                )
            self.conn.commit()
            LOGGER.info("Stored product details for %s", details.name)
            processed += 1
            if processed % PROGRESS_LOG_INTERVAL == 0 or processed == total_products:
                self._log_progress("Product", processed, total_products)

    # ------------------------------------------------------------------
    # Product detail parsing
    # ------------------------------------------------------------------
    def _parse_product_page(self, html: str) -> Optional[ProductDetails]:
        """Parse a product detail page into structured information."""

        root = parse_html(html)
        product_block = root.find(class_="detailpage") or root
        name_node = product_block.find(id_="product-title") or root.find(id_="product-title")
        description_node = (
            product_block.find(id_="product-details")
            or root.find(id_="product-details")
        )
        if not name_node:
            return None
        name = extract_text(name_node)
        description = extract_text(description_node) if description_node else ""
        image_url = self._extract_product_image(product_block)
        tooltip_map = self._build_tooltip_index(root)
        ingredients = self._extract_ingredients(root, tooltip_map)
        ingredient_functions = self._extract_ingredient_functions(root)
        highlights = self._extract_highlights(root, tooltip_map)
        discontinued = bool(
            root.find(class_="discontinued") or root.find(class_="product__discontinued")
        )
        replacement_anchor = root.find(class_="replacement-product")
        replacement_product_url = None
        if replacement_anchor and replacement_anchor.get("href"):
            replacement_product_url = self._absolute_url(replacement_anchor.get("href"))
        return ProductDetails(
            name=name,
            description=self._normalize_whitespace(description),
            image_url=image_url,
            ingredients=ingredients,
            ingredient_functions=ingredient_functions,
            highlights=highlights,
            discontinued=discontinued,
            replacement_product_url=replacement_product_url,
        )

    def _extract_product_image(self, product_block: Node) -> Optional[str]:
        """Return the hero image URL for the product if available."""

        image_container = product_block.find(class_="product__image")
        if not image_container:
            return None
        img_tag = image_container.find(tag="img")
        if img_tag and img_tag.get("src"):
            return self._absolute_url(img_tag.get("src"))
        return None

    def _build_tooltip_index(self, root: Node) -> Dict[str, Node]:
        """Map tooltip identifiers to DOM nodes for quick lookup."""

        tooltip_map: Dict[str, Node] = {}
        for tooltip in root.find_all(class_="tooltip-content"):
            tooltip_id = tooltip.get("id")
            if tooltip_id:
                tooltip_map[tooltip_id] = tooltip
        return tooltip_map

    def _extract_ingredients(
        self, root: Node, tooltip_map: Dict[str, Node]
    ) -> List[IngredientReference]:
        """Collect ingredient references listed on the product page."""

        container = root.find(id_="product-ingredients") or root
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
                    predicate=lambda n: n.get("href", "").startswith("/ingredients/"),
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
        """Locate the tooltip icon associated with ``node``."""

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
        """Parse the ingredient function table displayed on the page."""

        section = root.find(id_="ingredlist-table-section")
        if not section:
            return []
        rows: List[IngredientFunction] = []
        for tr in section.find_all(tag="tr"):
            cells = [
                child
                for child in tr.children
                if isinstance(child, Node) and child.tag == "td"
            ]
            if len(cells) < 2:
                continue
            ingred_cell, function_cell = cells[:2]
            ingred_anchor = ingred_cell.find(
                tag="a",
                predicate=lambda n: n.get("href", "").startswith("/ingredients/"),
            )
            if not ingred_anchor:
                continue
            ingredient_name = extract_text(ingred_anchor)
            ingredient_page = (
                self._absolute_url(ingred_anchor.get("href"))
                if ingred_anchor.get("href")
                else None
            )
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

    def _extract_highlights(
        self, root: Node, tooltip_map: Dict[str, Node]
    ) -> ProductHighlights:
        """Collect highlight hashtags and ingredient groupings."""

        section = root.find(id_="ingredlist-highlights-section")
        free_tags: List[FreeTag] = []
        key_entries: List[HighlightEntry] = []
        other_entries: List[HighlightEntry] = []
        if section:
            for node in section.find_all(tag="span"):
                if node.has_class("hashtag"):
                    text = extract_text(node)
                    if not text:
                        continue
                    tooltip_text = None
                    tooltip_attr = node.get("data-tooltip-content")
                    if tooltip_attr:
                        tooltip_id = tooltip_attr.lstrip("#")
                        tooltip_node = tooltip_map.get(tooltip_id)
                        if tooltip_node:
                            tooltip_text = self._normalize_whitespace(
                                extract_text(tooltip_node)
                            )
                    free_tags.append(FreeTag(tag=text, tooltip=tooltip_text))
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
            free_tags=free_tags,
            key_ingredients=key_entries,
            other_ingredients=other_entries,
        )

    # ------------------------------------------------------------------
    # Product detail persistence helpers
    # ------------------------------------------------------------------
    def _store_product_details(
        self,
        product_id: str,
        details: ProductDetails,
        image_path: Optional[str],
    ) -> None:
        """Persist the parsed product details and ingredient links."""

        ingredient_ids: List[str] = []
        ingredient_lookup_by_url: Dict[str, str] = {}
        ingredient_lookup_by_name: Dict[str, str] = {}

        def normalise_url(value: str) -> str:
            return value.rstrip("/").lower()

        for ingredient in details.ingredients:
            ingredient_id = self._ensure_ingredient(ingredient)
            ingredient.ingredient_id = ingredient_id
            ingredient_ids.append(ingredient_id)
            if ingredient.url:
                ingredient_lookup_by_url[normalise_url(ingredient.url)] = ingredient_id
            normalized_name = self._normalize_whitespace(ingredient.name).lower()
            if normalized_name:
                ingredient_lookup_by_name[normalized_name] = ingredient_id

        ingredient_ids_json = json.dumps(
            ingredient_ids,
            ensure_ascii=False,
            separators=(",", ":"),
        )

        def resolve_highlight_ids(entries: List[HighlightEntry]) -> List[str]:
            resolved: List[str] = []
            seen: Set[str] = set()
            for entry in entries:
                ingredient_id: Optional[str] = None
                if entry.ingredient_page:
                    lookup_key = normalise_url(entry.ingredient_page)
                    ingredient_id = ingredient_lookup_by_url.get(lookup_key)
                if not ingredient_id and entry.ingredient_name:
                    name_key = self._normalize_whitespace(entry.ingredient_name).lower()
                    ingredient_id = ingredient_lookup_by_name.get(name_key)
                if ingredient_id and ingredient_id not in seen:
                    resolved.append(ingredient_id)
                    seen.add(ingredient_id)
            return resolved

        key_ingredient_ids_json = json.dumps(
            resolve_highlight_ids(details.highlights.key_ingredients),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        other_ingredient_ids_json = json.dumps(
            resolve_highlight_ids(details.highlights.other_ingredients),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        free_tag_ids: List[str] = []
        for tag in details.highlights.free_tags:
            tag_id = self._ensure_free_tag(tag)
            free_tag_ids.append(tag_id)
        free_tag_ids_json = json.dumps(
            free_tag_ids,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        payload: Dict[str, object] = {
            "name": details.name,
            "description": details.description,
            "image_path": image_path,
            "ingredient_ids_json": ingredient_ids_json,
            "key_ingredient_ids_json": key_ingredient_ids_json,
            "other_ingredient_ids_json": other_ingredient_ids_json,
            "free_tag_ids_json": free_tag_ids_json,
            "discontinued": 1 if details.discontinued else 0,
            "replacement_product_url": details.replacement_product_url,
        }
        existing = self.conn.execute(
            """
            SELECT name, description, image_path, ingredient_ids_json,
                   key_ingredient_ids_json, other_ingredient_ids_json,
                   free_tag_ids_json, discontinued, replacement_product_url,
                   last_updated_at
            FROM products
            WHERE id = ?
            """,
            (product_id,),
        ).fetchone()
        now = self._current_timestamp()
        if image_path is None and existing and existing["image_path"]:
            payload["image_path"] = existing["image_path"]
        if existing is None:
            self.conn.execute(
                """
                UPDATE products
                SET name = :name,
                    description = :description,
                    image_path = :image_path,
                    ingredient_ids_json = :ingredient_ids_json,
                    key_ingredient_ids_json = :key_ingredient_ids_json,
                    other_ingredient_ids_json = :other_ingredient_ids_json,
                    free_tag_ids_json = :free_tag_ids_json,
                    discontinued = :discontinued,
                    replacement_product_url = :replacement_product_url,
                    last_checked_at = :last_checked_at,
                    last_updated_at = :last_updated_at
                WHERE id = :product_id
                """,
                {
                    **payload,
                    "product_id": product_id,
                    "last_checked_at": now,
                    "last_updated_at": now,
                },
            )
            return

        changed = False
        for column, value in payload.items():
            if existing[column] != value:
                changed = True
                break
        if changed or not existing["last_updated_at"]:
            update_values = {**payload, "last_checked_at": now, "last_updated_at": now}
            assignments = ", ".join(f"{column} = ?" for column in update_values)
            params = list(update_values.values()) + [product_id]
            self.conn.execute(
                f"UPDATE products SET {assignments} WHERE id = ?",
                params,
            )
        else:
            self.conn.execute(
                "UPDATE products SET last_checked_at = ? WHERE id = ?",
                (now, product_id),
            )

    def _ensure_ingredient(self, ingredient: IngredientReference) -> str:
        """Persist ingredient metadata and return the database identifier."""

        if ingredient.tooltip_ingredient_link:
            row = self.conn.execute(
                "SELECT id FROM ingredients WHERE url = ?",
                (ingredient.tooltip_ingredient_link,),
            ).fetchone()
            if row:
                return str(row["id"])
        row = self.conn.execute(
            "SELECT id FROM ingredients WHERE url = ?",
            (ingredient.url,),
        ).fetchone()
        if row:
            return str(row["id"])
        details = self._scrape_ingredient_page(ingredient.url)
        return self._store_ingredient_details(details)

    def _ensure_free_tag(self, free_tag: FreeTag) -> str:
        """Persist or update a free-form hashtag entry and return its id."""

        row = self.conn.execute(
            "SELECT id, tooltip FROM frees WHERE tag = ?",
            (free_tag.tag,),
        ).fetchone()
        if row:
            existing_tooltip = row["tooltip"] or ""
            new_tooltip = free_tag.tooltip or ""
            if new_tooltip and new_tooltip != existing_tooltip:
                self.conn.execute(
                    "UPDATE frees SET tooltip = ? WHERE id = ?",
                    (new_tooltip, row["id"]),
                )
            return str(row["id"])
        while True:
            tag_id = self._generate_id()
            try:
                self.conn.execute(
                    "INSERT INTO frees (id, tag, tooltip) VALUES (?, ?, ?)",
                    (tag_id, free_tag.tag, free_tag.tooltip),
                )
            except sqlite3.IntegrityError as exc:  # pragma: no cover - rare id collision
                message = str(exc)
                if "frees.id" in message:
                    continue
                if "frees.tag" in message:
                    row = self.conn.execute(
                        "SELECT id FROM frees WHERE tag = ?",
                        (free_tag.tag,),
                    ).fetchone()
                    if row:
                        return str(row["id"])
                raise
            return tag_id

    # ------------------------------------------------------------------
    # Ingredient scraping & persistence
    # ------------------------------------------------------------------
    def _scrape_ingredient_page(self, url: str) -> IngredientDetails:
        """Download and parse a single ingredient page."""

        LOGGER.info("Fetching ingredient details %s", url)
        html = self._fetch_html(url)
        if html is None:
            raise RuntimeError(f"Unable to download ingredient page {url}")
        return self._parse_ingredient_page(html, url)

    def _parse_ingredient_page(self, html: str, url: str) -> IngredientDetails:
        """Convert ingredient HTML into a structured :class:`IngredientDetails`."""

        root = parse_html(html)
        name_node = root.find(tag="h1", class_="klavikab") or root.find(tag="h1")
        rating_node = root.find(class_="ourtake")
        name = extract_text(name_node)
        rating_tag = extract_text(rating_node)
        label_map = self._build_label_map(root)
        also_called_node = label_map.get("also-called-like-this")
        irritancy_node = label_map.get("irritancy")
        comedogenicity_node = label_map.get("comedogenicity")
        cosing_record = self._retrieve_cosing_data(name)
        cosing_function_infos = [
            IngredientFunctionInfo(name=fn, url=None, description="")
            for fn in cosing_record.functions
        ]
        details_text = self._parse_details_text(root)
        quick_facts = self._parse_quick_facts(root)
        proof_references = self._parse_proof_references(root)
        raw_also_called = extract_text(also_called_node) if also_called_node else ""
        also_called_values: List[str] = []
        if raw_also_called:
            for part in re.split(r"[,;\n]", raw_also_called):
                candidate = self._normalize_whitespace(part)
                if candidate and candidate not in also_called_values:
                    also_called_values.append(candidate)
        return IngredientDetails(
            name=name,
            url=url,
            rating_tag=rating_tag,
            also_called=also_called_values,
            irritancy=self._extract_label_text(irritancy_node),
            comedogenicity=self._extract_label_text(comedogenicity_node),
            details_text=details_text,
            cosing_cas_numbers=cosing_record.cas_numbers,
            cosing_ec_numbers=cosing_record.ec_numbers,
            cosing_identified_ingredients=cosing_record.identified_ingredients,
            cosing_regulation_provisions=cosing_record.regulation_provisions,
            cosing_function_infos=cosing_function_infos,
            quick_facts=quick_facts,
            proof_references=proof_references,
        )

    def _retrieve_cosing_data(self, ingredient_name: str) -> CosIngRecord:
        """Fetch CosIng data for ``ingredient_name`` from the official portal."""

        ingredient_name = ingredient_name.strip()
        if not ingredient_name:
            return CosIngRecord()
        for search_term in self._cosing_search_terms(ingredient_name):
            detail_html = self._fetch_cosing_detail_via_playwright(
                search_term,
                original_name=ingredient_name,
            )
            if detail_html:
                return self._parse_cosing_detail_page(detail_html)
        return CosIngRecord()

    def _cosing_search_terms(self, ingredient_name: str) -> List[str]:
        """Return CosIng search fallbacks for names with slash-separated variants."""

        cleaned = ingredient_name.replace("\u200b", "").strip()
        if not cleaned:
            return []
        terms: List[str] = []
        if "/" in cleaned:
            for part in cleaned.split("/"):
                normalised = self._normalize_whitespace(part)
                if normalised and normalised not in terms:
                    terms.append(normalised)
        full_name = self._normalize_whitespace(cleaned)
        if full_name and full_name not in terms:
            terms.append(full_name)
        return terms

    def _fetch_cosing_detail_via_playwright(
        self,
        search_term: str,
        *,
        original_name: Optional[str] = None,
    ) -> Optional[str]:
        """Drive the CosIng interface with Playwright and return detail HTML."""

        page = self._get_cosing_playwright_page()
        if page is None:
            return None
        query = search_term.strip()
        if not query:
            return None
        display_name = original_name or query
        base_url = COSING_BASE_URL if COSING_BASE_URL.endswith("/") else f"{COSING_BASE_URL}/"
        try:
            page.goto(base_url, wait_until="domcontentloaded", timeout=15000)
        except PlaywrightTimeoutError:
            LOGGER.warning(
                "Timed out while loading CosIng search page for %s", display_name
            )
            return None
        except PlaywrightError:
            LOGGER.warning(
                "Failed to load CosIng search page for %s", display_name, exc_info=True
            )
            return None
        try:
            input_locator = page.locator("input#keyword")
            input_locator.wait_for(state="visible", timeout=5000)
            input_locator.fill(query)
        except PlaywrightError as exc:
            LOGGER.warning(
                "Unable to populate CosIng search input for %s: %s", display_name, exc
            )
            return None
        try:
            button_locator = page.locator("button.ecl-button--primary[type=submit]")
            if button_locator.count() == 0:
                button_locator = page.locator("button[type=submit]")
            button_locator.first.click()
        except PlaywrightError as exc:
            LOGGER.warning(
                "Unable to submit CosIng search for %s: %s", display_name, exc
            )
            return None
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeoutError:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=5000)
            except PlaywrightTimeoutError:
                LOGGER.debug("CosIng search results did not reach idle state", exc_info=True)
        self._wait_for_cosing_dynamic_content(page)
        html = page.content()
        root = parse_html(html)
        if self._is_cosing_detail_page(root):
            return html
        expected_name = None
        if original_name:
            expected_name = self._normalize_whitespace(original_name)
            if "/" not in original_name:
                expected_name = None
        anchor = self._find_cosing_result_anchor(
            root,
            query,
            expected_name=expected_name,
        )
        if anchor is None:
            return None
        href = anchor.get("href")
        if not href:
            return None
        try:
            locator = page.locator(f"a[href='{href}']")
            if locator.count() == 0:
                absolute_href = self._cosing_absolute_url(href)
                locator = page.locator(f"a[href='{absolute_href}']")
            if locator.count() == 0:
                return None
            locator.first.click()
        except PlaywrightError as exc:
            LOGGER.warning(
                "Failed to open CosIng search result %s for %s: %s",
                href,
                display_name,
                exc,
            )
            return None
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeoutError:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=5000)
            except PlaywrightTimeoutError:
                LOGGER.debug("CosIng detail page did not reach idle state", exc_info=True)
        self._wait_for_cosing_dynamic_content(page)
        detail_html = page.content()
        detail_root = parse_html(detail_html)
        if self._is_cosing_detail_page(detail_root):
            return detail_html
        return None

    def _wait_for_cosing_dynamic_content(self, page: Any, *, timeout: int = 15000) -> bool:
        """Wait until CosIng renders either the search results or detail table."""

        if page is None:
            return False
        selector = (
            "app-detail-subs table.ecl-table, "
            "app-results-subs table.ecl-table, "
            "table.ecl-table"
        )
        try:
            page.wait_for_selector(selector, timeout=timeout)
            return True
        except PlaywrightTimeoutError:
            LOGGER.debug("Timed out waiting for CosIng dynamic content", exc_info=True)
        except PlaywrightError:
            LOGGER.debug("Error waiting for CosIng dynamic content", exc_info=True)
        return False

    def _get_cosing_playwright_page(self) -> Optional[Any]:
        """Lazily initialise the shared Playwright browser instance."""

        if self._cosing_playwright_failed:
            return None
        if self._cosing_page is not None:
            return self._cosing_page
        if sync_playwright is None:
            LOGGER.debug("Playwright not available – skipping CosIng scraping")
            self._cosing_playwright_failed = True
            return None
        try:
            self._cosing_playwright = sync_playwright().start()
            self._cosing_browser = self._cosing_playwright.chromium.launch(headless=True)
            self._cosing_context = self._cosing_browser.new_context()
            self._cosing_page = self._cosing_context.new_page()
            return self._cosing_page
        except PlaywrightError:
            LOGGER.warning("Unable to initialise Playwright – CosIng scraping disabled", exc_info=True)
            self._cosing_playwright_failed = True
            return None

    def _find_cosing_result_anchor(
        self,
        root: Node,
        ingredient_name: str,
        *,
        expected_name: Optional[str] = None,
    ) -> Optional[Node]:
        """Choose the most likely search result for ``ingredient_name``."""

        table = root.find(tag="table", class_="ecl-table")
        if not table:
            return None
        search_name = ingredient_name.strip()
        if not search_name:
            return None
        target_key = self._cosing_lookup_key(search_name)
        query_words = self._cosing_lookup_words(search_name)
        expected_key = (
            self._cosing_lookup_key(expected_name)
            if expected_name
            else ""
        )
        expected_words = (
            self._cosing_lookup_words(expected_name)
            if expected_name
            else set()
        )
        best_rank: Tuple[int, int] = (4, 0)
        best_anchor: Optional[Node] = None
        exact_target_rank: Tuple[int, int] = (4, 0)
        exact_target_anchor: Optional[Node] = None
        for index, anchor in enumerate(table.find_all(tag="a")):
            href = anchor.get("href")
            if not href:
                continue
            anchor_text = self._normalize_whitespace(extract_text(anchor))
            if not anchor_text:
                if best_anchor is None:
                    best_rank = (3, index)
                    best_anchor = anchor
                continue
            anchor_key = self._cosing_lookup_key(anchor_text)
            row_node = anchor
            while row_node and row_node.tag != "tr":
                row_node = row_node.parent
            row_text = (
                self._normalize_whitespace(extract_text(row_node))
                if row_node
                else anchor_text
            )
            row_key = self._cosing_lookup_key(row_text)
            anchor_words = self._cosing_lookup_words(anchor_text)
            row_words = self._cosing_lookup_words(row_text)
            if expected_key:
                if anchor_key == expected_key or row_key == expected_key:
                    return anchor
            if anchor_key == target_key or row_key == target_key:
                if not expected_key:
                    return anchor
                target_exact_match = True
            else:
                target_exact_match = False
            if expected_words and (
                expected_words.issubset(anchor_words)
                or expected_words.issubset(row_words)
            ):
                return anchor
            match_type = 3
            match_score = index
            if target_key and (
                target_key in anchor_key or target_key in row_key
            ):
                match_type = 1
                container_length = (
                    len(anchor_key)
                    if target_key in anchor_key
                    else len(row_key)
                )
                match_score = max(container_length - len(target_key), 0)
            else:
                candidate_scores: List[int] = []
                if query_words and query_words.issubset(anchor_words):
                    candidate_scores.append(len(anchor_words) - len(query_words))
                if query_words and query_words.issubset(row_words):
                    candidate_scores.append(len(row_words) - len(query_words))
                if candidate_scores:
                    match_type = 2
                    match_score = min(candidate_scores)
            if target_exact_match:
                target_rank = (0, match_score)
                if (
                    exact_target_anchor is None
                    or target_rank < exact_target_rank
                ):
                    exact_target_rank = target_rank
                    exact_target_anchor = anchor
            if best_anchor is None or (match_type, match_score) < best_rank:
                best_rank = (match_type, match_score)
                best_anchor = anchor
        if expected_key and exact_target_anchor is not None:
            return exact_target_anchor
        return best_anchor

    def _is_cosing_detail_page(self, root: Node) -> bool:
        """Determine whether ``root`` already represents a CosIng detail page."""

        for cell in root.find_all(tag="td"):
            text = self._normalize_whitespace(extract_text(cell)).lower()
            if text == "inci name":
                return True
        return False

    def _parse_cosing_detail_page(self, html: str) -> CosIngRecord:
        """Parse the CosIng detail HTML page into a :class:`CosIngRecord`."""

        root = parse_html(html)
        table_body = root.find(tag="tbody")
        if not table_body:
            return CosIngRecord()
        record = CosIngRecord()
        for row in table_body.find_all(tag="tr"):
            cells = [
                child
                for child in row.children
                if isinstance(child, Node) and child.tag == "td"
            ]
            if len(cells) < 2:
                continue
            label = self._normalize_whitespace(extract_text(cells[0])).lower()
            value_node = cells[1]
            if label == "cas #":
                record.cas_numbers = self._extract_cosing_values(
                    value_node, split_commas=True
                )
            elif label == "ec #":
                record.ec_numbers = self._extract_cosing_values(
                    value_node, split_commas=True
                )
            elif label.startswith("identified ingredients"):
                record.identified_ingredients = self._extract_cosing_values(
                    value_node, split_commas=True
                )
            elif label.startswith("cosmetics regulation provisions"):
                record.regulation_provisions = self._extract_cosing_values(
                    value_node,
                    split_commas=False,
                    split_slashes=False,
                    split_semicolons=False,
                )
                if record.regulation_provisions:
                    if len(record.regulation_provisions) == 1:
                        single_value = record.regulation_provisions[0]
                        if re.fullmatch(r"[A-Z0-9/\s,;-]+", single_value):
                            parts = [
                                part.strip()
                                for part in re.split(
                                    r"\s+/\s+|,\s*|;\s*",
                                    single_value,
                                )
                                if part.strip()
                            ]
                            if parts and len(parts) > 1:
                                record.regulation_provisions = parts
            elif label == "functions":
                record.functions = [
                    self._normalise_cosing_function_name(value)
                    for value in self._extract_cosing_values(
                        value_node, split_commas=True
                    )
                ]
        return record

    def _extract_cosing_values(
        self,
        node: Node,
        *,
        split_commas: bool = False,
        split_slashes: bool = True,
        split_semicolons: bool = True,
    ) -> List[str]:
        """Normalise a CosIng table value into a list of strings."""

        items: List[str] = []
        list_container = node.find(tag="ul")
        if list_container:
            for li in list_container.find_all(tag="li"):
                text = self._normalize_whitespace(extract_text(li))
                if text:
                    items.append(text)
        else:
            raw_text = self._normalize_whitespace(extract_text(node))
            if raw_text:
                pattern_parts: List[str] = []
                if split_slashes:
                    pattern_parts.append(r"\s*/\s*")
                if split_commas:
                    pattern_parts.append(r",\s*")
                if split_semicolons:
                    pattern_parts.append(r";\s*")
                if pattern_parts:
                    pattern = "|".join(pattern_parts)
                    fragments = re.split(pattern, raw_text)
                else:
                    fragments = [raw_text]
                for part in fragments:
                    value = part.strip()
                    if value:
                        items.append(value)
        unique_items: List[str] = []
        seen: Set[str] = set()
        for item in items:
            key = item.casefold()
            if key in seen:
                continue
            seen.add(key)
            unique_items.append(item)
        return unique_items

    def _normalise_cosing_function_name(self, value: str) -> str:
        """Return the CosIng function name with each word capitalised."""

        parts = re.split(r"(\W+)", value.strip())
        normalised: List[str] = []
        for part in parts:
            if not part:
                continue
            if part.isalpha():
                normalised.append(part[0].upper() + part[1:].lower())
            else:
                normalised.append(part)
        formatted = "".join(normalised)
        return formatted or value.strip()

    def _cosing_absolute_url(self, href: str) -> str:
        """Convert relative CosIng links to absolute URLs."""

        if href.startswith("http://") or href.startswith("https://"):
            return href
        base = COSING_BASE_URL if COSING_BASE_URL.endswith("/") else f"{COSING_BASE_URL}/"
        return parse.urljoin(base, href)

    def _cosing_lookup_key(self, value: str) -> str:
        """Return a simplified representation suitable for equality checks."""

        normalized = unicodedata.normalize("NFKC", value)
        simplified = re.sub(r"[^0-9a-z]+", "", normalized.lower())
        return simplified

    def _cosing_lookup_words(self, value: str) -> Set[str]:
        """Break a CosIng label into comparable lowercase tokens."""

        normalized = unicodedata.normalize("NFKC", value)
        tokens = re.split(r"[^0-9a-z]+", normalized.lower())
        return {token for token in tokens if token}

    def _build_label_map(self, root: Node) -> Dict[str, Node]:
        """Associate label slugs with their corresponding value nodes."""

        label_map: Dict[str, Node] = {}

        def register(label_node: Optional[Node], value_node: Optional[Node]) -> None:
            if not label_node or not value_node:
                return
            label_text = self._normalize_whitespace(extract_text(label_node))
            if not label_text:
                return
            slug = label_text.lower().rstrip(":")
            slug = slug.replace(":", "")
            slug = slug.replace(" ", "-")
            if slug:
                label_map[slug] = value_node

        # Newer markup renders metadata rows with generic ``itemprop`` containers.
        for container in root.find_all(class_="itemprop"):
            label_node = container.find(class_="label")
            value_node = container.find(class_="value")
            if not value_node:
                value_node = self._find_value_node(label_node)
            register(label_node, value_node or container)

        # Legacy markup used a dedicated BEM style grid.
        for row in root.find_all(class_="ingredient-overview__row"):
            label_node = row.find(class_="ingredient-overview__row-title")
            value_node = row.find(class_="ingredient-overview__row-content")
            register(label_node, value_node or row)
        return label_map

    def _find_value_node(self, label_node: Node) -> Optional[Node]:
        """Return the sibling value node of ``label_node`` if present."""

        parent = getattr(label_node, "parent", None)
        if not parent:
            return None
        for child in getattr(parent, "children", []):
            if isinstance(child, Node) and child.has_class("value"):
                return child
        for child in parent.children:
            if isinstance(child, Node) and child is not label_node:
                return child
        return None

    def _extract_label_text(self, node: Optional[Node]) -> str:
        """Return the textual content of a label field."""

        if node is None:
            return ""
        target = node
        if isinstance(node, Node) and not node.has_class("value"):
            value_node = self._find_value_node(node)
            if value_node is None:
                return ""
            target = value_node
        return self._normalize_whitespace(extract_text(target))

    def _parse_details_text(self, root: Node) -> str:
        """Return the prose detail block as a clean string."""

        content_node = root.find(id_="details-text")
        if not content_node:
            details_section = root.find(id_="details")
            if details_section:
                content_node = details_section.find(class_="content") or details_section
        if not content_node:
            content_node = root.find(class_="detailmore")
        if not content_node:
            return ""
        blocks: List[str] = []

        def visit(node: Node) -> None:
            for item in node.children:
                if isinstance(item, str):
                    text = self._normalize_whitespace(item)
                    if text:
                        blocks.append(text)
                    continue
                if not isinstance(item, Node):
                    continue
                if item.tag == "p":
                    text = self._normalize_whitespace(extract_text(item))
                    if text:
                        blocks.append(text)
                    continue
                if item.has_class("showmore-link"):
                    continue
                if item.tag in {"ul", "ol"}:
                    entries: List[str] = []
                    for child in item.children:
                        if isinstance(child, Node) and child.tag == "li":
                            text = self._normalize_whitespace(extract_text(child))
                            if text:
                                entries.append(f"- {text}")
                    if entries:
                        blocks.append("\n".join(entries))
                    continue
                visit(item)

        visit(content_node)
        if not blocks:
            text = self._normalize_whitespace(extract_text(content_node))
            return text
        return "\n\n".join(blocks)

    def _parse_quick_facts(self, root: Node) -> List[str]:
        """Return the bullet point quick facts section as a list of strings."""

        section = root.find(id_="quickfacts")
        if not section:
            return []
        facts: List[str] = []
        for item in section.find_all(tag="li"):
            text = self._normalize_whitespace(extract_text(item))
            if text:
                facts.append(text)
        return facts

    def _parse_proof_references(self, root: Node) -> List[str]:
        """Collect the bibliography style entries from the proof section."""

        section = root.find(id_="proof")
        if not section:
            return []
        references: List[str] = []
        for item in section.find_all(tag="li"):
            text = self._normalize_whitespace(extract_text(item))
            if text:
                references.append(text)
        return references

    def _store_ingredient_details(self, details: IngredientDetails) -> str:
        """Persist ingredient metadata and return the database identifier."""

        cosing_function_ids: List[str] = []
        for function in details.cosing_function_infos:
            function_id = self._ensure_ingredient_function(function)
            if function_id is not None:
                cosing_function_ids.append(function_id)
        payload: Dict[str, object] = {
            "name": details.name,
            "rating_tag": details.rating_tag,
            "also_called": json.dumps(
                details.also_called,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "cosing_function_ids_json": json.dumps(
                cosing_function_ids,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "irritancy": details.irritancy,
            "comedogenicity": details.comedogenicity,
            "details_text": details.details_text,
            "cosing_cas_numbers_json": json.dumps(
                details.cosing_cas_numbers,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "cosing_ec_numbers_json": json.dumps(
                details.cosing_ec_numbers,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "cosing_identified_ingredients_json": json.dumps(
                details.cosing_identified_ingredients,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "cosing_regulation_provisions_json": json.dumps(
                details.cosing_regulation_provisions,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "quick_facts_json": json.dumps(
                details.quick_facts,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "proof_references_json": json.dumps(
                details.proof_references,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }
        existing = self.conn.execute(
            """
            SELECT id, name, rating_tag, also_called, cosing_function_ids_json,
                   irritancy, comedogenicity, details_text, cosing_cas_numbers_json,
                   cosing_ec_numbers_json, cosing_identified_ingredients_json,
                   cosing_regulation_provisions_json, quick_facts_json,
                   proof_references_json, last_updated_at
            FROM ingredients
            WHERE url = ?
            """,
            (details.url,),
        ).fetchone()
        now = self._current_timestamp()
        result_id: Optional[str]
        if existing is None:
            while True:
                ingredient_id = self._generate_id()
                try:
                    self.conn.execute(
                        """
                        INSERT INTO ingredients (
                            id, name, url, rating_tag, also_called, cosing_function_ids_json,
                            irritancy, comedogenicity, details_text, cosing_cas_numbers_json,
                            cosing_ec_numbers_json, cosing_identified_ingredients_json,
                            cosing_regulation_provisions_json, quick_facts_json,
                            proof_references_json, last_checked_at, last_updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            ingredient_id,
                            details.name,
                            details.url,
                            details.rating_tag,
                            payload["also_called"],
                            payload["cosing_function_ids_json"],
                            details.irritancy,
                            details.comedogenicity,
                            details.details_text,
                            payload["cosing_cas_numbers_json"],
                            payload["cosing_ec_numbers_json"],
                            payload["cosing_identified_ingredients_json"],
                            payload["cosing_regulation_provisions_json"],
                            payload["quick_facts_json"],
                            payload["proof_references_json"],
                            now,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError as exc:  # pragma: no cover - rare id collision
                    if "ingredients.id" in str(exc):
                        continue
                    raise
                break
            result_id = ingredient_id
        else:
            changed = False
            for column, value in payload.items():
                if existing[column] != value:
                    changed = True
                    break
            if changed or not existing["last_updated_at"]:
                update_values = {**payload, "last_checked_at": now, "last_updated_at": now}
                assignments = ", ".join(f"{column} = ?" for column in update_values)
                params = list(update_values.values()) + [existing["id"]]
                self.conn.execute(
                    f"UPDATE ingredients SET {assignments} WHERE id = ?",
                    params,
                )
            else:
                self.conn.execute(
                    "UPDATE ingredients SET last_checked_at = ? WHERE id = ?",
                    (now, existing["id"]),
                )
            result_id = str(existing["id"])
        row = self.conn.execute(
            "SELECT id FROM ingredients WHERE url = ?",
            (details.url,),
        ).fetchone()
        if not row:
            raise RuntimeError(f"Unable to store ingredient {details.url}")
        return result_id

    def _ensure_ingredient_function(self, info: IngredientFunctionInfo) -> Optional[str]:
        """Ensure an ingredient function entry exists and return its id."""

        raw_name = self._normalize_whitespace(info.name)
        if not raw_name:
            return None
        row = self.conn.execute(
            """
            SELECT id, name
            FROM functions
            WHERE LOWER(name) = LOWER(?)
            """,
            (raw_name,),
        ).fetchone()
        if row:
            stored_name = self._normalize_whitespace(row["name"] or "")
            if stored_name != raw_name:
                self.conn.execute(
                    "UPDATE functions SET name = ? WHERE id = ?",
                    (raw_name, row["id"]),
                )
            return str(row["id"])
        while True:
            function_id = self._generate_id()
            try:
                self.conn.execute(
                    "INSERT INTO functions (id, name) VALUES (?, ?)",
                    (function_id, raw_name),
                )
            except sqlite3.IntegrityError as exc:  # pragma: no cover - rare id collision
                if "functions.id" in str(exc):
                    continue
                raise
            return function_id

    # ------------------------------------------------------------------
    # Resource management
    # ------------------------------------------------------------------
    def _shutdown_cosing_resources(self) -> None:
        """Release any Playwright resources that may have been allocated."""

        if self._cosing_page is not None:
            try:
                self._cosing_page.close()
            except PlaywrightError:
                LOGGER.debug("Ignoring Playwright page close error", exc_info=True)
            self._cosing_page = None
        if self._cosing_context is not None:
            try:
                self._cosing_context.close()
            except PlaywrightError:
                LOGGER.debug("Ignoring Playwright context close error", exc_info=True)
            self._cosing_context = None
        if self._cosing_browser is not None:
            try:
                self._cosing_browser.close()
            except PlaywrightError:
                LOGGER.debug("Ignoring Playwright browser close error", exc_info=True)
            self._cosing_browser = None
        if self._cosing_playwright is not None:
            try:
                self._cosing_playwright.stop()
            except PlaywrightError:
                LOGGER.debug("Ignoring Playwright shutdown error", exc_info=True)
            self._cosing_playwright = None

