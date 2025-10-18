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
   image and store ingredient level data alongside the product record.

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
import socket
import sqlite3
import ssl
import time
from io import BytesIO
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple
from urllib import error, parse, request

try:
    from PIL import Image, ImageFile
except ModuleNotFoundError:  # pragma: no cover - optional dependency safeguard
    Image = None  # type: ignore[assignment]
    ImageFile = None  # type: ignore[assignment]

from .parser import Node, extract_text, parse_html

LOGGER = logging.getLogger(__name__)

BASE_URL = "https://incidecoder.com"
USER_AGENT = "INCIScraper/1.0 (+https://incidecoder.com)"
DEFAULT_TIMEOUT = 30
REQUEST_SLEEP = 0.5  # polite delay between HTTP requests
PROGRESS_LOG_INTERVAL = 10

EXPECTED_SCHEMA: Dict[str, Dict[str, str]] = {
    "brands": {
        "id": "INTEGER",
        "name": "TEXT NOT NULL",
        "url": "TEXT NOT NULL UNIQUE",
        "products_scraped": "INTEGER NOT NULL DEFAULT 0",
        "last_checked_at": "TEXT",
        "last_updated_at": "TEXT",
    },
    "products": {
        "id": "INTEGER",
        "brand_id": "INTEGER NOT NULL",
        "name": "TEXT NOT NULL",
        "url": "TEXT NOT NULL UNIQUE",
        "description": "TEXT",
        "image_path": "TEXT",
        "ingredient_ids_json": "TEXT",
        "ingredient_functions_json": "TEXT",
        "highlights_json": "TEXT",
        "discontinued": "INTEGER NOT NULL DEFAULT 0",
        "replacement_product_url": "TEXT",
        "details_scraped": "INTEGER NOT NULL DEFAULT 0",
        "last_checked_at": "TEXT",
        "last_updated_at": "TEXT",
    },
    "ingredients": {
        "id": "INTEGER",
        "name": "TEXT NOT NULL",
        "url": "TEXT NOT NULL UNIQUE",
        "rating_tag": "TEXT",
        "also_called": "TEXT",
        "function_ids_json": "TEXT",
        "irritancy": "TEXT",
        "comedogenicity": "TEXT",
        "details_text": "LONGTEXT",
        "cosing_all_functions": "TEXT",
        "cosing_description": "TEXT",
        "cosing_cas": "TEXT",
        "cosing_ec": "TEXT",
        "cosing_chemical_name": "TEXT",
        "cosing_restrictions": "TEXT",
        "last_checked_at": "TEXT",
        "last_updated_at": "TEXT",
    },
    "ingredient_functions": {
        "id": "INTEGER",
        "name": "TEXT NOT NULL",
        "url": "TEXT UNIQUE",
        "description": "TEXT",
        "last_checked_at": "TEXT",
        "last_updated_at": "TEXT",
    },
    "metadata": {"key": "TEXT PRIMARY KEY", "value": "TEXT NOT NULL"},
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
    ingredient_id: Optional[int] = None


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
class HighlightEntry:
    """Represents a highlighted ingredient and optional function link.

    Türkçe: Öne çıkarılan bileşen ve varsa fonksiyon bağlantısını temsil eder.
    """
    function_name: Optional[str]
    function_link: Optional[str]
    ingredient_name: Optional[str]
    ingredient_page: Optional[str]


@dataclass
class ProductHighlights:
    """Container for hashtag and ingredient highlight sections.

    Türkçe: Hashtag ve bileşen vurgularını tutan veri yapısıdır.
    """
    hashtags: List[str]
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
    functions: List["IngredientFunctionInfo"]
    irritancy: str
    comedogenicity: str
    details_text: str
    cosing_all_functions: str
    cosing_description: str
    cosing_cas: str
    cosing_ec: str
    cosing_chemical_name: str
    cosing_restrictions: str


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
        self._function_description_cache: Dict[str, str] = {}
        self._host_failover: Dict[str, str] = {}
        self._host_ip_overrides: Dict[str, str] = {}
        self._host_alternatives = self._build_host_alternatives(
            self.base_url, alternate_base_urls or []
        )
        self._ssl_context = ssl.create_default_context()

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
    ) -> None:
        """Collect brand listings and persist them to the database.

        Türkçe: Marka listelerini toplar ve veritabanına kaydeder.
        """
        self._reset_autoincrement_if_table_empty("brands")
        if reset_offset:
            self._set_metadata("brands_next_offset", "0")
        start_offset = int(self._get_metadata("brands_next_offset", "0") or 0)
        if start_offset > 0:
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
                self._set_metadata("brands_next_offset", "0")
                completed_normally = True
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

    def scrape_products(self, *, reset_completed: bool = False) -> None:
        """Discover products for each brand pending product scraping.

        Türkçe: Ürün taraması bekleyen markalar için ürünleri keşfeder.
        """
        if reset_completed:
            LOGGER.info("Resetting product scraping state for all brands")
            self.conn.execute("UPDATE brands SET products_scraped = 0")
            self.conn.execute(
                "DELETE FROM metadata WHERE key LIKE 'brand_products_next_offset:%'"
            )
            self.conn.execute(
                "DELETE FROM metadata WHERE key LIKE 'brand_empty_products:%'"
            )
            self.conn.commit()
        self._reset_brand_completion_flags_if_products_empty()
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
    def scrape_product_details(self, *, reset_completed: bool = False) -> None:
        """Download and persist detailed information for each product.

        Türkçe: Her ürünün detay sayfasını indirip veritabanına kaydeder.
        """
        if reset_completed:
            LOGGER.info("Resetting detail scraping state for all products")
            self.conn.execute("UPDATE products SET details_scraped = 0")
            self.conn.commit()
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
            self.conn.commit()
            LOGGER.info("Stored product details for %s", details.name)
            processed += 1
            if processed % PROGRESS_LOG_INTERVAL == 0 or processed == total_products:
                self._log_progress("Product", processed, total_products)
    def close(self) -> None:
        """Close the underlying SQLite connection.

        Türkçe: Kullanılan SQLite bağlantısını kapatır.
        """
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
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                products_scraped INTEGER NOT NULL DEFAULT 0,
                last_checked_at TEXT,
                last_updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brand_id INTEGER NOT NULL REFERENCES brands(id),
                name TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                description TEXT,
                image_path TEXT,
                ingredient_ids_json TEXT,
                ingredient_functions_json TEXT,
                highlights_json TEXT,
                discontinued INTEGER NOT NULL DEFAULT 0,
                replacement_product_url TEXT,
                details_scraped INTEGER NOT NULL DEFAULT 0,
                last_checked_at TEXT,
                last_updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS ingredients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                rating_tag TEXT,
                also_called TEXT,
                function_ids_json TEXT,
                irritancy TEXT,
                comedogenicity TEXT,
                details_text LONGTEXT,
                cosing_all_functions TEXT,
                cosing_description TEXT,
                cosing_cas TEXT,
                cosing_ec TEXT,
                cosing_chemical_name TEXT,
                cosing_restrictions TEXT,
                last_checked_at TEXT,
                last_updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS ingredient_functions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT UNIQUE,
                description TEXT,
                last_checked_at TEXT,
                last_updated_at TEXT
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
        if next_offset and next_offset not in {"", "0"}:
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

    def _reset_autoincrement_if_table_empty(self, table: str) -> None:
        """Reset the SQLite autoincrement counter when ``table`` has no rows.

        Türkçe: Tablo boşaldığında SQLite otomatik artış sayacını sıfırlar.
        """
        count = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if count != 0:
            return
        try:
            self.conn.execute("DELETE FROM sqlite_sequence WHERE name = ?", (table,))
            self.conn.commit()
            LOGGER.info("Reset autoincrement sequence for empty table %s", table)
        except sqlite3.OperationalError:
            LOGGER.debug("sqlite_sequence not available when resetting table %s", table)

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
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                rating_tag TEXT,
                also_called TEXT,
                function_ids_json TEXT,
                irritancy TEXT,
                comedogenicity TEXT,
                details_text LONGTEXT,
                cosing_all_functions TEXT,
                cosing_description TEXT,
                cosing_cas TEXT,
                cosing_ec TEXT,
                cosing_chemical_name TEXT,
                cosing_restrictions TEXT,
                last_checked_at TEXT,
                last_updated_at TEXT
            );
            """
        )
        backup_info = self.conn.execute("PRAGMA table_info(ingredients_backup)").fetchall()
        backup_columns = {row["name"] for row in backup_info}
        base_columns = [
            "id",
            "name",
            "url",
            "rating_tag",
            "also_called",
            "function_ids_json",
            "irritancy",
            "comedogenicity",
            "details_text",
            "cosing_all_functions",
            "cosing_description",
            "cosing_cas",
            "cosing_ec",
            "cosing_chemical_name",
            "cosing_restrictions",
        ]
        optional_columns = ["last_checked_at", "last_updated_at"]
        insert_columns = list(base_columns) + optional_columns
        select_columns = list(base_columns)
        for column in optional_columns:
            if column in backup_columns:
                select_columns.append(column)
            else:
                select_columns.append("NULL")
        self.conn.execute(
            f"INSERT INTO ingredients ({', '.join(insert_columns)}) SELECT {', '.join(select_columns)} FROM ingredients_backup"
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
        for table in existing_tables:
            if table.startswith("sqlite_"):
                continue
            if table not in EXPECTED_SCHEMA:
                LOGGER.info("Dropping unexpected table: %s", table)
                self.conn.execute(f"DROP TABLE IF EXISTS {table}")
        for table, expected_columns in EXPECTED_SCHEMA.items():
            cursor = self.conn.execute(f"PRAGMA table_info({table})")
            rows = cursor.fetchall()
            if not rows:
                continue
            actual_columns = {row["name"] for row in rows}
            expected_names = set(expected_columns.keys())
            extra_columns = actual_columns - expected_names
            if extra_columns:
                LOGGER.info(
                    "Recreating table %s due to unexpected columns (expected: %s, found extras: %s)",
                    table,
                    sorted(expected_names),
                    sorted(extra_columns),
                )
                self.conn.execute(f"DROP TABLE IF EXISTS {table}")
                continue
            missing_columns = expected_names - actual_columns
            for column in sorted(missing_columns):
                definition = expected_columns.get(column, "")
                if not definition:
                    LOGGER.warning(
                        "Skipping addition of column %s on table %s – no definition available",
                        column,
                        table,
                    )
                    continue
                LOGGER.info("Adding missing column %s to table %s", column, table)
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                )
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
        start_offset = int(self._get_metadata("brands_next_offset", "0") or 0)
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

    def _insert_brand(self, name: str, url: str) -> int:
        """Persist a brand if it does not already exist.

        Türkçe: Marka daha önce eklenmediyse veritabanına kaydeder.
        """
        now = self._current_timestamp()
        row = self.conn.execute(
            "SELECT id, name, last_updated_at FROM brands WHERE url = ?",
            (url,),
        ).fetchone()
        if row is None:
            self.conn.execute(
                """
                INSERT INTO brands (name, url, products_scraped, last_checked_at, last_updated_at)
                VALUES (?, ?, 0, ?, ?)
                """,
                (name, url, now, now),
            )
            return 1
        changed = False
        assignments: List[str] = []
        params: List[object] = []
        if row["name"] != name:
            assignments.append("name = ?")
            params.append(name)
            changed = True
        assignments.append("last_checked_at = ?")
        params.append(now)
        if changed:
            assignments.append("last_updated_at = ?")
            params.append(now)
        params.append(row["id"])
        assignment = ", ".join(assignments)
        self.conn.execute(
            f"UPDATE brands SET {assignment} WHERE id = ?",
            params,
        )
        return 0

    def _collect_products_for_brand(
        self, brand_id: int, brand_url: str, *, start_offset: int = 1
    ) -> Tuple[int, bool, int]:
        """Walk through paginated product listings for a brand.

        Türkçe: Bir marka için sayfalanmış ürün listelerini dolaşır.
        """
        offset = start_offset
        total = 0
        fallback_attempted = False
        while True:
            page_url = self._append_offset(brand_url, offset)
            current_url = page_url
            LOGGER.debug("Fetching product listing page %s", current_url)
            html = self._fetch_html(current_url)
            if html is None and offset == start_offset == 1 and not fallback_attempted:
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

    def _count_products_for_brand(self, brand_id: int) -> int:
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

    def _insert_product(self, brand_id: int, name: str, url: str) -> int:
        """Persist a product, updating its name if it already exists.

        Türkçe: Ürünü kaydeder; varsa adını günceller.
        """
        now = self._current_timestamp()
        row = self.conn.execute(
            "SELECT id, brand_id, name FROM products WHERE url = ?",
            (url,),
        ).fetchone()
        if row is None:
            self.conn.execute(
                """
                INSERT INTO products (brand_id, name, url, last_checked_at, last_updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (brand_id, name, url, now, now),
            )
            return 1
        changed = False
        assignments: List[str] = []
        params: List[object] = []
        if row["name"] != name:
            assignments.append("name = ?")
            params.append(name)
            changed = True
        if row["brand_id"] != brand_id:
            assignments.append("brand_id = ?")
            params.append(brand_id)
            changed = True
        assignments.append("last_checked_at = ?")
        params.append(now)
        if changed:
            assignments.append("last_updated_at = ?")
            params.append(now)
        params.append(row["id"])
        self.conn.execute(
            f"UPDATE products SET {', '.join(assignments)} WHERE id = ?",
            params,
        )
        return 0

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
        highlights = self._extract_highlights(root)
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

    def _extract_highlights(self, root: Node) -> ProductHighlights:
        """Collect highlight hashtags and ingredient groupings.

        Türkçe: Hashtag vurgularını ve bileşen gruplarını toplar.
        """
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
        """Persist the parsed product details and ingredient links.

        Türkçe: Ayrıştırılan ürün detaylarını ve bileşen ilişkilerini kaydeder.
        """
        now = self._current_timestamp()
        ingredient_entries: List[Dict[str, object | None]] = []
        for ingredient in details.ingredients:
            ingredient_id = self._ensure_ingredient(ingredient)
            ingredient.ingredient_id = ingredient_id
            ingredient_entries.append(
                {
                    "ingredient_id": ingredient_id,
                    "ingredient_name": ingredient.name,
                    "ingredient_url": ingredient.url,
                    "tooltip_text": ingredient.tooltip_text,
                    "tooltip_ingredient_link": ingredient.tooltip_ingredient_link,
                }
            )
        ingredient_json = json.dumps(ingredient_entries, ensure_ascii=False, sort_keys=True)
        functions_json = json.dumps(
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
            sort_keys=True,
        )
        highlights_json = json.dumps(
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
            sort_keys=True,
        )
        row = self.conn.execute(
            """
            SELECT name, description, image_path, ingredient_ids_json,
                   ingredient_functions_json, highlights_json, discontinued,
                   replacement_product_url, details_scraped
            FROM products
            WHERE id = ?
            """,
            (product_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Product {product_id} missing when storing details")
        existing_image = row["image_path"]
        final_image_path = image_path if image_path is not None else existing_image
        update_payload: Dict[str, object | None] = {
            "name": details.name,
            "description": details.description,
            "image_path": final_image_path,
            "ingredient_ids_json": ingredient_json,
            "ingredient_functions_json": functions_json,
            "highlights_json": highlights_json,
            "discontinued": 1 if details.discontinued else 0,
            "replacement_product_url": details.replacement_product_url,
        }
        assignments: List[str] = []
        params: List[object] = []
        changed = False
        for column, value in update_payload.items():
            if row[column] != value:
                assignments.append(f"{column} = ?")
                params.append(value)
                changed = True
        if row["details_scraped"] != 1:
            assignments.append("details_scraped = 1")
        assignments.append("last_checked_at = ?")
        params.append(now)
        if changed:
            assignments.append("last_updated_at = ?")
            params.append(now)
        params.append(product_id)
        self.conn.execute(
            f"UPDATE products SET {', '.join(assignments)} WHERE id = ?",
            params,
        )

    def _ensure_ingredient(self, ingredient: IngredientReference) -> int:
        """Ensure an ingredient record exists and return its identifier.

        Türkçe: Bileşen kaydını oluşturup kimliğini döndürür.
        """
        now = self._current_timestamp()
        row = self.conn.execute(
            "SELECT id, name FROM ingredients WHERE url = ?",
            (ingredient.url,),
        ).fetchone()
        if row:
            assignments = ["last_checked_at = ?"]
            params: List[object] = [now]
            if ingredient.name and ingredient.name != row["name"]:
                assignments.append("name = ?")
                params.append(ingredient.name)
                assignments.append("last_updated_at = ?")
                params.append(now)
            params.append(row["id"])
            self.conn.execute(
                f"UPDATE ingredients SET {', '.join(assignments)} WHERE id = ?",
                params,
            )
            return row["id"]
        try:
            details = self._scrape_ingredient_page(ingredient.url)
        except RuntimeError:
            LOGGER.exception("Failed to scrape ingredient %s", ingredient.url)
            self.conn.execute(
                """
                INSERT OR IGNORE INTO ingredients (name, url, last_checked_at, last_updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (ingredient.name, ingredient.url, now, now),
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
        what_it_does_nodes = label_map.get("what-it-does")
        irritancy_node = label_map.get("irritancy")
        comedogenicity_node = label_map.get("comedogenicity")
        functions = self._parse_ingredient_functions(what_it_does_nodes)
        cosing = self._parse_cosing_section(root)
        details_text = self._parse_details_text(root)
        return IngredientDetails(
            name=name,
            url=url,
            rating_tag=rating_tag,
            also_called=extract_text(also_called_node) if also_called_node else "",
            functions=functions,
            irritancy=self._extract_label_text(irritancy_node),
            comedogenicity=self._extract_label_text(comedogenicity_node),
            details_text=details_text,
            cosing_all_functions=cosing["all_functions"],
            cosing_description=cosing["description"],
            cosing_cas=cosing["cas_number"],
            cosing_ec=cosing["ec_number"],
            cosing_chemical_name=cosing["chemical_iupac_name"],
            cosing_restrictions=cosing["cosmetic_restrictions"],
        )

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

    def _parse_ingredient_functions(
        self, node: Optional[Node]
    ) -> List[IngredientFunctionInfo]:
        """Derive ingredient functions from structured or plain text blocks.

        Türkçe: Yapılandırılmış veya düz metin bloklarından bileşen fonksiyonlarını türetir.
        """
        if not node:
            return []
        functions: List[IngredientFunctionInfo] = []
        anchors = node.find_all(tag="a")
        if anchors:
            for anchor in anchors:
                name = self._normalize_whitespace(extract_text(anchor))
                href = anchor.get("href")
                url = self._absolute_url(href) if href else None
                description = self._fetch_function_description(url) if url else ""
                if name or url:
                    functions.append(
                        IngredientFunctionInfo(name=name, url=url, description=description)
                    )
        else:
            raw_text = self._normalize_whitespace(extract_text(node))
            if raw_text:
                for part in re.split(r",\s*", raw_text):
                    if part:
                        functions.append(
                            IngredientFunctionInfo(name=part, url=None, description="")
                        )
        return functions

    def _parse_cosing_section(self, root: Node) -> Dict[str, str]:
        """Parse the COSING information table into a dictionary.

        Türkçe: COSING bilgi tablosunu sözlüğe dönüştürür.
        """
        section = root.find(id_="cosing-data")
        empty = {
            "all_functions": "",
            "description": "",
            "cas_number": "",
            "ec_number": "",
            "chemical_iupac_name": "",
            "cosmetic_restrictions": "",
        }
        if not section:
            return empty
        key_map = {
            "all functions": "all_functions",
            "description": "description",
            "cas #": "cas_number",
            "ec #": "ec_number",
            "chemical/iupac name": "chemical_iupac_name",
            "cosmetic restrictions": "cosmetic_restrictions",
        }
        values: Dict[str, str] = dict(empty)
        for bold in section.find_all(tag="b"):
            label = extract_text(bold).strip().lower().strip(":")
            key = key_map.get(label)
            if not key:
                continue
            text_parts: List[str] = []
            for item in self._iter_next_content(bold):
                if isinstance(item, Node) and item.tag == "b":
                    break
                if isinstance(item, Node):
                    text_parts.append(extract_text(item))
                elif isinstance(item, str):
                    text_parts.append(item)
            raw_value = " ".join(part.strip() for part in text_parts if part)
            cleaned = self._normalize_whitespace(raw_value.replace("|", " "))
            values[key] = cleaned
        return values

    def _iter_next_content(self, node: Node) -> Iterable:
        """Yield sibling content items following ``node``.

        Türkçe: Düğümden sonra gelen kardeş içerik öğelerini sırasıyla döndürür.
        """
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

    def _normalize_whitespace(self, value: str) -> str:
        """Collapse whitespace runs and trim the string.

        Türkçe: Boşluk dizilerini tek boşluğa indirger ve metni kırpar.
        """
        value = value.strip()
        value = re.sub(r"\s+", " ", value)
        return value

    def _fetch_function_description(self, url: Optional[str]) -> str:
        """Retrieve the textual description of a cosmetic function.

        Türkçe: Kozmetik fonksiyon tanımının metin içeriğini getirir.
        """
        if not url:
            return ""
        if url in self._function_description_cache:
            return self._function_description_cache[url]
        html = self._fetch_html(url)
        if not html:
            self._function_description_cache[url] = ""
            return ""
        root = parse_html(html)
        content = root.find(id_="content") or root.find(class_="content")
        if content:
            description = self._normalize_whitespace(extract_text(content))
        else:
            description = ""
        self._function_description_cache[url] = description
        return description

    def _parse_details_text(self, root: Node) -> str:
        """Extract the free-form descriptive text from the ingredient page.

        Türkçe: Bileşen sayfasındaki serbest biçimli açıklama metnini çıkarır.
        """
        section = root.find(id_="showmore-section-details") or root.find(id_="details")
        if not section:
            return ""
        content_node = section.find(class_="content") or section
        paragraphs: List[str] = []
        for paragraph in content_node.find_all(tag="p"):
            text = self._normalize_whitespace(extract_text(paragraph))
            if text:
                paragraphs.append(text)
        if not paragraphs:
            text = self._normalize_whitespace(extract_text(content_node))
            return text
        return "\n\n".join(paragraphs)

    def _store_ingredient_details(self, details: IngredientDetails) -> int:
        """Persist ingredient metadata and return the database identifier.

        Türkçe: Bileşen metadatasını kaydedip veritabanı kimliğini döndürür.
        """
        function_ids: List[int] = []
        for function in details.functions:
            function_id = self._ensure_ingredient_function(function)
            if function_id is not None:
                function_ids.append(function_id)
        function_ids_json = json.dumps(function_ids, ensure_ascii=False)
        now = self._current_timestamp()
        payload: Dict[str, Optional[str]] = {
            "name": details.name,
            "rating_tag": details.rating_tag,
            "also_called": details.also_called,
            "function_ids_json": function_ids_json,
            "irritancy": details.irritancy,
            "comedogenicity": details.comedogenicity,
            "details_text": details.details_text,
            "cosing_all_functions": details.cosing_all_functions,
            "cosing_description": details.cosing_description,
            "cosing_cas": details.cosing_cas,
            "cosing_ec": details.cosing_ec,
            "cosing_chemical_name": details.cosing_chemical_name,
            "cosing_restrictions": details.cosing_restrictions,
        }
        row = self.conn.execute(
            """
            SELECT id, name, rating_tag, also_called, function_ids_json,
                   irritancy, comedogenicity, details_text, cosing_all_functions,
                   cosing_description, cosing_cas, cosing_ec, cosing_chemical_name,
                   cosing_restrictions
            FROM ingredients
            WHERE url = ?
            """,
            (details.url,),
        ).fetchone()
        if row is None:
            columns = ["url"] + list(payload.keys()) + ["last_checked_at", "last_updated_at"]
            values = [details.url] + [payload[col] for col in payload] + [now, now]
            placeholders = ", ".join("?" for _ in columns)
            self.conn.execute(
                f"INSERT INTO ingredients ({', '.join(columns)}) VALUES ({placeholders})",
                values,
            )
        else:
            assignments: List[str] = []
            params: List[object] = []
            changed = False
            for column, value in payload.items():
                if row[column] != value:
                    assignments.append(f"{column} = ?")
                    params.append(value)
                    changed = True
            assignments.append("last_checked_at = ?")
            params.append(now)
            if changed:
                assignments.append("last_updated_at = ?")
                params.append(now)
            params.append(row["id"])
            self.conn.execute(
                f"UPDATE ingredients SET {', '.join(assignments)} WHERE id = ?",
                params,
            )
        row = self.conn.execute(
            "SELECT id FROM ingredients WHERE url = ?",
            (details.url,),
        ).fetchone()
        if not row:
            raise RuntimeError(f"Unable to store ingredient {details.url}")
        return row["id"]
    def _ensure_ingredient_function(self, info: IngredientFunctionInfo) -> Optional[int]:
        """Ensure an ingredient function entry exists and return its id.

        Türkçe: Bileşen fonksiyonu kaydını oluşturup kimliğini döndürür.
        """
        name = info.name.strip()
        url = info.url
        description = info.description.strip()
        if not name and not url:
            return None
        now = self._current_timestamp()
        row = None
        if url:
            row = self.conn.execute(
                "SELECT id, name, description FROM ingredient_functions WHERE url = ?",
                (url,),
            ).fetchone()
        if row is None:
            row = self.conn.execute(
                """
                SELECT id, name, description
                FROM ingredient_functions
                WHERE url IS NULL AND name = ?
                """,
                (name,),
            ).fetchone()
        if row:
            assignments: List[str] = []
            params: List[object] = []
            changed = False
            if name and name != row["name"]:
                assignments.append("name = ?")
                params.append(name)
                changed = True
            if description and description != (row["description"] or ""):
                assignments.append("description = ?")
                params.append(description)
                changed = True
            assignments.append("last_checked_at = ?")
            params.append(now)
            if changed:
                assignments.append("last_updated_at = ?")
                params.append(now)
            params.append(row["id"])
            self.conn.execute(
                f"UPDATE ingredient_functions SET {', '.join(assignments)} WHERE id = ?",
                params,
            )
            return row["id"]
        cur = self.conn.execute(
            """
            INSERT INTO ingredient_functions (name, url, description, last_checked_at, last_updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (name, url, description, now, now),
        )
        return cur.lastrowid

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
        product_id: int,
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
        """Return the current UTC timestamp as an ISO 8601 string.

        Türkçe: Geçerli UTC zaman damgasını ISO 8601 biçiminde döndürür.
        """
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

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
        if offset <= 0:
            return base_url
        if "?" in base_url:
            return f"{base_url}&offset={offset}"
        return f"{base_url}?offset={offset}"

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
