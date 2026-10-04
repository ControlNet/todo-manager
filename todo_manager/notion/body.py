"""The Todo-Manager-maintained region of a Task/Project page body.

The region is ONE top-level callout whose first line carries ``MARKER``. Everything
outside it (the user's own notes) is never touched. Updates go through the markdown
endpoint as a search-and-replace of the exact region text read just before, so a
concurrent edit elsewhere on the page is preserved and an edit inside the region is
detected (and handed to the feedback flow) instead of being overwritten.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

MARKER = "当前说明（由 Todo Manager 维护；手写笔记请写在此框外）"
_ESCAPE = re.compile(r"([\\*~`$\[\]<>{}|^])")
MAX_DELIVERY_CHARS = 15000


_LEADING = re.compile(r"^(\s*)([#>+-]|\d+[.)])")


def escape_inline(text: str) -> str:
    """Escape enhanced-markdown special characters inside a block."""
    return _ESCAPE.sub(r"\\\1", text)


def escape(text: str) -> str:
    """Escape text that starts a block (also neutralises leading list/heading/quote marks)."""
    return _LEADING.sub(lambda m: m.group(1) + "\\" + m.group(2), escape_inline(text), count=1)


def _para(text: str, depth: int = 1) -> list[str]:
    """Plain text -> paragraphs (blank-line separated), line breaks kept with <br>."""
    out = []
    for chunk in re.split(r"\n\s*\n", text.strip()):
        lines = [line.rstrip() for line in chunk.splitlines()]
        if lines:
            out.append("\t" * depth + escape(lines[0]) + "".join("<br>" + escape_inline(x) for x in lines[1:]))
    return out


def _link(title: str, url: str | None) -> str:
    label = escape(title.strip() or url or "link")
    if url and url.startswith(("http://", "https://")) and len(url) < 1900:
        return f"[{label}]({url})"
    return label


def render(sections: dict[str, Any], marker: str = MARKER) -> str:
    """Render the managed callout. ``sections`` keys (all optional):

    goal, responsibility, done_criteria, summary, due_basis, focus_reason,
    sources: [{title, url, when, source}], delegations: [{title, status, versions}],
    delivery: {seq, when, summary, body, artifacts}, extra: [(label, text)]
    """
    lines = ['<callout icon="🤖" color="gray_bg">', f"\t**{escape(marker)}**"]
    for key, label in (("goal", "目标"), ("responsibility", "为什么是我的事"), ("done_criteria", "完成标准"),
                       ("due_basis", "截止依据"), ("focus_reason", "精选理由")):
        value = sections.get(key)
        if value:
            lines.append(f"\t**{label}**：" + "<br>".join(escape_inline(v.rstrip()) for v in str(value).strip().splitlines()))
    if sections.get("summary"):
        lines.append("\t**背景**")
        lines.extend(_para(str(sections["summary"])))
    for label, text in sections.get("extra") or []:
        lines.append(f"\t**{escape(label)}**：" + escape(str(text)))
    sources = sections.get("sources") or []
    if sources:
        lines.append("\t**来源**")
        for s in sources[:12]:
            meta = " · ".join(x for x in (s.get("source"), s.get("when")) if x)
            lines.append("\t- " + _link(s.get("title") or "", s.get("url")) + (f" · {escape(meta)}" if meta else ""))
        if len(sources) > 12:
            lines.append(f"\t- …另有 {len(sources) - 12} 条来源（todo-manager task show 查看全部）")
    delegations = sections.get("delegations") or []
    if delegations:
        lines.append("\t**委派**")
        for d in delegations[:8]:
            lines.append(f"\t- {escape(d['title'])} · {escape(d['status'])}"
                         + (f" · 交付 {d['versions']} 版" if d.get("versions") else ""))
    delivery = sections.get("delivery")
    if delivery:
        lines.append(f"\t**最新交付 v{delivery['seq']}**" + (f"（{escape(delivery['when'])}）" if delivery.get("when") else ""))
        if delivery.get("summary"):
            lines.append("\t" + escape(delivery["summary"].strip()))
        body = (delivery.get("body") or "").strip()
        if body and body != (delivery.get("summary") or "").strip():
            clipped = body[:MAX_DELIVERY_CHARS]
            lines.append("\t<details>")
            lines.append("\t<summary>展开交付内容</summary>")
            lines.extend(_para(clipped, depth=2))
            if len(body) > MAX_DELIVERY_CHARS:
                lines.append(f"\t\t（内容过长，已截断；完整内容见看板卡片 {escape(delivery.get('card') or '')}）")
            lines.append("\t</details>")
        for path in (delivery.get("artifacts") or [])[:10]:
            lines.append("\t- 附件：`" + str(path).replace("`", "'") + "`")
    lines.append("</callout>")
    return "\n".join(lines)


def locate(markdown: str, marker: str = MARKER) -> tuple[int, int] | None:
    """(start, end) offsets of the managed callout in a page's markdown, or None."""
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
            if close and marker[:12] in _unescape(head):
                return line_start, close.end()
        pos = start + 8


def _unescape(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text)


def content_fingerprint(region: str) -> str:
    """Hash of the visible text only, robust to markup re-rendering differences."""
    text = _unescape(re.sub(r"<[^>]+>|\{[^}]*\}|[*_`~#>\-]", " ", region))
    text = " ".join(text.split())
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def extract(markdown: str, marker: str = MARKER) -> str | None:
    span = locate(markdown, marker)
    return markdown[span[0]:span[1]] if span else None
