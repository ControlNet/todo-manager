"""Each delivered version of agent work as a page under its task page (2026-10-12).

The user reads deliveries in Notion, away from the machine that keeps the files. A version's
page holds, in order: the delivery summary and the agent's own notes, the PDFs and images
(uploaded and shown inline, Word/PowerPoint/Excel/CSV as downloadable files), each text file
(Markdown as formatted text, plain text in a copyable box), and a list of the files that stay
on the server (the agent's own records such as JSON and archives, missing files, files over the
workspace upload limit). The task page's agent box links to the latest version's page.

Steps are resumable: the page is created once (an uncertain create is looked up by its title
among the task page's child pages); uploaded files are appended under the "PDFs and images"
heading one at a time, and a retry counts the files already there before adding the rest.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from .. import clock, effects, i18n, outbox
from ..db import tx
from . import body
from .client import NotionError, NotionUncertain

TEXT = {".md": "markdown", ".markdown": "markdown", ".txt": "text", ".eml": "text"}
MEDIA = {
    ".pdf": ("pdf", "application/pdf"),
    ".png": ("image", "image/png"), ".jpg": ("image", "image/jpeg"), ".jpeg": ("image", "image/jpeg"),
    ".gif": ("image", "image/gif"), ".webp": ("image", "image/webp"), ".svg": ("image", "image/svg+xml"),
    ".docx": ("file", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ".doc": ("file", "application/msword"),
    ".pptx": ("file", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    ".ppt": ("file", "application/vnd.ms-powerpoint"),
    ".xlsx": ("file", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ".xls": ("file", "application/vnd.ms-excel"),
    ".csv": ("file", "text/csv"),
}
MEDIA_BLOCKS = ("pdf", "image", "file")
MAX_TEXT_CHARS = 40000       # per text file shown on the page
MAX_PAGE_CHARS = 200000      # all text on one page (a request carries at most 500 KB)


def sort_files(home: Path, artifacts: list[dict[str, Any]], max_bytes: int) -> dict[str, list[dict[str, Any]]]:
    """The kept files of a delivery: shown as text, uploaded, or kept on the server only."""
    out: dict[str, list[dict[str, Any]]] = {"text": [], "media": [], "server": []}
    seen: set[str] = set()
    for a in artifacts:
        if a.get("sha256"):       # one copy of each file (older deliveries kept some twice)
            if a["sha256"] in seen:
                continue
            seen.add(a["sha256"])
        rel = a.get("path")
        name = Path(rel or a.get("source") or "").name or "?"
        if not rel or a.get("missing") or a.get("not_copied"):
            out["server"].append({"name": name, "path": rel or a.get("source"), "why": "missing"})
            continue
        ext = Path(rel).suffix.lower()
        file = home / rel
        if ext in TEXT:
            out["text"].append({"name": name, "path": rel, "file": file, "kind": TEXT[ext]})
        elif ext in MEDIA:
            if (a.get("size") or file.stat().st_size) > max_bytes:
                out["server"].append({"name": name, "path": rel, "why": "too_large"})
            else:
                block, ctype = MEDIA[ext]
                out["media"].append({"name": name, "path": rel, "file": file, "block": block,
                                     "content_type": ctype or mimetypes.guess_type(name)[0]})
        else:
            out["server"].append({"name": name, "path": rel, "why": None})
    return out


def _text_section(item: dict[str, Any], lang: str | None, plain: bool) -> list[str]:
    text = item["file"].read_text(encoding="utf-8", errors="replace").strip()
    lines = [f"## {body.escape_inline(item['name'])}"]
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
        lines.append(body.escape(i18n.t("dlv.text_cut", lang, n=MAX_TEXT_CHARS, path=item["path"])))
    if item["kind"] == "markdown" and not plain:
        lines.append(text)
    elif "```" not in text and len(text) <= 1900 and not plain:
        lines += ["```text", text, "```"]
    else:
        lines += [line.lstrip("\t") for line in body._para(text)]
    return lines


def page_markdown(delivery: dict[str, Any], files: dict[str, list[dict[str, Any]]], lang: str | None,
                  plain: bool = False) -> str:
    """``plain``: the files' text escaped (the fallback when Notion refuses their Markdown)."""
    summary = (delivery.get("summary") or "").strip()
    notes = (delivery.get("body") or "").strip()
    texts = {t["path"]: t["file"].read_text(encoding="utf-8", errors="replace").strip() for t in files["text"]}
    lines: list[str] = []
    if summary:
        lines += [line.lstrip("\t") for line in body._para(summary)]
    if notes and notes != summary and notes not in texts.values():
        lines.append(f"## {body.escape_inline(i18n.t('dlv.notes', lang))}")
        lines += [line.lstrip("\t") for line in body._para(notes)] if plain else [notes[:MAX_TEXT_CHARS]]
    if files["media"]:
        lines.append(f"## {body.escape_inline(i18n.t('dlv.previews', lang))}")
    used = sum(len(x) for x in lines)
    for item in files["text"]:
        if used + min(len(texts[item["path"]]), MAX_TEXT_CHARS) > MAX_PAGE_CHARS:
            files["server"].append({"name": item["name"], "path": item["path"], "why": "too_long"})
            continue
        section = _text_section(item, lang, plain)
        used += sum(len(x) for x in section)
        lines += section
    if files["server"]:
        lines.append(f"## {body.escape_inline(i18n.t('dlv.server_only', lang))}")
        for item in files["server"]:
            why = i18n.t(f"dlv.why.{item['why']}", lang) if item["why"] else ""
            lines.append(f"- `{(item['path'] or item['name']).replace('`', chr(39))}`" + (f" {body.escape_inline(why)}" if why else ""))
    return "\n".join(lines) + "\n"


