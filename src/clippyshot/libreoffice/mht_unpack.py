"""Unpack MHT/MHTML archives so LibreOffice's HTML importer can render them.

LO's MHTML importer mishandles Word-flavored MHTs — the common output
from ``Save As > Web Page, Filtered`` — by dumping the raw MIME envelope
as body text. Every ``--infilter`` variant we tried (``MS MHTML``,
``writer_MS_MHTML_File``, ``HTML (StarWriter)``) reproduced the same
broken output, and renaming the file to ``.html`` didn't help either.

What works: extract the inner ``text/html`` part with Python's stdlib
``email`` module, write sibling parts (images, CSS) next to it, rewrite
``cid:`` and Content-Location references to point at the local copies,
and hand LO the resulting ``document.html``. LO's HTML importer is
dramatically more reliable than its MHTML importer, and the output is
visually close to what Word itself produces for the same file.

Security: ``email`` parses MIME exactly the way mail clients do. Every
written filename passes through ``_safe_filename`` to strip drive
letters, path separators, and non-allowlisted characters — so a MHT
declaring ``Content-Location: ../../etc/passwd`` can only ever land as
``etc_passwd`` in the output directory we chose.
"""

from __future__ import annotations

import email
import hashlib
import html.parser
import re
from pathlib import Path


_CID_RE = re.compile(r"cid:([^\"'>)\s]+)", re.IGNORECASE)
_URL_ATTR_RE = re.compile(
    r"(?P<prefix>\b(?:src|href|background)\s*=\s*)(?P<quote>[\"'])(?P<value>.*?)(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")

_STYLE_BACKGROUND_RE = re.compile(r"background(?:-color)?\s*:\s*([^;]+)", re.IGNORECASE)
_STYLE_COLOR_RE = re.compile(r"(?<!-)color\s*:\s*([^;]+)", re.IGNORECASE)
_STYLE_TEXT_ALIGN_RE = re.compile(
    r"text-align\s*:\s*(center|left|right|justify)", re.IGNORECASE
)
_STYLE_FONT_WEIGHT_RE = re.compile(r"font-weight\s*:\s*([^;]+)", re.IGNORECASE)
_STYLE_FONT_FAMILY_RE = re.compile(r"font-family\s*:\s*([^;]+)", re.IGNORECASE)
_STYLE_FONT_SIZE_RE = re.compile(r"font-size\s*:\s*([^;]+)", re.IGNORECASE)
_STYLE_TEXT_DECO_RE = re.compile(r"text-decoration\s*:\s*([^;]+)", re.IGNORECASE)


# HTML4 ``<font size>`` is a 1–7 enum, not a pt/px number. Map CSS sizes
# (parsed as pt) to the closest ``<font size>`` rung using the classic
# HTML4 defaults. Unknown units (em, %) get dropped rather than guessed.
def _pt_to_font_size(value: str) -> str | None:
    v = value.strip().lower()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(pt|px|pc|em|rem|%)?$", v)
    if not m:
        return None
    num = float(m.group(1))
    unit = (m.group(2) or "pt").lower()
    if unit == "px":
        pt = num * 0.75
    elif unit == "pc":
        pt = num * 12
    elif unit == "pt":
        pt = num
    else:
        return None  # em / rem / % need a parent font size to resolve
    if pt < 9:
        return "1"
    if pt < 11:
        return "2"
    if pt < 13.5:
        return "3"
    if pt < 16.5:
        return "4"
    if pt < 21:
        return "5"
    if pt < 30:
        return "6"
    return "7"


_SHORTHAND_HEX_RE = re.compile(r"^#([0-9a-fA-F])([0-9a-fA-F])([0-9a-fA-F])$")


def _css_value_to_attr(value: str) -> str:
    """Strip quotes / extra whitespace from a CSS value for use as an attribute."""
    v = value.strip().strip('"').strip("'")
    # ``<font color="#fff">`` is ignored by LO's HTML importer — it
    # wants the 6-digit form. Expand shorthand before we write it out.
    m = _SHORTHAND_HEX_RE.match(v)
    if m:
        return "#" + "".join(c * 2 for c in m.groups())
    return v


