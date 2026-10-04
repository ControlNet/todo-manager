"""Intake (entry point A): context rendering for the model and application of its decisions.

The model receives: relevant rules + compact current tasks/projects + the new evidence of
mature batches (full text, with earlier thread messages as context). It answers with a
decisions JSON. ``apply_decisions`` validates everything first and then applies it in one
transaction:

* every new item of the run must be covered (linked as evidence or explicitly ignored with
  a category and reason) - nothing is dropped silently;
* the rules version must equal the version the model read (otherwise it must re-read);
* writes to existing tasks carry ``based_on_version``; human corrections win (see store);
* items are marked processed only together with the decisions that cover them.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from . import batching, clock, prompts, rules, runs, store
from .config import Config
from .db import dumps, loads, rules_version, tx
from .sources.base import conversation_history, conversation_root, item_body, quote

IGNORE_CATEGORIES = ("advertising", "newsletter", "cfp", "review_invitation_unaccepted", "automated_notice",
                     "fyi_no_action", "handled_elsewhere", "social", "spam", "other")
MAX_ITEM_CHARS = 4000
BULK_ITEM_CHARS = 800
MAX_EARLIER_CHARS = 600
QUOTE_START = re.compile(r"^(On .{5,200} wrote:|在.{2,80}写道[:：]|-----Original Message-----|From: .+)$", re.M)


class DecisionError(ValueError):
    def __init__(self, code: str, errors: list[str]):
        super().__init__(f"{code}: " + "; ".join(errors[:20]))
        self.code = code
        self.errors = errors


def trim_quoted(text: str) -> str:
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith(">")]
    trimmed = "\n".join(lines)
    match = QUOTE_START.search(trimmed)
    if match and match.start() > 40:
        trimmed = trimmed[: match.start()].rstrip() + "\n[quoted earlier messages trimmed]"
    return trimmed.strip()


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... {len(text) - limit} more chars; `todo-manager source show <id>` for full text]"


# --------------------------------------------------------------------------- manifest helpers

def run_new_items(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT s.* FROM source_items s JOIN batches b ON b.id=s.batch_id"
        " WHERE b.claimed_run=? AND b.state='claimed' AND s.state='batched' ORDER BY s.occurred_at", (run_id,)).fetchall()
    return [{**dict(r), "meta": loads(r["meta"], {})} for r in rows]


def run_batches(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM batches WHERE claimed_run=? AND state='claimed' ORDER BY source, first_item_at", (run_id,))]


# --------------------------------------------------------------------------- rendering

def _task_line(task: dict[str, Any], project_titles: dict[str, str]) -> str:
    parts = [f"{task['id']} v{task['version']} [{task['status']}/{task['next_actor']}] {task['title']}"]
    if task.get("project_id"):
        parts.append(f"project={project_titles.get(task['project_id'], task['project_id'])}")
    if task.get("due_date"):
        parts.append(f"due={task['due_date']}")
    if task.get("follow_up_date"):
        parts.append(f"follow_up={task['follow_up_date']}")
    if task.get("waiting_on"):
        parts.append(f"waiting_on={task['waiting_on']}")
    if task.get("next_step"):
        parts.append(f"next={task['next_step'][:120]}")
    meta = loads(task.get("field_meta"), {})
    human = sorted(f for f, m in meta.items() if m.get("by") == "human")
    if human:
        parts.append(f"human_set={','.join(human)}")
    return " | ".join(parts)


def _item_header(item: dict[str, Any]) -> str:
    who = "ME" if item["author_is_me"] else (item.get("author") or "?")
    flags = []
    if item["meta"].get("bulk_hint"):
        flags.append("bulk-hint")
    if item.get("kind") not in ("message", None):
        flags.append(item["kind"])
    flag = f" ({', '.join(flags)})" if flags else ""
    return f"[{item['id']}] {item.get('occurred_at', '')} from {who}{flag}"


def _github_status(conn: sqlite3.Connection, cfg: Config, batch: dict[str, Any],
                   batch_items: list[dict[str, Any]]) -> str | None:
    """State of an issue/PR and the user's part in it, from the newest item that carries it."""
    if not batch["conversation_key"].startswith("github:"):
        return None
    meta = next((i["meta"] for i in reversed(batch_items) if i["meta"].get("state")), None)
    if meta is None:
        return None
    logins = {login.lower() for login in cfg.section("identity").get("github_logins", [])}
    assignees, requested = meta.get("assignees") or [], meta.get("requested_reviewers") or []
    parts = [f"state: {meta['state']}"]
    if meta.get("opener"):
        parts.append(f"opened by {meta['opener']}")
    if assignees:
        parts.append("assignees: " + ", ".join(assignees))
    if requested:
        parts.append("review requested from: " + ", ".join(requested))
    you = []
    if (meta.get("opener") or "").lower() in logins:
        you.append("author")
    if logins & {a.lower() for a in assignees}:
        you.append("assignee")
    if logins & {r.lower() for r in requested}:
        you.append("review requested")
    if meta.get("reason"):
        you.append(f"notified ({meta['reason']})")
    if conn.execute("SELECT 1 FROM source_items WHERE conversation_key=? AND author_is_me=1 LIMIT 1",
                    (batch["conversation_key"],)).fetchone():
        you.append("has commented")
    return "status: " + "; ".join(parts) + "\nyou: " + (", ".join(you) or "not involved so far")