def title(delivery: dict[str, Any], tz: Any, lang: str | None) -> str:
    when = clock.local_dt(tz, clock.parse(delivery["received_at"])).strftime("%m-%d %H:%M")
    return i18n.t("dlv.page_title", lang, seq=delivery["seq"], when=when)


def push(sync: Any, delivery_id: str) -> dict[str, Any]:
    conn, client, lang = sync.conn, sync.client, sync.state.language
    row = conn.execute("SELECT v.*, d.task_id FROM deliveries v JOIN delegations d ON d.id=v.delegation_id"
                       " WHERE v.id=?", (delivery_id,)).fetchone()
    if row is None:
        raise outbox.Permanent(f"delivery {delivery_id} no longer exists")
    delivery = dict(row)
    task_row = sync.entity_row("task", delivery["task_id"])
    if task_row is None:
        raise outbox.Retryable("the task page is not created yet", retry_after=60)
    if task_row["trashed"]:
        return {"skipped": "task page trashed"}
    state = conn.execute("SELECT * FROM delivery_pages WHERE delivery_id=?", (delivery_id,)).fetchone()
    if state is not None and state["done_at"]:
        return {"skipped": "done"}
    files = sort_files(sync.cfg.home, _artifacts(delivery), sync.max_upload_bytes())
    name = title(delivery, sync.cfg.tz, lang)
    page_id = state["page_id"] if state is not None else None
    if page_id is None and state is not None:     # a create whose outcome is unknown
        page_id = next((b["id"] for b in client.block_children(task_row["page_id"])
                        if b.get("type") == "child_page" and (b.get("child_page") or {}).get("title") == name), None)
        if page_id:
            _save(conn, delivery_id, page_id=page_id, url=client.retrieve_page(page_id).get("url"))
    if page_id is None:
        with tx(conn):
            conn.execute("INSERT OR IGNORE INTO delivery_pages(delivery_id, created_at) VALUES (?,?)",
                         (delivery_id, clock.iso()))
        page = _create(client, task_row["page_id"], name, delivery, files, lang)
        page_id = page["id"]
        _save(conn, delivery_id, page_id=page_id, url=page.get("url"))
    attached = _attach(client, page_id, files["media"], lang)
    with tx(conn):
        conn.execute("UPDATE delivery_pages SET done_at=? WHERE delivery_id=?", (clock.iso(), delivery_id))
    effects.task_push(conn, delivery["task_id"])   # the agent box links to the page
    return {"page": page_id, "uploaded": attached, "text": len(files["text"]), "server": len(files["server"])}


