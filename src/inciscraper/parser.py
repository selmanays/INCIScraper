"""Lightweight HTML parsing utilities used by the INCIScraper project.

The execution environment for these kata style exercises typically does not
allow fetching external dependencies such as BeautifulSoup.  To keep the
scraper implementation reasonably ergonomic while staying dependency free,
this module implements a tiny DOM builder on top of :mod:`html.parser` and a
few helper functions that make traversing the parsed tree pleasant enough for
our scraping needs.

The goal is not to be a perfect CSS selector implementation.  Only a small
subset of helpers required by the scraper are provided.  The helpers focus on
searching by tag name, CSS class, element id and arbitrary predicate
functions.  The interface purposely mirrors a subset of BeautifulSoup's API
(e.g. ``find``/``find_all`` and ``get_text``) to keep the scraper code easy to
read.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from html import escape, unescape
from html.parser import HTMLParser
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Tuple, Union


VOID_ELEMENTS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


ContentItem = Union["Node", str]


@dataclass
class Node:
    """Represents an HTML element.

    Attributes
    ----------
    tag:
        Tag name (``document`` for the artificial root node).
    attrs:
        Mapping of attribute names to their values.
    parent:
        Parent node or ``None`` for the artificial root.
    content:
        Ordered list of either child :class:`Node` objects or raw text.
    """

    tag: str
    attrs: Dict[str, str]
    parent: Optional["Node"] = None
    content: List[ContentItem] = field(default_factory=list)

    def append_child(self, child: "Node") -> None:
        child.parent = self
        self.content.append(child)

    def append_text(self, text: str) -> None:
        if text:
            self.content.append(text)

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------
    @property
    def children(self) -> Iterable["Node"]:
        for item in self.content:
            if isinstance(item, Node):
                yield item

    @property
    def text_items(self) -> Iterable[str]:
        for item in self.content:
            if isinstance(item, str):
                yield item

    def get(self, attr: str, default: Optional[str] = None) -> Optional[str]:
        return self.attrs.get(attr, default)

    def classes(self) -> List[str]:
        return [c for c in self.attrs.get("class", "").split() if c]

    def has_class(self, class_name: str) -> bool:
        return class_name in self.classes()

    # ------------------------------------------------------------------
    # Traversal helpers
    # ------------------------------------------------------------------
    def _match(
        self,
        tag: Optional[str] = None,
        class_: Optional[Union[str, Iterable[str]]] = None,
        id_: Optional[str] = None,
        attrs: Optional[Dict[str, str]] = None,
        predicate: Optional[Callable[["Node"], bool]] = None,
    ) -> bool:
        if tag and self.tag != tag:
            return False
        if id_ and self.attrs.get("id") != id_:
            return False
        if class_:
            required = (
                [class_]
                if isinstance(class_, str)
                else list(class_)
            )
            classes = set(self.classes())
            if any(req not in classes for req in required):
                return False
        if attrs:
            for key, value in attrs.items():
                if self.attrs.get(key) != value:
                    return False
        if predicate and not predicate(self):
            return False
        return True

    def find_all(
        self,
        tag: Optional[str] = None,
        class_: Optional[Union[str, Iterable[str]]] = None,
        id_: Optional[str] = None,
        attrs: Optional[Dict[str, str]] = None,
        predicate: Optional[Callable[["Node"], bool]] = None,
    ) -> List["Node"]:
        matches: List[Node] = []
        if self._match(tag, class_, id_, attrs, predicate):
            matches.append(self)
        for child in self.children:
            matches.extend(child.find_all(tag, class_, id_, attrs, predicate))
        return matches

    def find(
        self,
        tag: Optional[str] = None,
        class_: Optional[Union[str, Iterable[str]]] = None,
        id_: Optional[str] = None,
        attrs: Optional[Dict[str, str]] = None,
        predicate: Optional[Callable[["Node"], bool]] = None,
    ) -> Optional["Node"]:
        if self._match(tag, class_, id_, attrs, predicate):
            return self
        for child in self.children:
            found = child.find(tag, class_, id_, attrs, predicate)
            if found:
                return found
        return None

    def iter(self, tag: Optional[str] = None) -> Iterator["Node"]:
        if tag is None or self.tag == tag:
            yield self
        for child in self.children:
            yield from child.iter(tag)

    def next_siblings(self) -> Iterator["Node"]:
        if not self.parent:
            return iter(())
        found_self = False
        for item in self.parent.content:
            if item is self:
                found_self = True
                continue
            if not found_self:
                continue
            if isinstance(item, Node):
                yield item

    def previous_siblings(self) -> Iterator["Node"]:
        if not self.parent:
            return iter(())
        for item in reversed(self.parent.content):
            if item is self:
                break
            if isinstance(item, Node):
                yield item

    # ------------------------------------------------------------------
    # Data extraction
    # ------------------------------------------------------------------
    def get_text(self, strip: bool = True, separator: str = " ") -> str:
        parts: List[str] = []

        def walk(node: "Node") -> None:
            for item in node.content:
                if isinstance(item, str):
                    parts.append(unescape(item))
                else:
                    walk(item)

        walk(self)
        text = separator.join(part for part in parts if part)
        if strip:
            return " ".join(text.split())
        return text

    def get_inner_html(self) -> str:
        parts: List[str] = []

        def render(node: "Node") -> None:
            for item in node.content:
                if isinstance(item, str):
                    parts.append(escape(item, quote=False))
                else:
                    attrs = "".join(
                        f' {key}="{escape(value, quote=True)}"'
                        for key, value in item.attrs.items()
                    )
                    if item.tag in VOID_ELEMENTS:
                        parts.append(f"<{item.tag}{attrs}>")
                        continue
                    parts.append(f"<{item.tag}{attrs}>")
                    render(item)
                    parts.append(f"</{item.tag}>")

        render(self)
        return "".join(parts)

    # ------------------------------------------------------------------
    # Utility helpers used by higher level scraping logic
    # ------------------------------------------------------------------
    def find_by_id(self, element_id: str) -> Optional["Node"]:
        return self.find(id_=element_id)

    def find_all_by_class(self, class_name: str, tag: Optional[str] = None) -> List["Node"]:
        return [node for node in self.find_all(tag=tag) if node.has_class(class_name)]


class TreeBuilder(HTMLParser):
    """Parses raw HTML into a :class:`Node` tree."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("document", {})
        self.stack: List[Node] = [self.root]

    # HTMLParser interface -------------------------------------------------
    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        parent = self.stack[-1]
        attr_dict = {name: (value or "") for name, value in attrs}
        node = Node(tag, attr_dict, parent)
        parent.append_child(node)
        if tag not in VOID_ELEMENTS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        self.handle_starttag(tag, attrs)
        # Ensure self closing tags do not stay on the stack
        if tag not in VOID_ELEMENTS and self.stack and self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        # Pop until we encounter the requested tag.  The HTML on the site is
        # reasonably well formed, but this defensive approach keeps the parser
        # resilient to minor issues.
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return
        # If we did not find the tag we simply ignore the closing tag.

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1].append_text(data)

    def error(self, message: str) -> None:  # pragma: no cover - required override
        raise ValueError(message)


def parse_html(html: str) -> Node:
    """Parse *html* into a :class:`Node` tree.

    Parameters
    ----------
    html:
        Raw HTML document.

    Returns
    -------
    Node
        Artificial root node that contains the full parsed tree as children.
    """

    builder = TreeBuilder()
    builder.feed(html)
    builder.close()
    return builder.root


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def normalize_whitespace(value: str) -> str:
    return " ".join(value.split())


def extract_text(node: Optional[Node]) -> str:
    return node.get_text(strip=True) if node else ""


def find_first(node: Node, predicate: Callable[[Node], bool]) -> Optional[Node]:
    for child in node.find_all():
        if predicate(child):
            return child
    return None


def iter_descendants(node: Node) -> Iterator[Node]:
    for child in node.children:
        yield child
        yield from iter_descendants(child)
