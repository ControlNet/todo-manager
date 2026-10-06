"""``todo-manager`` command line (stdlib only).

The gates print the context of a Hermes cron run (or ``{"wakeAgent": false}``) and nothing
else; a gate that fails exits 1 so that Hermes reports a failed script. Every other command
prints JSON (or plain lines for lists); a rejected call exits with status 2 and a JSON body
``{"error": CODE, "details": [...]}`` the agent can act on. Runtime data lives in TODO_HOME
(default ``~/.hermes/workspace/todo-manager``), never in the source tree.

Commands that change Todo data take the context they act in (see ``ops``): ``--run`` (a cron
run), ``--feedback`` (the user's Notion feedback item), ``--request ID --said TEXT`` (the user's
chat request) or none (the user at the terminal).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import clock, config, db, delegation, feedback, health, i18n, ops, prefs, store
from .db import dumps, loads

EXIT_INVALID = 2
EXIT_NEEDS_INPUT = 3   # `install` stopped at a question or a confirmation (see its JSON)
_CONNECTIONS: list[Any] = []


# --------------------------------------------------------------------------- helpers

def _print(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=1, default=str))


def _fail(code: str, details: list[str] | str) -> int:
    _print({"error": code, "details": [details] if isinstance(details, str) else details})
    return EXIT_INVALID


def _open(args: argparse.Namespace) -> tuple[config.Config, Any]:
    cfg = config.load(args.home)
    conn = db.connect(cfg.db_path)
    _CONNECTIONS.append(conn)
    prefs.ensure_migrated(conn, cfg.home, cfg.language)
    return cfg, conn


def _parse_set(pairs: list[str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep:
            raise ops.OpError("INVALID", f"--set expects field=value, got {pair!r}")
        key = key.strip()
        if value == "":
            out[key] = None
        elif key == "focus":
            out[key] = value.lower() in ("1", "true", "yes", "y")
        else:
            out[key] = value
    return out


def _task_line(t: dict[str, Any]) -> str:
    due = f" due {t['due_date'][:10]}" if t.get("due_date") else ""
    return f"{t['id']} v{t['version']} [{t['status']}/{t['next_actor']}]{due} {t['title']}"


def _add_ctx(parser: argparse.ArgumentParser) -> None:
    g = parser.add_argument_group("context (who asks for this change)")
    g.add_argument("--run", help="the cron run this is part of")
    g.add_argument("--feedback", dest="feedback_id", help="the user's feedback item this carries out")
    g.add_argument("--request", help="id of the user's chat request (Matrix event id, or one id per request)")
    g.add_argument("--said", help="with --request: the user's words")
    g.add_argument("--scope", help="with --request: task:<id> | project:<id> | global")
    g.add_argument("--key", help="explicit identity of this command (a retry with the same key applies once)")


def _ctx(conn: Any, cfg: config.Config, args: argparse.Namespace) -> ops.Ctx:
    return ops.context(conn, cfg, run=args.run, feedback_id=args.feedback_id, request=args.request, said=args.said,
                       scope=args.scope, key=args.key)


def _run(fn: Any) -> int:
    try:
        _print(fn())
    except ops.OpError as exc:
        return _fail(exc.code, exc.details)
    except store.StoreError as exc:
        return _fail("INVALID", str(exc))
    except delegation.DelegationError as exc:
        return _fail("INVALID", str(exc))
    return 0


# --------------------------------------------------------------------------- gates

def cmd_gate(args: argparse.Namespace) -> int:
    """Prints only the gate output (Hermes reads its last line). A failure exits 1 with one line
    on stderr: Hermes then shows the run a Script Error, which the prompt reports as a failure."""
    from .locks import LockBusy, file_lock, lock_path
    try:
        cfg, conn = _open(args)
        with file_lock(lock_path(cfg.home, args.entry)):
            out = _run_gate(args.entry, conn, cfg)
    except LockBusy:
        out = '{"wakeAgent": false}'   # the previous run of this gate is still going
    except Exception as exc:  # noqa: BLE001 - reported to Hermes, never a fake idle
        print(f"todo-manager gate {args.entry} failed: {type(exc).__name__}: {str(exc)[:500]}", file=sys.stderr)
        return 1
    print(out)
    return 0


def _run_gate(entry: str, conn: Any, cfg: config.Config) -> str:
    from . import wiring
    if entry == "intake":
        from .gate_intake import run_gate
        from .sources.registry import commit_sources, conversation_sources
        return run_gate(conn, cfg, conversation_sources(cfg), commit_sources(cfg))[0]
    from .gate_feedback import run_gate as run_feedback
    return run_feedback(conn, cfg, notion=wiring.notion_sync(conn, cfg), kanban=wiring.kanban(cfg))[0]


def cmd_sync(args: argparse.Namespace) -> int:
    """The feedback job's sync steps only (no feedback claimed, no model)."""
    from . import wiring
    from .gate_feedback import sync_all
    from .locks import LockBusy, file_lock, lock_path
    cfg, conn = _open(args)
    try:
        with file_lock(lock_path(cfg.home, "feedback"), blocking=True, timeout=120):
            summary, problems = sync_all(conn, cfg, notion=wiring.notion_sync(conn, cfg), kanban=wiring.kanban(cfg))
    except LockBusy:
        return _fail("BUSY", "a feedback gate run is in progress; retry shortly")
    _print(summary)
    return 1 if problems else 0


# --------------------------------------------------------------------------- where things are

