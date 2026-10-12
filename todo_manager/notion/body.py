"""The Todo-Manager-maintained region of a Task/Project page body.

The region is ONE top-level callout whose first line carries the marker text
(``body.marker`` in the language catalogs; any language's marker is recognised). Everything
outside it (the user's own notes) is never touched. Updates go through the markdown
endpoint as a search-and-replace of the exact region text read just before, so a
concurrent edit elsewhere on the page is preserved and an edit inside the region is
detected (and handed to the feedback flow) instead of being overwritten.
"""

from __future__ import annotations

import hashlib
import re
from datetime import timedelta
from typing import Any

from .. import clock, i18n
from ..files import artifact_label

MARKER = "body.marker"        # catalog key of the task-page marker
_ESCAPE = re.compile(r"([\\*~`$\[\]<>{}|^])")
MAX_DELIVERY_CHARS = 15000


_LEADING = re.compile(r"^(\s*)([#>+-]|\d+[.)])")
# A bare http(s) URL in text: it ends at white space, markup characters or CJK text (U+2E80 on).
_URL = re.compile(r"https?://[^\s<>\[\]{}\"'`|^\\\u2e80-\uffff]+")
_MD_LINK = re.compile(r"(?<!\\)\[((?:\\.|[^\]\\])*)\]\(([^)\s]*)\)")


def edit_unread(last_edited_time: str | None, last_edited_by: str | None, body_read_at: str | None,
                bot_user_id: str | None) -> bool:
    """Whether the page's last edit (not ours) may postdate our last body read. The one check
    for re-reading a body, claiming body_edit items and applying them. No timestamp
    precision is assumed: a stamp on a whole minute (Notion rounds edit times) may stand for any
    moment of that minute, so only a read after the end of the minute covers it; a stamp with
    seconds is taken as precise to the second."""
    if not last_edited_time or last_edited_by == bot_user_id:
        return False
    if not body_read_at:
        return True
    edited = clock.parse(last_edited_time)
    grain = timedelta(seconds=1) if edited.second or edited.microsecond else timedelta(minutes=1)
    return clock.parse(body_read_at) < edited + grain


def escape_inline(text: str) -> str:
    """Escape enhanced-markdown special characters inside a block."""
    return _ESCAPE.sub(r"\\\1", text)


def escape(text: str) -> str:
    """Escape text that starts a block (also neutralises leading list/heading/quote marks)."""
    return _LEADING.sub(lambda m: m.group(1) + "\\" + m.group(2), escape_inline(text), count=1)


def _url_end(url: str) -> str:
    """The URL without the sentence punctuation or unmatched bracket that follows it."""
    while url and (url[-1] in ".,;:!?*_~" or (url[-1] == ")" and url.count(")") > url.count("("))):
        url = url[:-1]
    return url


def prose_inline(text: str) -> str:
    """Escape written text, with each bare http(s) URL as an explicit link: Notion would link
    it itself up to the next white space, taking any CJK text after it into the link."""
    out, pos = [], 0
    for m in _URL.finditer(text):
        url = _url_end(m.group(0))
        if url.endswith("://") or len(url) >= 1900:
            continue
        out.append(escape_inline(text[pos:m.start()]))
        out.append(f"[{escape_inline(url)}]({url.replace('(', '%28').replace(')', '%29')})")
        pos = m.start() + len(url)
    return "".join(out) + escape_inline(text[pos:])


def prose(text: str) -> str:
    """``prose_inline`` for text that starts a block (see ``escape``)."""
    return _LEADING.sub(lambda m: m.group(1) + "\\" + m.group(2), prose_inline(text), count=1)


def _para(text: str, depth: int = 1) -> list[str]:
    """Plain text -> paragraphs (blank-line separated), line breaks kept with <br>."""
    out = []
    for chunk in re.split(r"\n\s*\n", text.strip()):
        lines = [line.rstrip() for line in chunk.splitlines()]
        if lines:
            out.append("\t" * depth + prose(lines[0]) + "".join("<br>" + prose_inline(x) for x in lines[1:]))
    return out


def _link(title: str, url: str | None) -> str:
    label = escape(title.strip() or url or "link")
    if url and url.startswith(("http://", "https://")) and len(url) < 1900:
        return f"[{label}]({url})"
    return label


