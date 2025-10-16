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

import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib import error, parse, request

from .parser import Node, extract_text, parse_html

LOGGER = logging.getLogger(__name__)

BASE_URL = "https://incidecoder.com"
USER_AGENT = "INCIScraper/1.0 (+https://incidecoder.com)"
DEFAULT_TIMEOUT = 30
REQUEST_SLEEP = 0.5  # polite delay between HTTP requests
PROGRESS_LOG_INTERVAL = 10


@dataclass
class IngredientReference:
    name: str
    url: str
    tooltip_text: Optional[str]
    tooltip_ingredient_link: Optional[str]


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


@dataclass
class IngredientDetails:
    name: str
    url: str
    rating_tag: str
    also_called: str
    what_it_does: List[str]
    what_it_does_links: List[str]
    irritancy: str
    comedogenicity: str
    tooltip_links: List[str]
    official_cosing: Dict[str, str]
    details_section: str
    details_links: List[str]
    related_products: List[Dict[str, str]]


class INCIScraper:
    """Main entry point that orchestrates all scraping steps."""

    def __init__(
        self,
        *,
        db_path: str = "incidecoder.db",
        image_dir: str | os.PathLike[str] = "images",
        base_url: str = BASE_URL,
        request_timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = request_timeout
        self.image_dir = Path(image_dir)
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
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
        self._set_metadata("brands_total_offsets", str(final_total))
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
                ingredient_functions_json TEXT,
                highlights_json TEXT,
                details_scraped INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS ingredients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                rating_tag TEXT,
                also_called TEXT,
                what_it_does_json TEXT,
                what_it_does_links_json TEXT,
                irritancy TEXT,
                comedogenicity TEXT,
                tooltip_links_json TEXT,
                official_cosing_json TEXT,
                details_section_html TEXT,
                details_links_json TEXT
            );

            CREATE TABLE IF NOT EXISTS ingredient_related_products (
                ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
                product_name TEXT NOT NULL,
                product_url TEXT NOT NULL,
                UNIQUE (ingredient_id, product_url)
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
            LOGGER.debug("Fetching product listing page %s", page_url)
            html = self._fetch_html(page_url)
            if html is None:
                LOGGER.warning("Unable to download product listing %s", page_url)
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
        return ProductDetails(
            name=name,
            description=description,
            image_url=image_url,
            ingredients=ingredients,
            ingredient_functions=ingredient_functions,
            highlights=highlights,
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
            """
            UPDATE products
            SET name = ?, description = ?, image_path = ?,
                ingredient_functions_json = ?, highlights_json = ?
            WHERE id = ?
            """,
            (
                details.name,
                details.description,
                image_path,
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
                product_id,
            ),
        )
        self.conn.execute(
            "DELETE FROM product_ingredients WHERE product_id = ?",
            (product_id,),
        )
        for ingredient in details.ingredients:
            ingredient_id = self._ensure_ingredient(ingredient)
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
        what_it_does = self._extract_list_from_value(what_it_does_nodes)
        what_it_does_links = self._extract_links_from_value(what_it_does_nodes)
        tooltip_links: List[str] = []
        for container in root.find_all(class_="info-circle-comedog-details"):
            for anchor in container.find_all(tag="a"):
                href = anchor.get("href")
                if href:
                    tooltip_links.append(self._absolute_url(href))
        cosing = self._parse_cosing_section(root)
        details_section_html, details_links = self._parse_details_section(root)
        related_products = self._parse_related_products(root)
        return IngredientDetails(
            name=name,
            url=url,
            rating_tag=rating_tag,
            also_called=extract_text(also_called_node) if also_called_node else "",
            what_it_does=what_it_does,
            what_it_does_links=what_it_does_links,
            irritancy=extract_text(irritancy_node) if irritancy_node else "",
            comedogenicity=extract_text(comedogenicity_node) if comedogenicity_node else "",
            tooltip_links=tooltip_links,
            official_cosing=cosing,
            details_section=details_section_html,
            details_links=details_links,
            related_products=related_products,
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

    def _extract_list_from_value(self, node: Optional[Node]) -> List[str]:
        if not node:
            return []
        items: List[str] = []
        for anchor in node.find_all(tag="a"):
            text = extract_text(anchor)
            if text:
                items.append(text)
        if not items:
            text = extract_text(node)
            if text:
                items.append(text)
        return items

    def _extract_links_from_value(self, node: Optional[Node]) -> List[str]:
        if not node:
            return []
        links: List[str] = []
        for anchor in node.find_all(tag="a"):
            href = anchor.get("href")
            if href:
                links.append(self._absolute_url(href))
        return links

    def _parse_cosing_section(self, root: Node) -> Dict[str, str]:
        section = root.find(id_="cosing-data")
        if not section:
            return {"all_functions": "", "description": "", "cas_number": "", "ec_number": ""}
        values: Dict[str, str] = {"all_functions": "", "description": "", "cas_number": "", "ec_number": ""}
        for bold in section.find_all(tag="b"):
            label = extract_text(bold).lower().strip(":")
            key = label.replace(" ", "_")
            if key not in values:
                continue
            text_parts: List[str] = []
            for item in self._iter_next_content(bold):
                if isinstance(item, Node) and item.tag == "b":
                    break
                if isinstance(item, Node):
                    text_parts.append(extract_text(item))
                elif isinstance(item, str):
                    text_parts.append(item)
            values[key] = " ".join(part.strip() for part in text_parts if part).strip()
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

    def _parse_details_section(self, root: Node) -> Tuple[str, List[str]]:
        section = root.find(id_="details")
        if not section:
            return "", []
        content_node = section.find(class_="content") or section
        paragraphs = [p.get_inner_html() for p in content_node.find_all(tag="p")]
        links: List[str] = []
        for anchor in content_node.find_all(tag="a"):
            href = anchor.get("href")
            if href:
                links.append(self._absolute_url(href))
        return "\n".join(paragraphs), links

    def _parse_related_products(self, root: Node) -> List[Dict[str, str]]:
        container = root.find(id_="product")
        if not container:
            return []
        entries: List[Dict[str, str]] = []
        for anchor in container.find_all(tag="a"):
            if not anchor.has_class("simpletextlistitem"):
                continue
            href = anchor.get("href")
            name = extract_text(anchor)
            if href and name:
                entries.append(
                    {
                        "product_name": name,
                        "product_page": self._absolute_url(href),
                    }
                )
        return entries

    def _store_ingredient_details(self, details: IngredientDetails) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO ingredients (
                name, url, rating_tag, also_called, what_it_does_json, irritancy,
                what_it_does_links_json, comedogenicity, tooltip_links_json,
                official_cosing_json, details_section_html, details_links_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                details.name,
                details.url,
                details.rating_tag,
                details.also_called,
                json.dumps(details.what_it_does, ensure_ascii=False),
                json.dumps(details.what_it_does_links, ensure_ascii=False),
                details.irritancy,
                details.comedogenicity,
                json.dumps(details.tooltip_links, ensure_ascii=False),
                json.dumps(details.official_cosing, ensure_ascii=False),
                details.details_section,
                json.dumps(details.details_links, ensure_ascii=False),
            ),
        )
        ingredient_id = cur.lastrowid
        for entry in details.related_products:
            self.conn.execute(
                "INSERT OR IGNORE INTO ingredient_related_products (ingredient_id, product_name, product_url) VALUES (?, ?, ?)",
                (ingredient_id, entry["product_name"], entry["product_page"]),
            )
        return ingredient_id

    # ------------------------------------------------------------------
    # Networking helpers
    # ------------------------------------------------------------------
    def _fetch_html(self, url: str) -> Optional[str]:
        data = self._fetch(url)
        if data is None:
            return None
        return data.decode("utf-8", errors="replace")

    def _fetch(self, url: str) -> Optional[bytes]:
        LOGGER.debug("Downloading %s", url)
        req = request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                return response.read()
        except error.URLError as exc:  # pragma: no cover - network errors are hard to simulate
            LOGGER.error("Failed to download %s: %s", url, exc)
            return None

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
        filename = f"{product_id}_{self._slugify(product_name)}{suffix}"
        path = self.image_dir / filename
        path.write_bytes(data)
        return str(path)

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


__all__ = ["INCIScraper"]
