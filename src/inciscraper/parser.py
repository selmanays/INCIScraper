"""Lightweight HTML parsing utilities used by the INCIScraper project.

Türkçe: INCIScraper projesinde kullanılan hafif HTML ayrıştırma yardımcıları.

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
        """Attach ``child`` as the last child of the current node.

        Türkçe: Verilen ``child`` düğümünü mevcut düğümün altına ekler.
        """
        child.parent = self
        self.content.append(child)

    def append_text(self, text: str) -> None:
        """Append raw text to the node if the text is not empty.

        Türkçe: Boş olmayan düz metni düğümün içeriğine ekler.
        """
        if text:
            self.content.append(text)

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------
    @property
    def children(self) -> Iterable["Node"]:
        """Iterate over direct child nodes only.

        Türkçe: Düğümün yalnızca alt düğümlerini sırasıyla döndürür.
        """
        for item in self.content:
            if isinstance(item, Node):
                yield item

    @property
    def text_items(self) -> Iterable[str]:
        """Yield the raw text fragments contained in the node.

        Türkçe: Düğüm içerisindeki düz metin parçalarını sırasıyla verir.
        """
        for item in self.content:
            if isinstance(item, str):
                yield item

    def get(self, attr: str, default: Optional[str] = None) -> Optional[str]:
        """Return the value for attribute ``attr`` or ``default`` if missing.

        Türkçe: İstenen niteliğin değerini döndürür; bulunamazsa varsayılanı
        verir.
        """
        return self.attrs.get(attr, default)

    def classes(self) -> List[str]:
        """List all CSS classes defined on the node.

        Türkçe: Düğümde tanımlı tüm CSS sınıflarını listeler.
        """
        return [c for c in self.attrs.get("class", "").split() if c]

    def has_class(self, class_name: str) -> bool:
        """Check whether the node includes ``class_name`` in its class list.

        Türkçe: Düğümün sınıf listesinde verilen sınıfın olup olmadığını kontrol
        eder.
        """
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
        """Determine whether the node matches the given filters.

        Türkçe: Düğümün sağlanan kriterleri karşılayıp karşılamadığını belirler.
        """
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
        """Return a list of nodes matching the provided criteria.

        Türkçe: Verilen koşulları sağlayan tüm düğümleri liste olarak döndürür.
        """
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
        """Return the first node that satisfies the selection criteria.

        Türkçe: Sağlanan kriterlerle eşleşen ilk düğümü döndürür.
        """
        if self._match(tag, class_, id_, attrs, predicate):
            return self
        for child in self.children:
            found = child.find(tag, class_, id_, attrs, predicate)
            if found:
                return found
        return None

    def iter(self, tag: Optional[str] = None) -> Iterator["Node"]:
        """Yield nodes in depth-first order, optionally filtering by tag name.

        Türkçe: Düğüm ağacını derinlik öncelikli dolaşarak isteğe bağlı olarak
        belirli etiket adına göre süzer.
        """
        if tag is None or self.tag == tag:
            yield self
        for child in self.children:
            yield from child.iter(tag)

    def next_siblings(self) -> Iterator["Node"]:
        """Iterate over sibling nodes that appear after the current one.

        Türkçe: Mevcut düğümden sonra gelen kardeş düğümleri sırasıyla verir.
        """
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
        """Iterate over sibling nodes that appear before the current one.

        Türkçe: Mevcut düğümden önce gelen kardeş düğümleri sırasıyla verir.
        """
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
        """Extract the textual content of the node and its descendants.

        Türkçe: Düğüm ve alt düğümlerindeki metin içeriğini toplayıp döndürür.
        """
        parts: List[str] = []

        def walk(node: "Node") -> None:
            """Collect text recursively from ``node``.

            Türkçe: ``node`` düğümündeki metinleri özyinelemeli olarak toplar.
            """
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
        """Return the serialized HTML inside the node.

        Türkçe: Düğümün içindeki HTML içeriğini serileştirip döndürür.
        """
        parts: List[str] = []

        def render(node: "Node") -> None:
            """Serialise ``node`` and its children into HTML.

            Türkçe: ``node`` düğümünü ve alt öğelerini HTML olarak serileştirir.
            """
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
        """Locate a descendant by its ``id`` attribute.

        Türkçe: Alt düğümler arasında verilen ``id`` değerini arar.
        """
        return self.find(id_=element_id)

    def find_all_by_class(self, class_name: str, tag: Optional[str] = None) -> List["Node"]:
        """Return all descendants carrying the requested CSS class.

        Türkçe: İstenen CSS sınıfına sahip tüm alt düğümleri döndürür.
        """
        return [node for node in self.find_all(tag=tag) if node.has_class(class_name)]


class TreeBuilder(HTMLParser):
    """Parses raw HTML into a :class:`Node` tree."""

    def __init__(self) -> None:
        """Initialise the incremental HTML parser state.

        Türkçe: Artımlı HTML ayrıştırıcı durumunu oluşturur.
        """
        super().__init__(convert_charrefs=True)
        self.root = Node("document", {})
        self.stack: List[Node] = [self.root]

    # HTMLParser interface -------------------------------------------------
    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        """Handle an opening tag encountered in the HTML stream.

        Türkçe: Ayrıştırılan HTML akışında görülen açılış etiketini işler.
        """
        parent = self.stack[-1]
        attr_dict = {name: (value or "") for name, value in attrs}
        node = Node(tag, attr_dict, parent)
        parent.append_child(node)
        if tag not in VOID_ELEMENTS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        """Process self-closing tags such as ``<img/>``.

        Türkçe: ``<img/>`` benzeri kendi kendini kapatan etiketleri işler.
        """
        self.handle_starttag(tag, attrs)
        # Ensure self closing tags do not stay on the stack
        if tag not in VOID_ELEMENTS and self.stack and self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        """Close the most recent open tag matching ``tag``.

        Türkçe: Verilen ada sahip son açık etiketi kapatır.
        """
        # Pop until we encounter the requested tag.  The HTML on the site is
        # reasonably well formed, but this defensive approach keeps the parser
        # resilient to minor issues.
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return
        # If we did not find the tag we simply ignore the closing tag.

    def handle_data(self, data: str) -> None:
        """Append text content to the current node.

        Türkçe: Bulunduğu düğüme metin içeriği ekler.
        """
        if data:
            self.stack[-1].append_text(data)

    def error(self, message: str) -> None:  # pragma: no cover - required override
        """Propagate parser errors as :class:`ValueError`.

        Türkçe: Ayrıştırma hatalarını :class:`ValueError` olarak yükseltir.
        """
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

    Türkçe: Verilen ham HTML'yi :class:`Node` ağacına dönüştürerek yapay kök
    düğümünü döndürür.
    """

    builder = TreeBuilder()
    builder.feed(html)
    builder.close()
    return builder.root


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def normalize_whitespace(value: str) -> str:
    """Collapse runs of whitespace into single spaces.

    Türkçe: Birden fazla boşluk karakterini tek boşluğa indirger.
    """
    return " ".join(value.split())


def extract_text(node: Optional[Node]) -> str:
    """Return the text content of ``node`` or an empty string.

    Türkçe: Düğümün metin içeriğini döndürür; yoksa boş string verir.
    """
    return node.get_text(strip=True) if node else ""


def find_first(node: Node, predicate: Callable[[Node], bool]) -> Optional[Node]:
    """Find the first descendant that satisfies ``predicate``.

    Türkçe: Koşulu sağlayan ilk alt düğümü bulup döndürür.
    """
    for child in node.find_all():
        if predicate(child):
            return child
    return None


def iter_descendants(node: Node) -> Iterator[Node]:
    """Yield all descendant nodes in document order.

    Türkçe: Düğümün tüm alt düğümlerini belge sırasıyla üretir.
    """
    for child in node.children:
        yield child
        yield from iter_descendants(child)