def render_context(conn: sqlite3.Connection, cfg: Config, run_id: str, manifest: dict[str, Any]) -> str:
    out: list[str] = []
    now_local = clock.local_dt(cfg.tz)
    rv = rules_version(conn)
    out.append(f"# Todo Manager intake run {run_id}")
    out.append(f"Local time: {now_local:%Y-%m-%d %H:%M %Z} ({cfg.tz}). rules_version={rv}")
    ident = cfg.section("identity")
    out.append("\n## Who the user is (identity; messages from these are the user's own)")
    for key in ("display_names", "emails", "github_logins", "slack_user_ids"):
        if ident.get(key):
            out.append(f"- {key}: {', '.join(ident[key])}")

    batches = run_batches(conn, run_id)
    items = run_new_items(conn, run_id)
    sources = {b["source"] for b in batches}
    thread_keys = {i["thread_key"] for i in items if i.get("thread_key")}
    linked_tasks = store.tasks_for_threads(conn, thread_keys)
    out.append("\n## Active rules")
    out.append(rules.render_rules(rules.relevant_rules(conn, sources=sources | {"commit"},
                                                       project_ids={t["project_id"] for t in linked_tasks if t.get("project_id")},
                                                       task_ids={t["id"] for t in linked_tasks})))
    suggested = rules.list_rules(conn, ("suggested",))
    if suggested:
        out.append(f"(+{len(suggested)} suggested rules awaiting the user's confirmation; not binding)")

    projects = store.list_projects(conn, ("active", "paused"))
    titles = {p["id"]: p["title"] for p in projects}
    out.append("\n## Projects")
    for p in projects:
        repos = f" repos={','.join(p['repos'])}" if p["repos"] else ""
        out.append(f"- {p['id']} v{p['version']} [{p['status']}] {p['title']}{repos}"
                   + (f" | next={p['next_step'][:100]}" if p.get("next_step") else ""))
    if not projects:
        out.append("(none yet)")

    open_tasks = store.list_tasks(conn, store.OPEN_STATUSES, limit=200)
    out.append(f"\n## Open tasks ({len(open_tasks)})")
    for task in open_tasks:
        out.append("- " + _task_line(task, titles))
    closed_linked = [t for t in linked_tasks if t["status"] in store.CLOSED_STATUSES]
    if closed_linked:
        out.append("\n## Closed tasks linked to these conversations (a reminder must NOT reopen these;"
                   " genuinely new responsibility -> new task with follow_up_of)")
        for task in closed_linked:
            out.append(f"- {_task_line(task, titles)} | closed_reason={task.get('closed_reason') or ''}")

    followups = manifest.get("followups") or []
    if followups:
        out.append("\n## Follow-ups due (decide: nudge needed? update follow_up_date/next_step/status)")
        for tid in followups:
            task = store.get_task(conn, tid)
            if task:
                out.append("- " + _task_line(task, titles))

    out.append(f"\n## New evidence ({len(items)} items in {len(batches)} conversations)")
    for batch in batches:
        batch_items = [i for i in items if i["batch_id"] == batch["id"]]
        related = [t for t in linked_tasks if t["id"] in {
            r["entity_id"] for r in conn.execute(
                "SELECT e.entity_id FROM evidence_links e JOIN source_items s ON s.id=e.source_item_id"
                " WHERE e.entity_type='task' AND s.conversation_key=?", (batch["conversation_key"],))}]
        title = " ".join(next((i.get("title") for i in batch_items if i.get("title")), "").split())
        out.append(f"\n### {batch['source']} {batch['conversation_key']}  {title}")
        if batch_items and batch_items[0].get("url"):
            out.append(f"link: {batch_items[0]['url']}")
        if related:
            out.append("already linked tasks: " + "; ".join(f"{t['id']} [{t['status']}] {t['title']}" for t in related))
        status = _github_status(conn, cfg, batch, batch_items)
        if status:
            out.append(status)
        last_new = max((i["occurred_at"] for i in batch_items), default=None)
        earlier = conversation_history(conn, batch["source"], batch["conversation_key"], exclude_batch=batch["id"],
                                       limit=batching.EARLIER_SHOWN, before=last_new)
        root = conversation_root(conn, batch["source"], batch["conversation_key"], exclude_batch=batch["id"],
                                 before=last_new)
        if root and all(e["id"] != root["id"] for e in earlier):
            earlier.insert(0, root)
        for e in earlier:
            body = trim_quoted(item_body(conn, e["id"]))
            label = "first message" if root and e["id"] == root["id"] else "earlier"
            out.append(f"\n{_item_header(e)} [{label}]\n{quote(_clip(body, MAX_EARLIER_CHARS))}")
        for item in batch_items:
            body = item_body(conn, item["id"])
            limit = BULK_ITEM_CHARS if item["meta"].get("bulk_hint") else MAX_ITEM_CHARS
            body = trim_quoted(body) if item["source"] == "gmail" else body
            extras = []
            if item["meta"].get("to"):
                extras.append("to: " + ", ".join(item["meta"]["to"][:8]))
            if item["meta"].get("attachments"):
                extras.append("attachments: " + ", ".join(a["filename"] for a in item["meta"]["attachments"]))
            if item.get("title") and item["source"] == "gmail":
                extras.append(f"subject: {' '.join(item['title'].split())}")
            out.append(f"\n{_item_header(item)} NEW\n" + ("\n".join(extras) + "\n" if extras else "")
                       + quote(_clip(body, limit)))

    digest = manifest.get("digest")
    if digest:
        out.append(f"\n## Daily commit activity digest for {digest['day']} (update project recent_activity;"
                   " never create tasks per commit; commits are not completion or %-progress)")
        repo_projects = {}
        for row in conn.execute("SELECT project_id, repo FROM project_repos"):
            repo_projects.setdefault(row["repo"].lower(), []).append(row["project_id"])
        commits = [dict(r) for r in conn.execute(
            f"SELECT * FROM source_items WHERE id IN ({','.join('?' for _ in digest['item_ids'])}) ORDER BY account, occurred_at",
            digest["item_ids"])] if digest["item_ids"] else []
        current = None
        seen_titles: set[tuple[str, str]] = set()
        for c in commits:
            if c["account"] != current:
                current = c["account"]
                linked = repo_projects.get(current, [])
                out.append(f"\n### {current}  (projects: {', '.join(titles.get(p, p) for p in linked) or 'none linked - only a project hint'})")
            key = (c.get("author") or "", c.get("title") or "")
            if key in seen_titles:
                continue  # duplicate sync/cherry-pick of the same change
            seen_titles.add(key)
            out.append(f"- {c['external_id'][:8]} {c.get('author') or ''}: {c.get('title') or ''}")

    out.append("\n## Output")
    out.append(f"run_id={run_id} rules_version={rv}")
    out.append(prompts.load("intake_decisions.md").replace(
        "{max_questions}", str(int(cfg.get("wake.max_questions_per_run", 3)))).replace(
        "{evidence_roles}", ", ".join(f"`{r}`" for r in store.EVIDENCE_ROLES)))
    return "\n".join(out)


