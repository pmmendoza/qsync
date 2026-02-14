"""Restricted Markdown/HTML conversions used by qsync workbooks."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

MARKDOWN_SAFE_TAGS = {"p", "br", "strong", "b", "em", "i", "ul", "ol", "li"}
MARKDOWN_SAFE_VOID_TAGS = {"br"}


def normalize_text(value: str) -> str:
    """Normalize whitespace and line endings for stable comparisons."""

    if value is None:
        return ""
    text = str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Strip trailing whitespace on each line
    lines = [ln.rstrip() for ln in text.split("\n")]
    return "\n".join(lines).strip()


def should_treat_as_html(html_str: str) -> bool:
    """Heuristic: decide whether content is 'complex' HTML that we should not simplify.

    We treat content as HTML when we see tags that typically carry attributes
    or structure (links, lists, spans, etc.). Pure text or text with only `<br>`
    can stay in Markdown mode.
    """

    if "<" not in html_str or ">" not in html_str:
        return False

    lower = html_str.lower()
    # Tags/attributes that usually carry brittle structure or attributes
    # that we do not want to round-trip through Markdown.
    complex_markers = [
        "<a ",
        "<span",
        "<table",
        "<div",
        "onclick=",
        "data-",
    ]
    if any(marker in lower for marker in complex_markers):
        return True

    # If it's only <br> tags, keep as Markdown
    stripped = re.sub(r"<br\s*/?>", "", lower)
    # Remove &nbsp; for the check
    stripped = stripped.replace("&nbsp;", "").strip()
    return "<" in stripped and ">" in stripped


class _HTMLToMarkdown(HTMLParser):
    """Very small HTML → Markdown converter for our restricted subset."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.in_li = False
        # Track emphasis markers so we can drop empty tags like `<strong> </strong>`
        # (which otherwise produce phantom `****` diffs).
        self._emphasis_stack: list[dict[str, object]] = []
        # Used to avoid emitting an extra newline for `<br>` immediately after `</ul>`
        # because our `</li>` handler already ends the list with a newline.
        self._just_closed_list = False

    def _push_emphasis(self, marker: str) -> None:
        self.parts.append(marker)
        self._emphasis_stack.append(
            {
                "marker": marker,
                "open_idx": len(self.parts) - 1,
                "had_content": False,
            }
        )

    def _close_emphasis(self, marker: str) -> None:
        close_idx = len(self.parts)
        self.parts.append(marker)
        if not self._emphasis_stack:
            return
        entry = self._emphasis_stack.pop()
        if entry.get("marker") != marker:
            # Malformed markup; keep output rather than trying to repair.
            return
        if not bool(entry.get("had_content")):
            open_idx = entry.get("open_idx")
            if isinstance(open_idx, int) and 0 <= open_idx < len(self.parts):
                self.parts[open_idx] = ""
            if 0 <= close_idx < len(self.parts):
                self.parts[close_idx] = ""

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "br":
            # Avoid turning `</ul><br>` into a blank line: our list items already end
            # with a newline from `</li>`.
            if not (self._just_closed_list and self.parts and self.parts[-1].endswith("\n")):
                self.parts.append("\n")
            self._just_closed_list = False
        elif tag in {"p", "div"}:
            self._just_closed_list = False
            # Paragraphs become blank-line separated
            if self.parts and not self.parts[-1].endswith("\n\n"):
                self.parts.append("\n\n")
        elif tag == "li":
            self._just_closed_list = False
            self.in_li = True
            # Ensure list items start on a new line
            if self.parts and not self.parts[-1].endswith("\n"):
                self.parts.append("\n")
            self.parts.append("- ")
        elif tag in {"strong", "b"}:
            self._just_closed_list = False
            self._push_emphasis("**")
        elif tag in {"em", "i"}:
            self._just_closed_list = False
            self._push_emphasis("_")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "li":
            self.in_li = False
            if not self.parts or not self.parts[-1].endswith("\n"):
                self.parts.append("\n")
        elif tag in {"strong", "b"}:
            self._close_emphasis("**")
        elif tag in {"em", "i"}:
            self._close_emphasis("_")
        elif tag in {"ul", "ol"}:
            # Allow a following `<br>` to be ignored (see handle_starttag).
            self._just_closed_list = True
        elif tag in {"p", "div"}:
            self._just_closed_list = False
            if not self.parts or not self.parts[-1].endswith("\n\n"):
                self.parts.append("\n\n")

    def handle_data(self, data):
        if not data:
            return
        text = html.unescape(data)
        # Ignore pure whitespace between tags to avoid spurious blank lines.
        if not text.strip():
            return
        for entry in self._emphasis_stack:
            entry["had_content"] = True
        self.parts.append(text)

    def handle_startendtag(self, tag, attrs):
        # Handle self-closing tags such as <br />
        self.handle_starttag(tag, attrs)