_ALIGN_CONTAINER_TAGS: frozenset[str] = frozenset(
    {"table", "tr", "td", "th", "div", "p", "span"}
)
_ALIGN_APPLY_TAGS: frozenset[str] = frozenset({"td", "th", "p", "div"})
_BGCOLOR_APPLY_TAGS: frozenset[str] = frozenset({"table", "td", "th", "tr"})
# Leaf containers whose text we'll wrap in ``<font color=X>`` / ``<b>``
# / ``<u>`` when the corresponding CSS property was set on the element
# or an ancestor. ``<table>`` isn't in here because LO ignores decorators
# that aren't adjacent to text content.
_COLOR_APPLY_TAGS: frozenset[str] = frozenset({"td", "th", "div", "p", "span"})


class _LegacyAttrInjector(html.parser.HTMLParser):
    """Inject HTML4 ``bgcolor``/``align`` attrs derived from inline CSS.

    LibreOffice's HTML importer ignores most inline CSS on table-family
    elements but honors the corresponding HTML4 attributes. Doing this
    with a streaming regex misses two important cases:

    1. ``text-align`` cascades through CSS inheritance — a ``<table style=
       "text-align:center">`` centers content in every child ``<td>``,
       but the regex only saw the ``<table>`` tag itself. Parsing the
       tree means we can propagate the alignment to every descendant
       cell and paragraph.
    2. Attribute quoting / ordering / casing varies in real MHT output
       (Word, OWA, Outlook web); the parser normalises that so we don't
       leave half-transformed markup.

    We only emit tags we care about with modified attributes — every other
    tag, text, comment, and the decl passes through verbatim, so the DOM
    stays structurally identical. ``style=`` is preserved, so anything
    LO *does* honor still applies.
    """

    def __init__(self, class_styles: dict[str, dict[str, str]] | None = None) -> None:
        super().__init__(convert_charrefs=False)
        self.out: list[str] = []
        # Class/tag selector → property map, resolved from ``<style>``
        # blocks via ``_collect_class_styles``. Inline style still wins.
        self._class_styles: dict[str, dict[str, str]] = class_styles or {}
        # One entry per open container tag for each inheritable property.
        # Parallel stacks keep pop() cheap and avoid nested dict work on
        # every close tag.
        self._align_stack: list[str | None] = []
        self._color_stack: list[str | None] = []
        self._family_stack: list[str | None] = []
        self._size_stack: list[str | None] = []
        self._weight_stack: list[str | None] = []
        self._deco_stack: list[str | None] = []
        # Inline wrapper counts per open container, in the order they
        # were emitted. On close we emit matching ``</tag>`` in reverse.
        self._wrappers_stack: list[list[str]] = []
        # Whether we're currently inside a <style> block (we suppress
        # those since LO doesn't apply them anyway).
        self._in_style_block = False

    def handle_decl(self, decl: str) -> None:
        self.out.append(f"<!{decl}>")

    def handle_comment(self, data: str) -> None:
        self.out.append(f"<!--{data}-->")

    def handle_data(self, data: str) -> None:
        # ``<style>`` contents get class-selector-inlined during a
        # prepass; we strip the block itself here so LO doesn't trip
        # over CSS it will ignore anyway.
        if self._in_style_block:
            return
        self.out.append(data)

    def handle_entityref(self, name: str) -> None:
        self.out.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.out.append(f"&#{name};")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # Self-closing (XHTML-style) — same logic as start, no stack push.
        self.out.append(self._format_tag(tag, attrs, self_closing=True))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "style":
            self._in_style_block = True
            return
        formatted, effective = self._augment(tag, attrs)
        self.out.append(formatted)

        # Emit HTML4 wrappers in a stable order so close tags nest
        # correctly. Order: color → family/size → weight → decoration.
        # Each wrapper is only emitted when the corresponding CSS
        # property has a value, otherwise LO would render nothing new.
        wrappers: list[str] = []
        if tag in _COLOR_APPLY_TAGS:
            color = effective["color"]
            face = effective["family"]
            size = effective["size"]
            if color or face or size:
                font_attrs: list[str] = []
                if color:
                    font_attrs.append(f' color="{color}"')
                if face:
                    font_attrs.append(f' face="{face}"')
                if size:
                    font_attrs.append(f' size="{size}"')
                self.out.append(f"<font{''.join(font_attrs)}>")
                wrappers.append("font")
            if effective["weight"] == "bold":
                self.out.append("<b>")
                wrappers.append("b")
            if effective["decoration"] == "underline":
                self.out.append("<u>")
                wrappers.append("u")

        if tag in _ALIGN_CONTAINER_TAGS:
            self._align_stack.append(effective["align"])
            self._color_stack.append(effective["color"])
            self._family_stack.append(effective["family"])
            self._size_stack.append(effective["size"])
            self._weight_stack.append(effective["weight"])
            self._deco_stack.append(effective["decoration"])
            self._wrappers_stack.append(wrappers)

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self._in_style_block = False
            return
        if tag in _ALIGN_CONTAINER_TAGS and self._align_stack:
            self._align_stack.pop()
            self._color_stack.pop()
            self._family_stack.pop()
            self._size_stack.pop()
            self._weight_stack.pop()
            self._deco_stack.pop()
            wrappers = self._wrappers_stack.pop() if self._wrappers_stack else []
            for closing in reversed(wrappers):
                self.out.append(f"</{closing}>")
        self.out.append(f"</{tag}>")

    def _augment(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> tuple[str, dict[str, str | None]]:
        attrs_d: dict[str, str] = {k: (v if v is not None else "") for k, v in attrs}

        # Resolve the "effective" inline style by merging class-selector
        # rules under inline (inline wins). Tag selector rules from the
        # stylesheet apply first, then class, then inline.
        combined_style = self._resolve_style(tag, attrs_d)

        own_props = _extract_inherited_props(combined_style)

        # Everything inheritable falls back to the ancestor value.
        inherited = {
            "align": self._align_stack[-1] if self._align_stack else None,
            "color": self._color_stack[-1] if self._color_stack else None,
            "family": self._family_stack[-1] if self._family_stack else None,
            "size": self._size_stack[-1] if self._size_stack else None,
            "weight": self._weight_stack[-1] if self._weight_stack else None,
            "decoration": self._deco_stack[-1] if self._deco_stack else None,
        }
        effective = {k: own_props.get(k) or inherited[k] for k in inherited}

        # Background: bgcolor maps 1:1 to HTML4 attr, no inheritance.
        if combined_style and tag in _BGCOLOR_APPLY_TAGS and "bgcolor" not in attrs_d:
            bg = _STYLE_BACKGROUND_RE.search(combined_style)
            if bg:
                attrs_d["bgcolor"] = _css_value_to_attr(bg.group(1))

        # Alignment: propagate onto child cell/paragraph tags.
        if effective["align"] and tag in _ALIGN_APPLY_TAGS and "align" not in attrs_d:
            attrs_d["align"] = effective["align"]

        # Surface the resolved style back onto the element so any future
        # pass (or LO, for properties it does honor) still sees a
        # consistent style attribute.
        if combined_style and combined_style != attrs_d.get("style"):
            attrs_d["style"] = combined_style

        # Rebuild the tag. Quote values uniformly with double quotes; an
        # inner ``"`` gets HTML-entity encoded.
        parts = [f"<{tag}"]
        for name, value in attrs_d.items():
            if value == "":
                parts.append(f" {name}")
            else:
                safe = value.replace('"', "&quot;")
                parts.append(f' {name}="{safe}"')
        parts.append(">")
        return "".join(parts), effective

    def _resolve_style(self, tag: str, attrs_d: dict[str, str]) -> str:
        """Merge stylesheet class/tag rules with the inline style.

        Order of precedence (lowest to highest): tag selector → class
        selector → inline ``style=""``. The inline style always wins so
        author intent on the element takes priority over the
        ``<style>`` block.
        """
        inline = attrs_d.get("style", "")
        if not self._class_styles:
            return inline
        # Tag selector rules (e.g. ``p { color:#333 }``).
        base = dict(self._class_styles.get(tag, {}))
        # Class selectors (``.MsoNormal``, ``tag.MsoNormal``).
        cls_attr = attrs_d.get("class", "")
        if cls_attr:
            for cls in cls_attr.split():
                base.update(self._class_styles.get(f".{cls}", {}))
                base.update(self._class_styles.get(f"{tag}.{cls}", {}))
        if not base and not inline:
            return ""
        # Inline declarations override base.
        if inline:
            base.update(_parse_inline_decls(inline))
        # Reserialize in a predictable order so downstream regex match
        # the values we stored.
        return "; ".join(f"{k}:{v}" for k, v in base.items())

    def _format_tag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
        *,
        self_closing: bool,
    ) -> str:
        formatted, _ = self._augment(tag, attrs)
        if self_closing:
            # Convert the trailing ">" to " />" to stay XHTML-compliant.
            return formatted[:-1] + " />"
        return formatted