# --------------------------------------------------------------------------- applying

TASK_SET_FIELDS = set(store.TASK_FIELDS)


def _validate(conn: sqlite3.Connection, run_id: str, decisions: dict[str, Any], manifest: dict[str, Any],
              max_questions: int = 3) -> list[str]:
    errors: list[str] = []
    if decisions.get("run_id") != run_id:
        errors.append(f"run_id must be {run_id}")
    items = decisions.get("items")
    if not isinstance(items, list):
        return errors + ["'items' must be a list"]
    new_ids = {i["id"] for i in run_new_items(conn, run_id)}
    covered: set[str] = set()
    refs: set[str] = set()
    project_refs: set[str] = set()
    reviewed: set[str] = set()
    seen_ops: dict[str, int] = {}
    questions = 0
    for n, op in enumerate(items):
        kind = op.get("op")
        where = f"items[{n}] ({kind})"
        canonical = dumps(op)
        if canonical in seen_ops:
            errors.append(f"{where}: identical to items[{seen_ops[canonical]}]; give each decision once")
            continue
        seen_ops[canonical] = n
        if kind in ("update_task", "link", "followup_reviewed"):
            reviewed.add(op.get("task"))
        elif kind == "create_task" and op.get("follow_up_of"):
            reviewed.add(op["follow_up_of"])
        evidence = op.get("evidence", []) or []
        for ev in evidence:
            sid = ev.get("item")
            if not sid or conn.execute("SELECT 1 FROM source_items WHERE id=?", (sid,)).fetchone() is None:
                errors.append(f"{where}: unknown evidence item {sid!r}")
            if ev.get("role", "context") not in store.EVIDENCE_ROLES:
                errors.append(f"{where}: invalid role {ev.get('role')!r} (one of {', '.join(store.EVIDENCE_ROLES)})")
            covered.add(sid)
        if kind == "create_task":
            if not (op.get("title") or "").strip():
                errors.append(f"{where}: title required")
            if not (op.get("responsibility") or "").strip():
                errors.append(f"{where}: responsibility (why this is the user's) required")
            if not evidence and not op.get("follow_up_of"):
                errors.append(f"{where}: evidence required")
            if "ask" in op:
                questions += 1
                if not isinstance(op["ask"], str) or not op["ask"].strip():
                    errors.append(f"{where}: ask must be the one question for the user")
                if (op.get("status"), op.get("next_actor", "me")) != ("decision", "me"):
                    errors.append(f"{where}: ask requires status 'decision' and next_actor 'me'")
            try:
                store.normalize_state(op.get("status", "todo"), op.get("next_actor", "me"))
            except store.StoreError as exc:
                errors.append(f"{where}: {exc}")
            if op.get("ref"):
                if op["ref"] in refs:
                    errors.append(f"{where}: ref {op['ref']!r} used twice")
                refs.add(op["ref"])
            project = op.get("project")
            if isinstance(project, str) and project and not project.startswith("@") and \
                    conn.execute("SELECT 1 FROM projects WHERE id=?", (project,)).fetchone() is None:
                errors.append(f"{where}: unknown project {project}")
            if op.get("follow_up_of") and store.get_task(conn, op["follow_up_of"]) is None:
                errors.append(f"{where}: unknown follow_up_of task")
        elif kind in ("update_task", "link"):
            task = store.get_task(conn, op.get("task", ""))
            if task is None:
                errors.append(f"{where}: unknown task {op.get('task')!r}")
            if kind == "update_task":
                if not isinstance(op.get("based_on_version"), int):
                    errors.append(f"{where}: based_on_version (int) required")
                bad = set(op.get("set", {})) - TASK_SET_FIELDS
                if bad:
                    errors.append(f"{where}: unknown fields {sorted(bad)}")
                if op.get("override_human") and not evidence:
                    errors.append(f"{where}: override_human requires evidence")
        elif kind == "ignore":
            ids_ = op.get("items") or []
            if op.get("category") not in IGNORE_CATEGORIES:
                errors.append(f"{where}: category must be one of {IGNORE_CATEGORIES}")
            if not (op.get("rationale") or "").strip():
                errors.append(f"{where}: rationale required")
            for sid in ids_:
                if sid not in new_ids:
                    errors.append(f"{where}: {sid} is not a new item of this run")
                covered.add(sid)
        elif kind == "create_project":
            if not op.get("ref") or not op.get("title"):
                errors.append(f"{where}: ref and title required")
            if "@" + op.get("ref", "") in project_refs:
                errors.append(f"{where}: project ref {op.get('ref')!r} used twice")
            project_refs.add("@" + op.get("ref", ""))
        elif kind == "update_project":
            if conn.execute("SELECT 1 FROM projects WHERE id=?", (op.get("project", ""),)).fetchone() is None:
                errors.append(f"{where}: unknown project {op.get('project')!r}")
            bad = set(op.get("set", {})) - set(store.PROJECT_FIELDS)
            if bad:
                errors.append(f"{where}: unknown project fields {sorted(bad)}")
        elif kind == "suggest_rule":
            if not op.get("title") or not op.get("body"):
                errors.append(f"{where}: title and body required")
        elif kind == "followup_reviewed":
            if store.get_task(conn, op.get("task", "")) is None:
                errors.append(f"{where}: unknown task")
        else:
            errors.append(f"{where}: unknown op")
    if questions > max_questions:
        errors.append(f"{questions} questions (ask); at most {max_questions} questions per run: ignore what is"
                      " low-impact or unclear, ask only what could cost the user if missed")
    for n, op in enumerate(items):
        project = op.get("project")
        if op.get("op") == "create_task" and isinstance(project, str) and project.startswith("@") \
                and project not in project_refs:
            errors.append(f"items[{n}]: project ref {project} not created in this file")
    missing = new_ids - covered
    if missing:
        errors.append(f"uncovered new items (link as evidence or ignore with category): {sorted(missing)}")
    unreviewed = [t for t in manifest.get("followups") or [] if t not in reviewed]
    if unreviewed:
        errors.append(f"uncovered follow-ups (update_task/link the task, or followup_reviewed when nothing changes):"
                      f" {unreviewed}")
    return errors


