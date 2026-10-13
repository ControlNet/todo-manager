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

from .files import private_new_file

SCHEMA_VERSION = 15

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
    # 5: exactly-once application of a run's decisions, and follow-up checks that are claimed
    # by a run and completed only when its decisions are applied.
    5: """
CREATE TABLE applied_runs (
  run_id TEXT PRIMARY KEY,
  entry TEXT NOT NULL,
  decisions_hash TEXT NOT NULL,
  report TEXT NOT NULL,
  applied_at TEXT NOT NULL
);
CREATE TABLE followup_checks (
  task_id TEXT NOT NULL REFERENCES tasks(id),
  due TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('claimed','done','failed')),
  run_id TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  claimed_at TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (task_id, due)
);
INSERT OR IGNORE INTO followup_checks(task_id, due, state, run_id, attempts, claimed_at, updated_at)
  SELECT d.target_id, substr(t.follow_up_date, 1, 10), 'done', d.run_id, 1, d.created_at, d.created_at
  FROM decisions d JOIN tasks t ON t.id = d.target_id
  WHERE d.op = 'followup_surfaced' AND t.follow_up_date IS NOT NULL
    AND d.created_at >= substr(t.follow_up_date, 1, 10);
""",
    # 6: a persistent revision number per Notion feedback stream (a comment, a page's managed
    # box, a page's own notes): a later read tells an unchanged re-read from a revert (A->B->A
    # within one timestamp), and a run that claimed an older revision cannot apply it.
    # Additive; the box's last seen fingerprint is seeded from a pending box edit, else from what we wrote.
    # Also: accept/revision messages bound to the delivery (result seq + its review event) they
    # were written for, resume bound to the pause message it lifts, and delegations/messages
    # identified by feedback item + op (``<feedback id>#<op index>``). delegation_messages is
    # rebuilt (rows copied in order) because its table-level UNIQUE(delegation, feedback, kind)
    # swallowed a second op of the same kind; that uniqueness now holds only without an op key.
    # Rollback: restore the automatic pre-schema backup with the previous code version.
    6: """
ALTER TABLE notion_comments ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE notion_pages ADD COLUMN box_revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE notion_pages ADD COLUMN box_seen_hash TEXT;
ALTER TABLE notion_pages ADD COLUMN notes_revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE delegations ADD COLUMN op_key TEXT;
CREATE UNIQUE INDEX delegations_op_key ON delegations(op_key) WHERE op_key IS NOT NULL;
CREATE TABLE delegation_messages_v6 (
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
  target_event_id INTEGER,
  approval_id TEXT,
  cause TEXT,
  target_seq INTEGER,
  target_message_id TEXT,
  op_key TEXT
);
INSERT INTO delegation_messages_v6(id, delegation_id, kind, feedback_id, body, requirement_version, target_block_seq,
  status, card_comment_id, error, created_at, sent_at, target_event_id, approval_id, cause)
  SELECT id, delegation_id, kind, feedback_id, body, requirement_version, target_block_seq, status, card_comment_id,
    error, created_at, sent_at, target_event_id, approval_id, cause FROM delegation_messages ORDER BY rowid;
DROP TABLE delegation_messages;
ALTER TABLE delegation_messages_v6 RENAME TO delegation_messages;
CREATE INDEX delegation_messages_approval ON delegation_messages(approval_id);
CREATE UNIQUE INDEX delegation_messages_feedback_kind ON delegation_messages(delegation_id, feedback_id, kind)
  WHERE op_key IS NULL;
CREATE UNIQUE INDEX delegation_messages_op_key ON delegation_messages(op_key) WHERE op_key IS NOT NULL;
UPDATE notion_pages SET box_seen_hash = COALESCE((
  SELECT substr(f.source_version, 6) FROM feedback f
  WHERE f.origin = 'notion_page' AND f.kind = 'body_edit' AND f.source_id = notion_pages.page_id
    AND f.status = 'pending' AND f.source_version LIKE 'body:%'
  ORDER BY f.received_at DESC LIMIT 1), managed_hash);
""",
    # 7: where a message landed on its card (the card event id of its comment, and the last card
    # event seen before posting it), so a delivery's requirement version comes from the order of
    # card events instead of "sent by sync time"; feedback held for a reason that must clear
    # before it may be claimed or applied (e.g. a Notion comment missing from its page's list
    # whose deletion is not confirmed); whether a Notion comment is still listed on its page.
    # Additive only. Rows written before it have NULL positions and are read as before
    # (received). Rollback: restore the automatic pre-schema backup with the previous code.
    7: """
ALTER TABLE delegation_messages ADD COLUMN card_event_before INTEGER;
ALTER TABLE delegation_messages ADD COLUMN card_event_id INTEGER;
ALTER TABLE feedback ADD COLUMN hold_reason TEXT;
ALTER TABLE notion_comments ADD COLUMN presence TEXT NOT NULL DEFAULT 'listed';
ALTER TABLE notion_comments ADD COLUMN presence_at TEXT;
""",
    # 8: the Hermes-native design (docs/architecture.md). The agent records outcomes with small
    # commands instead of a decisions file: ``op_log`` makes a retried command return its first
    # result; a run keeps the hash of the workspace conventions it started with and when its
    # unhandled claims were released; a conversation segment can be deferred until a date.
    # Local execution capacity is gone: messages held only for capacity are pending again
    # (holds for a user's pause stay). Page notes are context now: pending notes items are closed
    # (the notes stay on the page and in notion_pages.notes_text). The rules tables stay for
    # audit and export; their content moves to the workspace files on the first open
    # (``prefs.migrate_rules``, flagged by meta ``prefs.migration_from``). An approval can be
    # given with the answer to a worker's question (``answer_message_id``: it goes out with that
    # answer; ``delivery_id`` is then the question).
    8: """
CREATE TABLE op_log (
  key TEXT PRIMARY KEY,
  context TEXT NOT NULL,
  command TEXT NOT NULL,
  result TEXT NOT NULL,
  created_at TEXT NOT NULL
);
ALTER TABLE runs ADD COLUMN rules_hash TEXT;
ALTER TABLE runs ADD COLUMN released_at TEXT;
ALTER TABLE batches ADD COLUMN not_before TEXT;
ALTER TABLE batches ADD COLUMN defer_reason TEXT;
ALTER TABLE approvals ADD COLUMN answer_message_id TEXT;
UPDATE delegation_messages SET status = 'pending', error = NULL
  WHERE status = 'waiting_capacity' AND COALESCE(error, '') != 'paused by user';
UPDATE feedback SET status = 'superseded', applied_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
  result = '{"reason":"page notes are kept as context, not interpreted as feedback (schema 8)"}'
  WHERE kind = 'page_notes' AND status = 'pending';
""",
    9: """
CREATE TABLE delegation_children (
  delegation_id TEXT NOT NULL REFERENCES delegations(id),
  card_id TEXT NOT NULL,
  event_cursor INTEGER NOT NULL DEFAULT 0,
  status TEXT,
  PRIMARY KEY (delegation_id, card_id)
);
CREATE TABLE delegation_child_events (
  delegation_id TEXT NOT NULL REFERENCES delegations(id),
  card_id TEXT NOT NULL,
  event_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY (delegation_id, card_id, event_id)
);
ALTER TABLE delegation_messages ADD COLUMN target_card_id TEXT;
ALTER TABLE delegations ADD COLUMN notification_route TEXT;
ALTER TABLE delegations ADD COLUMN previous_delegation_id TEXT REFERENCES delegations(id);
ALTER TABLE delegations ADD COLUMN previous_delivery_id TEXT REFERENCES deliveries(id);
""",
    10: """
ALTER TABLE delegations ADD COLUMN children_sync_pending INTEGER NOT NULL DEFAULT 1;
ALTER TABLE delegation_children ADD COLUMN attention TEXT;
""",
    11: """
ALTER TABLE tasks ADD COLUMN decision TEXT CHECK (decision IN ('pending','agree','decline','agent'));
""",
    12: """
ALTER TABLE delegations ADD COLUMN start_after TEXT;
""",
    13: """
ALTER TABLE delegations ADD COLUMN handled_at TEXT;
""",
    14: """
ALTER TABLE projects ADD COLUMN icon TEXT;
""",
    15: """
CREATE TABLE delivery_pages (
  delivery_id TEXT PRIMARY KEY REFERENCES deliveries(id),
  page_id TEXT,
  url TEXT,
  created_at TEXT NOT NULL,
  done_at TEXT
);
""",
}


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
        private_new_file(path)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    migrate(conn)
    return conn


