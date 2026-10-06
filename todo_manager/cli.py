"""``todo-manager`` command line (stdlib only).

The gates print the context of a Hermes cron run (or ``{"wakeAgent": false}``) and nothing
else; a gate that fails exits 1 so that Hermes reports a failed script. Every other command
prints structured business JSON or administrative summaries (--json for automation); a rejected call exits with status 2 and a JSON body
``{"error": CODE, "details": [...]}`` the agent can act on. Runtime data lives in TODO_HOME
(default ``~/.hermes/workspace/todo-manager``), never in the source tree.

Commands that change Todo data take the context they act in (see ``ops``): ``--run`` (a cron
run), ``--feedback`` (the user's Notion feedback item), ``--request ID --said TEXT`` (the user's
chat request) or --human (explicit manual invocation).
"""

from __future__ import annotations

import argparse
import json
import sys
import sqlite3
from pathlib import Path
from typing import Any

from . import clock, config, db, delegation, feedback, health, i18n, ops, prefs, store
from .db import dumps, loads
from .locks import LockBusy

EXIT_INVALID = 2
EXIT_NEEDS_INPUT = 3   # `install` stopped at a question or a confirmation (see its JSON)
_CONNECTIONS: list[Any] = []


# --------------------------------------------------------------------------- helpers

def _print(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=1, default=str))


def _fail(code: str, details: list[str] | str) -> int:
    _print({"error": code, "details": [details] if isinstance(details, str) else details})
    return EXIT_INVALID


def _inspection(args: argparse.Namespace) -> bool:
    return (getattr(args, 'command', '') == 'status' or
            getattr(args, 'action', '') in ('list', 'show', 'get'))


def _admin_print(args: argparse.Namespace, data: dict[str, Any]) -> None:
    if getattr(args, 'json', False):
        _print(data)
    else:
        for key, value in data.items():
            print(f"{key}: {json.dumps(value, ensure_ascii=False, default=str)}")


def cmd_control(args: argparse.Namespace) -> int:
    from . import hermes_install
    cfg = config.load(args.home)
    if getattr(args, 'release_recovery_hold', False):
        if not args.yes:
            return _fail('CONFIRM', 'inspect the board and uncertain actions, then use resume --release-recovery-hold --yes')
        cfg, conn = _open(args)
        _admin_print(args, {'released': delegation.release_hold(conn), 'cron': 'unchanged'})
        return 0
    inst = hermes_install.Installer(cfg, hermes_install.Paths.from_config(cfg))
    _admin_print(args, {'cron': inst.set_jobs_state(args.command), 'workers': 'unchanged', 'recovery_hold': 'unchanged'})
    return 0


def _open(args: argparse.Namespace) -> tuple[config.Config, Any]:
    cfg = config.load(args.home)
    try:
        conn = db.open_existing(cfg.db_path, readonly=_inspection(args))
    except RuntimeError as exc:
        raise ops.OpError("WORKSPACE", str(exc)) from exc
    _CONNECTIONS.append(conn)
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
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--human", action="store_true", help="explicit manual human invocation (provenance, not access control)")
    g.add_argument("--run", help="the cron run this is part of")
    g.add_argument("--feedback", dest="feedback_id", help="the user's feedback item this carries out")
    g.add_argument("--request", help="id of the user's chat request (Matrix event id, or one id per request)")
    parser.add_argument("--said", help="with --request: the user's words")
    parser.add_argument("--scope", help="with --request: task:<id> | project:<id> | global")
    parser.add_argument("--key", help="explicit identity of this command (a retry with the same key applies once)")


def _ctx(conn: Any, cfg: config.Config, args: argparse.Namespace) -> ops.Ctx:
    return ops.context(conn, cfg, run=args.run, feedback_id=args.feedback_id, request=args.request, said=args.said,
                       scope=args.scope, key=args.key, human=args.human)


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
        _print(rows)
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
        if args.history:
            t["history"] = store.history(conn, "task", args.id)
        _print(t)
        return 0

    def act() -> dict[str, Any]:
        ctx = _ctx(conn, cfg, args)
        if args.action == "create":
            return ops.task_create(conn, cfg, ctx, title=args.title, project=args.project, fields=_parse_set(args.set),
                                   evidence=ops.parse_evidence(args.evidence, "origin"),
                                   follow_up_of=args.follow_up_of, reason=args.reason)
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
        _print(store.list_projects(conn, args.status.split(",") if args.status else None))
        return 0
    if args.action == "show":
        p = store.get_project(conn, args.id)
        if p is None:
            return _fail("NOT_FOUND", args.id)
        p["tasks"] = [_task_line(t) for t in store.list_tasks(conn, store.TASK_STATUSES, project_id=args.id)]
        if args.history:
            p["history"] = store.history(conn, "project", args.id)
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


