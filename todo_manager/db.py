"""SQLite storage: connection handling and schema migrations.

SQLite is the structured system of record for Todo management. Markdown snapshots are
derived from it, and Notion edits are imported back into it as versioned human events.
Execution state (runs, claims, worker processes) stays in the Hermes Kanban DB; this
database keeps the delegation links and the snapshots needed for sync and export.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 4

MIGRATIONS: dict[int, str] = {
    1: """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- Projects: long-lived goals and scope.
CREATE TABLE projects (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  domain TEXT,
  status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active','paused','done','archived')),
  goal TEXT,
  next_step TEXT,
  blockers TEXT,
  recent_activity TEXT,
  recent_activity_at TEXT,
  origin TEXT NOT NULL,
  notion_page_id TEXT UNIQUE,
  field_meta TEXT NOT NULL DEFAULT '{}',
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE project_repos (
  project_id TEXT NOT NULL REFERENCES projects(id),
  repo TEXT NOT NULL,
  PRIMARY KEY (project_id, repo)
);

-- Tasks: one responsibility of the user (not one per message, not a whole project).
CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  project_id TEXT REFERENCES projects(id),
  status TEXT NOT NULL
    CHECK (status IN ('todo','in_progress','waiting','decision','review','done','cancelled')),
  next_actor TEXT NOT NULL CHECK (next_actor IN ('me','agent','external')),
  waiting_on TEXT,
  responsibility TEXT,
  next_step TEXT,
  done_criteria TEXT,
  summary TEXT,
  due_date TEXT,
  due_basis TEXT,
  follow_up_date TEXT,
  snooze_until TEXT,
  focus INTEGER NOT NULL DEFAULT 0,
  focus_reason TEXT,
  change_note TEXT,
  change_note_at TEXT,
  origin TEXT NOT NULL,
  closed_at TEXT,
  closed_reason TEXT,
  notion_page_id TEXT UNIQUE,
  field_meta TEXT NOT NULL DEFAULT '{}',
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX tasks_status ON tasks(status);
CREATE INDEX tasks_project ON tasks(project_id);

CREATE TABLE task_links (
  task_id TEXT NOT NULL REFERENCES tasks(id),
  other_task_id TEXT NOT NULL REFERENCES tasks(id),
  kind TEXT NOT NULL CHECK (kind IN ('follow_up_of','duplicate_of','related','split_from')),
  created_at TEXT NOT NULL,
  PRIMARY KEY (task_id, other_task_id, kind)
);

-- Change history for tasks/projects (field-level, with actor and cause).
CREATE TABLE history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  actor TEXT NOT NULL,          -- human | agent | program
  origin TEXT NOT NULL,         -- notion | matrix | cli | intake | feedback | kanban | migration
  cause_ref TEXT,               -- feedback / decision / source id
  changes TEXT NOT NULL,        -- JSON {field: [old, new]}
  at TEXT NOT NULL
);
CREATE INDEX history_entity ON history(entity_type, entity_id);

-- Normalised source items (email messages, Slack messages, GitHub events, commits...).
CREATE TABLE source_items (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  account TEXT NOT NULL DEFAULT '',
  external_id TEXT NOT NULL,
  version TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'message',
  thread_key TEXT,
  conversation_key TEXT NOT NULL,
  occurred_at TEXT,
  author TEXT,
  author_is_me INTEGER NOT NULL DEFAULT 0,
  title TEXT,
  url TEXT,
  content_hash TEXT,
  body_path TEXT,
  excerpt TEXT,
  meta TEXT NOT NULL DEFAULT '{}',
  fetched_at TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending'
    CHECK (state IN ('pending','batched','processed','noise','superseded')),
  batch_id TEXT,
  processed_at TEXT,
  processed_run TEXT,
  UNIQUE (source, account, external_id, version)
);
CREATE TABLE source_bodies (
  item_id TEXT PRIMARY KEY REFERENCES source_items(id),
  body TEXT NOT NULL
);
CREATE INDEX source_items_state ON source_items(state, source);
CREATE INDEX source_items_conv ON source_items(source, conversation_key);
CREATE INDEX source_items_thread ON source_items(thread_key);

