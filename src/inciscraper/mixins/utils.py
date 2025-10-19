"""Utility helpers shared by several mixins."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlsplit


class UtilityMixin:
    """Provide generic helper methods for URL and string handling."""

    base_url: str

    def _current_timestamp(self) -> str:
        """Return the current UTC timestamp in ISO 8601 format."""

        return datetime.now(timezone.utc).isoformat()

    def _absolute_url(self, href: str) -> str:
        """Resolve ``href`` relative to the configured base URL."""

        if not href:
            return href
        if href.startswith(("http://", "https://")):
            return href
        if href.startswith("//"):
            scheme = urlsplit(self.base_url).scheme or "https"
            return f"{scheme}:{href}"
        return f"{self.base_url}{href}" if href.startswith("/") else href

    def _append_offset(self, base_url: str, offset: int) -> str:
        """Append the pagination offset query parameter to ``base_url``."""

        if offset <= 1:
            return base_url
        offset_value = offset - 1
        if "?" in base_url:
            return f"{base_url}&offset={offset_value}"
        return f"{base_url}?offset={offset_value}"

    def _slugify(self, value: str) -> str:
        """Generate a filesystem-friendly slug from ``value``."""

        value = value.lower()
        value = re.sub(r"[^a-z0-9]+", "-", value)
        value = value.strip("-")
        return value or "product"

    def _normalize_whitespace(self, value: str) -> str:
        """Collapse consecutive whitespace characters to single spaces."""

        return re.sub(r"\s+", " ", value).strip()

