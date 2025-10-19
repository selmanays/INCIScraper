"""Shared configuration constants for the INCIScraper package."""

from __future__ import annotations

from typing import Dict, Set


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