def _parse_inline_decls(style: str) -> dict[str, str]:
    """Split an inline ``style="..."`` declaration block into a dict.

    CSS values may contain quoted commas (``font-family:"Arial, Helvetica"``)
    so we split on semicolons outside of quotes. Empty declarations and
    values are skipped; property names are lowercased for lookup.
    """
    out: dict[str, str] = {}
    for decl in _split_outside_quotes(style, ";"):
        if ":" not in decl:
            continue
        name, _, value = decl.partition(":")
        name = name.strip().lower()
        value = value.strip()
        if name and value:
            out[name] = value
    return out


def _split_outside_quotes(value: str, sep: str) -> list[str]:
    out: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for ch in value:
        if quote:
            if ch == quote:
                quote = None
            buf.append(ch)
        elif ch in ('"', "'"):
            quote = ch
            buf.append(ch)
        elif ch == sep:
            out.append("".join(buf))
            buf.clear()
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def _extract_inherited_props(style: str) -> dict[str, str | None]:
    """Pull the CSS properties we propagate out of a serialized style."""
    props: dict[str, str | None] = {
        "align": None,
        "color": None,
        "family": None,
        "size": None,
        "weight": None,
        "decoration": None,
    }
    if not style:
        return props
    m = _STYLE_TEXT_ALIGN_RE.search(style)
    if m:
        props["align"] = _css_value_to_attr(m.group(1)).lower()
    m = _STYLE_COLOR_RE.search(style)
    if m:
        props["color"] = _css_value_to_attr(m.group(1))
    m = _STYLE_FONT_FAMILY_RE.search(style)
    if m:
        # font-family values are a comma-separated stack — LO's
        # ``<font face>`` takes a single name, so pick the first.
        first = _split_outside_quotes(m.group(1), ",")[0]
        props["family"] = _css_value_to_attr(first)
    m = _STYLE_FONT_SIZE_RE.search(style)
    if m:
        props["size"] = _pt_to_font_size(m.group(1))
    m = _STYLE_FONT_WEIGHT_RE.search(style)
    if m:
        w = _css_value_to_attr(m.group(1)).lower()
        # CSS ``font-weight`` accepts keywords (bold / normal) or
        # numerics (100-900). Everything >= 600 renders as bold in
        # practice; ``bolder`` is relative to parent but real input
        # almost always sets an absolute value alongside.
        if w == "bold" or w == "bolder":
            props["weight"] = "bold"
        elif w.isdigit() and int(w) >= 600:
            props["weight"] = "bold"
        elif w in ("normal", "lighter") or (w.isdigit() and int(w) < 600):
            props["weight"] = "normal"
    m = _STYLE_TEXT_DECO_RE.search(style)
    if m:
        d = _css_value_to_attr(m.group(1)).lower()
        if "underline" in d:
            props["decoration"] = "underline"
        elif "none" in d:
            props["decoration"] = "none"
    return props