def _ownership_problems(conn: sqlite3.Connection, run_id: str, manifest: dict[str, Any]) -> list[str]:
    """Everything the manifest lists must still be claimed by this run (checked inside the
    applying transaction: a late result of a stale claim never applies over a newer run)."""
    problems = []
    batch_ids = manifest.get("batch_ids") or []
    if batch_ids:
        marks = ",".join("?" for _ in batch_ids)
        owned = conn.execute(f"SELECT COUNT(*) FROM batches WHERE id IN ({marks}) AND claimed_run=? AND state='claimed'",
                             (*batch_ids, run_id)).fetchone()[0]
        if owned != len(batch_ids):
            problems.append(f"{len(batch_ids) - owned} of {len(batch_ids)} conversations are no longer claimed by this"
                            " run (released after a timeout or taken over by a newer run)")
    digest = manifest.get("digest")
    if digest and conn.execute("SELECT 1 FROM daily_jobs WHERE job='commit_digest' AND day=? AND state='claimed'"
                               " AND run_id=?", (digest["day"], run_id)).fetchone() is None:
        problems.append("the commit digest is no longer claimed by this run")
    for tid in manifest.get("followups") or []:
        if conn.execute("SELECT 1 FROM followup_checks WHERE task_id=? AND run_id=? AND state='claimed'",
                        (tid, run_id)).fetchone() is None:
            problems.append(f"follow-up {tid} is no longer claimed by this run")
    return problems


