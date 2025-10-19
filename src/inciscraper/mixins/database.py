"""Database and metadata helpers shared by the scraper implementation."""

from __future__ import annotations

import logging
import sqlite3
from typing import Dict, Optional, Set

from ..constants import ADDITIONAL_COLUMN_DEFINITIONS, EXPECTED_SCHEMA

LOGGER = logging.getLogger(__name__)


class DatabaseMixin:
    """Utility mixin exposing schema and metadata helpers."""

    conn: sqlite3.Connection

    def _init_db(self) -> None:
        """Create required tables and ensure the schema is up to date."""

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
                value TEXT
            );
            """
        )
        self.conn.commit()
        self._ensure_ingredient_details_capacity()
        self._ensure_functions_allow_null_urls()

    # ------------------------------------------------------------------
    # Metadata helpers
    # ------------------------------------------------------------------
    def _get_metadata(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Return metadata for ``key`` or ``default`` when not stored."""

        row = self.conn.execute(
            "SELECT value FROM metadata WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return default
        return row["value"]

    def _set_metadata(self, key: str, value: str) -> None:
        """Persist a metadata value."""

        self.conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
            (key, value),
        )
        self.conn.commit()

    def _delete_metadata(self, key: str) -> None:
        """Remove ``key`` from the metadata table if it exists."""

        self.conn.execute("DELETE FROM metadata WHERE key = ?", (key,))
        self.conn.commit()

    def _count_metadata_with_prefix(self, prefix: str) -> int:
        """Count how many metadata keys share ``prefix``."""

        row = self.conn.execute(
            "SELECT COUNT(*) FROM metadata WHERE key LIKE ?",
            (f"{prefix}%",),
        ).fetchone()
        return int(row[0]) if row else 0

    def _metadata_has_incomplete_brands(self) -> bool:
        """Return ``True`` when brand scraping metadata signals pending work."""

        if self._get_metadata("brands_complete") == "0":
            return True
        next_offset = self._get_metadata("brands_next_offset")
        if next_offset and next_offset not in {"", "1"}:
            return True
        return False

    def _reset_brand_completion_flags_if_products_empty(self) -> None:
        """Reset brand completion flags when the products table has been cleared."""

        completed_brands = self.conn.execute(
            "SELECT COUNT(*) FROM brands WHERE products_scraped = 1",
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
            "DELETE FROM metadata WHERE key LIKE 'brand_products_next_offset:%'",
        )
        self.conn.execute(
            "DELETE FROM metadata WHERE key LIKE 'brand_empty_products:%'",
        )
        self.conn.commit()

    def _ensure_ingredient_details_capacity(self) -> None:
        """Ensure the ingredient details column can store lengthy text values."""

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
            f"INSERT INTO ingredients ({columns}) SELECT {columns} FROM ingredients_backup",
        )
        self.conn.execute("DROP TABLE ingredients_backup")
        self.conn.commit()

    def _ensure_functions_allow_null_urls(self) -> None:
        """Rebuild the functions table if ``url`` is incorrectly NOT NULL."""

        cursor = self.conn.execute("PRAGMA table_info(functions)")
        rows = cursor.fetchall()
        url_row = None
        for row in rows:
            if row["name"] == "url":
                url_row = row
                break
        if not url_row or not url_row["notnull"]:
            return
        LOGGER.info(
            "Rebuilding functions table to allow NULL url entries",
        )
        self.conn.execute("ALTER TABLE functions RENAME TO functions_backup")
        self.conn.executescript(
            """
            CREATE TABLE functions (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                url TEXT UNIQUE,
                description TEXT
            );
            INSERT INTO functions (id, name, url, description)
            SELECT id, name, NULLIF(TRIM(url), ''), description
            FROM functions_backup;
            DROP TABLE functions_backup;
            """
        )
        self.conn.commit()

    def _enforce_schema(self) -> None:
        """Ensure only expected tables and columns exist in the database."""

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
                "SELECT name FROM sqlite_master WHERE type='table'",
            )
            remaining_tables = {row["name"] for row in cursor.fetchall()}
            self._reset_progress_after_schema_changes(dropped_tables, remaining_tables)

    def _reset_progress_after_schema_changes(
        self, dropped_tables: Set[str], remaining_tables: Set[str]
    ) -> None:
        """Update metadata when tables were rebuilt during schema enforcement."""

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
                    "DELETE FROM metadata WHERE key LIKE 'brand_products_next_offset:%'",
                )
                self.conn.execute(
                    "DELETE FROM metadata WHERE key LIKE 'brand_empty_products:%'",
                )

        detail_tables = {
            "ingredients",
            "functions",
            "ingredient_functions",
        }
        if detail_tables & dropped_tables and products_available:
            LOGGER.info(
                "Resetting product detail flags after ingredient table rebuild",
            )
            self.conn.execute("UPDATE products SET details_scraped = 0")

        if dropped_tables:
            self.conn.commit()