# Matches a single CSS selector with optional tag and class (plus whatever
# trailing combinators / pseudo-classes we'll intentionally ignore).
_CSS_RULE_RE = re.compile(
    r"([^{}]+)\{([^{}]*)\}",
    re.DOTALL,
)
_SELECTOR_SPLIT_RE = re.compile(r"\s*,\s*")
# Accept plain tag (``p``), class (``.MsoNormal``), and tag.class
# (``p.MsoNormal``) selectors. Pseudo-classes / descendants get
# dropped since we can't meaningfully apply them without a full CSS
# engine.
_SIMPLE_SELECTOR_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9]*)?(?:\.([A-Za-z_][\w-]*))?$")


def _collect_class_styles(html_text: str) -> dict[str, dict[str, str]]:
    """Parse ``<style>`` blocks and return a selector → property map.

    Handles the subset Word and email-marketing tooling actually emit:
    plain tag selectors (``p``), class selectors (``.MsoNormal``), and
    tag.class selectors (``p.MsoNormal``), plus comma-separated lists
    of any of those. Pseudo-classes, combinators, and at-rules are
    silently skipped — they need a real CSS engine to apply correctly,
    and in Word-origin files the content we care about is in the
    simple selectors.
    """
    out: dict[str, dict[str, str]] = {}
    for m in re.finditer(
        r"<style[^>]*>(.*?)</style>", html_text, re.DOTALL | re.IGNORECASE
    ):
        css = m.group(1)
        # Strip CSS comments (``/* ... */``) once so they don't
        # interfere with the rule regex.
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
        for rule in _CSS_RULE_RE.finditer(css):
            selectors = _SELECTOR_SPLIT_RE.split(rule.group(1).strip())
            decls = _parse_inline_decls(rule.group(2))
            if not decls:
                continue
            for sel in selectors:
                sel = sel.strip()
                if not sel or sel.startswith("@"):
                    continue
                sm = _SIMPLE_SELECTOR_RE.match(sel)
                if not sm:
                    continue
                tag = (sm.group(1) or "").lower()
                cls = sm.group(2)
                if tag and cls:
                    key = f"{tag}.{cls}"
                elif cls:
                    key = f".{cls}"
                elif tag:
                    key = tag
                else:
                    continue
                existing = out.setdefault(key, {})
                existing.update(decls)
    return out


