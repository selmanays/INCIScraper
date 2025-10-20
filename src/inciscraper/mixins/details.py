"""Product detail scraping mixin for INCIScraper."""
from __future__ import annotations

import json
import re
import time
from collections import OrderedDict
from typing import List, Optional, Tuple

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from inciscraper.constants import (
    CACHE_SIZE_LIMIT,
    DEFAULT_BATCH_SIZE,
    PROGRESS_LOG_INTERVAL,
)
from inciscraper.mixins.database import DatabaseMixin
from inciscraper.mixins.monitoring import MonitoringMixin
from inciscraper.mixins.network import NetworkMixin
from inciscraper.types import Ingredient, ProductDetails

LOGGER = logging.getLogger(__name__)


class LRUCache:
    """Simple LRU cache implementation."""
    
    def __init__(self, capacity: int):
        self.capacity = capacity
        self.cache = OrderedDict()
    
    def get(self, key: str) -> Optional[dict]:
        """Get value from cache and move to end (most recently used)."""
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        return None
    
    def put(self, key: str, value: dict) -> None:
        """Put value in cache and evict least recently used if over capacity."""
        if key in self.cache:
            self.cache.move_to_end(key)
        elif len(self.cache) >= self.capacity:
            self.cache.popitem(last=False)  # Remove least recently used
        self.cache[key] = value
    
    def size(self) -> int:
        """Return current cache size."""
        return len(self.cache)


