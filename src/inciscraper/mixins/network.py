"""HTTP utilities and failover logic for INCIScraper."""

from __future__ import annotations

import http.client
import json
import logging
import os
import socket
import ssl
import time
from io import BytesIO
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib import error, parse, request

try:
    from PIL import Image, ImageFile
except ModuleNotFoundError:  # pragma: no cover - optional dependency safeguard
    Image = None  # type: ignore[assignment]
    ImageFile = None  # type: ignore[assignment]


_PILLOW_WARNING_EMITTED = False

from ..constants import USER_AGENT

LOGGER = logging.getLogger(__name__)


class NetworkMixin:
    """Provide HTTP helpers with host failover support."""

    timeout: int
    image_dir: Path
    _host_failover: Dict[str, str]
    _host_ip_overrides: Dict[str, str]
    _host_alternatives: Dict[str, List[str]]
    _ssl_context: ssl.SSLContext

    def _fetch_html(self, url: str, *, attempts: int = 3) -> Optional[str]:
        """Download ``url`` and return the HTML body as text."""

        payload = self._fetch(url, attempts=attempts)
        if payload is None:
            return None
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError:
            return payload.decode("latin-1", errors="replace")

    def _fetch(self, url: str, *, attempts: int = 3) -> Optional[bytes]:
        """Download raw bytes from ``url`` with retry and failover logic."""

        current_url = url
        for attempt in range(1, attempts + 1):
            try:
                request_url = self._apply_host_override(current_url)
                req = request.Request(request_url, headers={"User-Agent": USER_AGENT})
                with request.urlopen(req, timeout=self.timeout) as response:
                    if response.status >= 400:
                        raise error.HTTPError(
                            request_url,
                            response.status,
                            f"HTTP error {response.status}",
                            response.headers,
                            None,
                        )
                    data = response.read()
                    original_host = parse.urlsplit(current_url).hostname
                    final_host = parse.urlsplit(response.geturl()).hostname
                    if original_host and final_host and original_host != final_host:
                        self._host_failover[original_host] = final_host
                    return data
            except (error.URLError, error.HTTPError, socket.timeout) as exc:
                delay = min(2 ** attempt, 10)
                parts = parse.urlsplit(current_url)
                canonical_host = parts.hostname
                if canonical_host:
                    alternatives = self._host_alternatives.get(canonical_host, [])
                    fallback = alternatives[0] if alternatives else None
                    if fallback:
                        LOGGER.warning(
                            "Request to %s failed (%s) – retrying via fallback host %s",
                            canonical_host,
                            exc,
                            fallback,
                        )
                        replacement = self._replace_host(parts, fallback)
                        if replacement:
                            current_url = replacement
                            continue
                    if isinstance(exc, error.URLError) and isinstance(exc.reason, socket.gaierror):
                        resolved_ip = self._resolve_host_via_doh(canonical_host)
                        if resolved_ip:
                            LOGGER.warning(
                                "DNS resolution failed for %s – attempting direct IP connection via %s",
                                canonical_host,
                                resolved_ip,
                            )
                            data = self._fetch_via_direct_ip(parts, resolved_ip)
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
                current_url = self._apply_host_override(url)
        return None

    def _fetch_via_direct_ip(
        self, parts: parse.SplitResult, ip_address: str
    ) -> Optional[bytes]:
        """Attempt an HTTPS request by connecting directly to ``ip_address``."""

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
        """Rewrite ``url`` to use a previously successful fallback host."""

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
        """Resolve ``hostname`` via DNS-over-HTTPS, returning an IPv4 string."""

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
        """Fetch a DNS-over-HTTPS JSON response."""

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
        except Exception:  # pragma: no cover - defensive logging only
            LOGGER.debug("Unexpected error querying DoH endpoint", exc_info=True)
        return None

    def _download_doh_payload_via_ip(
        self, doh_url: str
    ) -> Optional[Dict[str, object]]:
        """Query the DoH endpoint by connecting to a hard-coded IP address."""

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
        """Return hard coded DNS-over-HTTPS host to IP overrides."""

        return {
            "dns.google": "8.8.8.8",
            "dns.google.com": "8.8.8.8",
            "cloudflare-dns.com": "1.1.1.1",
        }

    def _build_host_alternatives(
        self, base_url: str, alternate_base_urls: Iterable[str]
    ) -> Dict[str, List[str]]:
        """Compute fallback hostnames that can serve INCIDecoder content."""

        hosts: List[str] = []

        def _ensure_host(value: Optional[str]) -> None:
            if value and value not in hosts:
                hosts.append(value)

        base_host = parse.urlsplit(base_url).hostname
        _ensure_host(base_host)
        for candidate in alternate_base_urls:
            parsed_host = parse.urlsplit(candidate.rstrip("/")).hostname
            _ensure_host(parsed_host)
        for existing in list(hosts):
            if existing.startswith("www."):
                _ensure_host(existing[4:])
            else:
                _ensure_host(f"www.{existing}")
        alternatives: Dict[str, List[str]] = {}
        for host in hosts:
            others = [candidate for candidate in hosts if candidate != host]
            alternatives[host] = others
        return alternatives

    def _replace_host(
        self, parts: parse.SplitResult, new_host: str
    ) -> Optional[str]:
        """Return ``parts`` with the hostname replaced by ``new_host``."""

        if not parts.hostname:
            return None
        return parse.urlunsplit(
            (
                parts.scheme,
                f"{new_host}:{parts.port}" if parts.port else new_host,
                parts.path,
                parts.query,
                parts.fragment,
            )
        )

    def _download_product_image(
        self,
        image_url: Optional[str],
        product_name: str,
        product_id: str,
    ) -> Optional[str]:
        """Download and optionally compress the product lead image."""

        if not image_url:
            return None
        data = self._fetch(image_url)
        if data is None:
            return None
        suffix = self._guess_extension(image_url)
        data, suffix = self._compress_image(data, suffix)
        product_dir = self.image_dir / product_id
        product_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{product_id}_cover{suffix}"
        path = product_dir / filename
        path.write_bytes(data)
        return str(path)

    def _compress_image(self, data: bytes, original_suffix: str) -> Tuple[bytes, str]:
        """Compress ``data`` when Pillow is available."""

        global _PILLOW_WARNING_EMITTED
        if Image is None or ImageFile is None:
            if not _PILLOW_WARNING_EMITTED:
                LOGGER.warning(
                    "Pillow is not installed; storing product images without compression. "
                    "Install Pillow to enable image optimization."
                )
                _PILLOW_WARNING_EMITTED = True
            return data, original_suffix
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        try:
            with Image.open(BytesIO(data)) as image:
                image.load()
                if image.format == "WEBP":
                    buffer = BytesIO()
                    image.save(buffer, format="WEBP", lossless=True, method=6)
                    return buffer.getvalue(), ".webp"
                if image.format in {"JPEG", "JPG"}:
                    buffer = BytesIO()
                    image.save(buffer, format="JPEG", optimize=True, quality=85, progressive=True)
                    return buffer.getvalue(), ".jpg"
                buffer = BytesIO()
                try:
                    save_kwargs = {"format": "WEBP", "lossless": True, "method": 6}
                    if image.mode not in {"RGB", "RGBA", "L", "LA"}:
                        image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
                    image.save(buffer, **save_kwargs)
                    return buffer.getvalue(), ".webp"
                except (OSError, ValueError):
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

    @staticmethod
    def _extension_to_format(suffix: str) -> str:
        """Translate a filename suffix to a Pillow image format string."""

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

    @staticmethod
    def _guess_extension(url: str) -> str:
        """Infer the most likely file extension from ``url``."""

        parsed = parse.urlparse(url)
        _, ext = os.path.splitext(parsed.path)
        return ext if ext else ".jpg"


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
        super().__init__(host, timeout=timeout, context=context)
        self._server_hostname = server_hostname

    def connect(self) -> None:  # pragma: no cover - exercised via network operations
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