def _boost_legacy_attrs(html_text: str) -> str:
    """Translate CSS that LO's HTML importer ignores into HTML4 attrs.

    Two-stage pipeline:

    1. **Style-block inlining**: parse ``<style>`` blocks once, extract
       tag + class selector rules, and resolve them as an additional
       layer under each element's inline ``style=""`` attribute. Word-
       origin MHTs put the bulk of their formatting in ``<style>`` so
       without this step the rendered output uses Writer's defaults.

    2. **Cascade + wrap**: walk the DOM, propagate inherited CSS
       (``text-align``, ``color``, ``font-family``/``size``/``weight``,
       ``text-decoration``) through the ancestor chain, and emit
       legacy HTML4 wrappers (``<font face size color>``, ``<b>``,
       ``<u>``) plus attributes (``bgcolor=``, ``align=``) on the
       leaf-container tags LO's HTML importer does honor.

    Inline ``style=""`` always wins over stylesheet rules so explicit
    author intent on a specific element keeps priority.
    """
    class_styles = _collect_class_styles(html_text)
    parser = _LegacyAttrInjector(class_styles=class_styles)
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:
        return html_text
    return "".join(parser.out)


def _rewrite_html_resource_urls(html_text: str, url_map: dict[str, str]) -> str:
    """Rewrite URL-bearing attribute values to extracted local filenames.

    Restrict rewriting to quoted ``src=`` / ``href=`` / ``background=``
    attributes so plain text content and unrelated CSS strings are not
    accidentally mutated by basename aliases.
    """

    if not url_map:
        return html_text
    sorted_keys = [key for key in sorted(url_map, key=len, reverse=True) if key]

    def repl(match: re.Match) -> str:
        value = match.group("value")
        rewritten = value
        for key in sorted_keys:
            if key in rewritten:
                rewritten = rewritten.replace(key, url_map[key])
        return (
            f"{match.group('prefix')}{match.group('quote')}"
            f"{rewritten}{match.group('quote')}"
        )

    return _URL_ATTR_RE.sub(repl, html_text)