def open_existing(path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    """Never create or migrate a workspace at an inspection/business boundary."""
    path = Path(path).resolve()
    if not path.is_file():
        raise RuntimeError("workspace is absent; run todo-manager install")
    conn = sqlite3.connect(path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"), uri=True,
                           timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        version = int(row[0]) if row else 0
        if version != SCHEMA_VERSION:
            raise RuntimeError(f"workspace schema {version}; run todo-manager install to upgrade (expected {SCHEMA_VERSION})")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn
    except BaseException:
        conn.close()
        raise


def schema_version(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    return int(row["value"]) if row else 0


def migrate(conn: sqlite3.Connection, target: int = SCHEMA_VERSION) -> None:
    """Upgrade the schema to ``target`` (default: this code's version)."""
    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema {current} is newer than this code ({SCHEMA_VERSION}); upgrade the code")
    if 0 < current < target:
        backup_before_migration(conn, current)
    for version in range(current + 1, target + 1):
        with tx(conn):
            for statement in _split_sql(MIGRATIONS[version]):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(version),))
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('rules_version', '0') ON CONFLICT(key) DO NOTHING")
            if version == 8 and current > 0:
                # The rules of this workspace move to its files on the first open (prefs.ensure_migrated).
                set_meta(conn, "prefs.migration_from", str(current))


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
