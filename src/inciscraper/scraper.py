"""High level scraping logic for collecting data from INCIDecoder.

Türkçe: INCIDecoder verilerini toplamak için kullanılan üst düzey scraping
mantığı.

The scraper follows three sequential stages that mirror the data hierarchy on
INCIDecoder:

1. **Brands** – iterate over the paginated brand list and persist every brand
   name and URL in the ``brands`` table.
2. **Products** – for each brand, walk through the paginated product listing and
   store every product in the ``products`` table.
3. **Product details** – visit each product page, capture the structured
   information (description, ingredients, highlights, etc.), download the lead
   image and make sure ingredient level data is stored in the ``ingredients``
   table. Ingredient references that previously required the
   ``product_ingredients`` bridge table are now persisted directly on the
   product record.

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
import secrets
import socket
import sqlite3
import ssl
import time
import unicodedata
from datetime import datetime, timezone
from io import BytesIO
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib import error, parse, request

try:
    from PIL import Image, ImageFile
except ModuleNotFoundError:  # pragma: no cover - optional dependency safeguard
    Image = None  # type: ignore[assignment]
    ImageFile = None  # type: ignore[assignment]

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

from .parser import Node, extract_text, parse_html

LOGGER = logging.getLogger(__name__)

BASE_URL = "https://incidecoder.com"
COSING_BASE_URL = "https://ec.europa.eu/growth/tools-databases/cosing"
USER_AGENT = "INCIScraper/1.0 (+https://incidecoder.com)"
DEFAULT_TIMEOUT = 30
REQUEST_SLEEP = 0.5  # polite delay between HTTP requests
PROGRESS_LOG_INTERVAL = 10

EXPECTED_SCHEMA: Dict[str, Set[str]] = {
    "brands": {
        "id",
        "name",
        "url",
        "products_scraped",
        "last_checked_at",
        "last_updated_at",
    },
    "products": {
        "id",
        "brand_id",
        "name",
        "url",
        "description",
        "image_path",
        "ingredient_ids_json",
        "key_ingredient_ids_json",
        "other_ingredient_ids_json",
        "free_tag_ids_json",
        "discontinued",
        "replacement_product_url",
        "details_scraped",
        "last_checked_at",
        "last_updated_at",
    },
    "ingredients": {
        "id",
        "name",
        "url",
        "rating_tag",
        "also_called",
        "irritancy",
        "comedogenicity",
        "details_text",
        "cosing_cas_numbers_json",
        "cosing_ec_numbers_json",
        "cosing_identified_ingredients_json",
        "cosing_regulation_provisions_json",
        "cosing_function_ids_json",
        "quick_facts_json",
        "proof_references_json",
        "last_checked_at",
        "last_updated_at",
    },
    "functions": {
        "id",
        "name",
        "url",
        "description",
    },
    "frees": {
        "id",
        "tag",
        "tooltip",
    },
    "metadata": {"key", "value"},
}

ADDITIONAL_COLUMN_DEFINITIONS: Dict[str, Dict[str, str]] = {
    "brands": {
        "last_checked_at": "last_checked_at TEXT",
        "last_updated_at": "last_updated_at TEXT",
    },
    "products": {
        "key_ingredient_ids_json": "key_ingredient_ids_json TEXT",
        "other_ingredient_ids_json": "other_ingredient_ids_json TEXT",
        "free_tag_ids_json": "free_tag_ids_json TEXT",
        "last_checked_at": "last_checked_at TEXT",
        "last_updated_at": "last_updated_at TEXT",
    },
    "ingredients": {
        "last_checked_at": "last_checked_at TEXT",
        "last_updated_at": "last_updated_at TEXT",
        "cosing_cas_numbers_json": "cosing_cas_numbers_json TEXT",
        "cosing_ec_numbers_json": "cosing_ec_numbers_json TEXT",
        "cosing_identified_ingredients_json": "cosing_identified_ingredients_json TEXT",
        "cosing_regulation_provisions_json": "cosing_regulation_provisions_json TEXT",
        "cosing_function_ids_json": "cosing_function_ids_json TEXT",
        "quick_facts_json": "quick_facts_json TEXT",
        "proof_references_json": "proof_references_json TEXT",
    },
}


@dataclass
class IngredientReference:
    """Reference to an ingredient mentioned within a product listing.

    Türkçe: Ürün sayfasında geçen bir bileşene ait referans bilgisini temsil eder.
    """
    name: str
    url: str
    tooltip_text: Optional[str]
    tooltip_ingredient_link: Optional[str]
    ingredient_id: Optional[str] = None


@dataclass
class IngredientFunction:
    """Function metadata extracted for an ingredient.

    Türkçe: Bir bileşen için çıkarılan fonksiyon bilgilerini temsil eder.
    """
    ingredient_name: str
    ingredient_page: Optional[str]
    what_it_does: List[str]
    function_links: List[str]


@dataclass
class CosIngRecord:
    """Structured data retrieved from the CosIng public database."""

    cas_numbers: List[str] = field(default_factory=list)
    ec_numbers: List[str] = field(default_factory=list)
    identified_ingredients: List[str] = field(default_factory=list)
    regulation_provisions: List[str] = field(default_factory=list)
    functions: List[str] = field(default_factory=list)


@dataclass
class HighlightEntry:
    """Represents a highlighted ingredient and optional function link.

    Türkçe: Öne çıkarılan bileşen ve varsa fonksiyon bağlantısını temsil eder.
    """
    function_name: Optional[str]
    function_link: Optional[str]
    ingredient_name: Optional[str]
    ingredient_page: Optional[str]


@dataclass
class FreeTag:
    """A hashtag style marketing claim with an optional tooltip.

    Türkçe: Tooltip açıklamasıyla beraber gelen hashtag tarzı pazarlama ifadesi.
    """

    tag: str
    tooltip: Optional[str]


@dataclass
class ProductHighlights:
    """Container for hashtag and ingredient highlight sections.

    Türkçe: Hashtag ve bileşen vurgularını tutan veri yapısıdır.
    """

    free_tags: List[FreeTag]
    key_ingredients: List[HighlightEntry]
    other_ingredients: List[HighlightEntry]


@dataclass
class ProductDetails:
    """Structured representation of all parsed product details.

    Türkçe: Ayrıştırılan ürün detaylarını yapılandırılmış şekilde tutar.
    """
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
    """Normalized information fetched from an ingredient page.

    Türkçe: Bir bileşen sayfasından toplanan normalleştirilmiş bilgileri içerir.
    """
    name: str
    url: str
    rating_tag: str
    also_called: str
    irritancy: str
    comedogenicity: str
    details_text: str
    cosing_cas_numbers: List[str]
    cosing_ec_numbers: List[str]
    cosing_identified_ingredients: List[str]
    cosing_regulation_provisions: List[str]
    cosing_function_infos: List["IngredientFunctionInfo"]
    quick_facts: List[str]
    proof_references: List[str]


@dataclass
class IngredientFunctionInfo:
    """Describes a single cosmetic function entry.

    Türkçe: Tek bir kozmetik fonksiyon kaydını açıklar.
    """
    name: str
    url: Optional[str]
    description: str


class INCIScraper:
    """Main entry point that orchestrates all scraping steps.

    Türkçe: Scraping sürecindeki tüm aşamaları yöneten ana giriş sınıfı.
    """

    def __init__(
        self,
        *,
        db_path: str = "incidecoder.db",
        image_dir: str | os.PathLike[str] = "images",
        base_url: str = BASE_URL,
        request_timeout: int = DEFAULT_TIMEOUT,
        alternate_base_urls: Optional[Iterable[str]] = None,
    ) -> None:
        """Configure the scraper runtime and open the database.

        Türkçe: Scraper çalışma zamanını yapılandırır ve veritabanı bağlantısını
        açar.
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = request_timeout
        self.image_dir = Path(image_dir)
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_db()
        self._host_failover: Dict[str, str] = {}
        self._host_ip_overrides: Dict[str, str] = {}
        self._host_alternatives = self._build_host_alternatives(
            self.base_url, alternate_base_urls or []
        )
        self._ssl_context = ssl.create_default_context()
        self._cosing_playwright: Optional[Any] = None
        self._cosing_browser: Optional[Any] = None
        self._cosing_context: Optional[Any] = None
        self._cosing_page: Optional[Any] = None
        self._cosing_playwright_failed = False

    @staticmethod
    def _generate_id() -> str:
        """Return a random identifier suitable for primary keys."""

        return secrets.token_hex(16)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def run(self) -> None:
        """Execute the full scraping pipeline.

        Türkçe: Tüm scraping adımlarını sırasıyla çalıştırır.
        """

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
        """Create a compact dataset used for smoke testing the scraper.

        Türkçe: Scraper'ın hızlı doğrulaması için küçük bir örnek veri seti
        oluşturur.
        """

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
        """Complete any partial work recorded in the metadata table.

        Türkçe: Metadata tablosundaki yarım kalmış işleri tamamlar.
        """

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
        max_brands: int | None = None,
    ) -> None:
        """Collect brand listings and persist them to the database.

        Türkçe: Marka listelerini toplar ve veritabanına kaydeder.
        """
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
            page_url = self._append_offset(f"{self.base_url}/brands", offset)
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
            limit_reached = False
            for name, url in brands:
                inserted = self._insert_brand(name, url)
                if inserted:
                    new_entries += 1
                if (
                    inserted
                    and max_brands is not None
                    and existing_brand_total + new_entries >= max_brands
                ):
                    limit_reached = True
                    break
            LOGGER.info("Stored %s brands from %s", new_entries, page_url)
            self.conn.commit()
            self._set_metadata("brands_next_offset", str(offset + 1))
            existing_brand_total += new_entries
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
            if limit_reached:
                LOGGER.info(
                    "Reached brand limit (%s) – stopping brand scraping early",
                    max_brands,
                )
                break
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

    def scrape_products(
        self,
        *,
        max_brands: int | None = None,
        max_products_per_brand: int | None = None,
        rescan_all: bool = False,
    ) -> None:
        """Discover products for each brand pending product scraping.

        Türkçe: Ürün taraması bekleyen markalar için ürünleri keşfeder.
        """
        self._reset_brand_completion_flags_if_products_empty()
        self._retry_incomplete_brand_products()
        if rescan_all:
            cursor = self.conn.execute(
                "SELECT id, name, url FROM brands ORDER BY id"
            )
        else:
            cursor = self.conn.execute(
                "SELECT id, name, url FROM brands WHERE products_scraped = 0 ORDER BY id"
            )
        pending_brands = cursor.fetchall()
        if max_brands is not None:
            pending_brands = pending_brands[:max_brands]
        total_brands = len(pending_brands)
        if total_brands == 0:
            LOGGER.info("No brands require product scraping – skipping stage")
            return
        if rescan_all:
            LOGGER.info("Product workload: revalidating %s brand(s)", total_brands)
        else:
            LOGGER.info("Product workload: %s brand(s) awaiting scraping", total_brands)
        processed = 0
        for brand in pending_brands:
            brand_id = brand["id"]
            brand_url = brand["url"]
            resume_key = f"brand_products_next_offset:{brand_id}"
            if rescan_all:
                start_offset = 1
                self._delete_metadata(resume_key)
            else:
                start_offset = int(self._get_metadata(resume_key, "1"))
            if start_offset > 1:
                LOGGER.info(
                    "Resuming product collection for brand %s from offset %s",
                    brand["name"],
                    start_offset,
                )
            LOGGER.info("Collecting products for brand %s (%s)", brand["name"], brand_url)
            products_found, completed, next_offset = self._collect_products_for_brand(
                brand_id,
                brand_url,
                start_offset=start_offset,
                max_products=max_products_per_brand,
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

    def scrape_product_details(self, *, rescan_all: bool = False) -> None:
        """Download and persist detailed information for each product.

        Türkçe: Her ürünün detay sayfasını indirip veritabanına kaydeder.
        """
        if rescan_all:
            cursor = self.conn.execute(
                "SELECT id, brand_id, name, url, details_scraped FROM products ORDER BY id"
            )
        else:
            cursor = self.conn.execute(
                "SELECT id, brand_id, name, url, details_scraped FROM products WHERE details_scraped = 0 ORDER BY id"
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
            image_path = self._download_product_image(details.image_url, details.name, product["id"])
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

    def close(self) -> None:
        """Close the underlying SQLite connection and release Playwright resources.

        Türkçe: Kullanılan SQLite bağlantısını ve Playwright kaynaklarını kapatır.
        """
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
        self.conn.close()

    # ------------------------------------------------------------------
    # Database initialisation
    # ------------------------------------------------------------------
    def _init_db(self) -> None:
        """Create required tables and ensure the schema is up to date.

        Türkçe: Gerekli tabloları oluşturur ve şemanın güncel olduğundan emin olur.
        """
        cursor = self.conn.cursor()
        self._enforce_schema()
        cursor.executescript(
            """
            CREATE TABLE IF NOT EXISTS brands (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                products_scraped INTEGER NOT NULL DEFAULT 0,
                last_checked_at TEXT,
                last_updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS products (
                id TEXT PRIMARY KEY,
                brand_id TEXT NOT NULL REFERENCES brands(id),
                name TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                description TEXT,
                image_path TEXT,
                ingredient_ids_json TEXT,
                key_ingredient_ids_json TEXT,
                other_ingredient_ids_json TEXT,
                free_tag_ids_json TEXT,
                discontinued INTEGER NOT NULL DEFAULT 0,
                replacement_product_url TEXT,
                details_scraped INTEGER NOT NULL DEFAULT 0,
                last_checked_at TEXT,
                last_updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS ingredients (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                rating_tag TEXT,
                also_called TEXT,
                cosing_function_ids_json TEXT,
                irritancy TEXT,
                comedogenicity TEXT,
                details_text LONGTEXT,
                cosing_cas_numbers_json TEXT,
                cosing_ec_numbers_json TEXT,
                cosing_identified_ingredients_json TEXT,
                cosing_regulation_provisions_json TEXT,
                quick_facts_json TEXT,
                proof_references_json TEXT,
                last_checked_at TEXT,
                last_updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS functions (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                url TEXT UNIQUE,
                description TEXT
            );

            CREATE TABLE IF NOT EXISTS frees (
                id TEXT PRIMARY KEY,
                tag TEXT NOT NULL UNIQUE,
                tooltip TEXT
            );

            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self.conn.commit()
        self._ensure_ingredient_details_capacity()

    # ------------------------------------------------------------------
    # Metadata helpers
    # ------------------------------------------------------------------
    def _get_metadata(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Fetch a metadata value, returning ``default`` when missing.

        Türkçe: Metadata anahtarına karşılık gelen değeri döndürür; yoksa
        varsayılanı verir.
        """
        cursor = self.conn.execute("SELECT value FROM metadata WHERE key = ?", (key,))
        row = cursor.fetchone()
        if row is None:
            return default
        return row["value"]

    def _set_metadata(self, key: str, value: str) -> None:
        """Insert or update a metadata entry.

        Türkçe: Metadata tablosuna yeni bir kayıt ekler veya mevcut değeri
        günceller.
        """
        self.conn.execute(
            """
            INSERT INTO metadata (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self.conn.commit()

    def _delete_metadata(self, key: str) -> None:
        """Remove a metadata entry if it exists.

        Türkçe: Metadata tablosundan belirtilen anahtarı siler.
        """
        self.conn.execute("DELETE FROM metadata WHERE key = ?", (key,))
        self.conn.commit()

    def _count_metadata_with_prefix(self, prefix: str) -> int:
        """Count metadata entries whose key starts with ``prefix``.

        Türkçe: Anahtarı verilen önekle başlayan metadata kayıtlarının sayısını
        döndürür.
        """
        cursor = self.conn.execute(
            "SELECT COUNT(*) AS total FROM metadata WHERE key LIKE ?",
            (f"{prefix}%",),
        )
        row = cursor.fetchone()
        return int(row["total"]) if row else 0

    def _metadata_has_incomplete_brands(self) -> bool:
        """Check whether brand collection metadata indicates unfinished work.

        Türkçe: Marka toplama adımının yarım kalıp kalmadığını metadata üzerinden
        kontrol eder.
        """
        if self._get_metadata("brands_complete") == "0":
            return True
        next_offset = self._get_metadata("brands_next_offset")
        if next_offset and next_offset not in {"", "1"}:
            return True
        return False

    def _reset_brand_completion_flags_if_products_empty(self) -> None:
        """Reset brand completion flags when the products table has been cleared.

        Türkçe: Ürünler tablosu boşaldığında marka tamamlama bayraklarını
        sıfırlar.
        """
        completed_brands = self.conn.execute(
            "SELECT COUNT(*) FROM brands WHERE products_scraped = 1"
        ).fetchone()[0]
        if completed_brands == 0:
            return
        total_products = self.conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        if total_products > 0:
            return
        LOGGER.info(
            "Products table is empty but %s brand(s) marked complete – resetting state",
            completed_brands,
        )
        self.conn.execute("UPDATE brands SET products_scraped = 0")
        self.conn.execute(
            "DELETE FROM metadata WHERE key LIKE 'brand_products_next_offset:%'"
        )
        self.conn.execute(
            "DELETE FROM metadata WHERE key LIKE 'brand_empty_products:%'"
        )
        self.conn.commit()

    def _ensure_ingredient_details_capacity(self) -> None:
        """Ensure the ingredient details column can store lengthy text values.

        Türkçe: Bileşen detay sütununun uzun metinleri saklayabildiğini garanti eder.
        """
        cursor = self.conn.execute("PRAGMA table_info(ingredients)")
        rows = cursor.fetchall()
        target_row = None
        for row in rows:
            if row["name"] == "details_text":
                target_row = row
                break
        if target_row is None:
            return
        column_type = (target_row["type"] or "").upper()
        if column_type in {"", "TEXT", "LONGTEXT"}:
            return
        LOGGER.info(
            "Rebuilding ingredients table to expand details_text capacity (previous type: %s)",
            column_type,
        )
        self.conn.execute("ALTER TABLE ingredients RENAME TO ingredients_backup")
        self.conn.executescript(
            """
            CREATE TABLE ingredients (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                rating_tag TEXT,
                also_called TEXT,
                cosing_function_ids_json TEXT,
                irritancy TEXT,
                comedogenicity TEXT,
                details_text LONGTEXT,
                cosing_cas_numbers_json TEXT,
                cosing_ec_numbers_json TEXT,
                cosing_identified_ingredients_json TEXT,
                cosing_regulation_provisions_json TEXT,
                quick_facts_json TEXT,
                proof_references_json TEXT,
                last_checked_at TEXT,
                last_updated_at TEXT
            );
            """
        )
        columns = (
            "id, name, url, rating_tag, also_called, cosing_function_ids_json, irritancy, "
            "comedogenicity, details_text, cosing_cas_numbers_json, cosing_ec_numbers_json, "
            "cosing_identified_ingredients_json, cosing_regulation_provisions_json, "
            "quick_facts_json, proof_references_json, "
            "last_checked_at, last_updated_at"
        )
        self.conn.execute(
            f"INSERT INTO ingredients ({columns}) SELECT {columns} FROM ingredients_backup"
        )
        self.conn.execute("DROP TABLE ingredients_backup")
        self.conn.commit()

    def _enforce_schema(self) -> None:
        """Ensure only expected tables and columns exist in the database.

        Türkçe: Veritabanında sadece beklenen tablo ve sütunların bulunduğunu
        doğrular.
        """
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
            missing_columns = expected_columns - actual_columns
            recreate_table = False
            if missing_columns:
                definitions = ADDITIONAL_COLUMN_DEFINITIONS.get(table, {})
                for column in sorted(missing_columns):
                    definition = definitions.get(column)
                    if not definition:
                        recreate_table = True
                        break
                    LOGGER.info("Adding missing column %s.%s", table, column)
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")
                if recreate_table:
                    LOGGER.info(
                        "Recreating table %s due to missing columns without definitions (%s)",
                        table,
                        sorted(missing_columns),
                    )
                    self.conn.execute(f"DROP TABLE IF EXISTS {table}")
                    dropped_tables.add(table)
                    continue
                actual_columns.update(missing_columns)
            extra_columns = actual_columns - expected_columns
            if extra_columns:
                LOGGER.info(
                    "Recreating table %s due to unexpected columns (expected: %s, found: %s)",
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
            "functions",
            # Legacy name for ``functions`` prior to the Playwright migration.
            # Databases created before the rename will see ``ingredient_functions``
            # dropped during schema reconciliation, which must also invalidate
            # cached ingredient detail progress so CosIng data is re-scraped.
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
        """Return ``True`` when brand scraping still has pending work.

        Türkçe: Marka toplama adımında iş kalıp kalmadığını bildirir.
        """
        return self._get_metadata("brands_complete") != "1"

    def has_product_work(self) -> bool:
        """Return ``True`` when there are brands awaiting product scraping.

        Türkçe: Ürün taraması bekleyen marka olup olmadığını bildirir.
        """
        cursor = self.conn.execute(
            "SELECT 1 FROM brands WHERE products_scraped = 0 LIMIT 1"
        )
        return cursor.fetchone() is not None

    def has_product_detail_work(self) -> bool:
        """Return ``True`` when product detail scraping is still required.

        Türkçe: Ürün detay taraması gerektiren kayıt olup olmadığını bildirir.
        """
        cursor = self.conn.execute(
            "SELECT 1 FROM products WHERE details_scraped = 0 LIMIT 1"
        )
        return cursor.fetchone() is not None

    @staticmethod
    def _log_progress(stage: str, processed: int, total: int, *, extra: str | None = None) -> None:
        """Log a progress message for long running stages.

        Türkçe: Uzun süren aşamalar için ilerleme bilgisini günlükler.
        """
        if total > 0:
            percent = (processed / total) * 100
            message = f"{stage} progress: {processed}/{total} ({percent:.1f}%)"
        else:
            message = f"{stage} progress: processed {processed} item(s)"
        if extra:
            message = f"{message} – {extra}"
        LOGGER.info(message)

    def get_workload_summary(self) -> Dict[str, Optional[int]]:
        """Return a snapshot summarising remaining scraping work.

        Türkçe: Kalan scraping iş yükünü özetleyen bir sözlük döndürür.
        """
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
        """Extract brand names and URLs from a listing page.

        Türkçe: Marka listeleme sayfasından marka adlarını ve URL'lerini çıkarır.
        """
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

    def _insert_brand(self, name: str, url: str) -> bool:
        """Persist a brand if it does not already exist.

        Türkçe: Marka daha önce eklenmediyse veritabanına kaydeder.
        """
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
                except sqlite3.IntegrityError as exc:  # pragma: no cover - rare id collision
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

    def _collect_products_for_brand(
        self,
        brand_id: str,
        brand_url: str,
        *,
        start_offset: int = 1,
        max_products: Optional[int] = None,
    ) -> Tuple[int, bool, int]:
        """Walk through paginated product listings for a brand.

        Türkçe: Bir marka için sayfalanmış ürün listelerini dolaşır.
        """
        offset = start_offset
        total = 0
        existing_total = 0
        if max_products is not None:
            existing_total = self._count_products_for_brand(brand_id)
            if existing_total >= max_products:
                LOGGER.debug(
                    "Brand %s already has %s product(s) – skipping due to limit",
                    brand_url,
                    existing_total,
                )
                return 0, True, offset
        fallback_attempted = False
        while True:
            page_url = self._append_offset(brand_url, offset)
            current_url = page_url
            LOGGER.debug("Fetching product listing page %s", current_url)
            html = self._fetch_html(current_url)
            if (
                html is None
                and offset == start_offset == 1
                and not fallback_attempted
                and page_url != brand_url
            ):
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
                and page_url != brand_url
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
                inserted = self._insert_product(brand_id, name, url)
                if inserted:
                    total += 1
                if (
                    max_products is not None
                    and existing_total + total >= max_products
                ):
                    LOGGER.debug(
                        "Reached product limit (%s) for brand %s",
                        max_products,
                        brand_url,
                    )
                    return total, True, offset
            offset += 1
            time.sleep(REQUEST_SLEEP)

    def _retry_incomplete_brand_products(self) -> None:
        """Requeue brands that were marked complete without stored products.

        Türkçe: Ürünü olmayan ancak tamamlandı işaretli markaları yeniden kuyruğa alır.
        """
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

    def _count_products_for_brand(self, brand_id: str) -> int:
        """Return how many products have been stored for the brand.

        Türkçe: Belirtilen marka için kaydedilen ürün sayısını verir.
        """
        cursor = self.conn.execute(
            "SELECT COUNT(*) FROM products WHERE brand_id = ?",
            (brand_id,),
        )
        return cursor.fetchone()[0]

    def _parse_product_list(self, html: str) -> List[Tuple[str, str]]:
        """Extract product names and URLs from a listing page.

        Türkçe: Ürün listeleme sayfasından ürün adlarını ve URL'lerini çıkarır.
        """
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

    def _insert_product(self, brand_id: str, name: str, url: str) -> bool:
        """Persist a product, updating its name if it already exists.

        Türkçe: Ürünü kaydeder; varsa adını günceller.
        """
        now = self._current_timestamp()
        row = self.conn.execute(
            "SELECT id, brand_id, name, last_updated_at FROM products WHERE url = ?",
            (url,),
        ).fetchone()
        if row is None:
            while True:
                product_id = self._generate_id()
                try:
                    self.conn.execute(
                        """
                        INSERT INTO products (
                            id, brand_id, name, url, details_scraped, last_checked_at, last_updated_at
                        ) VALUES (?, ?, ?, ?, 0, ?, ?)
                        """,
                        (product_id, brand_id, name, url, now, now),
                    )
                except sqlite3.IntegrityError as exc:  # pragma: no cover - rare id collision
                    if "products.id" in str(exc):
                        continue
                    raise
                return True
        updates: Dict[str, object] = {"last_checked_at": now}
        changed = False
        if row["name"] != name:
            updates["name"] = name
            changed = True
        if row["brand_id"] != brand_id:
            updates["brand_id"] = brand_id
            changed = True
        if changed or not row["last_updated_at"]:
            updates["last_updated_at"] = now
        if updates:
            assignments = ", ".join(f"{column} = ?" for column in updates)
            params = list(updates.values()) + [row["id"]]
            self.conn.execute(
                f"UPDATE products SET {assignments} WHERE id = ?",
                params,
            )
        return False

    # ------------------------------------------------------------------
    # Product detail parsing
    # ------------------------------------------------------------------
    def _parse_product_page(self, html: str) -> Optional[ProductDetails]:
        """Parse a product detail page into structured information.

        Türkçe: Bir ürün detay sayfasını yapılandırılmış bilgilere dönüştürür.
        """
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
        highlights = self._extract_highlights(root, tooltip_map)
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
        """Retrieve the absolute URL of the primary product image.

        Türkçe: Ürünün ana görselinin mutlak URL'sini döndürür.
        """
        for node in product_block.find_all(tag="img"):
            src = node.get("data-src") or node.get("src")
            if not src:
                continue
            absolute = self._absolute_url(src)
            return absolute
        return None

    def _build_tooltip_index(self, root: Node) -> Dict[str, Node]:
        """Map tooltip identifiers to their DOM nodes for quick lookup.

        Türkçe: Tooltip kimliklerini hızlı erişim için ilgili düğümlere eşler.
        """
        index: Dict[str, Node] = {}
        for node in root.find_all(class_="tooltip_templates"):
            tooltip_id = node.get("id")
            if tooltip_id:
                index[tooltip_id] = node
            for child in node.find_all(predicate=lambda n: bool(n.get("id"))):
                child_id = child.get("id")
                if child_id:
                    index[child_id] = child
        return index

    def _extract_ingredients(
        self,
        product_block: Node,
        tooltip_map: Dict[str, Node],
    ) -> List[IngredientReference]:
        """Gather ingredient references from the product summary section.

        Türkçe: Ürün özet bölümünden bileşen referanslarını toplar.
        """
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
        """Locate the tooltip icon associated with ``node``.

        Türkçe: Düğümle ilişkili tooltip simgesini bulur.
        """
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
        """Parse the ingredient function table displayed on the page.

        Türkçe: Sayfadaki bileşen fonksiyonu tablosunu çözümler.
        """
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

    def _extract_highlights(
        self, root: Node, tooltip_map: Dict[str, Node]
    ) -> ProductHighlights:
        """Collect highlight hashtags and ingredient groupings.

        Türkçe: Hashtag vurgularını ve bileşen gruplarını toplar.
        """
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
        """Persist the parsed product details and ingredient links.

        Türkçe: Ayrıştırılan ürün detaylarını ve bileşen ilişkilerini kaydeder.
        """
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
        if existing is None:
            raise RuntimeError(f"Unable to locate product {product_id} for detail storage")
        changed = False
        for column, value in payload.items():
            existing_value = existing[column]
            new_value = value
            if column == "discontinued":
                existing_value = int(existing_value or 0)
            if column in {"image_path", "replacement_product_url"}:
                if existing_value == "":
                    existing_value = None
                if new_value == "":
                    new_value = None
            if existing_value != new_value:
                changed = True
                break
        now = self._current_timestamp()
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
        """Ensure an ingredient record exists and return its identifier.

        Türkçe: Bileşen kaydını oluşturup kimliğini döndürür.
        """
        row = self.conn.execute(
            "SELECT id FROM ingredients WHERE url = ?",
            (ingredient.url,),
        ).fetchone()
        if row:
            return str(row["id"])
        try:
            details = self._scrape_ingredient_page(ingredient.url)
        except RuntimeError:
            LOGGER.exception("Failed to scrape ingredient %s", ingredient.url)
            while True:
                generated_id = self._generate_id()
                cursor = self.conn.execute(
                    "INSERT OR IGNORE INTO ingredients (id, name, url) VALUES (?, ?, ?)",
                    (generated_id, ingredient.name, ingredient.url),
                )
                if cursor.rowcount:
                    break
                row = self.conn.execute(
                    "SELECT id FROM ingredients WHERE url = ?",
                    (ingredient.url,),
                ).fetchone()
                if row:
                    break
        else:
            ingredient_id = self._store_ingredient_details(details)
            return ingredient_id
        row = self.conn.execute(
            "SELECT id FROM ingredients WHERE url = ?",
            (ingredient.url,),
        ).fetchone()
        if row:
            return str(row["id"])
        raise RuntimeError(f"Unable to store ingredient {ingredient.url}")

    def _ensure_free_tag(self, free_tag: FreeTag) -> str:
        """Persist or update a free-form hashtag entry and return its id.

        Türkçe: Hashtag tarzı ifadeyi saklayıp kimliğini döndürür.
        """
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
        """Download and parse a single ingredient page.

        Türkçe: Tek bir bileşen sayfasını indirip ayrıştırır.
        """
        LOGGER.info("Fetching ingredient details %s", url)
        html = self._fetch_html(url)
        if html is None:
            raise RuntimeError(f"Unable to download ingredient page {url}")
        return self._parse_ingredient_page(html, url)

    def _parse_ingredient_page(self, html: str, url: str) -> IngredientDetails:
        """Convert ingredient HTML into a structured :class:`IngredientDetails`.

        Türkçe: Bileşen HTML içeriğini yapılandırılmış :class:`IngredientDetails`
        nesnesine dönüştürür.
        """
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
        return IngredientDetails(
            name=name,
            url=url,
            rating_tag=rating_tag,
            also_called=extract_text(also_called_node) if also_called_node else "",
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
        detail_html = self._fetch_cosing_detail_via_playwright(ingredient_name)
        if not detail_html:
            return CosIngRecord()
        return self._parse_cosing_detail_page(detail_html)

    def _fetch_cosing_detail_via_playwright(self, ingredient_name: str) -> Optional[str]:
        """Drive the CosIng interface with Playwright and return detail HTML."""

        page = self._get_cosing_playwright_page()
        if page is None:
            return None
        base_url = COSING_BASE_URL if COSING_BASE_URL.endswith("/") else f"{COSING_BASE_URL}/"
        try:
            page.goto(base_url, wait_until="domcontentloaded", timeout=15000)
        except PlaywrightTimeoutError:
            LOGGER.warning("Timed out while loading CosIng search page for %s", ingredient_name)
            return None
        except PlaywrightError:
            LOGGER.warning("Failed to load CosIng search page for %s", ingredient_name, exc_info=True)
            return None
        try:
            input_locator = page.locator("input#keyword")
            input_locator.wait_for(state="visible", timeout=5000)
            input_locator.fill(ingredient_name)
        except PlaywrightError as exc:
            LOGGER.warning("Unable to populate CosIng search input for %s: %s", ingredient_name, exc)
            return None
        try:
            button_locator = page.locator("button.ecl-button--primary[type=submit]")
            if button_locator.count() == 0:
                button_locator = page.locator("button[type=submit]")
            button_locator.first.click()
        except PlaywrightError as exc:
            LOGGER.warning("Unable to submit CosIng search for %s: %s", ingredient_name, exc)
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
        anchor = self._find_cosing_result_anchor(root, ingredient_name)
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
                ingredient_name,
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
            page.wait_for_selector(selector, state="attached", timeout=timeout)
            return True
        except PlaywrightTimeoutError:
            return False
        except PlaywrightError:
            LOGGER.debug(
                "Encountered an error while waiting for CosIng dynamic content",
                exc_info=True,
            )
            return False

    def _get_cosing_playwright_page(self) -> Optional[Any]:
        """Return a Playwright page instance ready for CosIng navigation."""

        if self._cosing_playwright_failed:
            return None
        if sync_playwright is None:
            LOGGER.warning(
                "Playwright is not installed – CosIng lookups will be skipped",
            )
            self._cosing_playwright_failed = True
            return None
        if self._cosing_page is not None:
            return self._cosing_page
        try:
            playwright = sync_playwright().start()
        except PlaywrightError:
            LOGGER.error("Unable to start Playwright runtime for CosIng lookups", exc_info=True)
            self._cosing_playwright_failed = True
            return None
        self._cosing_playwright = playwright
        for browser_name in ("chromium", "firefox", "webkit"):
            browser_type = getattr(playwright, browser_name, None)
            if browser_type is None:
                continue
            try:
                browser = browser_type.launch(headless=True)
                context = browser.new_context()
                page = context.new_page()
            except PlaywrightError:
                LOGGER.debug(
                    "CosIng lookup browser %s failed to launch",
                    browser_name,
                    exc_info=True,
                )
                continue
            self._cosing_browser = browser
            self._cosing_context = context
            self._cosing_page = page
            return page
        try:
            playwright.stop()
        except PlaywrightError:
            LOGGER.debug(
                "Ignoring Playwright shutdown error after browser launch failures",
                exc_info=True,
            )
        self._cosing_playwright = None
        self._cosing_playwright_failed = True
        LOGGER.error("No supported Playwright browsers are available for CosIng lookups")
        return None

    def _find_cosing_result_anchor(self, root: Node, ingredient_name: str) -> Optional[Node]:
        """Pick the most relevant CosIng search result anchor."""

        table = root.find(tag="table")
        if not table:
            return None
        search_name = ingredient_name.strip()
        if not search_name:
            return None
        target_key = self._cosing_lookup_key(search_name)
        query_words = self._cosing_lookup_words(search_name)
        best_rank: Tuple[int, int] = (4, 0)
        best_anchor: Optional[Node] = None
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
            if anchor_key == target_key or row_key == target_key:
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
                anchor_words = self._cosing_lookup_words(anchor_text)
                if query_words and query_words.issubset(anchor_words):
                    candidate_scores.append(len(anchor_words) - len(query_words))
                row_words = self._cosing_lookup_words(row_text)
                if query_words and query_words.issubset(row_words):
                    candidate_scores.append(len(row_words) - len(query_words))
                if candidate_scores:
                    match_type = 2
                    match_score = min(candidate_scores)
            if best_anchor is None or (match_type, match_score) < best_rank:
                best_rank = (match_type, match_score)
                best_anchor = anchor
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
        """Associate label slugs with their corresponding value nodes.

        Türkçe: Etiket anahtarlarını ilgili değer düğümleriyle eşler.
        """
        label_map: Dict[str, Node] = {}
        for label in root.find_all(class_="label"):
            text = extract_text(label).lower().strip(":")
            key = text.replace(" ", "-")
            value_node = self._find_value_node(label)
            if value_node:
                label_map[key] = value_node
        return label_map

    def _find_value_node(self, label_node: Node) -> Optional[Node]:
        """Locate the value container associated with ``label_node``.

        Türkçe: Verilen etiket düğümüne karşılık gelen değer düğümünü bulur.
        """
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
        """Extract and normalise text from a label value node.

        Türkçe: Etiket değer düğümündeki metni çıkarıp normalleştirir.
        """
        if not node:
            return ""
        text = extract_text(node)
        return self._normalize_whitespace(text)

    def _normalize_whitespace(self, value: str) -> str:
        """Collapse whitespace runs and trim the string.

        Türkçe: Boşluk dizilerini tek boşluğa indirger ve metni kırpar.
        """
        value = value.strip()
        value = re.sub(r"\s+", " ", value)
        return value

    def _parse_details_text(self, root: Node) -> str:
        """Extract the free-form descriptive text from the ingredient page.

        Türkçe: Bileşen sayfasındaki serbest biçimli açıklama metnini çıkarır.
        """
        section = root.find(id_="showmore-section-details")
        if not section:
            section = root.find(
                predicate=lambda n: n.has_class("showmore-section")
                and (
                    (n.get("id") or "").endswith("-details")
                    or "details" in (n.get("id") or "")
                )
            )
        if not section:
            section = root.find(id_="details")
        if not section:
            return ""
        content_node = None
        for candidate in section.find_all(tag="div"):
            if candidate.has_class("content") or candidate.has_class("showmore-content"):
                content_node = candidate
                break
        if content_node is None:
            content_node = section
        blocks: List[str] = []

        def visit(node: Node) -> None:
            for item in node.content:
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
        """Return the bullet point quick facts section as a list of strings.

        Türkçe: "Quick Facts" bölümündeki madde işaretli metinleri liste olarak döndürür.
        """
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
        """Collect the bibliography style entries from the proof section.

        Türkçe: "Show me some proof" bölümündeki kaynakları liste hâlinde toplar.
        """
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
            "also_called": details.also_called,
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
                            details.also_called,
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
        """Ensure an ingredient function entry exists and return its id.

        Türkçe: Bileşen fonksiyonu kaydını oluşturup kimliğini döndürür.
        """
        name = info.name.strip()
        url = info.url
        description = info.description.strip()
        if not name and not url:
            return None
        row = None
        if url:
            row = self.conn.execute(
                "SELECT id, name, description FROM functions WHERE url = ?",
                (url,),
            ).fetchone()
        if row is None:
            row = self.conn.execute(
                """
                SELECT id, name, description
                FROM functions
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
                    f"UPDATE functions SET {assignments} WHERE id = ?",
                    params,
                )
            return str(row["id"])
        while True:
            function_id = self._generate_id()
            try:
                self.conn.execute(
                    """
                    INSERT INTO functions (id, name, url, description)
                    VALUES (?, ?, ?, ?)
                    """,
                    (function_id, name, url, description),
                )
            except sqlite3.IntegrityError as exc:  # pragma: no cover - rare id collision
                if "functions.id" in str(exc):
                    continue
                raise
            return function_id

    # ------------------------------------------------------------------
    # Networking helpers
    # ------------------------------------------------------------------
    def _fetch_html(self, url: str, *, attempts: int = 3) -> Optional[str]:
        """Download ``url`` and return decoded HTML content.

        Türkçe: Belirtilen ``url`` adresini indirip çözümlenmiş HTML olarak döndürür.
        """
        data = self._fetch(url, attempts=attempts)
        if data is None:
            return None
        return data.decode("utf-8", errors="replace")

    def _fetch(self, url: str, *, attempts: int = 3) -> Optional[bytes]:
        """Download a resource with retry and failover logic.

        Türkçe: Yeniden deneme ve alternatif host mantığıyla kaynak indirir.
        """
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
        """Attempt an HTTPS request by connecting directly to ``ip_address``.

        Türkçe: Doğrudan ``ip_address`` üzerinden HTTPS isteği yapmayı dener.
        """
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
        """Rewrite ``url`` to use a previously successful fallback host.

        Türkçe: Daha önce başarılı olan alternatif host'u kullanacak şekilde URL'yi yeniden yazar.
        """
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
        """Resolve ``hostname`` via DNS-over-HTTPS, returning an IPv4 string.

        Türkçe: ``hostname`` değerini DNS-over-HTTPS kullanarak çözer ve IPv4 adresi döndürür.
        """
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
        """Fetch a DNS-over-HTTPS JSON response.

        Türkçe: DNS-over-HTTPS JSON yanıtını indirir.
        """
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
        """Query the DoH endpoint by connecting to a hard-coded IP address.

        Türkçe: DoH uç noktasını sabit IP adresi üzerinden sorgular.
        """
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
        """Return hard coded DNS-over-HTTPS host to IP overrides.

        Türkçe: DNS-over-HTTPS sunucuları için kullanılan sabit IP eşleştirmelerini döndürür.
        """
        return {
            "dns.google": "8.8.8.8",
            "dns.google.com": "8.8.8.8",
            "cloudflare-dns.com": "1.1.1.1",
        }

    def _build_host_alternatives(
        self, base_url: str, alternate_base_urls: Iterable[str]
    ) -> Dict[str, List[str]]:
        """Compute fallback hostnames that can serve INCIDecoder content.

        Türkçe: INCIDecoder içeriğini sunabilecek alternatif ana makineleri hesaplar.
        """
        hosts: List[str] = []

        def _ensure_host(value: Optional[str]) -> None:
            """Add a host to the list if it has not been seen before.

            Türkçe: Yeni host değerini daha önce eklenmediyse listeye ilave eder.
            """
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
        """Construct a new URL replacing the hostname with ``new_host``.

        Türkçe: Ana makine adını ``new_host`` ile değiştirerek yeni URL üretir.
        """
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
        product_id: str,
    ) -> Optional[str]:
        """Download, optimise and store a product image on disk.

        Türkçe: Ürün görselini indirir, optimize eder ve diske kaydeder.
        """
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
        """Convert raw image ``data`` to an optimised WebP variant when possible.

        Türkçe: Mümkün olduğunda ham görsel verisini optimize edilmiş WebP sürümüne dönüştürür.
        """
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
        """Translate a filename suffix to a Pillow image format string.

        Türkçe: Dosya uzantısını Pillow'un beklediği görsel formatına çevirir.
        """
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
        """Infer the most likely file extension from ``url``.

        Türkçe: Verilen URL'den olası dosya uzantısını tahmin eder.
        """
        parsed = parse.urlparse(url)
        _, ext = os.path.splitext(parsed.path)
        return ext if ext else ".jpg"

    # ------------------------------------------------------------------
    # Misc helpers
    # ------------------------------------------------------------------
    def _current_timestamp(self) -> str:
        """Return the current UTC timestamp in ISO 8601 format."""

        return datetime.now(timezone.utc).isoformat()

    def _absolute_url(self, href: str) -> str:
        """Resolve ``href`` relative to the configured base URL.

        Türkçe: Verilen ``href`` değerini temel URL'ye göre mutlak adrese çevirir.
        """
        if href.startswith("http://") or href.startswith("https://"):
            return href
        return f"{self.base_url}{href}" if href.startswith("/") else href

    def _append_offset(self, base_url: str, offset: int) -> str:
        """Append the pagination offset query parameter to ``base_url``.

        Türkçe: ``base_url`` adresine sayfalama ofseti sorgu parametresi ekler.
        """
        if offset <= 1:
            return base_url
        offset_value = offset - 1
        if "?" in base_url:
            return f"{base_url}&offset={offset_value}"
        return f"{base_url}?offset={offset_value}"

    def _slugify(self, value: str) -> str:
        """Generate a filesystem-friendly slug from ``value``.

        Türkçe: Verilen metinden dosya sistemi dostu bir kısa ad üretir.
        """
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
        """Initialise the HTTPS connection with a custom SNI host.

        Türkçe: TLS SNI adını özelleştirilmiş şekilde ayarlayarak HTTPS bağlantısını başlatır.
        """
        super().__init__(host, timeout=timeout, context=context)
        self._server_hostname = server_hostname

    def connect(self) -> None:  # pragma: no cover - exercised via network operations
        """Open the socket and perform TLS handshake using the override host.

        Türkçe: Soketi açar ve TLS el sıkışmasını belirtilen sunucu adıyla gerçekleştirir.
        """
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