def cmd_run(args: argparse.Namespace) -> int:
    from . import runs
    cfg, conn = _open(args)
    if args.action == "done":
        return _run(lambda: ops.run_done(conn, cfg, args.id, args.note, checked=args.checked, digest_reviewed=args.digest_reviewed))
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
    from . import hermes_install
    out['installation'] = hermes_install.status(cfg)
    if args.details:
        out['paths'] = {'home': str(cfg.home), 'database': str(cfg.db_path), 'language': cfg.language}
        out['health'] = health.report(conn, cfg)
        out['metrics'] = health.metrics(conn, args.days)
        manifest = hermes_install.load_manifest(hermes_install.Paths.from_config(cfg))
        out['usage'] = health.usage(cfg, [j['id'] for j in (manifest.get('jobs') or {}).values() if j.get('id')], args.days)
    _admin_print(args, out)
    return 1 if out['problems'] else 0


def cmd_source(args: argparse.Namespace) -> int:
    if args.action != "show":
        return cmd_item(args)
    from .sources.base import item_body
    cfg, conn = _open(args)
    row = conn.execute("SELECT * FROM source_items WHERE id=?", (args.id,)).fetchone()
    if row is None:
        return _fail("NOT_FOUND", args.id)
    _print({**dict(row), "body": item_body(conn, args.id) or row['excerpt'] or '',
            "content_role": "untrusted source material"})
    return 0


# --------------------------------------------------------------------------- feedback

def cmd_feedback(args: argparse.Namespace) -> int:
    cfg, conn = _open(args)
    if args.action == "resolve":
        return _run(lambda: ops.feedback_resolve(conn, cfg, args.id, outcome=args.outcome, reply=args.reply,
                                                 reason=args.reason, settles=args.settles))
    if args.action == "record":
        return _run(lambda: ops.feedback_record(conn, request=args.request, said=args.said, scope=args.scope,
                                                origin=args.origin))
    if args.action == "show":
        fb = feedback.get(conn, args.id)
        if fb is None:
            return _fail("NOT_FOUND", args.id)
        _print({**fb, "incomplete": ops.incomplete_reason(fb), "not_ready": ops.not_ready(conn, cfg, fb)})
        return 0
    rows = conn.execute("SELECT * FROM feedback WHERE status=? ORDER BY received_at DESC LIMIT ?",
                        (args.status, args.limit)).fetchall()
    _print([feedback.get(conn, r["id"]) for r in rows])
    return 0


# --------------------------------------------------------------------------- delegations

