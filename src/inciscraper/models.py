"""Dataclasses that model structured INCIDecoder data."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class IngredientReference:
    """Reference to an ingredient mentioned within a product listing."""

    name: str
    url: str
    tooltip_text: Optional[str]
    tooltip_ingredient_link: Optional[str]
    ingredient_id: Optional[str] = None


@dataclass
class IngredientFunction:
    """Function metadata extracted for an ingredient."""

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
    """Represents a highlighted ingredient and optional function link."""

    function_name: Optional[str]
    function_link: Optional[str]
    ingredient_name: Optional[str]
    ingredient_page: Optional[str]


@dataclass
class FreeTag:
    """A hashtag style marketing claim with an optional tooltip."""

    tag: str
    tooltip: Optional[str]


@dataclass
class ProductHighlights:
    """Container for hashtag and ingredient highlight sections."""

    free_tags: List[FreeTag]
    key_ingredients: List[HighlightEntry]
    other_ingredients: List[HighlightEntry]


@dataclass
class ProductDetails:
    """Structured representation of all parsed product details."""

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
    """Normalized information fetched from an ingredient page."""

    name: str
    url: str
    rating_tag: str
    also_called: List[str]
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
    """Describes a single cosmetic function entry."""

    name: str
    url: Optional[str] = None
    description: str = ""