class DetailScraperMixin(DatabaseMixin, NetworkMixin, MonitoringMixin):
    """Mixin for scraping product details and ingredient information."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Initialize CosIng record cache with LRU eviction
        self._cosing_record_cache: LRUCache = LRUCache(CACHE_SIZE_LIMIT)
        self._load_cosing_cache_from_db()

    def _load_cosing_cache_from_db(self) -> None:
        """Load cached CosIng records from database into LRU cache."""
        try:
            cursor = self.conn.execute(
                "SELECT name, cosing_cas_numbers_json, cosing_ec_numbers_json, "
                "cosing_identified_ingredients_json, cosing_regulation_provisions_json, "
                "cosing_function_ids_json FROM ingredients WHERE "
                "cosing_cas_numbers_json IS NOT NULL OR cosing_ec_numbers_json IS NOT NULL "
                "OR cosing_identified_ingredients_json IS NOT NULL "
                "OR cosing_regulation_provisions_json IS NOT NULL "
                "OR cosing_function_ids_json IS NOT NULL"
            )
            
            loaded_count = 0
            for row in cursor.fetchall():
                cache_data = {
                    'cas_numbers': json.loads(row['cosing_cas_numbers_json'] or '[]'),
                    'ec_numbers': json.loads(row['cosing_ec_numbers_json'] or '[]'),
                    'identified_ingredients': json.loads(row['cosing_identified_ingredients_json'] or '[]'),
                    'regulation_provisions': json.loads(row['cosing_regulation_provisions_json'] or '[]'),
                    'function_ids': json.loads(row['cosing_function_ids_json'] or '[]')
                }
                self._cosing_record_cache.put(row['name'], cache_data)
                loaded_count += 1
            
            LOGGER.info(f"Loaded {loaded_count} CosIng records into LRU cache")
            
        except Exception as e:
            LOGGER.warning(f"Failed to load CosIng cache from database: {e}")

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
        if not pending_products:
            LOGGER.info("No products require detail scraping – skipping stage")
            return
        
        total_products = len(pending_products)
        LOGGER.info("Product detail workload: %s product(s) awaiting scraping", total_products)
        
        # Create progress bar for product detail processing
        self.create_progress_bar("product_details", total_products, "Processing product details")
        
        # Process products in batches
        batch_size = getattr(self, 'batch_size', 50)
        processed = 0
        
        for i in range(0, total_products, batch_size):
            batch = pending_products[i:i + batch_size]
            
            # Collect image tasks for parallel processing
            image_tasks = []
            for product in batch:
                product_id = product["id"]
                product_url = product["url"]
                product_name = product["name"]
                
                # Fetch product details
                details = self._scrape_product_page(product_url, product_name)
                if details is None:
                    LOGGER.warning("Failed to scrape product %s", product_name)
                    continue
                
                # Collect image task
                if details.image_url:
                    image_tasks.append((details.image_url, product_name, product_id, None))
                
                processed += 1
                self.update_progress("product_details", 1)
                
                if processed % PROGRESS_LOG_INTERVAL == 0:
                    self._log_progress("Product details", processed, total_products)
            
            # Process images in parallel
            image_results = []
            if image_tasks and not getattr(self, 'skip_images', False):
                image_results = self.download_images_parallel(image_tasks, skip_images=False)
            
            # Store product details with image paths
            for i, product in enumerate(batch):
                product_id = product["id"]
                product_url = product["url"]
                product_name = product["name"]
                
                # Fetch product details again (or use cached version)
                details = self._scrape_product_page(product_url, product_name)
                if details is None:
                    continue
                
                # Get image path from results
                image_path = None
                if i < len(image_results) and image_results[i]:
                    image_path = image_results[i]
                
                # Store product details with image path
                self._store_product_details(product_id, details, image_path)
            
            # Batch update product scraped status
            product_updates = [(1, product["id"]) for product in batch]
            self.batch_update_products_scraped(product_updates)
        
        self.close_progress_bar("product_details")
        LOGGER.info("Product detail scraping completed")

    def _scrape_product_page(self, url: str, product_name: str) -> Optional[ProductDetails]:
        """Scrape detailed product information from a product page."""

        response = self._fetch(url)
        if not response:
            return None

        soup = BeautifulSoup(response.content, "html.parser")
        
        # Extract basic product information
        description_elem = soup.select_one('div[class*="productdesc"] p')
        description = description_elem.get_text(strip=True) if description_elem else None
        
        # Check for discontinued products
        discontinued, replacement_url = self._check_discontinued_product(soup)
        
        # Extract product image URL
        image_url = self._extract_product_image_url(soup)
        
        # Extract ingredients
        ingredients = self._extract_ingredients(soup)
        
        # Extract ingredient functions
        ingredient_functions = self._extract_ingredient_functions(soup)
        
        # Extract highlights
        highlights = self._extract_product_highlights(soup)
        
        return ProductDetails(
            description=description,
            discontinued=discontinued,
            replacement_product_url=replacement_url,
            image_url=image_url,
            ingredients=ingredients,
            ingredient_functions=ingredient_functions,
            highlights=highlights,
        )

    def _extract_product_image_url(self, soup: BeautifulSoup) -> Optional[str]:
        """Extract product image URL from the page."""
        
        # Try multiple selectors for product images
        selectors = [
            'img[class*="productimg"]',
            'img[class*="product-image"]',
            'div[class*="productimg"] img',
            'div[class*="product-image"] img',
            'img[alt*="product"]',
            'img[src*="product"]'
        ]
        
        for selector in selectors:
            img_elem = soup.select_one(selector)
            if img_elem and img_elem.get('src'):
                src = img_elem.get('src')
                if src.startswith('//'):
                    src = 'https:' + src
                elif src.startswith('/'):
                    src = self.base_url + src
                return src
        
        return None

    def _extract_ingredients(self, soup: BeautifulSoup) -> List[Ingredient]:
        """Extract ingredient list from product page."""
        
        ingredients = []
        
        # Try multiple selectors for ingredient lists
        selectors = [
            'div[class*="ingredlist"]',
            'div[class*="ingredient"]',
            'div[class*="ingredients"]',
            'ul[class*="ingredient"]',
            'ol[class*="ingredient"]'
        ]
        
        for selector in selectors:
            ingredient_container = soup.select_one(selector)
            if ingredient_container:
                # Look for ingredient links
                ingredient_links = ingredient_container.select('a[href*="/ingredients/"]')
                
                for link in ingredient_links:
                    name = link.get_text(strip=True)
                    url = link.get('href')
                    
                    if url and url.startswith('/'):
                        url = self.base_url + url
                    
                    if name and url:
                        ingredients.append(Ingredient(name=name, url=url))
                
                if ingredients:
                    break
        
        return ingredients

    def _extract_ingredient_functions(self, soup: BeautifulSoup) -> List[str]:
        """Extract ingredient functions from product page."""
        
        functions = []
        
        # Try multiple selectors for function information
        selectors = [
            'div[class*="function"]',
            'span[class*="function"]',
            'div[class*="ingredlist"] div[class*="function"]'
        ]
        
        for selector in selectors:
            function_elems = soup.select(selector)
            for elem in function_elems:
                text = elem.get_text(strip=True)
                if text and 'function' in text.lower():
                    functions.append(text)
        
        return functions

    def _extract_product_highlights(self, soup: BeautifulSoup) -> List[str]:
        """Extract product highlights from the page."""
        
        highlights = []
        
        # Try multiple selectors for highlights
        selectors = [
            'div[class*="highlight"]',
            'div[class*="feature"]',
            'ul[class*="highlight"]',
            'ul[class*="feature"]'
        ]
        
        for selector in selectors:
            highlight_elems = soup.select(selector)
            for elem in highlight_elems:
                text = elem.get_text(strip=True)
                if text:
                    highlights.append(text)
        
        return highlights

    def _check_discontinued_product(self, soup: BeautifulSoup) -> Tuple[bool, Optional[str]]:
        """Check if product is discontinued and extract replacement URL."""
        
        # Look for discontinued indicators
        discontinued_indicators = [
            'discontinued',
            'no longer available',
            'out of stock',
            'unavailable'
        ]
        
        page_text = soup.get_text().lower()
        for indicator in discontinued_indicators:
            if indicator in page_text:
                # Look for replacement product link
                replacement_link = soup.select_one('a[href*="/products/"]')
                replacement_url = replacement_link.get('href') if replacement_link else None
                return True, replacement_url
        
        return False, None

    def _store_product_details(self, product_id: str, details: ProductDetails, image_path: Optional[str] = None) -> None:
        """Store product details in the database."""
        
        # Update product record with basic details
        self.conn.execute(
            """
            UPDATE products SET 
                description = ?, 
                discontinued = ?, 
                replacement_product_url = ?,
                image_path = ?,
                last_updated_at = ?
            WHERE id = ?
            """,
            (
                details.description,
                1 if details.discontinued else 0,
                details.replacement_product_url,
                image_path,
                self._current_timestamp(),
                product_id,
            ),
        )
        
        # Process ingredients
        ingredient_ids = []
        for ingredient in details.ingredients:
            ingredient_id = self._ensure_ingredient(ingredient.name, ingredient.url)
            if ingredient_id:
                ingredient_ids.append(ingredient_id)
        
        # Update product with ingredient IDs
        if ingredient_ids:
            self.conn.execute(
                "UPDATE products SET ingredient_ids_json = ? WHERE id = ?",
                (json.dumps(ingredient_ids), product_id),
            )
        
        # Process ingredient functions
        for func in details.ingredient_functions:
            self._store_ingredient_function(func)
        
        # Process highlights
        self._store_product_highlights(product_id, details.highlights)
        
        self.conn.commit()

    def _ensure_ingredient(self, name: str, url: str) -> Optional[str]:
        """Ensure ingredient exists in database and return its ID."""
        
        # Check if ingredient already exists
        row = self.conn.execute(
            "SELECT id FROM ingredients WHERE name = ? OR url = ?",
            (name, url),
        ).fetchone()
        
        if row:
            return row["id"]
        
        # Create new ingredient
        ingredient_id = self._generate_id()
        now = self._current_timestamp()
        
        # Scrape ingredient details
        ingredient_details = self._scrape_ingredient_page(url, name)
        
        if ingredient_details:
            # Store ingredient with details
            self.conn.execute(
                """
                INSERT INTO ingredients (
                    id, name, url, rating_tag, also_called, irritancy, comedogenicity,
                    details_text, cosing_cas_numbers_json, cosing_ec_numbers_json,
                    cosing_identified_ingredients_json, cosing_regulation_provisions_json,
                    cosing_function_ids_json, quick_facts_json, proof_references_json,
                    last_checked_at, last_updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ingredient_id, name, url, ingredient_details.rating_tag,
                    json.dumps(ingredient_details.also_called),
                    ingredient_details.irritancy, ingredient_details.comedogenicity,
                    ingredient_details.details_text,
                    json.dumps(ingredient_details.cosing_cas_numbers),
                    json.dumps(ingredient_details.cosing_ec_numbers),
                    json.dumps(ingredient_details.cosing_identified_ingredients),
                    json.dumps(ingredient_details.cosing_regulation_provisions),
                    json.dumps(ingredient_details.cosing_function_ids),
                    json.dumps(ingredient_details.quick_facts),
                    json.dumps(ingredient_details.proof_references),
                    now, now
                ),
            )
        else:
            # Store ingredient without details
            self.conn.execute(
                """
                INSERT INTO ingredients (id, name, url, last_checked_at, last_updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (ingredient_id, name, url, now, now),
            )
        
        self.conn.commit()
        return ingredient_id

    def _scrape_ingredient_page(self, url: str, ingredient_name: str) -> Optional[dict]:
        """Scrape detailed ingredient information from ingredient page."""
        
        response = self._fetch(url)
        if not response:
            return None

        soup = BeautifulSoup(response.content, "html.parser")
        
        # Extract rating tag
        rating_elem = soup.select_one('span[class*="rating"]')
        rating_tag = rating_elem.get_text(strip=True) if rating_elem else None
        
        # Extract "also called" information
        also_called = []
        also_called_elem = soup.select_one('div[class*="alsocalled"]')
        if also_called_elem:
            also_called_links = also_called_elem.select('a')
            for link in also_called_links:
                name = link.get_text(strip=True)
                if name:
                    also_called.append(name)
        
        # Extract irritancy and comedogenicity
        irritancy = None
        comedogenicity = None
        
        irritancy_elem = soup.select_one('span[class*="irritancy"]')
        if irritancy_elem:
            irritancy = irritancy_elem.get_text(strip=True)
        
        comedogenicity_elem = soup.select_one('span[class*="comedogenicity"]')
        if comedogenicity_elem:
            comedogenicity = comedogenicity_elem.get_text(strip=True)
        
        # Extract detailed text
        details_elem = soup.select_one('div[class*="ingreddesc"]')
        details_text = details_elem.get_text(strip=True) if details_elem else None
        
        # Extract CosIng data
        cosing_data = self._retrieve_cosing_data(ingredient_name)
        
        return {
            'rating_tag': rating_tag,
            'also_called': also_called,
            'irritancy': irritancy,
            'comedogenicity': comedogenicity,
            'details_text': details_text,
            'cosing_cas_numbers': cosing_data.get('cas_numbers', []),
            'cosing_ec_numbers': cosing_data.get('ec_numbers', []),
            'cosing_identified_ingredients': cosing_data.get('identified_ingredients', []),
            'cosing_regulation_provisions': cosing_data.get('regulation_provisions', []),
            'cosing_function_ids': cosing_data.get('function_ids', []),
            'quick_facts': cosing_data.get('quick_facts', []),
            'proof_references': cosing_data.get('proof_references', []),
        }

    def _retrieve_cosing_data(self, ingredient_name: str) -> dict:
        """Retrieve CosIng data for an ingredient using optimized approach."""
        
        # Check cache first
        cached_data = self._cosing_record_cache.get(ingredient_name)
        if cached_data:
            LOGGER.debug(f"CosIng cache hit for {ingredient_name}")
            return cached_data
        
        LOGGER.debug(f"CosIng cache miss for {ingredient_name}, fetching from web")
        
        # Try optimized CosIng scraping
        cosing_data = self._fetch_cosing_detail_optimized(ingredient_name)
        
        if cosing_data:
            # Cache the result
            self._cosing_record_cache.put(ingredient_name, cosing_data)
            return cosing_data
        
        return {
            'cas_numbers': [],
            'ec_numbers': [],
            'identified_ingredients': [],
            'regulation_provisions': [],
            'function_ids': [],
            'quick_facts': [],
            'proof_references': []
        }

    def _fetch_cosing_detail_optimized(self, ingredient_name: str) -> Optional[dict]:
        """Optimized CosIng detail fetching with smart fallback strategy."""
        
        # First try: Regular Playwright (3s timeout)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                
                # Set shorter timeouts for faster failure
                page.set_default_timeout(3000)
                
                # Try direct URL first
                direct_url = self._try_direct_cosing_url(ingredient_name)
                if direct_url:
                    try:
                        page.goto(direct_url, timeout=3000)
                        page.wait_for_load_state('domcontentloaded', timeout=3000)
                        
                        # Quick check if page loaded successfully
                        if self._wait_for_cosing_dynamic_content(page, timeout=2000):
                            cosing_data = self._extract_cosing_data_from_page(page)
                            if cosing_data:
                                browser.close()
                                return cosing_data
                    except Exception as e:
                        LOGGER.debug(f"Direct URL failed for {ingredient_name}: {e}")
                
                browser.close()
        except Exception as e:
            LOGGER.debug(f"Regular Playwright failed for {ingredient_name}: {e}")
        
        # Second try: Optimized Playwright (20s timeout)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                
                # Set longer timeout for search-based approach
                page.set_default_timeout(20000)
                
                # Try search-based approach
                search_url = f"https://ec.europa.eu/growth/tools-databases/cosing/search"
                page.goto(search_url, timeout=10000)
                page.wait_for_load_state('domcontentloaded', timeout=10000)
                
                # Wait for search form to be ready
                page.wait_for_selector('input[name="search_term"]', timeout=10000)
                
                # Fill search form
                page.fill('input[name="search_term"]', ingredient_name)
                page.click('button[type="submit"]')
                
                # Wait for results
                page.wait_for_load_state('networkidle', timeout=10000)
                
                if self._wait_for_cosing_dynamic_content(page, timeout=5000):
                    cosing_data = self._extract_cosing_data_from_page(page)
                    if cosing_data:
                        browser.close()
                        return cosing_data
                
                browser.close()
        except Exception as e:
            LOGGER.debug(f"Optimized Playwright failed for {ingredient_name}: {e}")
        
        return None

    def _try_direct_cosing_url(self, ingredient_name: str) -> Optional[str]:
        """Try to construct direct CosIng URL using search-based approach."""
        
        # Clean ingredient name for URL
        clean_name = ingredient_name.lower().replace(' ', '-').replace('/', '-')
        
        # Try common CosIng URL patterns
        possible_urls = [
            f"https://ec.europa.eu/growth/tools-databases/cosing/search?search_term={clean_name}",
            f"https://ec.europa.eu/growth/tools-databases/cosing/search?search_term={ingredient_name}",
        ]
        
        return possible_urls[0]  # Return first option for now

    def _fetch_cosing_detail_via_playwright_optimized(self, ingredient_name: str, page) -> Optional[dict]:
        """Optimized Playwright-based CosIng detail fetching."""
        
        try:
            # Try direct URL first
            direct_url = self._try_direct_cosing_url(ingredient_name)
            if direct_url:
                page.goto(direct_url, timeout=5000)
                page.wait_for_load_state('domcontentloaded', timeout=5000)
                
                if self._wait_for_cosing_dynamic_content(page, timeout=3000):
                    return self._extract_cosing_data_from_page(page)
            
            # Fallback to search
            search_url = "https://ec.europa.eu/growth/tools-databases/cosing/search"
            page.goto(search_url, timeout=5000)
            page.wait_for_load_state('domcontentloaded', timeout=5000)
            
            # Wait for search form
            page.wait_for_selector('input[name="search_term"]', timeout=5000)
            
            # Fill and submit search
            page.fill('input[name="search_term"]', ingredient_name)
            page.click('button[type="submit"]')
            
            # Wait for results
            page.wait_for_load_state('networkidle', timeout=10000)
            
            if self._wait_for_cosing_dynamic_content(page, timeout=5000):
                return self._extract_cosing_data_from_page(page)
            
        except Exception as e:
            LOGGER.debug(f"Optimized Playwright failed for {ingredient_name}: {e}")
        
        return None

    def _wait_for_cosing_dynamic_content(self, page, timeout: int = 5000) -> bool:
        """Wait for CosIng dynamic content to load with multiple selector attempts."""
        
        selectors_to_try = [
            '.search-results',
            '.result-item',
            '[class*="result"]',
            '[class*="search"]',
            'table',
            '.content'
        ]
        
        timeout_per_selector = timeout // len(selectors_to_try)
        
        for selector in selectors_to_try:
            try:
                page.wait_for_selector(selector, timeout=timeout_per_selector)
                return True
            except:
                continue
        
        return False

    def _extract_cosing_data_from_page(self, page) -> Optional[dict]:
        """Extract CosIng data from the current page."""
        
        try:
            # Extract CAS numbers
            cas_numbers = []
            cas_elements = page.query_selector_all('[data-cas], .cas-number, [class*="cas"]')
            for elem in cas_elements:
                cas_text = elem.inner_text().strip()
                if cas_text and 'cas' in cas_text.lower():
                    cas_numbers.append(cas_text)
            
            # Extract EC numbers
            ec_numbers = []
            ec_elements = page.query_selector_all('[data-ec], .ec-number, [class*="ec"]')
            for elem in ec_elements:
                ec_text = elem.inner_text().strip()
                if ec_text and 'ec' in ec_text.lower():
                    ec_numbers.append(ec_text)
            
            # Extract identified ingredients
            identified_ingredients = []
            ingredient_elements = page.query_selector_all('[class*="ingredient"], [class*="substance"]')
            for elem in ingredient_elements:
                ingredient_text = elem.inner_text().strip()
                if ingredient_text:
                    identified_ingredients.append(ingredient_text)
            
            # Extract regulation provisions
            regulation_provisions = []
            regulation_elements = page.query_selector_all('[class*="regulation"], [class*="provision"]')
            for elem in regulation_elements:
                regulation_text = elem.inner_text().strip()
                if regulation_text:
                    regulation_provisions.append(regulation_text)
            
            # Extract function IDs
            function_ids = []
            function_elements = page.query_selector_all('[class*="function"], [data-function]')
            for elem in function_elements:
                function_text = elem.inner_text().strip()
                if function_text:
                    function_ids.append(function_text)
            
            return {
                'cas_numbers': cas_numbers,
                'ec_numbers': ec_numbers,
                'identified_ingredients': identified_ingredients,
                'regulation_provisions': regulation_provisions,
                'function_ids': function_ids,
                'quick_facts': [],
                'proof_references': []
            }
            
        except Exception as e:
            LOGGER.debug(f"Failed to extract CosIng data: {e}")
            return None

    def _store_ingredient_function(self, function: str) -> None:
        """Store ingredient function in database."""
        
        # Check if function already exists
        existing = self.conn.execute(
            "SELECT id FROM functions WHERE name = ?",
            (function,),
        ).fetchone()
        
        if existing:
            return
        
        # Create new function
        function_id = self._generate_id()
        now = self._current_timestamp()
        
        self.conn.execute(
            """
            INSERT INTO functions (id, name, last_updated_at)
            VALUES (?, ?, ?)
            """,
            (function_id, function, now),
        )
        self.conn.commit()

    def _store_product_highlights(self, product_id: str, highlights: List[str]) -> None:
        """Store product highlights in database."""
        
        if not highlights:
            return
        
        # Delete existing highlights
        self.conn.execute(
            "DELETE FROM product_highlights WHERE product_id = ?",
            (product_id,),
        )
        
        # Insert new highlights
        for highlight in highlights:
            highlight_id = self._generate_id()
            now = self._current_timestamp()
            
            self.conn.execute(
                """
                INSERT INTO product_highlights (id, product_id, highlight_text, last_updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (highlight_id, product_id, highlight, now),
            )
        
        self.conn.commit()

    def _log_progress(self, stage: str, processed: int, total: int) -> None:
        """Log progress for a specific stage."""
        
        percentage = (processed / total) * 100 if total > 0 else 0
        LOGGER.info(
            "%s progress: %s/%s (%.1f%%)",
            stage, processed, total, percentage
        )


# Add missing import
import logging