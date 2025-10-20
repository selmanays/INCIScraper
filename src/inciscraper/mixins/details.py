"""Product detail scraping, ingredient persistence and CosIng helpers."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import unicodedata
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib import parse

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

from ..constants import (
    COSING_BASE_URL,
    INGREDIENT_FETCH_ATTEMPTS,
    INGREDIENT_PLACEHOLDER_MARKER,
    PROGRESS_LOG_INTERVAL,
    CACHE_SIZE_LIMIT,
)
from ..models import (
    CosIngRecord,
    FreeTag,
    HighlightEntry,
    IngredientDetails,
    IngredientFunction,
    IngredientFunctionInfo,
    IngredientReference,
    ProductDetails,
    ProductHighlights,
)
from ..parser import Node, extract_text, parse_html

LOGGER = logging.getLogger(__name__)


class LRUCache:
    """Simple LRU cache implementation with size limit."""
    
    def __init__(self, max_size: int = CACHE_SIZE_LIMIT):
        self.max_size = max_size
        self.cache = OrderedDict()
    
    def get(self, key: str) -> Optional[Any]:
        """Get value from cache, updating access order."""
        if key in self.cache:
            # Move to end (most recently used)
            value = self.cache.pop(key)
            self.cache[key] = value
            return value
        return None
    
    def put(self, key: str, value: Any) -> None:
        """Put value in cache, evicting least recently used if needed."""
        if key in self.cache:
            # Update existing key
            self.cache.pop(key)
        elif len(self.cache) >= self.max_size:
            # Evict least recently used (first item)
            self.cache.popitem(last=False)
        
        self.cache[key] = value
    
    def __contains__(self, key: str) -> bool:
        return key in self.cache
    
    def __len__(self) -> int:
        return len(self.cache)


class DetailScraperMixin:
    """Handle product details, ingredient parsing and CosIng integration."""

    conn: sqlite3.Connection
    _cosing_playwright: Optional[Any]
    _cosing_browser: Optional[Any]
    _cosing_context: Optional[Any]
    _cosing_page: Optional[Any]
    _cosing_playwright_failed: bool
    _cosing_record_cache: LRUCache

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
                
                # Store product details
                self._store_product_details(product_id, details)
                
                # Collect image task
                if details.image_url:
                    image_tasks.append((details.image_url, product_name, product_id, None))
                
                processed += 1
                self.update_progress("product_details", 1)
                
                if processed % PROGRESS_LOG_INTERVAL == 0:
                    self._log_progress("Product details", processed, total_products)
            
            # Process images in parallel
            if image_tasks and not getattr(self, 'skip_images', False):
                self.download_images_parallel(image_tasks, skip_images=False)
            
            # Batch update product scraped status
            product_updates = [(1, product["id"]) for product in batch]
            self.batch_update_products_scraped(product_updates)
        
        self.close_progress_bar("product_details")
        LOGGER.info("Product detail scraping completed")

    def _scrape_product_page(self, url: str, name: str) -> Optional[ProductDetails]:
        """Scrape detailed information from a product page."""

        html = self._fetch_html(url)
        if html is None:
            return None
        
        root = parse_html(html)
        
        # Extract basic product information
        description = self._extract_product_description(root)
        image_url = self._extract_product_image_url(root)
        
        # Extract ingredients
        ingredients = self._extract_ingredients(root)
        
        # Extract ingredient functions
        ingredient_functions = self._extract_ingredient_functions(root)
        
        # Extract highlights
        highlights = self._extract_highlights(root)
        
        # Check for discontinued products
        discontinued, replacement_url = self._check_discontinued(root)
        
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

    def _extract_product_description(self, root: Node) -> str:
        """Extract product description from the page."""
        
        # Try multiple selectors for description
        selectors = [
            '.product__description',
            '.product-description',
            '.description',
            '[data-testid="product-description"]',
        ]
        
        for selector in selectors:
            desc_elem = root.find(class_=selector.replace('.', ''))
            if desc_elem:
                return desc_elem.get_text(strip=True)
        
        return ""

    def _extract_product_image_url(self, root: Node) -> Optional[str]:
        """Extract product image URL from the page."""
        
        # Try multiple selectors for product image
        selectors = [
            '.product__image img',
            '.product-image img',
            '.product__hero img',
            '[data-testid="product-image"] img',
        ]
        
        for selector in selectors:
            img_elem = root.find(tag='img', class_=selector.split()[-1] if ' ' in selector else None)
            if img_elem:
                src = img_elem.get('src')
                if src:
                    return self._absolute_url(src)
        
        return None

    def _extract_ingredients(self, root: Node) -> List[IngredientReference]:
        """Extract ingredient references from the page."""
        
        ingredients = []
        
        # Try multiple selectors for ingredients
        selectors = [
            '.ingredients-list',
            '.product__ingredients',
            '.ingredients',
            '[data-testid="ingredients"]',
        ]
        
        for selector in selectors:
            ingredients_elem = root.find(class_=selector.replace('.', ''))
            if ingredients_elem:
                # Extract ingredient links
                links = ingredients_elem.find_all(tag='a')
                for link in links:
                    href = link.get('href')
                    name = extract_text(link)
                    if href and name:
                        ingredients.append(IngredientReference(
                            name=name,
                            url=self._absolute_url(href),
                            tooltip_text=None,
                            tooltip_ingredient_link=None,
                        ))
                break
        
        return ingredients

    def _extract_ingredient_functions(self, root: Node) -> List[IngredientFunction]:
        """Extract ingredient functions from the page."""
        
        functions = []
        
        # Try multiple selectors for ingredient functions
        selectors = [
            '.ingredient-functions',
            '.product__functions',
            '.functions',
        ]
        
        for selector in selectors:
            functions_elem = root.find(class_=selector.replace('.', ''))
            if functions_elem:
                # Extract function information
                function_items = functions_elem.find_all(class_='function-item')
                for item in function_items:
                    ingredient_name = extract_text(item.find(class_='ingredient-name'))
                    what_it_does = [extract_text(func) for func in item.find_all(class_='function')]
                    function_links = [link.get('href') for link in item.find_all(tag='a') if link.get('href')]
                    
                    if ingredient_name:
                        functions.append(IngredientFunction(
                            ingredient_name=ingredient_name,
                            ingredient_page=None,
                            what_it_does=what_it_does,
                            function_links=function_links,
                        ))
                break
        
        return functions

    def _extract_highlights(self, root: Node) -> ProductHighlights:
        """Extract product highlights from the page."""
        
        free_tags = []
        key_ingredients = []
        other_ingredients = []
        
        # Extract free tags
        free_tag_elements = root.find_all(class_='free-tag')
        for tag_elem in free_tag_elements:
            tag_text = extract_text(tag_elem)
            tooltip = tag_elem.get('title') or tag_elem.get('data-tooltip')
            if tag_text:
                free_tags.append(FreeTag(tag=tag_text, tooltip=tooltip))
        
        # Extract key ingredients
        key_ingredient_elements = root.find_all(class_='key-ingredient')
        for ing_elem in key_ingredient_elements:
            ingredient_name = extract_text(ing_elem.find(class_='ingredient-name'))
            function_name = extract_text(ing_elem.find(class_='function-name'))
            function_link = ing_elem.find(class_='function-link')
            function_url = function_link.get('href') if function_link else None
            
            if ingredient_name:
                key_ingredients.append(HighlightEntry(
                    function_name=function_name,
                    function_link=function_url,
                    ingredient_name=ingredient_name,
                    ingredient_page=None,
                ))
        
        # Extract other ingredients
        other_ingredient_elements = root.find_all(class_='other-ingredient')
        for ing_elem in other_ingredient_elements:
            ingredient_name = extract_text(ing_elem.find(class_='ingredient-name'))
            function_name = extract_text(ing_elem.find(class_='function-name'))
            function_link = ing_elem.find(class_='function-link')
            function_url = function_link.get('href') if function_link else None
            
            if ingredient_name:
                other_ingredients.append(HighlightEntry(
                    function_name=function_name,
                    function_link=function_url,
                    ingredient_name=ingredient_name,
                    ingredient_page=None,
                ))
        
        return ProductHighlights(
            free_tags=free_tags,
            key_ingredients=key_ingredients,
            other_ingredients=other_ingredients,
        )

    def _check_discontinued(self, root: Node) -> Tuple[bool, Optional[str]]:
        """Check if product is discontinued and get replacement URL."""
        
        # Check for discontinued indicators
        discontinued_indicators = [
            '.discontinued',
            '.product-discontinued',
            '.out-of-stock',
        ]
        
        for indicator in discontinued_indicators:
            if root.find(class_=indicator.replace('.', '')):
                # Look for replacement product link
                replacement_link = root.find(class_='replacement-product')
                replacement_url = replacement_link.get('href') if replacement_link else None
                return True, replacement_url
        
        return False, None

    def _store_product_details(self, product_id: str, details: ProductDetails) -> None:
        """Store product details in the database."""
        
        # Update product record with basic details
        self.conn.execute(
            """
            UPDATE products SET 
                description = ?, 
                discontinued = ?, 
                replacement_product_url = ?,
                last_updated_at = ?
            WHERE id = ?
            """,
            (
                details.description,
                1 if details.discontinued else 0,
                details.replacement_product_url,
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
                    json.dumps([f.id for f in ingredient_details.cosing_function_infos]),
                    json.dumps(ingredient_details.quick_facts),
                    json.dumps(ingredient_details.proof_references),
                    now, now
                ),
            )
        else:
            # Store basic ingredient record
            self.conn.execute(
                """
                INSERT INTO ingredients (
                    id, name, url, last_checked_at, last_updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (ingredient_id, name, url, now, now),
            )
        
        self.conn.commit()
        return ingredient_id

    def _scrape_ingredient_page(self, url: str, name: str) -> Optional[IngredientDetails]:
        """Scrape detailed information from an ingredient page."""
        
        html = self._fetch_html(url)
        if html is None:
            return None
        
        root = parse_html(html)
        
        # Extract basic ingredient information
        rating_tag = self._extract_rating_tag(root)
        also_called = self._extract_also_called(root)
        irritancy = self._extract_irritancy(root)
        comedogenicity = self._extract_comedogenicity(root)
        details_text = self._extract_details_text(root)
        quick_facts = self._extract_quick_facts(root)
        proof_references = self._extract_proof_references(root)
        
        # Extract CosIng data
        cosing_data = self._retrieve_cosing_data(name)
        
        # Extract function information
        function_infos = self._extract_function_infos(root)
        
        return IngredientDetails(
            name=name,
            url=url,
            rating_tag=rating_tag,
            also_called=also_called,
            irritancy=irritancy,
            comedogenicity=comedogenicity,
            details_text=details_text,
            cosing_cas_numbers=cosing_data.cas_numbers if cosing_data else [],
            cosing_ec_numbers=cosing_data.ec_numbers if cosing_data else [],
            cosing_identified_ingredients=cosing_data.identified_ingredients if cosing_data else [],
            cosing_regulation_provisions=cosing_data.regulation_provisions if cosing_data else [],
            cosing_function_infos=function_infos,
            quick_facts=quick_facts,
            proof_references=proof_references,
        )

    def _extract_rating_tag(self, root: Node) -> str:
        """Extract rating tag from ingredient page."""
        
        rating_elem = root.find(class_='rating-tag')
        if rating_elem:
            return extract_text(rating_elem)
        return ""

    def _extract_also_called(self, root: Node) -> List[str]:
        """Extract also called names from ingredient page."""
        
        also_called_elem = root.find(class_='also-called')
        if also_called_elem:
            names = [extract_text(span) for span in also_called_elem.find_all(tag='span')]
            return [name for name in names if name]
        return []

    def _extract_irritancy(self, root: Node) -> str:
        """Extract irritancy information from ingredient page."""
        
        irritancy_elem = root.find(class_='irritancy')
        if irritancy_elem:
            return extract_text(irritancy_elem)
        return ""

    def _extract_comedogenicity(self, root: Node) -> str:
        """Extract comedogenicity information from ingredient page."""
        
        comedogenicity_elem = root.find(class_='comedogenicity')
        if comedogenicity_elem:
            return extract_text(comedogenicity_elem)
        return ""

    def _extract_details_text(self, root: Node) -> str:
        """Extract detailed text from ingredient page."""
        
        details_elem = root.find(class_='ingredient-details')
        if details_elem:
            return details_elem.get_text(strip=True)
        return ""

    def _extract_quick_facts(self, root: Node) -> List[str]:
        """Extract quick facts from ingredient page."""
        
        facts_elem = root.find(class_='quick-facts')
        if facts_elem:
            facts = [extract_text(li) for li in facts_elem.find_all(tag='li')]
            return [fact for fact in facts if fact]
        return []

    def _extract_proof_references(self, root: Node) -> List[str]:
        """Extract proof references from ingredient page."""
        
        refs_elem = root.find(class_='proof-references')
        if refs_elem:
            refs = [extract_text(li) for li in refs_elem.find_all(tag='li')]
            return [ref for ref in refs if ref]
        return []

    def _extract_function_infos(self, root: Node) -> List[IngredientFunctionInfo]:
        """Extract function information from ingredient page."""
        
        function_infos = []
        
        function_elements = root.find_all(class_='function-info')
        for func_elem in function_elements:
            name = extract_text(func_elem.find(class_='function-name'))
            description = extract_text(func_elem.find(class_='function-description'))
            link_elem = func_elem.find(tag='a')
            url = link_elem.get('href') if link_elem else None
            
            if name:
                function_infos.append(IngredientFunctionInfo(
                    name=name,
                    url=url,
                    description=description,
                ))
        
        return function_infos

    def _store_ingredient_function(self, func: IngredientFunction) -> None:
        """Store ingredient function in the database."""
        
        # Store function entries
        for func_name in func.what_it_does:
            if func_name:
                func_id = self._ensure_function(func_name)
                if func_id:
                    # Store ingredient-function relationship
                    self.conn.execute(
                        """
                        INSERT OR IGNORE INTO ingredient_functions (
                            ingredient_id, function_id
                        ) VALUES (?, ?)
                        """,
                        (func.ingredient_id, func_id),
                    )
        
        self.conn.commit()

    def _ensure_function(self, name: str) -> Optional[str]:
        """Ensure function exists in database and return its ID."""
        
        # Check if function already exists
        row = self.conn.execute(
            "SELECT id FROM functions WHERE name = ?",
            (name,),
        ).fetchone()
        
        if row:
            return row["id"]
        
        # Create new function
        func_id = self._generate_id()
        self.conn.execute(
            "INSERT INTO functions (id, name) VALUES (?, ?)",
            (func_id, name),
        )
        self.conn.commit()
        return func_id

    def _store_product_highlights(self, product_id: str, highlights: ProductHighlights) -> None:
        """Store product highlights in the database."""
        
        # Store free tags
        free_tag_ids = []
        for tag in highlights.free_tags:
            tag_id = self._ensure_free_tag(tag.tag, tag.tooltip)
            if tag_id:
                free_tag_ids.append(tag_id)
        
        # Store key ingredients
        key_ingredient_ids = []
        for ingredient in highlights.key_ingredients:
            if ingredient.ingredient_name:
                ingredient_id = self._ensure_ingredient(ingredient.ingredient_name, ingredient.ingredient_page or "")
                if ingredient_id:
                    key_ingredient_ids.append(ingredient_id)
        
        # Store other ingredients
        other_ingredient_ids = []
        for ingredient in highlights.other_ingredients:
            if ingredient.ingredient_name:
                ingredient_id = self._ensure_ingredient(ingredient.ingredient_name, ingredient.ingredient_page or "")
                if ingredient_id:
                    other_ingredient_ids.append(ingredient_id)
        
        # Update product with highlight IDs
        self.conn.execute(
            """
            UPDATE products SET 
                free_tag_ids_json = ?,
                key_ingredient_ids_json = ?,
                other_ingredient_ids_json = ?
            WHERE id = ?
            """,
            (
                json.dumps(free_tag_ids),
                json.dumps(key_ingredient_ids),
                json.dumps(other_ingredient_ids),
                product_id,
            ),
        )
        
        self.conn.commit()

    def _ensure_free_tag(self, tag: str, tooltip: Optional[str]) -> Optional[str]:
        """Ensure free tag exists in database and return its ID."""
        
        # Check if free tag already exists
        row = self.conn.execute(
            "SELECT id FROM frees WHERE tag = ?",
            (tag,),
        ).fetchone()
        
        if row:
            return row["id"]
        
        # Create new free tag
        free_id = self._generate_id()
        self.conn.execute(
            "INSERT INTO frees (id, tag, tooltip) VALUES (?, ?, ?)",
            (free_id, tag, tooltip),
        )
        self.conn.commit()
        return free_id

    def _retrieve_cosing_data(self, ingredient_name: str) -> Optional[CosIngRecord]:
        """Retrieve CosIng data for an ingredient."""
        
        # Check cache first
        cache_key = ingredient_name.lower().strip()
        cached_data = self._cosing_record_cache.get(cache_key)
        if cached_data:
            return cached_data
        
        # Try to fetch from CosIng
        cosing_html = self._fetch_cosing_detail_optimized(ingredient_name)
        if cosing_html:
            cosing_record = self._parse_cosing_page(cosing_html, ingredient_name)
            if cosing_record:
                # Cache the result
                self._cosing_record_cache.put(cache_key, cosing_record)
                return cosing_record
        
        return None

    def _fetch_cosing_detail_optimized(self, ingredient_name: str) -> Optional[str]:
        """Optimized CosIng fetch with smart fallback strategy."""
        
        # Try regular Playwright first (faster)
        try:
            result = self._fetch_cosing_detail_via_playwright(ingredient_name)
            if result:
                return result
        except Exception as exc:
            LOGGER.debug("Regular Playwright failed for %s: %s", ingredient_name, exc)
        
        # Fallback to optimized Playwright
        try:
            result = self._fetch_cosing_detail_via_playwright_optimized(ingredient_name)
            if result:
                return result
        except Exception as exc:
            LOGGER.debug("Optimized Playwright failed for %s: %s", ingredient_name, exc)
        
        return None

    def _fetch_cosing_detail_via_playwright(self, ingredient_name: str) -> Optional[str]:
        """Fetch CosIng data using Playwright with regular timeouts."""
        
        page = self._get_cosing_playwright_page()
        if page is None:
            return None
        
        base_url = f"{COSING_BASE_URL}/index.cfm?fuseaction=search.results&keyword={parse.quote(ingredient_name)}"
        display_name = ingredient_name
        
        try:
            page.goto(base_url, wait_until="domcontentloaded", timeout=8000)  # Reduced from 15000
        except PlaywrightTimeoutError:
            LOGGER.warning(
                "Timed out while loading CosIng search page for %s", display_name
            )
            return None
        
        input_locator = page.locator('input#keyword')
        try:
            input_locator.wait_for(state="visible", timeout=3000)  # Reduced from 5000
        except PlaywrightTimeoutError:
            LOGGER.warning(
                "CosIng search input not visible for %s", display_name
            )
            return None
        
        input_locator.fill(ingredient_name)
        input_locator.press("Enter")
        
        try:
            page.wait_for_load_state("networkidle", timeout=8000)  # Reduced from 15000
        except PlaywrightTimeoutError:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=3000)  # Reduced from 5000
            except PlaywrightTimeoutError:
                LOGGER.debug("CosIng search results did not reach idle state", exc_info=True)
        
        # Look for results table
        table_locator = page.locator('table.ecl-table')
        try:
            table_locator.wait_for(state="visible", timeout=5000)
        except PlaywrightTimeoutError:
            LOGGER.debug(
                "No results table found for %s", display_name
            )
            return None
        
        # Click on first result
        first_row = table_locator.locator('tbody tr').first
        if first_row.count() > 0:
            first_row.click()
            
            try:
                page.wait_for_load_state("networkidle", timeout=8000)  # Reduced from 15000
            except PlaywrightTimeoutError:
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=3000)  # Reduced from 5000
                except PlaywrightTimeoutError:
                    LOGGER.debug("CosIng detail page did not reach idle state", exc_info=True)
            
            return page.content()
        
        return None

    def _fetch_cosing_detail_via_playwright_optimized(self, ingredient_name: str) -> Optional[str]:
        """Optimized CosIng fetch with reduced timeouts."""
        
        page = self._get_cosing_playwright_page()
        if page is None:
            return None
        
        query = ingredient_name.strip()
        if not query:
            return None
        
        # Try direct URL first (faster than search if we know the structure)
        direct_url = self._try_direct_cosing_url(query)
        if direct_url:
            try:
                page.goto(direct_url, wait_until="domcontentloaded", timeout=5000)
                page.wait_for_load_state("networkidle", timeout=3000)
                self._wait_for_cosing_dynamic_content(page)
                html = page.content()
                root = parse_html(html)
                if root.find(class_='ecl-table'):
                    return html
            except Exception:
                pass
        
        # Fallback to search method
        base_url = f"{COSING_BASE_URL}/index.cfm?fuseaction=search.results&keyword={parse.quote(query)}"
        
        try:
            page.goto(base_url, wait_until="domcontentloaded", timeout=5000)
        except PlaywrightTimeoutError:
            return None
        
        input_locator = page.locator('input#keyword')
        try:
            input_locator.wait_for(state="visible", timeout=2000)
        except PlaywrightTimeoutError:
            return None
        
        input_locator.fill(query)
        input_locator.press("Enter")
        
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except PlaywrightTimeoutError:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=2000)
            except PlaywrightTimeoutError:
                pass
        
        # Look for results table
        table_locator = page.locator('table.ecl-table')
        try:
            table_locator.wait_for(state="visible", timeout=3000)
        except PlaywrightTimeoutError:
            return None
        
        # Click on first result
        first_row = table_locator.locator('tbody tr').first
        if first_row.count() > 0:
            first_row.click()
            
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except PlaywrightTimeoutError:
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=2000)
                except PlaywrightTimeoutError:
                    pass
            
            self._wait_for_cosing_dynamic_content(page)
            return page.content()
        
        return None

    def _try_direct_cosing_url(self, ingredient_name: str) -> Optional[str]:
        """Try to construct a direct URL to CosIng ingredient page."""
        
        # CosIng uses a search-based approach, so we'll use the search URL
        # This is a heuristic - CosIng URLs often follow patterns
        normalized = ingredient_name.lower().replace(" ", "-").replace("/", "-")
        return f"{COSING_BASE_URL}/index.cfm?fuseaction=search.results&keyword={parse.quote(normalized)}"

    def _wait_for_cosing_dynamic_content(self, page: Any, *, timeout: int = 8000) -> bool:
        """Wait for dynamic content to load on CosIng page."""
        
        # Try multiple selectors for dynamic content
        selectors = [
            'table.ecl-table',
            'app-detail',
            '.ecl-table',
            '[data-testid="results-table"]',
        ]
        
        # Distribute timeout among selectors
        timeout_per_selector = timeout // len(selectors)
        
        for selector in selectors:
            try:
                page.wait_for_selector(selector, timeout=timeout_per_selector)
                return True
            except PlaywrightTimeoutError:
                continue
        
        return False

    def _parse_cosing_page(self, html: str, ingredient_name: str) -> Optional[CosIngRecord]:
        """Parse CosIng page to extract structured data."""
        
        root = parse_html(html)
        
        # Extract CAS numbers
        cas_numbers = []
        cas_elements = root.find_all(class_='cas-number')
        for elem in cas_elements:
            cas_text = extract_text(elem)
            if cas_text:
                cas_numbers.append(cas_text)
        
        # Extract EC numbers
        ec_numbers = []
        ec_elements = root.find_all(class_='ec-number')
        for elem in ec_elements:
            ec_text = extract_text(elem)
            if ec_text:
                ec_numbers.append(ec_text)
        
        # Extract identified ingredients
        identified_ingredients = []
        ing_elements = root.find_all(class_='identified-ingredient')
        for elem in ing_elements:
            ing_text = extract_text(elem)
            if ing_text:
                identified_ingredients.append(ing_text)
        
        # Extract regulation provisions
        regulation_provisions = []
        reg_elements = root.find_all(class_='regulation-provision')
        for elem in reg_elements:
            reg_text = extract_text(elem)
            if reg_text:
                regulation_provisions.append(reg_text)
        
        # Extract functions
        functions = []
        func_elements = root.find_all(class_='function')
        for elem in func_elements:
            func_text = extract_text(elem)
            if func_text:
                functions.append(func_text)
        
        return CosIngRecord(
            cas_numbers=cas_numbers,
            ec_numbers=ec_numbers,
            identified_ingredients=identified_ingredients,
            regulation_provisions=regulation_provisions,
            functions=functions,
        )

    def _get_cosing_playwright_page(self) -> Optional[Any]:
        """Get or create CosIng Playwright page."""
        
        if sync_playwright is None:
            LOGGER.warning("Playwright not available for CosIng scraping")
            return None
        
        if self._cosing_playwright_failed:
            return None
        
        try:
            if self._cosing_playwright is None:
                self._cosing_playwright = sync_playwright().start()
            
            if self._cosing_browser is None:
                self._cosing_browser = self._cosing_playwright.chromium.launch(
                    headless=True,
                    args=['--no-sandbox', '--disable-dev-shm-usage']
                )
            
            if self._cosing_context is None:
                self._cosing_context = self._cosing_browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
                )
            
            if self._cosing_page is None:
                self._cosing_page = self._cosing_context.new_page()
            
            return self._cosing_page
            
        except Exception as exc:
            LOGGER.error("Failed to initialize CosIng Playwright: %s", exc)
            self._cosing_playwright_failed = True
            return None

    def _shutdown_cosing_resources(self) -> None:
        """Shutdown CosIng Playwright resources."""
        
        try:
            if self._cosing_page:
                self._cosing_page.close()
                self._cosing_page = None
            
            if self._cosing_context:
                self._cosing_context.close()
                self._cosing_context = None
            
            if self._cosing_browser:
                self._cosing_browser.close()
                self._cosing_browser = None
            
            if self._cosing_playwright:
                self._cosing_playwright.stop()
                self._cosing_playwright = None
                
        except Exception as exc:
            LOGGER.debug("Error shutting down CosIng resources: %s", exc)

    def _load_cosing_cache_from_db(self) -> None:
        """Load CosIng cache from SQLite database."""
        
        try:
            cursor = self.conn.execute(
                "SELECT lookup_key, detail_html, source_term FROM cosing_cache"
            )
            
            for row in cursor.fetchall():
                lookup_key = row["lookup_key"]
                detail_html = row["detail_html"]
                source_term = row["source_term"]
                
                if detail_html and source_term:
                    # Parse the cached HTML to create CosIngRecord
                    cosing_record = self._parse_cosing_page(detail_html, source_term)
                    if cosing_record:
                        self._cosing_record_cache.put(lookup_key, cosing_record)
            
            LOGGER.info("Loaded %d CosIng records from cache", len(self._cosing_record_cache))
            
        except Exception as exc:
            LOGGER.error("Failed to load CosIng cache from database: %s", exc)