def cmd_delegate(args: argparse.Namespace) -> int:
    cfg, conn = _open(args)
    if args.action == "list":
        active = conn.execute(f"SELECT id FROM delegations WHERE status IN ({','.join(repr(s) for s in delegation.ACTIVE)})"
                              " ORDER BY created_at").fetchall()
        _print([delegation.get(conn, r["id"]) for r in active])
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

    def act() -> dict[str, Any]:
        ctx = _ctx(conn, cfg, args)
        if args.action == "create":
            task = store.get_task(conn, args.task)
            if task is None:
                raise ops.OpError('NOT_FOUND', args.task)
            if not (args.allow or '').strip():
                raise ops.OpError('INVALID', '--allow must explicitly name authorized actions')
            brief = Path(args.brief_file).read_text(encoding='utf-8') if args.brief_file else args.brief
            return ops.delegate_create(conn, cfg, ctx, task_id=args.task, title=args.title or task['title'], goal=brief,
                                       context_text=None, constraints=None,
                                       deliverable=None, done_criteria=None,
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


def cmd_restore(args: argparse.Namespace) -> int:
    import tempfile
    import sqlite3
    from . import export
    cfg = config.load(args.home)
    source = Path(args.file).expanduser().resolve()
    try:
        info = export.check_bundle(source) if source.is_dir() else export.check_backup(source)
        if not source.is_dir() and (info['integrity'] != 'ok' or not info['schema_version']
                                   or info['schema_version'] > db.SCHEMA_VERSION):
            raise ValueError('backup integrity/schema is not compatible')
        existing = cfg.home.exists() and any(cfg.home.iterdir())
        if existing and not args.yes:
            _admin_print(args, {'would_restore': str(source), 'target': str(cfg.home),
                               'replacement': 'database and included workspace files; safety backup first',
                               'confirmation': 'repeat with --yes; native profile and cron are not restored'})
            return EXIT_INVALID
        if source.is_dir() and not existing:
            _admin_print(args, export.import_bundle(source, cfg.home))
            return 0
        with tempfile.TemporaryDirectory(prefix='todo-restore-') as tmp:
            if source.is_dir():
                staged = Path(tmp) / 'workspace'
                export.import_bundle(source, staged)
                staged_cfg = config.load(staged)
                staged_conn = db.open_existing(staged_cfg.db_path)
                try:
                    backup_source = export.backup(staged_conn, staged_cfg)
                finally:
                    staged_conn.close()
            else:
                staged = None
                backup_source = source
            cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(cfg.db_path), isolation_level=None)
            conn.row_factory = sqlite3.Row
            try:
                if not existing:
                    db.migrate(conn)
                result = export.restore(conn, cfg, backup_source)
                if staged:
                    import shutil
                    # Data/claims are already reconciled. Restore source text and run artifacts too.
                    for child in staged.iterdir():
                        if child.name in ('state', 'backups', 'exports'):
                            continue
                        if child.is_dir():
                            shutil.copytree(child, cfg.home / child.name, dirs_exist_ok=True)
                        else:
                            shutil.copy2(child, cfg.home / child.name)
                    result['coverage'] = 'export workspace files plus database; native profile excluded'
                _admin_print(args, result)
            finally:
                conn.close()
    except (export.ImportError_, ValueError, OSError, sqlite3.Error, RuntimeError) as exc:
        return _fail('RESTORE', str(exc))
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    import shlex

    from . import config_file, install
    if args.language and not i18n.normalize(args.language):
        return _fail("INVALID", f"unsupported language {args.language!r}; choose one of {', '.join(i18n.LANGUAGES)}")
    answers: dict[str, str] = {}
    for pair in args.answer or []:
        key, sep, value = pair.partition("=")
        if not sep:
            return _fail("INVALID", f"--answer expects key=value, got {pair!r}")
        answers[key.strip()] = value.strip()
    from .install_flow import complete
    interactive = sys.stdin.isatty() and not (answers or args.yes or args.json)
    prompter = install.TerminalPrompter() if interactive else install.AnswerPrompter(
        answers, {'all'} if args.yes else set(), output=lambda _: None)
    try:
        report = complete(args.home, prompter, language=args.language, confirmed=args.yes)
    except (install.NeedsAnswer, install.NeedsConfirmation) as exc:
        command = ['todo-manager'] + (["--home", args.home] if args.home else []) + ['install']
        command += (['--json'] if args.json else [])
        command += (["--language", args.language] if args.language else [])
        command += [x for k, v in answers.items() for x in ("--answer", f"{k}={v}")]
        result = exc.to_json()
        if isinstance(exc, install.NeedsConfirmation):
            result['confirm_with'] = shlex.join([*command, '--yes'])
            result.pop('decline_with', None)
        else:
            result['answer_with'] = shlex.join([*command, '--answer', f'{exc.key}=VALUE'])
        _print(result)
        return EXIT_NEEDS_INPUT
    except (install.InstallError, config_file.ConfigEditError) as exc:
        return _fail('INSTALL', str(exc))
    except (EOFError, KeyboardInterrupt):
        return EXIT_NEEDS_INPUT
    _admin_print(args, report)
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
    s.add_argument("--yes", action="store_true", help="confirm the combined installation changes; missing choices still stop")
    s.set_defaults(fn=cmd_install)

    s = sub.add_parser("sync", help="run the Notion/Kanban/outbox sync now (no model)")
    s.set_defaults(fn=cmd_sync)

    s = sub.add_parser("config", help="show or change config.toml settings (sources, quiet windows, sizes)")
    cs = s.add_subparsers(dest="action", required=True)
    x = cs.add_parser("show")
    x.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    x = cs.add_parser("get")
    x.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    x.add_argument("key")
    x = cs.add_parser("set")
    x.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
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
    x.add_argument("--history", action="store_true")
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
    s.set_defaults(fn=cmd_task)

    s = sub.add_parser("project", help="projects")
    ps = s.add_subparsers(dest="action", required=True)
    x = ps.add_parser("list")
    x.add_argument("--status")
    x = ps.add_parser("show")
    x.add_argument("id")
    x.add_argument("--history", action="store_true")
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

    s = sub.add_parser("source", help="read full source material, dismiss it or defer it")
    its = s.add_subparsers(dest="action", required=True)
    x = its.add_parser("show")
    x.add_argument("id")
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
    s.set_defaults(fn=cmd_source)

    s = sub.add_parser("run", help="cron runs")
    rs = s.add_subparsers(dest="action", required=True)
    x = rs.add_parser("show", help="what a run was given and what it recorded (diagnostics)")
    x.add_argument("id")
    x.add_argument("--context", action="store_true", help="print the context the run's agent was shown")
    x = rs.add_parser("done", help="end of a run: what it left unhandled is offered again")
    x.add_argument("id")
    x.add_argument("--note")
    x.add_argument("--checked", nargs="+", default=[])
    x.add_argument("--digest-reviewed", action="store_true")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("status", help="tasks, waiting material, feedback, agent work, last runs, problems")
    s.add_argument("--details", action="store_true")
    s.add_argument("--days", type=int, default=7)
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("feedback", help="feedback from Notion and chats")
    fs = s.add_subparsers(dest="action", required=True)
    x = fs.add_parser("resolve", help="close a feedback item (after acting on it with --feedback)")
    x.add_argument("id")
    x.add_argument("--outcome", required=True, choices=ops.FEEDBACK_OUTCOMES)
    x.add_argument("--reply", help="short reply posted in the same Notion discussion (clarify: the question)")
    x.add_argument("--reason")
    x.add_argument("--settles", help="ID of the clarified body-edit feedback this answer settles")
    x = fs.add_parser("record", help="store what the user said for the next feedback run")
    x.add_argument("--request", required=True)
    x.add_argument("--said", required=True)
    x.add_argument("--scope", help="task:<id> | project:<id> | global")
    x.add_argument("--origin", default="matrix", choices=("matrix", "cli"))
    x = fs.add_parser("show", help="one feedback item in full (text and payload)")
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
    x.add_argument("--title")
    brief = x.add_mutually_exclusive_group(required=True)
    brief.add_argument("--brief")
    brief.add_argument("--brief-file")
    x.add_argument("--allow", required=True, help="explicit comma list of authorized actions")
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
    s.set_defaults(fn=cmd_delegate)

    s = sub.add_parser("export", help="Markdown + JSON snapshot under TODO_HOME/exports")
    s.add_argument("--verify", action="store_true", help="also rebuild a scratch DB from the export and compare")
    s.set_defaults(fn=cmd_export)
    s = sub.add_parser("restore", help="rebuild from export or SQLite safety backup; existing targets require confirmation")
    s.add_argument("file")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(fn=cmd_restore)
    from . import hermes_install
    for command in ('pause', 'resume'):
        s = sub.add_parser(command, help=f'{command} the two Todo cron jobs only')
        s.add_argument('--yes', action='store_true')
        if command == 'resume':
            s.add_argument('--release-recovery-hold', action='store_true')
        s.set_defaults(fn=cmd_control)
    s = sub.add_parser('uninstall', help='remove confirmed owned automation; keep business data')
    s.add_argument('--yes', action='store_true')
    s.add_argument('--dry-run', action='store_true')
    s.set_defaults(fn=hermes_install.cmd_uninstall)
    for name, command in sub.choices.items():
        if name in {'install','status','config','pause','resume','sync','export','restore','uninstall'}:
            command.add_argument('--json', action='store_true', help='structured output')
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        from .locks import file_lock, lock_path
        import contextlib
        cfg = config.load(args.home)
        populated = cfg.home.exists() and any(cfg.home.iterdir())
        lock = (contextlib.nullcontext() if _inspection(args) or not populated else
                file_lock(lock_path(cfg.home, 'workspace'), blocking=True, timeout=120,
                          shared=args.command not in ('restore', 'install')))
        with lock:
            if args.command in {'config', 'sync', 'export'} and not getattr(args, 'json', False):
                import io
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = args.fn(args)
                if code:
                    print(output.getvalue(), end='')
                else:
                    value = json.loads(output.getvalue())
                    _admin_print(args, value if isinstance(value, dict) else {'result': value})
                return code
            return args.fn(args)
    except ops.OpError as exc:
        return _fail(exc.code, exc.details)
    except config.ConfigError as exc:
        return _fail("CONFIG", str(exc))
    except LockBusy as exc:
        return _fail("BUSY", f"workspace operation in progress: {exc}")
    except OSError as exc:
        return _fail('IO', str(exc))
    except (RuntimeError, sqlite3.Error) as exc:
        return _fail('STATE', str(exc))
    finally:
        while _CONNECTIONS:
            _CONNECTIONS.pop().close()


if __name__ == "__main__":
    sys.exit(main())