def apply_decisions(conn: sqlite3.Connection, cfg: Config, run_id: str, decisions: dict[str, Any],
                    manifest: dict[str, Any]) -> dict[str, Any]:
    """Apply a run's decisions exactly once: validation, the ownership check and every write
    happen in one transaction that also records the run as applied."""
    digest_hash = runs.decisions_hash(decisions)
    with tx(conn):
        try:
            prior = runs.applied_report(conn, run_id, digest_hash)
        except runs.AlreadyApplied as exc:
            raise DecisionError("ALREADY_APPLIED", [str(exc)]) from None
        if prior is not None:
            return prior
        current_rv = rules_version(conn)
        if decisions.get("rules_version") != current_rv:
            changed = [dict(r) for r in conn.execute(
                "SELECT rule_id, revision, title, body, status FROM rule_revisions WHERE rules_version > ?"
                " ORDER BY rules_version", (int(decisions.get("rules_version") or 0),))]
            raise DecisionError("STALE_RULES", [f"rules changed since you read them (now {current_rv}); re-check"
                                                " against: " + dumps(changed)])
        stale = _ownership_problems(conn, run_id, manifest)
        if stale:
            raise DecisionError("STALE_RUN", stale)
        errors = _validate(conn, run_id, decisions, manifest,
                           max_questions=int(cfg.get("wake.max_questions_per_run", 3)))
        if errors:
            raise DecisionError("INVALID", errors)
        report = _apply(conn, run_id, decisions, manifest, current_rv)
        runs.record_applied(conn, run_id, "intake", digest_hash, report)
    return report