def cmd_paths(args: argparse.Namespace) -> int:
    cfg = config.load(args.home)
    _print({"home": str(cfg.home), "framework_context": str(prefs.agents_path(cfg.home)),
            "rules_archive": str(prefs.archive_path(cfg.home)),
            "config": str(cfg.home / "config.toml"), "database": str(cfg.db_path),
            "language": cfg.language, "language_name": i18n.language_name(cfg.language), "timezone": cfg.tz})
    return 0


def cmd_prefs(args: argparse.Namespace) -> int:
    from . import templates
    cfg = config.load(args.home)
    if cfg.db_path.exists():
        cfg, _conn = _open(args)        # an upgraded database moves its rules here first
    if args.action == "path":
        print(prefs.agents_path(cfg.home))
        return 0
    if args.action == "example":
        try:
            print(templates.asset(f"examples/{args.name}.md"))
        except FileNotFoundError:
            return _fail("NOT_FOUND", f"no example {args.name!r}; available: academic")
        return 0
    text = prefs.show(cfg.home)
    print(f"# Managed framework context: {prefs.agents_path(cfg.home)}")
    for problem in prefs.problems(cfg.home):
        print(f"# problem: {problem}")
    print(text if text else "(managed AGENTS.md is missing; run the installer to deploy it)")
    return 0


def _config_value(key: str, raw: str) -> Any:
    default = config.DEFAULTS
    for part in key.split("."):
        if not isinstance(default, dict) or part not in default:
            raise ops.OpError("UNKNOWN_KEY", f"{key} is not a setting; `todo-manager config show` lists them")
        default = default[part]
    if isinstance(default, dict):
        raise ops.OpError("INVALID", f"{key} is a section; name one of its keys")
    if isinstance(default, bool):
        if raw.lower() not in ("true", "false", "1", "0", "yes", "no"):
            raise ops.OpError("INVALID", f"{key} is true or false")
        return raw.lower() in ("true", "1", "yes")
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError:
            raise ops.OpError("INVALID", f"{key} is a whole number") from None
    if isinstance(default, float):
        try:
            return float(raw)
        except ValueError:
            raise ops.OpError("INVALID", f"{key} is a number") from None
    if isinstance(default, list):
        text = raw.strip()
        if text.startswith("["):
            try:
                value = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ops.OpError("INVALID", f"{key}: {exc}") from None
            if not isinstance(value, list):
                raise ops.OpError("INVALID", f"{key} is a list")
            return value
        return [v.strip() for v in text.split(",") if v.strip()]
    return raw


def cmd_config(args: argparse.Namespace) -> int:
    cfg = config.load(args.home)
    if args.action == "show":
        _print(cfg.data)
        return 0
    if args.action == "get":
        value = cfg.get(args.key, KeyError)
        if value is KeyError:
            return _fail("UNKNOWN_KEY", f"{args.key} is not set; `todo-manager config show` lists the settings")
        _print({args.key: value})
        return 0
    cfg, conn = _open(args)
    try:
        value = _config_value(args.key, args.value)
    except ops.OpError as exc:
        return _fail(exc.code, exc.details)
    return _run(lambda: ops.config_set(conn, cfg, _ctx(conn, cfg, args), args.key, value))


# --------------------------------------------------------------------------- tasks / projects

def _search(conn: Any, words: str, statuses: tuple[str, ...], limit: int) -> list[dict[str, Any]]:
    terms = [w for w in words.split() if w]
    sql = f"SELECT * FROM tasks WHERE status IN ({','.join('?' * len(statuses))})"
    params: list[Any] = list(statuses)
    for term in terms:
        sql += (" AND (title LIKE ? ESCAPE '\\' OR COALESCE(next_step,'') LIKE ? ESCAPE '\\'"
                " OR COALESCE(summary,'') LIKE ? ESCAPE '\\' OR COALESCE(waiting_on,'') LIKE ? ESCAPE '\\')")
        like = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        params += [like] * 4
    sql += " ORDER BY status IN ('done','cancelled'), updated_at DESC LIMIT ?"
    return [dict(r) for r in conn.execute(sql, (*params, limit))]