def _safe_filename(name: str, fallback: str) -> str:
    """Reduce an MHT-supplied name to a path-traversal-safe basename."""
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = _UNSAFE_NAME_CHARS.sub("_", name).strip("._")
    return name or fallback


def unpack_mht(mht_path: Path, out_dir: Path) -> Path | None:
    """Extract an MHT into ``out_dir``; return the root HTML path or None.

    Resource parts (images, CSS, etc.) land next to the HTML with
    sanitized names; ``cid:`` and absolute-URL references in the HTML
    are rewritten in-place to the local filenames.

    Returns None on any parse failure — callers should fall back to
    feeding the raw file to LO (which produces a crummy but non-fatal
    text dump instead of an exception).
    """
    try:
        data = mht_path.read_bytes()
    except OSError:
        return None
    try:
        msg = email.message_from_bytes(data)
    except Exception:
        return None

    html_part = None
    # Two separate indexes so case handling is explicit:
    #   cid_map:  lowercased Content-ID → local filename (RFC 2392 says
    #             cid: references are case-insensitive).
    #   url_map:  original-case Content-Location → local filename.
    #             We substitute these into the HTML via html.replace(),
    #             and the HTML keeps the URL as Word wrote it (mixed
    #             case, e.g. ``file:///C:/fake/image0.png``). Lowercasing
    #             the key previously made the replace a silent no-op.
    cid_map: dict[str, str] = {}
    url_map: dict[str, str] = {}
    used_names: set[str] = set()

    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = (part.get_content_type() or "").lower()
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            continue
        if not payload:
            continue
        # First text/html wins: Word always emits exactly one, and edge
        # cases (forwarded chains, etc.) should render the top document.
        if ctype == "text/html" and html_part is None:
            html_part = part
            continue
        cid = (part.get("Content-ID") or "").strip().strip("<>")
        cloc = (part.get("Content-Location") or "").strip()
        fallback = f"part-{hashlib.sha1(payload).hexdigest()[:10]}.bin"
        base = _safe_filename(cloc, fallback)
        local_name = base
        suffix = 1
        while local_name in used_names:
            local_name = f"{base}_{suffix}"
            suffix += 1
        used_names.add(local_name)
        try:
            (out_dir / local_name).write_bytes(payload)
        except OSError:
            continue
        if cid:
            cid_map[cid.lower()] = local_name
        if cloc:
            url_map[cloc] = local_name
            # Also index the basename so ``<img src="image0.png">`` hits
            # when Word writes relative refs instead of the full URL.
            cloc_base = _safe_filename(cloc, "")
            if cloc_base and cloc_base != cloc:
                url_map[cloc_base] = local_name

    if html_part is None:
        return None

    charset = html_part.get_content_charset() or "utf-8"
    try:
        html = html_part.get_payload(decode=True).decode(charset, errors="replace")
    except Exception:
        return None

    def rewrite_cid(m: re.Match) -> str:
        key = m.group(1).strip("<>").lower()
        return cid_map.get(key, m.group(0))

    html = _CID_RE.sub(rewrite_cid, html)

    html = _rewrite_html_resource_urls(html, url_map)

    # Port inline CSS LO ignores (bgcolor, text-align) to HTML4 attrs
    # that LO honors. Improves fidelity for email-marketing-style HTML
    # (the kind Word emits for "Save As Web Page, Filtered") without
    # changing correctness — the original style= attribute is kept.
    html = _boost_legacy_attrs(html)

    html_path = out_dir / "document.html"
    try:
        html_path.write_text(html, encoding="utf-8")
    except OSError:
        return None
    return html_path
