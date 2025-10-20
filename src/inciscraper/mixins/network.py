"""Network utilities mixin for INCIScraper."""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from PIL import Image
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from inciscraper.constants import (
    ADAPTIVE_RATE_FACTOR,
    DEFAULT_IMAGE_WORKERS,
    MAX_RATE_LIMIT,
    MIN_RATE_LIMIT,
)
from inciscraper.mixins.monitoring import MonitoringMixin

LOGGER = logging.getLogger(__name__)


class NetworkMixin(MonitoringMixin):
    """Mixin for network operations and HTTP utilities."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.base_url = kwargs.get('base_url', 'https://incidecoder.com')
        self.alternate_base_urls = kwargs.get('alternate_base_urls', [])
        self.image_workers = kwargs.get('image_workers', DEFAULT_IMAGE_WORKERS)
        
        # Initialize rate limiting
        self._init_rate_limiting()
        
        # Setup HTTP session
        self.session = requests.Session()
        self._setup_session()

    def _init_rate_limiting(self) -> None:
        """Initialize adaptive rate limiting variables."""
        self._current_rate_limit = MIN_RATE_LIMIT
        self._last_request_time = 0
        self._consecutive_successes = 0
        self._consecutive_failures = 0

    def _setup_session(self) -> None:
        """Setup HTTP session with retry strategy."""
        
        # Configure retry strategy
        retry_strategy = Retry(
            total=3,
            status_forcelist=[429, 500, 502, 503, 504],
            method_whitelist=["HEAD", "GET", "OPTIONS"],
            backoff_factor=1
        )
        
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        
        # Set user agent
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })

    def _apply_rate_limit(self) -> None:
        """Apply adaptive rate limiting."""
        
        current_time = time.time()
        time_since_last = current_time - self._last_request_time
        
        if time_since_last < self._current_rate_limit:
            sleep_time = self._current_rate_limit - time_since_last
            LOGGER.debug(f"Rate limiting: sleeping for {sleep_time:.2f}s")
            time.sleep(sleep_time)
        
        self._last_request_time = time.time()

    def _update_rate_limit_on_success(self) -> None:
        """Update rate limit based on successful request."""
        
        self._consecutive_successes += 1
        self._consecutive_failures = 0
        
        # Decrease rate limit (faster requests) after consecutive successes
        if self._consecutive_successes >= 3:
            self._current_rate_limit = max(
                MIN_RATE_LIMIT,
                self._current_rate_limit * ADAPTIVE_RATE_FACTOR
            )
            self._consecutive_successes = 0
            LOGGER.debug(f"Rate limit decreased to {self._current_rate_limit:.2f}s")

    def _update_rate_limit_on_failure(self) -> None:
        """Update rate limit based on failed request."""
        
        self._consecutive_failures += 1
        self._consecutive_successes = 0
        
        # Increase rate limit (slower requests) after consecutive failures
        if self._consecutive_failures >= 2:
            self._current_rate_limit = min(
                MAX_RATE_LIMIT,
                self._current_rate_limit / ADAPTIVE_RATE_FACTOR
            )
            self._consecutive_failures = 0
            LOGGER.debug(f"Rate limit increased to {self._current_rate_limit:.2f}s")

    def _fetch(self, url: str) -> Optional[requests.Response]:
        """Fetch a URL with failover support and adaptive rate limiting."""
        
        # Apply rate limiting
        self._apply_rate_limit()
        
        urls_to_try = [url]
        if self.alternate_base_urls:
            parsed_url = urlparse(url)
            for alt_base in self.alternate_base_urls:
                alt_url = urljoin(alt_base, parsed_url.path)
                urls_to_try.append(alt_url)
        
        for attempt_url in urls_to_try:
            try:
                response = self.session.get(attempt_url, timeout=30)
                response.raise_for_status()
                
                # Update rate limit on success
                self._update_rate_limit_on_success()
                
                return response
                
            except requests.RequestException as e:
                LOGGER.warning(
                    "Request failed for %s: %s. Trying next URL...",
                    attempt_url, e
                )
                continue
        
        # Update rate limit on failure
        self._update_rate_limit_on_failure()
        
        LOGGER.error("All URL attempts failed for %s", url)
        return None

    def download_images_parallel(self, image_tasks: List[Tuple], skip_images: bool = False) -> List[Optional[str]]:
        """Download and process images in parallel."""
        
        if skip_images or not image_tasks:
            return [None] * len(image_tasks)
        
        results = [None] * len(image_tasks)
        
        with ThreadPoolExecutor(max_workers=self.image_workers) as executor:
            # Submit all image download tasks
            future_to_index = {}
            for i, (image_url, product_name, product_id, _) in enumerate(image_tasks):
                if image_url:
                    future = executor.submit(
                        self._download_product_image_parallel,
                        image_url, product_name, product_id
                    )
                    future_to_index[future] = i
            
            # Process completed tasks as they finish
            for future in as_completed(future_to_index):
                try:
                    result = future.result()
                    index = future_to_index[future]
                    results[index] = result
                except Exception as e:
                    index = future_to_index[future]
                    LOGGER.error(f"Failed to download image for task {index}: {e}")
                    results[index] = None
        
        return results

    def _download_product_image_parallel(self, image_url: str, product_name: str, product_id: str) -> Optional[str]:
        """Download and process a product image (thread-safe wrapper)."""
        
        try:
            return self._download_product_image(image_url, product_name, product_id)
        except Exception as e:
            LOGGER.error(f"Failed to download image for product {product_name}: {e}")
            return None

    def _download_product_image(self, image_url: str, product_name: str, product_id: str) -> Optional[str]:
        """Download and process a product image."""
        
        if not image_url:
            return None
        
        try:
            # Download image
            response = self._fetch(image_url)
            if not response:
                return None
            
            # Create image directory
            image_dir = Path(self.image_dir)
            image_dir.mkdir(parents=True, exist_ok=True)
            
            # Generate filename
            filename = f"{product_id}.webp"
            image_path = image_dir / filename
            
            # Process and save image
            with Image.open(response.raw) as img:
                # Convert to RGB if necessary
                if img.mode in ('RGBA', 'LA', 'P'):
                    img = img.convert('RGB')
                
                # Resize if too large
                max_size = (800, 800)
                if img.size[0] > max_size[0] or img.size[1] > max_size[1]:
                    img.thumbnail(max_size, Image.Resampling.LANCZOS)
                
                # Save as WebP
                img.save(image_path, 'WebP', quality=85, optimize=True)
            
            return str(image_path)
            
        except Exception as e:
            LOGGER.error(f"Failed to download image for product {product_name}: {e}")
            return None

    def close(self) -> None:
        """Close network resources."""
        
        if hasattr(self, 'session'):
            self.session.close()