def _apply(conn: sqlite3.Connection, run_id: str, decisions: dict[str, Any], manifest: dict[str, Any],
           current_rv: int) -> dict[str, Any]:
    report: dict[str, Any] = {"created_tasks": [], "questions": [], "updated_tasks": [], "rejected_fields": [],
                              "ignored": 0, "created_projects": [], "updated_projects": [], "suggested_rules": []}
    touched_tasks: set[str] = set()
    touched_projects: set[str] = set()
    project_map: dict[str, str] = {}
    for op in decisions["items"]:
        if op["op"] == "create_project":
            pid = store.create_project(conn, op["title"], origin="intake", actor="agent", cause_ref=run_id,
                                       domain=op.get("domain"), goal=op.get("goal"), next_step=op.get("next_step"),
                                       repos=op.get("repos", []))
            project_map["@" + op["ref"]] = pid
            report["created_projects"].append(pid)
            touched_projects.add(pid)
            store.record_decision(conn, run_id=run_id, op="create_project", target_type="project", target_id=pid,
                                  source_item_ids=[e["item"] for e in op.get("evidence", [])],
                                  rationale=op.get("rationale"), rules_version=current_rv)
            for ev in op.get("evidence", []):
                store.link_evidence(conn, "project", pid, ev["item"], ev.get("role", "context"), ev.get("note"))
    task_refs: dict[str, str] = {}
    for op in decisions["items"]:
        kind = op["op"]
        evidence = op.get("evidence", []) or []
        source_ids = [e["item"] for e in evidence]
        if kind == "create_task":
            project = op.get("project")
            if isinstance(project, dict):
                project = None
            if isinstance(project, str) and project.startswith("@"):
                project = project_map[project]
            fields = {k: op.get(k) for k in ("waiting_on", "responsibility", "next_step", "done_criteria", "summary",
                                             "due_date", "due_basis", "follow_up_date", "focus", "focus_reason")
                      if op.get(k) is not None}
            if op.get("ask"):
                fields["next_step"] = op["ask"].strip()
            did = store.record_decision(conn, run_id=run_id, op="create_task", target_type="task", target_id=None,
                                        source_item_ids=source_ids, rationale=op.get("rationale"),
                                        rules_version=current_rv)
            tid = store.create_task(conn, op["title"], origin="intake", actor="agent", cause_ref=did,
                                    project_id=project or None, status=op.get("status", "todo"),
                                    next_actor=op.get("next_actor", "me"), **fields)
            conn.execute("UPDATE decisions SET target_id=? WHERE id=?", (tid, did))
            for ev in evidence:
                store.link_evidence(conn, "task", tid, ev["item"], ev.get("role", "origin"), ev.get("note"), did)
            if op.get("follow_up_of"):
                store.link_tasks(conn, tid, op["follow_up_of"], "follow_up_of")
            if op.get("ref"):
                task_refs[op["ref"]] = tid
            report["created_tasks"].append(tid)
            if op.get("ask"):
                report["questions"].append(tid)
            touched_tasks.add(tid)
        elif kind in ("update_task", "link"):
            tid = op["task"]
            did = store.record_decision(conn, run_id=run_id, op=kind, target_type="task", target_id=tid,
                                        source_item_ids=source_ids, rationale=op.get("rationale"),
                                        rules_version=current_rv)
            for ev in evidence:
                store.link_evidence(conn, "task", tid, ev["item"], ev.get("role", "context"), ev.get("note"), did)
            if kind == "update_task" and op.get("set"):
                changes = dict(op["set"])
                if op.get("note"):
                    changes["change_note"] = op["note"]
                result = store.update_task(conn, tid, changes, actor="agent", origin="intake", cause_ref=did,
                                           based_on_version=op["based_on_version"],
                                           override_human=bool(op.get("override_human")))
                if "change_note" in result.applied:
                    conn.execute("UPDATE tasks SET change_note_at=? WHERE id=?", (clock.iso(), tid))
                if result.rejected:
                    report["rejected_fields"].append({"task": tid, "rejected": result.rejected})
                    conn.execute("UPDATE decisions SET outcome=?, detail=? WHERE id=?",
                                 ("partial" if result.applied else "rejected", dumps(result.rejected), did))
                if result.applied:
                    report["updated_tasks"].append(tid)
                    if result.applied.get("status") in store.CLOSED_STATUSES and op.get("closed_reason"):
                        store.set_closed_reason(conn, tid, op["closed_reason"])
            touched_tasks.add(tid)
        elif kind == "ignore":
            store.record_decision(conn, run_id=run_id, op="ignore", target_type=None, target_id=None,
                                  source_item_ids=op["items"], category=op["category"],
                                  rationale=op.get("rationale"), rules_version=current_rv)
            report["ignored"] += len(op["items"])
        elif kind == "update_project":
            pid = op["project"]
            changes = dict(op.get("set", {}))
            did = store.record_decision(conn, run_id=run_id, op="update_project", target_type="project",
                                        target_id=pid, source_item_ids=source_ids, rationale=op.get("rationale"),
                                        rules_version=current_rv)
            if "recent_activity" in changes:
                conn.execute("UPDATE projects SET recent_activity_at=? WHERE id=?", (clock.iso(), pid))
            result = store.update_project(conn, pid, changes, actor="agent", origin="intake", cause_ref=did,
                                          based_on_version=op.get("based_on_version"),
                                          repos_add=op.get("repos_add", []))
            for ev in evidence:
                store.link_evidence(conn, "project", pid, ev["item"], ev.get("role", "context"), ev.get("note"), did)
            if result.rejected:
                report["rejected_fields"].append({"project": pid, "rejected": result.rejected})
            report["updated_projects"].append(pid)
            touched_projects.add(pid)
        elif kind == "suggest_rule":
            rid = rules.add_rule(conn, title=op["title"], body=op["body"], scope_type=op.get("scope_type", "global"),
                                 scope_id=op.get("scope_id"), status="suggested", actor="agent")
            report["suggested_rules"].append(rid)
        elif kind == "followup_reviewed":
            store.record_decision(conn, run_id=run_id, op="followup_reviewed", target_type="task",
                                  target_id=op["task"], rationale=op.get("rationale"), rules_version=current_rv)
    # Follow-ups are done only now that the decisions about them are applied.
    conn.execute("UPDATE followup_checks SET state='done', updated_at=? WHERE run_id=? AND state='claimed'",
                 (clock.iso(), run_id))
    now = clock.iso()
    conn.execute("UPDATE source_items SET state='processed', processed_at=?, processed_run=? WHERE batch_id IN"
                 " (SELECT id FROM batches WHERE claimed_run=? AND state='claimed') AND state='batched'",
                 (now, run_id, run_id))
    conn.execute("UPDATE batches SET state='done', done_at=? WHERE claimed_run=? AND state='claimed'", (now, run_id))
    digest = manifest.get("digest")
    if digest:
        marks = ",".join("?" for _ in digest["item_ids"])
        if digest["item_ids"]:
            conn.execute(f"UPDATE source_items SET state='processed', processed_at=?, processed_run=? WHERE id IN ({marks})",
                         (now, run_id, *digest["item_ids"]))
        conn.execute("UPDATE daily_jobs SET state='done', run_id=?, updated_at=? WHERE job='commit_digest' AND day=?",
                     (run_id, now, digest["day"]))
    conn.execute("UPDATE runs SET agent_completed_at=? WHERE id=?", (now, run_id))
    report["touched_tasks"] = sorted(touched_tasks)
    report["touched_projects"] = sorted(touched_projects)
    return report