def cmd_task(args: argparse.Namespace) -> int:
    cfg, conn = _open(args)
    if args.action == "list":
        statuses = tuple(args.status.split(",")) if args.status else None
        if args.search:
            rows = _search(conn, args.search, statuses or store.TASK_STATUSES, args.limit or 30)
        else:
            rows = store.list_tasks(conn, statuses, project_id=args.project, limit=args.limit)
        if args.json:
            _print(rows)
        else:
            for t in rows:
                print(_task_line(t))
        return 0
    if args.action == "show":
        t = store.get_task(conn, args.id)
        if t is None:
            return _fail("NOT_FOUND", args.id)
        t["evidence"] = [{k: e[k] for k in ("role", "source", "title", "url", "occurred_at", "id")}
                         for e in store.evidence_for(conn, "task", args.id)]
        t["agent_work"] = [delegation.summary_line(d) for d in delegation.for_task(conn, args.id)]
        t["deliveries"] = [{"delegation": d["id"], "delivery": v["id"], "kind": v["kind"], "seq": v["seq"],
                            "received_at": v["received_at"], "summary": (v["summary"] or "")[:160]}
                           for d in delegation.for_task(conn, args.id) for v in delegation.deliveries(conn, d["id"])]
        t["links"] = store.task_links(conn, args.id)
        t["field_meta"] = loads(t["field_meta"], {})
        t["notion_url"] = delegation._notion_url(conn, t)
        notes = conn.execute("SELECT notes_text FROM notion_pages WHERE entity_type='task' AND entity_id=?",
                             (args.id,)).fetchone()
        if notes and notes["notes_text"]:
            t["page_notes"] = notes["notes_text"]
        _print(t)
        return 0

    def act() -> dict[str, Any]:
        ctx = _ctx(conn, cfg, args)
        if args.action == "create":
            return ops.task_create(conn, cfg, ctx, title=args.title, project=args.project, fields=_parse_set(args.set),
                                   evidence=ops.parse_evidence(args.evidence, "origin"),
                                   follow_up_of=args.follow_up_of, reason=args.reason)
        if args.action == "link":
            return ops.task_link(conn, cfg, ctx, args.id, ops.parse_evidence(args.evidence, "context"), args.reason)
        if args.action == "rewrite-box":
            return ops.task_rewrite_box(conn, cfg, ctx, args.id)
        changes = _parse_set(args.set)
        if args.note:
            changes["change_note"] = args.note
        return ops.task_update(conn, cfg, ctx, args.id, changes=changes, based_on=args.based_on,
                               evidence=ops.parse_evidence(args.evidence, "context"),
                               override_human=args.override_human, closed_reason=args.closed_reason,
                               reason=args.reason)
    return _run(act)


def cmd_project(args: argparse.Namespace) -> int:
    cfg, conn = _open(args)
    if args.action == "list":
        for p in store.list_projects(conn, args.status.split(",") if args.status else None):
            print(f"{p['id']} v{p['version']} [{p['status']}] {p['title']}"
                  + (f" ({', '.join(p['repos'])})" if p["repos"] else ""))
        return 0
    if args.action == "show":
        p = store.get_project(conn, args.id)
        if p is None:
            return _fail("NOT_FOUND", args.id)
        p["tasks"] = [_task_line(t) for t in store.list_tasks(conn, store.TASK_STATUSES, project_id=args.id)]
        _print(p)
        return 0

    def act() -> dict[str, Any]:
        ctx = _ctx(conn, cfg, args)
        if args.action == "create":
            return ops.project_create(conn, cfg, ctx, title=args.title, fields=_parse_set(args.set),
                                      repos=args.repo or [], evidence=ops.parse_evidence(args.evidence, "context"),
                                      reason=args.reason)
        return ops.project_update(conn, cfg, ctx, args.id, changes=_parse_set(args.set), based_on=args.based_on,
                                  repos_add=args.repo, repos_remove=args.remove_repo,
                                  evidence=ops.parse_evidence(args.evidence, "context"), reason=args.reason)
    return _run(act)


# --------------------------------------------------------------------------- material, follow-ups, runs

def cmd_item(args: argparse.Namespace) -> int:
    cfg, conn = _open(args)

    def act() -> dict[str, Any]:
        ctx = _ctx(conn, cfg, args)
        if args.action == "dismiss":
            return ops.item_dismiss(conn, cfg, ctx, args.items, outcome=args.outcome, reason=args.reason,
                                    label=args.label)
        return ops.item_defer(conn, cfg, ctx, args.items, reason=args.reason, until=args.until)
    return _run(act)


def cmd_followup(args: argparse.Namespace) -> int:
    cfg, conn = _open(args)
    return _run(lambda: ops.followup_checked(conn, cfg, _ctx(conn, cfg, args), args.task, args.reason))


def cmd_digest(args: argparse.Namespace) -> int:
    cfg, conn = _open(args)
    return _run(lambda: ops.digest_done(conn, cfg, _ctx(conn, cfg, args), args.reason))


