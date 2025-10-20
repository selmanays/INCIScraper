"""Database operations mixin for INCIScraper."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from inciscraper.constants import PROGRESS_LOG_INTERVAL
from inciscraper.mixins.monitoring import MonitoringMixin

LOGGER = logging.getLogger(__name__)


class DatabaseMixin(MonitoringMixin):
    """Mixin for database operations and metadata management."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.db_path = kwargs.get('db_path')
        self.conn = None

    def _init_db(self) -> None:
        """Initialize the SQLite database with required tables."""
        
        db_path_obj = Path(self.db_path)
        db_path_obj.parent.mkdir(parents=True, exist_ok=True)
        
        # Use check_same_thread=False for thread safety
        self.conn = sqlite3.connect(str(db_path_obj), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        
        # Enable WAL mode for better concurrency
        self.conn.execute("PRAGMA journal_mode=WAL")
        
        # Create tables
        self._create_tables()
        self._ensure_minimal_schema()

    def _create_tables(self) -> None:
        """Create all required database tables."""
        
        # Brands table
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS brands (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                products_scraped INTEGER DEFAULT 0,
                last_checked_at TEXT,
                last_updated_at TEXT
            )
            """
        )
        
        # Products table
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                id TEXT PRIMARY KEY,
                brand_id TEXT NOT NULL,
                name TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                image_path TEXT,
                description TEXT,
                discontinued INTEGER DEFAULT 0,
                replacement_product_url TEXT,
                ingredient_ids_json TEXT,
                details_scraped INTEGER DEFAULT 0,
                last_checked_at TEXT,
                last_updated_at TEXT,
                FOREIGN KEY (brand_id) REFERENCES brands (id)
            )
            """
        )
        
        # Ingredients table
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ingredients (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                rating_tag TEXT,
                also_called TEXT,
                cosing_function_ids_json TEXT,
                irritancy TEXT,
                comedogenicity TEXT,
                details_text TEXT,
                cosing_cas_numbers_json TEXT,
                cosing_ec_numbers_json TEXT,
                cosing_identified_ingredients_json TEXT,
                cosing_regulation_provisions_json TEXT,
                quick_facts_json TEXT,
                proof_references_json TEXT,
                last_checked_at TEXT,
                last_updated_at TEXT
            )
            """
        )
        
        # Functions table
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS functions (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                last_updated_at TEXT
            )
            """
        )
        
        # Product highlights table
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS product_highlights (
                id TEXT PRIMARY KEY,
                product_id TEXT NOT NULL,
                highlight_text TEXT NOT NULL,
                last_updated_at TEXT,
                FOREIGN KEY (product_id) REFERENCES products (id)
            )
            """
        )
        
        # Metadata table
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        
        self.conn.commit()

    def _ensure_minimal_schema(self) -> None:
        """Ensure minimal schema requirements are met."""
        
        # Ensure ingredients table has LONGTEXT for details_text
        self._ensure_ingredients_minimal_schema()
        
        # Ensure functions table exists
        self._ensure_functions_minimal_schema()

    def _ensure_ingredients_minimal_schema(self) -> None:
        """Ensure ingredients table can store large text content."""
        
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

    def _ensure_functions_minimal_schema(self) -> None:
        """Ensure functions table exists with minimal schema."""
        
        # Check if functions table exists
        cursor = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='functions'"
        )
        if cursor.fetchone() is None:
            LOGGER.info("Creating functions table")
            self.conn.execute(
                """
                CREATE TABLE functions (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    last_updated_at TEXT
                )
                """
            )
            self.conn.commit()

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

    def _get_metadata_thread_safe(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Return metadata for ``key`` or ``default`` when not stored (thread-safe)."""
        
        conn = self._get_thread_safe_connection()
        try:
            row = conn.execute(
                "SELECT value FROM metadata WHERE key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return default
            return row["value"]
        finally:
            conn.close()

    def _set_metadata_thread_safe(self, key: str, value: str) -> None:
        """Persist a metadata value (thread-safe)."""
        
        conn = self._get_thread_safe_connection()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
                (key, value),
            )
            conn.commit()
        finally:
            conn.close()

    def _delete_metadata(self, key: str) -> None:
        """Remove ``key`` from the metadata table if it exists."""

        self.conn.execute("DELETE FROM metadata WHERE key = ?", (key,))
        self.conn.commit()

    # ------------------------------------------------------------------
    # Thread-safe connection helper
    # ------------------------------------------------------------------
    def _get_thread_safe_connection(self) -> sqlite3.Connection:
        """Create a new thread-safe SQLite connection."""
        
        db_path_obj = Path(self.db_path)
        conn = sqlite3.connect(str(db_path_obj), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        
        # Enable WAL mode for better concurrency
        conn.execute("PRAGMA journal_mode=WAL")
        
        return conn

    # ------------------------------------------------------------------
    # Batch operations
    # ------------------------------------------------------------------
    def batch_insert_brands(self, brands: List[Tuple]) -> None:
        """Batch insert brands using executemany."""
        
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO brands (id, name, url, last_checked_at, last_updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            brands
        )
        self.conn.commit()

    def batch_insert_products(self, products: List[Tuple]) -> None:
        """Batch insert products using executemany."""
        
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO products (id, brand_id, name, url, last_checked_at, last_updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            products
        )
        self.conn.commit()

    def batch_insert_ingredients(self, ingredients: List[Tuple]) -> None:
        """Batch insert ingredients using executemany."""
        
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO ingredients (id, name, url, last_checked_at, last_updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ingredients
        )
        self.conn.commit()

    def batch_insert_functions(self, functions: List[Tuple]) -> None:
        """Batch insert functions using executemany."""
        
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO functions (id, name, last_updated_at)
            VALUES (?, ?, ?)
            """,
            functions
        )
        self.conn.commit()

    def batch_insert_frees(self, frees: List[Tuple]) -> None:
        """Batch insert frees using executemany."""
        
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO product_highlights (id, product_id, highlight_text, last_updated_at)
            VALUES (?, ?, ?, ?)
            """,
            frees
        )
        self.conn.commit()

    def batch_update_products_scraped(self, updates: List[Tuple]) -> None:
        """Batch update product scraped status using executemany."""
        
        self.conn.executemany(
            """
            UPDATE products SET details_scraped = ? WHERE id = ?
            """,
            updates
        )
        self.conn.commit()

    def batch_update_brands_scraped(self, updates: List[Tuple]) -> None:
        """Batch update brand scraped status using executemany."""
        
        self.conn.executemany(
            """
            UPDATE brands SET products_scraped = ? WHERE id = ?
            """,
            updates
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # Utility methods
    # ------------------------------------------------------------------
    def _generate_id(self) -> str:
        """Generate a unique ID for database records."""
        
        import uuid
        return str(uuid.uuid4())

    def _current_timestamp(self) -> str:
        """Get current timestamp as ISO string."""
        
        from datetime import datetime
        return datetime.utcnow().isoformat()

    def close(self) -> None:
        """Close database connection."""
        
        if self.conn:
            self.conn.close()
            self.conn = None


# Add missing import
import logging