def html_to_md(html_str: str) -> str:
    """Convert a small subset of HTML into Markdown.

    Intended for simple text with paragraphs, line breaks, bold/italic.
    More complex markup should be handled via the raw HTML mode.

    Args:
        html_str: HTML fragment (typically from Qualtrics).

    Returns:
        Restricted Markdown string suitable for the Excel workbook columns.

    Example:
        >>> from qsync.markdown_codec import html_to_md
        >>> html_to_md("<p>Hello <strong>world</strong></p>")
        'Hello **world**'
    """

    parser = _HTMLToMarkdown()
    parser.feed(html_str or "")
    parser.close()
    text = "".join(parser.parts)
    # Normalise whitespace
    return normalize_text(text)


def html_to_md_canonical(html_str: str) -> str:
    """Convenience wrapper: normalised Markdown representation of HTML.

    Used for comparing JSON HTML against Excel Markdown in a stable way.
    """

    return html_to_md(html_str or "")


_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_ITALIC_RE = re.compile(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", re.DOTALL)
# Disallow `**` immediately after an opening `*` so sequences like `****` don't
# get mis-parsed into `<em>**</em>`.
_ITALIC_STAR_RE = re.compile(r"(?<!\*)\*(?![\s*])(.+?)(?<!\s)\*(?!\*)", re.DOTALL)


_EXTRA_BLANK_LINES_RE = re.compile(r"\n{3,}")


def normalize_markdown_for_compare(value: str) -> str:
    """Normalize Markdown for stable comparisons in preview/apply.

    We intentionally collapse *excess* blank lines because Excel cells often
    accumulate extra newlines (manual edits, copy/paste, or HTML→MD heuristics),
    which should not trigger wording diffs when the rendered output is the same.
    """

    text = normalize_text(value or "")
    text = _EXTRA_BLANK_LINES_RE.sub("\n\n", text)

    # Legacy HTML->MD conversion could emit empty bold markers for empty tags like
    # `<strong> </strong>`. Drop these so qsync-generated workbooks don't create
    # phantom diffs against canonicalized upstream HTML.
    text = text.replace("****", "")

    # HTML like `</ul><br>` historically round-tripped to Markdown with a blank line
    # between the list and following text (due to `</li>` + `<br>` both emitting `\n`).
    # Treat that as equivalent to a single newline for comparisons.
    text = re.sub(r"(?m)(^\s*- [^\n]*\n)\n(?!\s*- )", r"\1", text)
    return text


def md_to_html(md_str: str) -> str:
    """Convert restricted Markdown back to safe HTML.

    - Double newlines → paragraph breaks.
    - Single newlines → `<br>` within a paragraph.
    - `**bold**` → `<strong>`.
    - `_italic_` → `<em>`.

    Args:
        md_str: Restricted Markdown string (from Excel).

    Returns:
        HTML fragment (safe subset) suitable for writing to Qualtrics JSON fields.

    Example:
        >>> from qsync.markdown_codec import md_to_html
        >>> md_to_html("Hello **world**")
        'Hello <strong>world</strong>'
    """

    if md_str is None:
        return ""

    text = normalize_text(md_str)

    # Escape HTML special chars first
    # We do not render HTML attributes in this conversion, so escaping quotes
    # only creates noisy diffs (e.g., `'` → `&#x27;`) without improving safety.
    text = html.escape(text, quote=False)

    # Restore Markdown markers inside the escaped text, then wrap.
    def bold_sub(match: re.Match) -> str:
        return f"<strong>{match.group(1)}</strong>"

    def italic_sub(match: re.Match) -> str:
        return f"<em>{match.group(1)}</em>"

    text = _BOLD_RE.sub(lambda m: bold_sub(m), text)
    text = _ITALIC_RE.sub(lambda m: italic_sub(m), text)
    text = _ITALIC_STAR_RE.sub(lambda m: italic_sub(m), text)

    # Split into paragraphs on blank lines
    paragraphs = re.split(r"\n\s*\n", text) if text else []

    html_parts: list[str] = []
    for para in paragraphs:
        if not para:
            continue
        lines = para.split("\n")

        trimmed = [ln for ln in lines if ln.strip()]
        if not trimmed:
            continue

        def _is_bullet(ln: str) -> bool:
            return ln.lstrip().startswith("- ")

        # Three cases:
        # 1) Pure list paragraph -> <ul><li>...
        # 2) Pure text paragraph -> <br>-separated lines
        # 3) Mixed (intro text + bullet lines) -> intro lines + <ul> bullets
        all_bullets = all(_is_bullet(ln) for ln in trimmed)
        any_bullets = any(_is_bullet(ln) for ln in trimmed)

        if all_bullets:
            items: list[str] = []
            for ln in trimmed:
                content = ln.lstrip()[2:]  # drop leading "- "
                items.append(f"<li>{content}</li>")
            html_parts.append("<ul>\n" + "\n".join(items) + "\n</ul>")
            continue

        if not any_bullets:
            html_parts.append(para.replace("\n", "<br>"))
            continue

        # Mixed paragraph: keep non-bullet lines as intro, then render bullet groups.
        segments: list[str] = []
        intro_lines: list[str] = []
        bullet_items: list[str] = []

        def flush_intro() -> None:
            nonlocal intro_lines
            if not intro_lines:
                return
            segments.append("\n".join(intro_lines).replace("\n", "<br>"))
            intro_lines = []

        def flush_bullets() -> None:
            nonlocal bullet_items
            if not bullet_items:
                return
            segments.append("<ul>\n" + "\n".join(bullet_items) + "\n</ul>")
            bullet_items = []

        for ln in trimmed:
            if _is_bullet(ln):
                flush_intro()
                content = ln.lstrip()[2:]
                bullet_items.append(f"<li>{content}</li>")
            else:
                flush_bullets()
                intro_lines.append(ln)
        flush_bullets()
        flush_intro()

        html_parts.append("<br>".join(segments))

    return "<br><br>\n".join(html_parts) if html_parts else ""


class _MarkdownSafeHTMLParser(HTMLParser):
    """Classifies whether HTML is limited to the Markdown-safe subset."""

    def __init__(self) -> None:
        super().__init__()
        self.safe = True

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag not in MARKDOWN_SAFE_TAGS:
            self.safe = False
            return
        if attrs:
            self.safe = False

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag not in MARKDOWN_SAFE_TAGS:
            self.safe = False

    def handle_comment(self, data):
        self.safe = False

    def handle_decl(self, decl):
        self.safe = False

    def handle_pi(self, data):
        self.safe = False


def is_markdown_safe_html(html_str: str) -> bool:
    """Return True if the HTML is limited to the Markdown-safe subset."""

    if not html_str:
        return True

    # Fast path: no tags at all
    if "<" not in html_str and ">" not in html_str:
        return True

    parser = _MarkdownSafeHTMLParser()
    try:
        parser.feed(html_str)
        parser.close()
    except Exception:
        return False
    return parser.safe


def validate_html_fragment(html_str: str) -> list[str]:
    """Very lightweight HTML validation for user-supplied fragments.

    This is *not* a full HTML validator; it just checks for obviously
    mismatched start/end tags for non-void elements. It is primarily used
    to warn about potential broken HTML before syncing to Qualtrics.
    """

    class _TagStackParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack: list[str] = []
            self.errors: list[str] = []

        def handle_starttag(self, tag, attrs):
            tag = tag.lower()
            # Treat common void tags as self-closing
            if tag in {"br", "img", "hr", "meta", "link", "input"}:
                return
            self.stack.append(tag)

        def handle_endtag(self, tag):
            tag = tag.lower()
            if tag in {"br", "img", "hr", "meta", "link", "input"}:
                return
            if not self.stack:
                self.errors.append(f"Unexpected closing tag </{tag}>")
                return
            top = self.stack.pop()
            if top != tag:
                self.errors.append(
                    f"Mismatched closing tag </{tag}> (expected </{top}>)"
                )

    parser = _TagStackParser()
    parser.feed(html_str or "")
    parser.close()

    errors = list(parser.errors)
    for tag in parser.stack:
        errors.append(f"Unclosed tag <{tag}>")
    return errors