-- Accumulating / mature groups of source items awaiting one semantic pass.
CREATE TABLE batches (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  conversation_key TEXT NOT NULL,
  state TEXT NOT NULL
    CHECK (state IN ('accumulating','mature','claimed','done','failed')),
  first_item_at TEXT,
  last_item_at TEXT,
  last_activity_at TEXT,
  matured_at TEXT,
  forced INTEGER NOT NULL DEFAULT 0,
  claimed_run TEXT,
  claimed_at TEXT,
  done_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX batches_state ON batches(state, source);
CREATE UNIQUE INDEX batches_open_conv ON batches(source, conversation_key)
  WHERE state = 'accumulating';

-- Evidence links: which source items support which task/project and in what role.
CREATE TABLE evidence_links (
  entity_type TEXT NOT NULL CHECK (entity_type IN ('task','project')),
  entity_id TEXT NOT NULL,
  source_item_id TEXT NOT NULL REFERENCES source_items(id),
  role TEXT NOT NULL
    CHECK (role IN ('origin','commitment','follow_up','completion','context','reminder','cancellation')),
  note TEXT,
  decision_id TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (entity_type, entity_id, source_item_id, role)
);
CREATE INDEX evidence_by_item ON evidence_links(source_item_id);

-- Every semantic decision with its rationale and evidence pointers (incl. ignores).
CREATE TABLE decisions (
  id TEXT PRIMARY KEY,
  run_id TEXT,
  op TEXT NOT NULL,
  target_type TEXT,
  target_id TEXT,
  source_item_ids TEXT NOT NULL DEFAULT '[]',
  category TEXT,
  rationale TEXT,
  rules_version INTEGER,
  outcome TEXT NOT NULL DEFAULT 'applied',
  detail TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX decisions_run ON decisions(run_id);
CREATE INDEX decisions_op ON decisions(op, created_at);

-- Human feedback from Notion comments/properties, Matrix, CLI; persisted before use.
CREATE TABLE feedback (
  id TEXT PRIMARY KEY,
  origin TEXT NOT NULL
    CHECK (origin IN ('notion_comment','notion_property','notion_page','matrix','cli','kanban')),
  source_id TEXT NOT NULL,
  source_version TEXT NOT NULL DEFAULT '',
  author TEXT,
  author_is_user INTEGER NOT NULL DEFAULT 1,
  received_at TEXT NOT NULL,
  text TEXT,
  payload TEXT NOT NULL DEFAULT '{}',
  scope_type TEXT NOT NULL CHECK (scope_type IN ('task','project','delegation','global')),
  scope_id TEXT,
  kind TEXT,                    -- comment | property_edit | new_task | delegate_request | body_edit | ...
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','applied','clarify','ignored','superseded','failed')),
  result TEXT,
  applied_at TEXT,
  rules_version INTEGER,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  claimed_run TEXT,
  claimed_at TEXT,
  reply_to TEXT,
  UNIQUE (origin, source_id, source_version)
);
CREATE INDEX feedback_status ON feedback(status);
CREATE INDEX feedback_scope ON feedback(scope_type, scope_id);

-- Business rules (responsibility, preferences). Every change bumps meta.rules_version.
CREATE TABLE rules (
  id TEXT PRIMARY KEY,
  scope_type TEXT NOT NULL CHECK (scope_type IN ('global','source','project','task')),
  scope_id TEXT,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active','suggested','retired')),
  revision INTEGER NOT NULL DEFAULT 1,
  from_feedback_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE rule_revisions (
  rule_id TEXT NOT NULL REFERENCES rules(id),
  revision INTEGER NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  status TEXT NOT NULL,
  actor TEXT NOT NULL,
  feedback_id TEXT,
  rules_version INTEGER NOT NULL,
  at TEXT NOT NULL,
  PRIMARY KEY (rule_id, revision)
);

-- Delegations: one explicit piece of work handed to an agent. One card each.
CREATE TABLE delegations (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  title TEXT NOT NULL,
  goal TEXT NOT NULL,
  context TEXT,
  constraints TEXT,
  deliverable TEXT,
  done_criteria TEXT,
  allowed_actions TEXT NOT NULL DEFAULT '["read","analyze","draft"]',
  assignee TEXT,
  requirement_version INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL CHECK (status IN (
    'pending_submit','queued','running','blocked','review','paused',
    'ended','cancelled','reconcile','quarantined')),
  pause_requested INTEGER NOT NULL DEFAULT 0,
  board TEXT NOT NULL,
  idem_key TEXT NOT NULL UNIQUE,
  card_id TEXT UNIQUE,
  card_status TEXT,
  card_run_id TEXT,
  block_seq INTEGER NOT NULL DEFAULT 0,
  block_event_id TEXT,
  block_reason TEXT,
  event_cursor INTEGER NOT NULL DEFAULT 0,
  submit_attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  from_feedback_id TEXT,
  base_task_version INTEGER NOT NULL DEFAULT 0,
  comment_cursor INTEGER NOT NULL DEFAULT 0,
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX delegations_task ON delegations(task_id);
CREATE INDEX delegations_status ON delegations(status);

-- Instructions sent to a card (requirements, supplements, answers, revisions, pauses).
CREATE TABLE delegation_messages (
  id TEXT PRIMARY KEY,
  delegation_id TEXT NOT NULL REFERENCES delegations(id),
  kind TEXT NOT NULL CHECK (kind IN ('requirement','supplement','answer','revision','pause','resume','cancel','accept','approval')),
  feedback_id TEXT,
  body TEXT NOT NULL,
  requirement_version INTEGER NOT NULL,
  target_block_seq INTEGER,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','sending','sent','superseded','failed','waiting_capacity')),
  card_comment_id TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  sent_at TEXT,
  UNIQUE (delegation_id, feedback_id, kind)
);

-- Deliveries received from cards (one per delegation/card/run/version).
CREATE TABLE deliveries (
  id TEXT PRIMARY KEY,
  delegation_id TEXT NOT NULL REFERENCES delegations(id),
  card_id TEXT NOT NULL,
  run_id TEXT NOT NULL DEFAULT '',
  seq INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('result','question','failure')),
  summary TEXT,
  body TEXT,
  artifacts TEXT NOT NULL DEFAULT '[]',
  requirement_version INTEGER,
  received_at TEXT NOT NULL,
  UNIQUE (delegation_id, card_id, run_id, kind, seq)
);

-- Approvals for substantive external actions, bound to a delivery version.
CREATE TABLE approvals (
  id TEXT PRIMARY KEY,
  delegation_id TEXT NOT NULL REFERENCES delegations(id),
  delivery_id TEXT REFERENCES deliveries(id),
  action TEXT NOT NULL,
  detail TEXT,
  feedback_id TEXT,
  granted_at TEXT NOT NULL,
  consumed_at TEXT,
  revoked_at TEXT
);

-- Durable outbound side effects (Notion writes, Kanban ops, notifications).
CREATE TABLE outbox (
  id TEXT PRIMARY KEY,
  target TEXT NOT NULL CHECK (target IN ('notion','kanban','matrix')),
  op TEXT NOT NULL,
  entity_type TEXT,
  entity_id TEXT,
  dedupe_key TEXT NOT NULL UNIQUE,
  payload TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','in_flight','done','failed','uncertain','dead','superseded')),
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  last_error TEXT,
  result TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX outbox_status ON outbox(status, next_attempt_at);

-- Fetch cursors per source scope (separate from per-item processing state).
CREATE TABLE cursors (
  source TEXT NOT NULL,
  scope TEXT NOT NULL,
  cursor TEXT,
  last_attempt_at TEXT,
  last_success_at TEXT,
  last_error TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  meta TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (source, scope)
);

-- Runs of every entry point, with wake reasons and cost accounting.
CREATE TABLE runs (
  id TEXT PRIMARY KEY,
  entry TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL DEFAULT 'running',
  wake INTEGER NOT NULL DEFAULT 0,
  wake_reasons TEXT NOT NULL DEFAULT '[]',
  summary TEXT NOT NULL DEFAULT '{}',
  error TEXT,
  agent_completed_at TEXT,
  hermes_job_id TEXT,
  hermes_session_id TEXT,
  model_calls INTEGER,
  tokens_in INTEGER,
  tokens_out INTEGER
);
CREATE INDEX runs_entry ON runs(entry, started_at);

-- Daily digests (e.g. commit activity) with a per-day model-attempt budget.
CREATE TABLE daily_jobs (
  job TEXT NOT NULL,
  day TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('skipped','claimed','done','failed','exhausted')),
  attempts INTEGER NOT NULL DEFAULT 0,
  run_id TEXT,
  detail TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (job, day)
);

-- Notion mirror bookkeeping.
CREATE TABLE notion_pages (
  page_id TEXT PRIMARY KEY,
  entity_type TEXT NOT NULL CHECK (entity_type IN ('task','project','home')),
  entity_id TEXT,
  url TEXT,
  last_edited_time TEXT,
  last_edited_by TEXT,
  last_seen_props TEXT NOT NULL DEFAULT '{}',  -- normalised values as last read or written
  rendered_hash TEXT,                          -- hash of the managed region we last rendered
  managed_hash TEXT,                           -- fingerprint of that region as Notion stores it
  trashed INTEGER NOT NULL DEFAULT 0,
  comments_checked_at TEXT,
  last_comment_at TEXT,
  UNIQUE (entity_type, entity_id)
);
CREATE TABLE notion_comments (
  comment_id TEXT PRIMARY KEY,
  page_id TEXT NOT NULL,
  discussion_id TEXT,
  author_id TEXT,
  is_bot INTEGER NOT NULL DEFAULT 0,
  ref TEXT,                                    -- for our own comments: the logical key (e.g. dlv:<id>)
  last_edited_time TEXT,
  content_hash TEXT NOT NULL,
  feedback_id TEXT,
  seen_at TEXT NOT NULL
);
CREATE INDEX notion_comments_discussion ON notion_comments(discussion_id);
""",
    # 2: delegation transitions bound to Kanban events, cancel confirmation, approval validity.
    # Additive only (ADD COLUMN / new indexes): an older code version can still read the
    # tables; rolling back means restoring the automatic pre-migration backup.
    2: """
ALTER TABLE delegation_messages ADD COLUMN target_event_id INTEGER;
ALTER TABLE delegation_messages ADD COLUMN approval_id TEXT;
ALTER TABLE delegation_messages ADD COLUMN cause TEXT;
ALTER TABLE delegations ADD COLUMN pause_event_id INTEGER;
ALTER TABLE delegations ADD COLUMN cancel_state TEXT;
ALTER TABLE deliveries ADD COLUMN source_event_id INTEGER;
ALTER TABLE deliveries ADD COLUMN after_cancel INTEGER NOT NULL DEFAULT 0;
CREATE UNIQUE INDEX deliveries_source_event ON deliveries(delegation_id, card_id, source_event_id)
  WHERE source_event_id IS NOT NULL;
ALTER TABLE approvals ADD COLUMN outcome TEXT;
CREATE INDEX delegation_messages_approval ON delegation_messages(approval_id);
""",
    # 3: Notion property writes whose outcome is not confirmed yet (expected values), so a
    # later pull recognises our own write instead of importing it as a human edit.
    3: """
ALTER TABLE notion_pages ADD COLUMN pending_write TEXT;
""",
    # 4: page bodies are read after a human edit (not only when we push): when we last read the
    # body, and the user's own notes outside the managed region as last read.
    4: """
ALTER TABLE notion_pages ADD COLUMN body_read_at TEXT;
ALTER TABLE notion_pages ADD COLUMN notes_hash TEXT;
ALTER TABLE notion_pages ADD COLUMN notes_text TEXT;
""",
}


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    migrate(conn)
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    return int(row["value"]) if row else 0


def migrate(conn: sqlite3.Connection) -> None:
    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema {current} is newer than this code ({SCHEMA_VERSION}); upgrade the code")
    if 0 < current < SCHEMA_VERSION:
        backup_before_migration(conn, current)
    for version in range(current + 1, SCHEMA_VERSION + 1):
        with tx(conn):
            for statement in _split_sql(MIGRATIONS[version]):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(version),))
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('rules_version', '0') ON CONFLICT(key) DO NOTHING")


def backup_before_migration(conn: sqlite3.Connection, current: int) -> Path | None:
    """Copy an existing database before upgrading its schema (the rollback point).

    Written to ``<home>/backups/pre-schema-v<new>-<UTC stamp>.sqlite``; restoring it with
    the previous code version undoes the migration. In-memory databases are skipped.
    """
    row = conn.execute("PRAGMA database_list").fetchone()
    if row is None or not row["file"]:
        return None
    db_file = Path(row["file"])
    folder = db_file.parent.parent / "backups" if db_file.parent.name == "state" else db_file.parent
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = folder / f"pre-schema-v{SCHEMA_VERSION}-from-v{current}-{stamp}.sqlite"
    n = 1
    while target.exists():
        n += 1
        target = folder / f"pre-schema-v{SCHEMA_VERSION}-from-v{current}-{stamp}-{n}.sqlite"
    dest = sqlite3.connect(str(target))
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return target


def _split_sql(script: str) -> list[str]:
    statements: list[str] = []
    buffer: list[str] = []
    for line in script.splitlines():
        if line.strip().startswith("--"):
            continue
        buffer.append(line)
        candidate = "\n".join(buffer)
        if candidate.rstrip().endswith(";") and sqlite3.complete_statement(candidate):
            statements.append(candidate.strip())
            buffer = []
    if "".join(buffer).strip():
        statements.append("\n".join(buffer).strip())
    return statements


@contextlib.contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Write transaction that takes the RESERVED lock up front (BEGIN IMMEDIATE).

    Nested use re-uses the outer transaction via a SAVEPOINT.
    """
    if conn.in_transaction:
        name = f"sp_{id(conn)}_{conn.total_changes}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
            raise
        conn.execute(f"RELEASE {name}")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value))


def rules_version(conn: sqlite3.Connection) -> int:
    return int(get_meta(conn, "rules_version", "0") or 0)


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def loads(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None