def cmd_run(args: argparse.Namespace) -> int:
    from . import runs
    cfg, conn = _open(args)
    if args.action == "done":
        return _run(lambda: ops.run_done(conn, cfg, args.id, args.note))
    run = runs.get(conn, args.id)
    if run is None:
        return _fail("NOT_FOUND", args.id)
    if args.context:
        text = runs.read_text(cfg.home, args.id, "context.md")
        if text is None:
            return _fail("NOT_FOUND", f"run {args.id} printed no context (it did not wake the agent)")
        print(text)
        return 0
    run["manifest"] = runs.read_json(cfg.home, args.id, "manifest.json")
    contexts = [f"run:{args.id}"] + [f"feedback:{f}" for f in (run["manifest"] or {}).get("feedback_ids") or []]
    run["commands"] = [{"command": r["command"], "context": r["context"], "result": loads(r["result"], {}),
                        "at": r["created_at"]}
                       for r in conn.execute(f"SELECT * FROM op_log WHERE context IN ({','.join('?' * len(contexts))})"
                                             " ORDER BY created_at", contexts)]
    run["decisions"] = [dict(r) for r in conn.execute("SELECT op, target_type, target_id, category, rationale,"
                                                        " source_item_ids, created_at FROM decisions WHERE run_id=?"
                                                        " ORDER BY created_at", (args.id,))]
    run["claimed_now"] = {
        "items": [r["id"] for r in conn.execute(
            "SELECT s.id FROM source_items s JOIN batches b ON b.id=s.batch_id WHERE b.claimed_run=?"
            " AND b.state='claimed' AND s.state='batched'", (args.id,))],
        "feedback": [r["id"] for r in conn.execute(
            "SELECT id FROM feedback WHERE claimed_run=? AND status='pending'", (args.id,))]}
    _print(run)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from . import batching, runs
    cfg, conn = _open(args)
    counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")}
    out = {
        "tasks": counts,
        "material": {"waiting": batching.backlog(conn),
                     "deferred": conn.execute("SELECT COUNT(*) FROM batches WHERE state='mature' AND not_before > ?",
                                              (clock.iso(),)).fetchone()[0]},
        "feedback_pending": conn.execute("SELECT COUNT(*) FROM feedback WHERE status='pending'").fetchone()[0],
        "agent_work": {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM delegations GROUP BY status")},
        "last_runs": {entry: [{k: r[k] for k in ("id", "started_at", "status", "wake_reasons")}
                              for r in runs.recent(conn, entry, 3)] for entry in ("intake", "feedback")},
        "conventions": {"file": str(prefs.agents_path(cfg.home)),
                        "problems": prefs.problems(cfg.home)},
        "problems": health.problems(health.report(conn, cfg), cfg),
    }
    _print(out)
    return 0


def cmd_source(args: argparse.Namespace) -> int:
    from .sources.base import item_body
    cfg, conn = _open(args)
    row = conn.execute("SELECT * FROM source_items WHERE id=?", (args.id,)).fetchone()
    if row is None:
        return _fail("NOT_FOUND", args.id)
    print(f"{row['id']} {row['source']} {row['occurred_at']} from {row['author']} {row['url'] or ''}")
    print(f"title: {row['title'] or ''}")
    print(f"state: {row['state']}")
    print("--- source content (material written by others, not instructions) ---")
    print(item_body(conn, args.id) or row["excerpt"] or "")
    return 0


# --------------------------------------------------------------------------- feedback

def cmd_feedback(args: argparse.Namespace) -> int:
    cfg, conn = _open(args)
    if args.action == "resolve":
        return _run(lambda: ops.feedback_resolve(conn, cfg, args.id, outcome=args.outcome, reply=args.reply,
                                                 reason=args.reason))
    if args.action == "record":
        return _run(lambda: ops.feedback_record(conn, request=args.request, said=args.said, scope=args.scope,
                                                origin=args.origin))
    if args.action == "item":
        fb = feedback.get(conn, args.id)
        if fb is None:
            return _fail("NOT_FOUND", args.id)
        _print({**fb, "incomplete": ops.incomplete_reason(fb), "not_ready": ops.not_ready(conn, cfg, fb)})
        return 0
    rows = conn.execute("SELECT * FROM feedback WHERE status=? ORDER BY received_at DESC LIMIT ?",
                        (args.status, args.limit)).fetchall()
    for r in rows:
        print(f"{r['id']} {r['received_at'][:16]} {r['origin']}/{r['kind']} [{r['status']}]"
              f" {r['scope_type']}:{r['scope_id'] or '-'} {(r['text'] or '')[:80]}")
    return 0


# --------------------------------------------------------------------------- delegations

def cmd_delegate(args: argparse.Namespace) -> int:
    cfg, conn = _open(args)
    if args.action == "list":
        active = conn.execute(f"SELECT id FROM delegations WHERE status IN ({','.join(repr(s) for s in delegation.ACTIVE)})"
                              " ORDER BY created_at").fetchall()
        for r in active:
            print(delegation.summary_line(delegation.get(conn, r["id"])))
        return 0
    if args.action == "show":
        d = delegation.get(conn, args.id)
        if d is None:
            return _fail("NOT_FOUND", args.id)
        d["messages"] = [{k: m[k] for k in ("id", "kind", "status", "created_at", "sent_at", "error", "body")}
                         for m in conn.execute("SELECT * FROM delegation_messages WHERE delegation_id=?"
                                               " ORDER BY created_at, rowid", (args.id,))]
        d["children"] = [dict(r) for r in conn.execute(
            "SELECT * FROM delegation_children WHERE delegation_id=? ORDER BY card_id", (args.id,))]
        d["deliveries"] = delegation.deliveries(conn, args.id)
        d["approvals"] = [dict(a) for a in conn.execute("SELECT * FROM approvals WHERE delegation_id=?"
                                                         " ORDER BY granted_at", (args.id,))]
        _print(d)
        return 0
    if args.action == "release-hold":
        _print({"released": delegation.release_hold(conn)})
        return 0

    def act() -> dict[str, Any]:
        ctx = _ctx(conn, cfg, args)
        if args.action == "create":
            return ops.delegate_create(conn, cfg, ctx, task_id=args.task, title=args.title, goal=args.goal,
                                       context_text=args.context, constraints=args.constraints,
                                       deliverable=args.deliverable, done_criteria=args.done,
                                       allowed=args.allow.split(",") if args.allow else None,
                                       parallel=args.parallel, submit=not args.no_submit)
        if args.action == "message":
            return ops.delegate_message(conn, cfg, ctx, args.id, kind=args.kind, body=args.body,
                                        delivery=args.delivery)
        if args.action == "answer":
            return ops.delegate_answer(conn, cfg, ctx, args.id, body=args.body, approve=args.approve, question=args.question)
        return ops.delegate_approve(conn, cfg, ctx, args.id, delivery=args.delivery, action=args.action_name,
                                    detail=args.detail)
    return _run(act)


# --------------------------------------------------------------------------- notion

def cmd_notion(args: argparse.Namespace) -> int:
    from .notion import setup
    from .notion.client import NotionClient
    cfg, conn = _open(args)
    if args.action == "plan":
        for n, step in enumerate(setup.plan(args.parent, args.home_page), 1):
            print(f"{n}. {step}")
        return 0
    client = NotionClient.from_config(cfg)
    if args.action == "whoami":
        me = client.me()
        people = [u for u in client.users() if u.get("type") == "person"]
        _print({"bot": {"id": me.get("id"), "name": me.get("name")}, "workspace_people": len(people),
                "people": [{"id": u["id"], "name": u.get("name")} for u in people]})
        return 0
    if not (args.parent or args.home_page):
        return _fail("INVALID", "pass --home-page <existing page id> or --parent <page id>")
    if not args.yes:
        return _fail("CONFIRM", "notion setup writes to the production workspace; review `notion plan` and pass --yes")
    _print(setup.apply(conn, client, args.parent, args.home_page))
    return 0


# --------------------------------------------------------------------------- operations

def cmd_health(args: argparse.Namespace) -> int:
    cfg, conn = _open(args)
    rep = health.report(conn, cfg)
    if args.json:
        _print(rep)
    else:
        print("OK" if not rep["problems"] else "PROBLEMS:")
        for p in rep["problems"]:
            print(f"- {p}")
        for note in rep.get("notes") or []:
            print(f"note: {note}")
        print(f"outbox {rep['outbox']} feedback {rep['feedback']} delegations {rep['delegations']}")
        for entry, info in rep["runs"].items():
            print(f"{entry}: last {info['last']} last_wake {info['last_wake']}")
    return 1 if rep["problems"] else 0


def cmd_metrics(args: argparse.Namespace) -> int:
    cfg, conn = _open(args)
    out = health.metrics(conn, args.days)
    install = cfg.home / "state" / "hermes" / "install.json"
    job_ids = [j for j in (loads(install.read_text(encoding="utf-8"), {}).get("jobs") or {}).values() if j] \
        if install.exists() else []
    out["hermes_usage"] = health.usage(cfg, job_ids, args.days)
    _print(out)
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    cfg, conn = _open(args)
    kind = "project" if args.id.startswith("prj_") else "task"
    for h in store.history(conn, kind, args.id):
        print(f"v{h['version']} {h['at'][:16]} {h['actor']}/{h['origin']} {h['cause_ref'] or ''}"
              f" {dumps(h['changes'])[:400]}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from . import export
    cfg, conn = _open(args)
    root = export.export_snapshot(conn, cfg)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    result: dict[str, Any] = {"export": str(root), "integrity": manifest["integrity"],
                              "artifact_problems": manifest["artifact_problems"]}
    if args.verify:
        result["rebuild"] = export.verify_rebuild(root / "data.json")
    _print(result)
    return 0 if not manifest["integrity"] and (not args.verify or result["rebuild"]["ok"]) else 1


def cmd_backup(args: argparse.Namespace) -> int:
    from . import export
    cfg, conn = _open(args)
    _print({"backup": str(export.backup(conn, cfg, keep=args.keep))})
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    from . import export
    from .locks import file_lock, lock_path
    cfg, conn = _open(args)
    path = Path(args.file).expanduser()
    if not args.yes:
        _print({"would_restore": str(path), "backup": export.check_backup(path),
                "note": "this REPLACES the live database and the workspace files kept with the backup (config.toml,"
                        " AGENTS.md, rules-archive.md); a safety backup of both is made first; re-run with --yes"})
        return EXIT_INVALID
    with file_lock(lock_path(cfg.home, "intake"), blocking=True, timeout=300), \
            file_lock(lock_path(cfg.home, "feedback"), blocking=True, timeout=300):
        _print(export.restore(conn, cfg, path))
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    from . import export
    try:
        report = export.import_bundle(Path(args.bundle), Path(args.into))
    except export.ImportError_ as exc:
        return _fail("INVALID", str(exc))
    report["next"] = ("set the secrets again (environment / token files), check config.toml, run "
                      "`todo-manager --home <new> health`, check the Kanban board, then "
                      "`todo-manager --home <new> delegate release-hold`")
    _print(report)
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    from . import migration
    cfg, conn = _open(args)
    if args.action == "fetch":
        from .notion.client import NotionClient
        root = migration.fetch_old(NotionClient.from_config(cfg), cfg, args.tasks_ds, args.projects_ds)
        _print({"fetched": str(root), "next": f"todo-manager migrate propose {root}"})
        return 0
    root = Path(args.dir).expanduser()
    if args.action == "propose":
        _print({**migration.propose(root, cfg, set(args.bot_id or [])), "review": str(root / "summary.md")})
        return 0
    resolver = None
    if args.resolve_threads:
        from .sources.registry import conversation_sources
        mailboxes = [a for a in conversation_sources(cfg) if a.name == "gmail"]

        def resolver(old_id: str) -> str | None:
            for box in mailboxes:
                key = box.thread_key_for(old_id)
                if key:
                    return key
            return None
    try:
        if not args.yes:
            _print({"preview": migration.preview(conn, cfg, root, resolve_thread=resolver),
                    "note": "nothing was written; re-run with --yes to import into the local database"})
            return 0
        _print(migration.apply(conn, cfg, root, dry_run=False, resolve_thread=resolver))
    except ValueError as exc:
        return _fail("INVALID", str(exc))
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    from .install import init_workspace
    language = i18n.normalize(args.language) if args.language else i18n.from_environment()
    if args.language and not language:
        raise SystemExit(f"unsupported language {args.language!r}; choose one of {', '.join(i18n.LANGUAGES)}")
    _print(init_workspace(args.home, language))
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    import shlex

    from . import config_file, install
    if args.language and not i18n.normalize(args.language):
        raise SystemExit(f"unsupported language {args.language!r}; choose one of {', '.join(i18n.LANGUAGES)}")
    answers: dict[str, str] = {}
    for pair in args.answer or []:
        key, sep, value = pair.partition("=")
        if not sep:
            return _fail("INVALID", f"--answer expects key=value, got {pair!r}")
        answers[key.strip()] = value.strip()
    unknown = sorted((set(args.yes or []) | set(args.no or [])) - {*install.STEPS, "all"})
    if unknown:
        return _fail("INVALID", f"unknown step(s) {', '.join(unknown)}; steps: {', '.join(install.STEPS)}, all")
    interactive = sys.stdin.isatty() and not (answers or args.yes or args.no)
    prompter = install.TerminalPrompter() if interactive else install.AnswerPrompter(
        answers, set(args.yes or []), set(args.no or []), output=lambda _line: None)
    run = install.Install(args.home, prompter, install.Env(), language=args.language)
    try:
        report = run.run()
    except (install.NeedsAnswer, install.NeedsConfirmation) as exc:
        prior = (["--home", args.home] if args.home else []) + (["--language", args.language] if args.language else [])
        prior += [x for k, v in answers.items() for x in ("--answer", f"{k}={v}")]
        prior += [x for step in args.yes or [] for x in ("--yes", step)]
        prior += [x for step in args.no or [] for x in ("--no", step)]
        out = {**exc.to_json(shlex.join(prior)), "done": run.report}
        if interactive:
            print(f"stopped: {exc}")
        else:
            _print(out)
        return EXIT_NEEDS_INPUT
    except (EOFError, KeyboardInterrupt):
        print("\nstopped; what was done so far is kept, run `todo-manager install` again to continue")
        return EXIT_NEEDS_INPUT
    except (install.InstallError, config_file.ConfigEditError) as exc:
        return _fail("INSTALL", [*run.report, str(exc)])
    if interactive:
        print("install complete; `todo-manager health` shows how it runs")
    else:
        _print({"status": "complete", "done": report})
    return 0


# --------------------------------------------------------------------------- parser

def _evidence(parser: argparse.ArgumentParser, default: str) -> None:
    parser.add_argument("--evidence", action="append", metavar="ITEM[:ROLE]",
                        help=f"a source item this is based on (repeatable); roles: {', '.join(store.EVIDENCE_ROLES)};"
                             f" default {default}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="todo-manager", description="Personal Todo Manager (Notion + Hermes)")
    p.add_argument("--home", help="runtime workspace (default: $TODO_HOME or ~/.hermes/workspace/todo-manager)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("install", help="install or update everything (settings inferred from Hermes); re-runnable")
    s.add_argument("--language", help="en or zh-CN for a new workspace (default: Hermes' display.language)")
    s.add_argument("--answer", action="append", metavar="KEY=VALUE",
                   help="answer a question without the terminal, e.g. notion.home_page=<page id or URL>")
    s.add_argument("--yes", action="append", metavar="STEP", help="confirm a step's changes (or: all)")
    s.add_argument("--no", action="append", metavar="STEP", help="decline a step's changes")
    s.set_defaults(fn=cmd_install)

    s = sub.add_parser("init", help="create only the runtime workspace and database (install does this too)")
    s.add_argument("--language", help="en or zh-CN (default: from TODO_MANAGER_LANG / the system locale)")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("gate", help="Hermes cron pre-run gate (prints the run's context or the idle line)")
    s.add_argument("entry", choices=("intake", "feedback"))
    s.set_defaults(fn=cmd_gate)

    s = sub.add_parser("sync", help="run the Notion/Kanban/outbox sync now (no model)")
    s.set_defaults(fn=cmd_sync)

    s = sub.add_parser("paths", help="where the workspace, conventions and settings are")
    s.set_defaults(fn=cmd_paths)

    s = sub.add_parser("prefs", help="legacy preferences archive / managed workspace context; personal preferences use native memory")
    s.add_argument("action", nargs="?", default="show", choices=("show", "path", "example"))
    s.add_argument("name", nargs="?", default="academic", help="example name (example)")
    s.set_defaults(fn=cmd_prefs)

    s = sub.add_parser("config", help="show or change config.toml settings (sources, quiet windows, sizes)")
    cs = s.add_subparsers(dest="action", required=True)
    cs.add_parser("show")
    x = cs.add_parser("get")
    x.add_argument("key")
    x = cs.add_parser("set")
    x.add_argument("key", help="dotted key, e.g. slack.quiet_minutes")
    x.add_argument("value", help="lists: comma separated or JSON")
    _add_ctx(x)
    s.set_defaults(fn=cmd_config)

    s = sub.add_parser("task", help="tasks")
    ts = s.add_subparsers(dest="action", required=True)
    x = ts.add_parser("list")
    x.add_argument("--status", help="comma list (default: open; with --search: all)")
    x.add_argument("--search", help="words that must all appear (title, next step, summary, waiting on)")
    x.add_argument("--project")
    x.add_argument("--limit", type=int)
    x.add_argument("--json", action="store_true")
    x = ts.add_parser("show")
    x.add_argument("id")
    x = ts.add_parser("create")
    x.add_argument("--title", required=True)
    x.add_argument("--project")
    x.add_argument("--set", action="append", metavar="FIELD=VALUE", help=f"fields: {', '.join(store.TASK_FIELDS)}")
    _evidence(x, "origin")
    x.add_argument("--follow-up-of", help="the closed task this continues")
    x.add_argument("--reason", help="one line: why (kept with the decision)")
    _add_ctx(x)
    x = ts.add_parser("update")
    x.add_argument("id")
    x.add_argument("--set", action="append", metavar="FIELD=VALUE", help="repeatable; an empty value clears")
    x.add_argument("--note", help="change note shown in Notion")
    x.add_argument("--based-on", type=int, help="the task version you read (required in a run)")
    _evidence(x, "context")
    x.add_argument("--override-human", action="store_true", help="change a field the user set (needs --evidence)")
    x.add_argument("--closed-reason", help="why the task is closed (with status done/cancelled)")
    x.add_argument("--reason")
    _add_ctx(x)
    x = ts.add_parser("link", help="link evidence to a task without changing it")
    x.add_argument("id")
    _evidence(x, "context")
    x.add_argument("--reason")
    _add_ctx(x)
    x = ts.add_parser("rewrite-box", help="let the Todo-Manager box of the page be written again (after a clarified"
                                          " edit inside it)")
    x.add_argument("id")
    _add_ctx(x)
    s.set_defaults(fn=cmd_task)

    s = sub.add_parser("project", help="projects")
    ps = s.add_subparsers(dest="action", required=True)
    x = ps.add_parser("list")
    x.add_argument("--status")
    x = ps.add_parser("show")
    x.add_argument("id")
    x = ps.add_parser("create")
    x.add_argument("--title", required=True)
    x.add_argument("--repo", action="append")
    x.add_argument("--set", action="append", metavar="FIELD=VALUE", help="domain, goal, next_step, status")
    _evidence(x, "context")
    x.add_argument("--reason")
    _add_ctx(x)
    x = ps.add_parser("update")
    x.add_argument("id")
    x.add_argument("--set", action="append", metavar="FIELD=VALUE", help=f"fields: {', '.join(store.PROJECT_FIELDS)}")
    x.add_argument("--based-on", type=int)
    x.add_argument("--repo", action="append")
    x.add_argument("--remove-repo", action="append")
    _evidence(x, "context")
    x.add_argument("--reason")
    _add_ctx(x)
    s.set_defaults(fn=cmd_project)

    s = sub.add_parser("item", help="source material that needs no task, or not yet")
    its = s.add_subparsers(dest="action", required=True)
    x = its.add_parser("dismiss", help="no task for these items")
    x.add_argument("items", nargs="+")
    x.add_argument("--outcome", required=True, choices=ops.OUTCOMES)
    x.add_argument("--label", help="your own name for the outcome (e.g. one a convention names)")
    x.add_argument("--reason", required=True)
    _add_ctx(x)
    x = its.add_parser("defer", help="decide later: offered again with new activity or at --until")
    x.add_argument("items", nargs="+")
    x.add_argument("--reason", required=True)
    x.add_argument("--until", help=f"YYYY-MM-DD (default: in {ops.DEFER_DAYS} days)")
    _add_ctx(x)
    s.set_defaults(fn=cmd_item)

    s = sub.add_parser("followup", help="due follow-ups")
    fs = s.add_subparsers(dest="action", required=True)
    x = fs.add_parser("checked", help="a due follow-up that needs no change today")
    x.add_argument("task")
    x.add_argument("--reason")
    _add_ctx(x)
    s.set_defaults(fn=cmd_followup)

    s = sub.add_parser("digest", help="the daily commit digest")
    gs = s.add_subparsers(dest="action", required=True)
    x = gs.add_parser("done", help="the digest of this run is reflected in the projects")
    x.add_argument("--reason")
    _add_ctx(x)
    s.set_defaults(fn=cmd_digest)

    s = sub.add_parser("run", help="cron runs")
    rs = s.add_subparsers(dest="action", required=True)
    x = rs.add_parser("show", help="what a run was given and what it recorded (diagnostics)")
    x.add_argument("id")
    x.add_argument("--context", action="store_true", help="print the context the run's agent was shown")
    x = rs.add_parser("done", help="end of a run: what it left unhandled is offered again")
    x.add_argument("id")
    x.add_argument("--note")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("status", help="tasks, waiting material, feedback, agent work, last runs, problems")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("feedback", help="feedback from Notion and chats")
    fs = s.add_subparsers(dest="action", required=True)
    x = fs.add_parser("resolve", help="close a feedback item (after acting on it with --feedback)")
    x.add_argument("id")
    x.add_argument("--outcome", required=True, choices=ops.FEEDBACK_OUTCOMES)
    x.add_argument("--reply", help="short reply posted in the same Notion discussion (clarify: the question)")
    x.add_argument("--reason")
    x = fs.add_parser("record", help="store what the user said for the next feedback run")
    x.add_argument("--request", required=True)
    x.add_argument("--said", required=True)
    x.add_argument("--scope", help="task:<id> | project:<id> | global")
    x.add_argument("--origin", default="matrix", choices=("matrix", "cli"))
    x = fs.add_parser("item", help="one feedback item in full (text and payload)")
    x.add_argument("id")
    x = fs.add_parser("list")
    x.add_argument("--status", default="pending", choices=("pending", *feedback.FINAL))
    x.add_argument("--limit", type=int, default=30)
    s.set_defaults(fn=cmd_feedback)

    s = sub.add_parser("delegate", help="agent work on the Hermes Kanban board")
    ds = s.add_subparsers(dest="action", required=True)
    ds.add_parser("list")
    x = ds.add_parser("show")
    x.add_argument("id")
    x = ds.add_parser("create")
    x.add_argument("--task", required=True)
    x.add_argument("--title", required=True)
    x.add_argument("--goal", required=True)
    x.add_argument("--context")
    x.add_argument("--constraints")
    x.add_argument("--deliverable")
    x.add_argument("--done")
    x.add_argument("--allow", help="comma list of allowed actions (default read,analyze,draft)")
    x.add_argument("--parallel", action="store_true",
                   help="genuinely separate work next to the task's active agent work (default: refused)")
    x.add_argument("--no-submit", action="store_true", help="leave the card to the next sync")
    _add_ctx(x)
    x = ds.add_parser("message")
    x.add_argument("id")
    x.add_argument("--kind", required=True, choices=ops.MESSAGE_KINDS)
    x.add_argument("--body")
    x.add_argument("--delivery", help="accept/revision: the delivered version meant (default: the one replied to,"
                                      " else the latest)")
    _add_ctx(x)
    x = ds.add_parser("answer", help="answer the agent's open question")
    x.add_argument("--question", help="delivery ID of the exact root or child question")
    x.add_argument("id")
    x.add_argument("--body", required=True)
    x.add_argument("--approve", action="append", metavar="ACTION[:DETAIL]",
                   help="an external action the user allows with this answer (repeatable), e.g."
                        " send_email:'to Alice only'")
    _add_ctx(x)
    x = ds.add_parser("approve", help="approve an external action for a delivered version")
    x.add_argument("id")
    x.add_argument("--delivery", required=True)
    x.add_argument("--action", dest="action_name", required=True)
    x.add_argument("--detail")
    _add_ctx(x)
    ds.add_parser("release-hold", help="resume card submissions and messages after a restore or import")
    s.set_defaults(fn=cmd_delegate)

    s = sub.add_parser("notion", help="Notion workspace setup")
    ns = s.add_subparsers(dest="action", required=True)
    x = ns.add_parser("plan", help="print the setup writes without performing them")
    x.add_argument("--parent", default="<parent-page-id>")
    x.add_argument("--home-page", help="use this existing page as the home page")
    ns.add_parser("whoami", help="show the integration bot and workspace members (read-only)")
    x = ns.add_parser("setup", help="create home page, databases and views (production write)")
    g = x.add_mutually_exclusive_group()
    g.add_argument("--home-page", help="existing page (shared with the integration) to use as the home page")
    g.add_argument("--parent", help="create a new home page under this page")
    x.add_argument("--yes", action="store_true")
    s.set_defaults(fn=cmd_notion)

    s = sub.add_parser("health", help="freshness, queues and stuck work")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_health)
    s = sub.add_parser("metrics", help="runs, wakes and outcomes over N days")
    s.add_argument("--days", type=int, default=7)
    s.set_defaults(fn=cmd_metrics)
    s = sub.add_parser("history", help="field-level history of a task/project")
    s.add_argument("id")
    s.set_defaults(fn=cmd_history)
    s = sub.add_parser("source", help="show a stored source item in full")
    s.add_argument("action", choices=("show",))
    s.add_argument("id")
    s.set_defaults(fn=cmd_source)
    s = sub.add_parser("export", help="Markdown + JSON snapshot under TODO_HOME/exports")
    s.add_argument("--verify", action="store_true", help="also rebuild a scratch DB from the export and compare")
    s.set_defaults(fn=cmd_export)
    s = sub.add_parser("backup", help="online SQLite backup (plus config.toml, AGENTS.md) under TODO_HOME/backups")
    s.add_argument("--keep", type=int, help="keep only the newest N backups")
    s.set_defaults(fn=cmd_backup)
    s = sub.add_parser("restore", help="replace the live DB and workspace files with a backup (safety backup first)")
    s.add_argument("file")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(fn=cmd_restore)
    s = sub.add_parser("import", help="build a new workspace from an export (into an empty directory)")
    s.add_argument("bundle", help="an exports/<stamp> directory")
    s.add_argument("--into", required=True, help="new workspace directory (absent or empty)")
    s.set_defaults(fn=cmd_import)

    s = sub.add_parser("migrate", help="proposal-based migration from the old Notion Todo Manager")
    ms = s.add_subparsers(dest="action", required=True)
    x = ms.add_parser("fetch", help="read-only copy of the old data sources into TODO_HOME/migration/")
    x.add_argument("--tasks-ds", required=True)
    x.add_argument("--projects-ds", required=True)
    x = ms.add_parser("propose", help="classify old pages; writes summary.md, tasks.csv, projects.csv")
    x.add_argument("dir")
    x.add_argument("--bot-id", action="append", help="old automation's Notion bot user id (if not in meta.json)")
    x = ms.add_parser("apply", help="import the reviewed proposal (preview unless --yes)")
    x.add_argument("dir")
    x.add_argument("--yes", action="store_true")
    x.add_argument("--resolve-threads", action="store_true", help="look up Gmail thread ids (read-only)")
    s.set_defaults(fn=cmd_migrate)

    from . import hermes_install
    hermes_install.add_parser(sub)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except config.ConfigError as exc:
        return _fail("CONFIG", str(exc))
    finally:
        while _CONNECTIONS:
            _CONNECTIONS.pop().close()


if __name__ == "__main__":
    sys.exit(main())