def _artifacts(delivery: dict[str, Any]) -> list[dict[str, Any]]:
    from ..db import loads
    return [a if isinstance(a, dict) else {"source": a, "missing": True} for a in loads(delivery["artifacts"], [])]


def _create(client: Any, parent: str, name: str, delivery: dict[str, Any], files: dict[str, Any],
            lang: str | None) -> dict[str, Any]:
    props = {"title": {"title": [{"type": "text", "text": {"content": name}}]}}
    for plain in (False, True):
        try:
            return client.create_page({"type": "page_id", "page_id": parent}, props,
                                      markdown=page_markdown(delivery, {**files, "server": list(files["server"])},
                                                             lang, plain=plain),
                                      icon={"type": "emoji", "emoji": "📦"})
        except NotionUncertain as exc:
            if exc.committed_id:
                return client.retrieve_page(exc.committed_id)
            raise outbox.Uncertain(str(exc)) from None
        except NotionError as exc:
            if exc.status != 400 or plain:
                raise
    raise AssertionError("unreachable")


def _attach(client: Any, page_id: str, media: list[dict[str, Any]], lang: str | None) -> int:
    """Upload and place the files not on the page yet, in order, under the previews heading."""
    if not media:
        return 0
    heading = body.escape_inline(i18n.t("dlv.previews", lang))
    blocks = client.block_children(page_id)
    at = next((i for i, b in enumerate(blocks) if _heading(b) in (heading, i18n.t("dlv.previews", lang))), None)
    if at is None:
        raise outbox.Permanent(f"delivery page {page_id} has no previews heading")
    done = 0
    while at + 1 + done < len(blocks) and blocks[at + 1 + done].get("type") in MEDIA_BLOCKS:
        done += 1
    anchor = blocks[at + done]["id"]
    for item in media[done:]:
        upload = client.upload_file(item["name"], item["content_type"], item["file"].read_bytes())
        payload: dict[str, Any] = {"type": "file_upload", "file_upload": {"id": upload},
                                   "caption": [{"type": "text", "text": {"content": item["name"]}}]}
        try:
            new = client.append_children(page_id, [{"type": item["block"], item["block"]: payload}],
                                         position={"type": "after_block", "after_block": {"id": anchor}})
        except NotionUncertain as exc:
            raise outbox.Uncertain(str(exc)) from None
        anchor = new[0]["id"]
    return len(media) - done


def _heading(block: dict[str, Any]) -> str | None:
    kind = block.get("type", "")
    if not kind.startswith("heading_"):
        return None
    return "".join(t.get("plain_text") or t.get("text", {}).get("content", "")
                   for t in block[kind].get("rich_text", [])).strip()


def _save(conn: Any, delivery_id: str, **values: Any) -> None:
    with tx(conn):
        conn.execute("INSERT OR IGNORE INTO delivery_pages(delivery_id, created_at) VALUES (?,?)",
                     (delivery_id, clock.iso()))
        conn.execute("UPDATE delivery_pages SET page_id=?, url=? WHERE delivery_id=?",
                     (values["page_id"], values.get("url"), delivery_id))


def page_url(conn: Any, delivery_id: str) -> str | None:
    row = conn.execute("SELECT url FROM delivery_pages WHERE delivery_id=? AND done_at IS NOT NULL",
                       (delivery_id,)).fetchone()
    return row["url"] if row else None