def render(sections: dict[str, Any], marker: str = MARKER, lang: str | None = None) -> str:
    """Render the managed callout. ``marker`` is a catalog key; ``sections`` keys (all optional):

    goal, responsibility, done_criteria, summary, due_basis, focus_reason,
    sources: [{title, url, when, source}], delegations: [{title, status, versions}],
    delivery: {seq, when, summary, body, artifacts}, extra: [(label, text)]
    """
    def t(key: str, **values: Any) -> str:
        return i18n.t(key, lang, **values)

    sep = t("body.sep")
    lines = ['<callout icon="🤖" color="gray_bg">', f"\t**{escape(t(marker))}**"]
    for key in ("goal", "responsibility", "done_criteria", "due_basis", "focus_reason"):
        value = sections.get(key)
        if value:
            lines.append(f"\t**{t('body.' + key)}**{sep}"
                         + "<br>".join(prose_inline(v.rstrip()) for v in str(value).strip().splitlines()))
    if sections.get("summary"):
        lines.append(f"\t**{t('body.summary')}**")
        lines.extend(_para(str(sections["summary"])))
    for label, text in sections.get("extra") or []:
        lines.append(f"\t**{escape(label)}**{sep}" + escape(str(text)))
    sources = sections.get("sources") or []
    if sources:
        lines.append(f"\t**{t('body.sources')}**")
        for s in sources[:12]:
            meta = " · ".join(x for x in (s.get("source"), s.get("when")) if x)
            lines.append("\t- " + _link(s.get("title") or "", s.get("url")) + (f" · {escape(meta)}" if meta else ""))
        if len(sources) > 12:
            lines.append("\t- " + escape_inline(t("body.more_sources", n=len(sources) - 12)))
    delegations = sections.get("delegations") or []
    if delegations:
        lines.append(f"\t**{t('body.delegations')}**")
        for d in delegations[:8]:
            lines.append(f"\t- {escape(d['title'])} · {escape(d['status'])}"
                         + (" · " + t("body.delivery_versions", n=d["versions"]) if d.get("versions") else ""))
    delivery = sections.get("delivery")
    if delivery:
        lines.append(f"\t**{t('body.latest_delivery', seq=delivery['seq'])}**"
                     + (t("body.when", when=escape(delivery["when"])) if delivery.get("when") else ""))
        if delivery.get("summary"):
            lines.append("\t" + prose(delivery["summary"].strip()))
        body = (delivery.get("body") or "").strip()
        if body and body != (delivery.get("summary") or "").strip():
            clipped = body[:MAX_DELIVERY_CHARS]
            lines.append("\t<details>")
            lines.append(f"\t<summary>{escape_inline(t('body.expand_delivery'))}</summary>")
            lines.extend(_para(clipped, depth=2))
            if len(body) > MAX_DELIVERY_CHARS:
                lines.append("\t\t" + escape(t("body.truncated", card=delivery.get("card") or "")))
            lines.append("\t</details>")
        for artifact in (delivery.get("artifacts") or [])[:10]:
            lines.append(f"\t- {t('body.attachment')}{sep}`" + artifact_label(artifact).replace("`", "'") + "`")
    lines.append("</callout>")
    return "\n".join(lines)


def locate(markdown: str, marker: str = MARKER) -> tuple[int, int] | None:
    """(start, end) offsets of the managed callout in a page's markdown, or None.
    ``marker`` is a catalog key; the marker text of any language is recognised."""
    heads = [text[:12] for text in i18n.variants(marker)]
    pos = 0
    while True:
        start = markdown.find("<callout", pos)
        if start < 0:
            return None
        line_start = markdown.rfind("\n", 0, start) + 1
        if markdown[line_start:start].strip() == "":
            end_tag = re.compile(r"^</callout>[ \t]*$", re.M)
            head = markdown[start:start + 400]
            close = end_tag.search(markdown, start)
            if close and any(h in _unescape(head) for h in heads):
                return line_start, close.end()
        pos = start + 8


def _unescape(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text)


def _visible(region: str, links: bool = True) -> str:
    if links:
        region = _MD_LINK.sub(r"\1", region)
    return " ".join(_unescape(re.sub(r"<[^>]+>|\{[^}]*\}|[*_`~#>\-]", " ", region)).split())


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def content_fingerprint(region: str) -> str:
    """Hash of the visible text only, robust to markup re-rendering differences. A link counts
    by its text: Notion stores a bare URL or domain as a link on its own, and the response to
    the write that sent it does not show that yet."""
    return _hash(_visible(region))


def _unlinked_url(m: re.Match) -> str:
    text, href = _unescape(m.group(1)), m.group(2)
    if text.startswith(("http://", "https://")) or href in (text, "http://" + text, "https://" + text):
        return m.group(1)
    return m.group(0)


def same_content(region: str, fingerprint: str | None) -> bool:
    """Whether ``region`` shows the content a stored ``fingerprint`` was taken of. Fingerprints
    stored before links counted by their text (2026-10-12) are recognised too, including after
    Notion turned that content's bare URLs or domains into links."""
    if not fingerprint:
        return False
    return fingerprint in (content_fingerprint(region), _hash(_visible(region, links=False)),
                           _hash(_visible(_MD_LINK.sub(_unlinked_url, region), links=False)))


def extract(markdown: str, marker: str = MARKER) -> str | None:
    span = locate(markdown, marker)
    return markdown[span[0]:span[1]] if span else None


def outside(markdown: str, marker: str = MARKER) -> str:
    """The page text outside the managed region: the user's own notes."""
    span = locate(markdown, marker)
    return markdown if span is None else markdown[:span[0]] + markdown[span[1]:]
