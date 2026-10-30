# CLI contract (0.2)

Notion is the primary task interface; Hermes chat interprets requests. The CLI exposes business
operations and installation/recovery without introducing another runtime or scheduler.

## Surface and output

```text
install
status [--details] [--days N]
config show|get|set
pause
resume [--release-recovery-hold --yes]
sync
export [--verify]
restore FILE_OR_BUNDLE [--yes]
uninstall [--dry-run] [--yes]
task list|show|create|update
project list|show|icons|create|update
source show|search|dismiss|defer
feedback list|show|record|resolve
delegate list|show|create|message|answer|approve
run show|done
outbox retry ID...
daemon run|status|enroll|token|reset-token|restart
gmail-labels show
```

Business responses are JSON, including list/show. `run show --context` deliberately returns the
original context text. Administration defaults to concise summaries and accepts `--json`.
Use `config show --json`, `config get KEY --json`, or `config set KEY VALUE --json` for automation.
A queued Notion push, reply or card request is not a remote confirmation or channel receipt.
Inspect `applied` and `rejected` for partial updates; do not infer success from exit 0 alone.

Exit codes: 0 for a completed command; 1 for a synchronization/health/integrity problem; 2 for
invalid syntax, refused operations, unavailable/incompatible workspace or unconfirmed recovery;
3 when installation needs an answer or confirmation. Domain errors use JSON `error` and
`details`. Argparse syntax errors use stderr and exit 2. Installation questions always use
structured JSON outside a terminal. Inspection never initializes or migrates a workspace;
`WORKSPACE` names `install` as the supported preparation/upgrade boundary.

## Explicit provenance and direct chat

Business writes require exactly one of `--run`, `--feedback`, `--request`, `--human`; a feedback
run gives `--feedback F --run R` (and `feedback resolve F --run R`).
The manual marker is an audit assertion, not an operating-system security boundary.
`feedback resolve` and `run done` already name the feedback/run being settled.

```bash
todo-manager task create --title 'Draft expense report' --request chat-2026-10-06-a --said 'Draft the expense report, do not submit it.'
todo-manager task list --search expense
```

Use the returned Task ID for later operations. The first use of a request ID records its words;
subsequent operations reuse `--request` without repeating `--said`. A new ID requires words,
and different words under an existing ID produce `REQUEST_ID_REUSED`. Identical wording in
separate genuine requests uses separate IDs. A successfully committed identical retry returns
`duplicate: true`; context invalidation cannot undo that already committed result.

`feedback record` deliberately defers interpretation to a later feedback run. A recorded request
handled directly is superseded, and recording an already handled direct request does not enqueue
it again. Registration alone is audit-only (`ignored`, `result.handled=false`), not business
success and not an automatically queued request. A failed operation keeps its words reusable
and leaves previously queued feedback pending. A result with rejected fields also leaves the
queue pending, even when other fields applied. Only an operation without rejected fields
commits direct-handling bookkeeping and supersedes its queued counterpart in the same SQL
transaction. Historical `applied` registration alone is insufficient without a successful op log.
This selects direct handling instead of later interpretation; it does not prove that every step
of a natural-language request is complete. The chat agent must finish its remaining steps or
queue a distinct, explicitly scoped request for remaining work. Agents must not use `--human`
to evade the applicable run/feedback provenance.

Manual operations explicitly say so:

```bash
todo-manager config set slack.quiet_minutes 60 --human
todo-manager task create --title 'Review draft' --human
```

Native cron settings remain in Hermes. Bootstrap `hermes.model`, `hermes.provider` and `hermes.reasoning_effort` values in
configuration do not retune existing jobs; attempts to use those settings after installation
return `NATIVE_SETTING`. Use the real owned job ID with `hermes cron edit`.

## Incremental judgments and final settlement

Task/project updates remain per logical item. Evidence-only `task update --evidence` links and
handles material, records the decision, and queues the projection without inventing a field
edit or Task content version. Source dismissal and deferral retain their distinct outcomes.

At the end of a run issue **one** settlement containing all applicable explicit confirmations:

```text
todo-manager run done RUN_ID --checked TASK_ID_1 TASK_ID_2 --digest-reviewed
```

These metavariables are returned business IDs, not files to create. Omit `--checked` when there
are no unchanged follow-ups; a mutation that settled a follow-up needs no additional check.
A settled check that leaves the task open without a follow-up date ahead moves its
`follow_up_date` to the next look: as many days ahead as the task has been quiet (its newest
evidence, else its creation), between `followup.recheck_min_days` and `recheck_max_days`.
Omit `--digest-reviewed` unless that run's digest was actually reviewed. Confirmation ownership
and current follow-up dates are validated transactionally. Any invalid confirmation rolls back
all settlement changes. Retrying the same set is idempotent; a different set after completion
returns `SETTLEMENT_CHANGED`. Unhandled items keep their existing retry/exhaustion behavior.
A crash before settlement never rolls back previously committed business changes.

## Body clarification and delegation

Resolve a later answer with `feedback resolve ANSWER_ID --outcome applied --settles BODY_EDIT_ID`.
The original must be a clarified body edit of the same Task, and its revision must still be
current and fully read. A stale link returns `STALE_BODY`; an unrelated link returns
`INVALID_LINK`. Resolving an unrelated comment never releases body protection. Ordinary body
feedback still releases its own managed region when applied/ignored; incomplete feedback only
allows clarification before any business mutation.

Delegation creation uses a Task, `--brief TEXT` or `--brief-file PATH`, explicit `--allow`, and
source context. The brief carries goal, background, constraints and delivery requirements.
The title defaults to the Task title. Associations, action authorization, question/delivery
versions and the machine protocol remain structured; workers do not inherit feedback chat.
`--parallel` retains the explicit independent-work exception; `--no-submit` queues locally.
`--start-after "YYYY-MM-DD HH:MM"` (workspace time zone, or ISO with an offset) is for a user who
asked for a later start: the card is created parked in Hermes' `scheduled` state, inside the same
bridge call, so no dispatcher tick sees it ready; the feedback gate lets it go once the time has
passed (no model run). A time already passed starts it at once.
Message/answer/approve remain separate operations. Existing root/child question binding,
unknown-action recovery and cancellation ownership rules are unchanged.

## Restore and controls

`pause` and ordinary `resume` affect the two owned cron jobs only. They do not stop or resume
workers, release human blocks, or release a post-restore hold. `sync` performs deterministic
Notion/Kanban/outbox synchronization without claiming semantic feedback or waking a model.

`export` is the user-facing save operation. `restore` accepts an export directory or an existing
SQLite safety backup. A bundle is checked and rebuilt before replacing data. Existing targets
need a preview and `--yes`; a safety backup precedes replacement. Supported CLI writers and
fixed pre-run entry points share a workspace lock; restore holds it exclusively. Raw Python/SQL
writers and old releases are outside that locking contract and must be stopped before recovery.

The safety backup includes the database, config, AGENTS and legacy rule archive. Export also
includes source text, run/delivery artifacts, actual cron configuration and framework archives.
An existing-target bundle restore overlays included artifacts and replaces the database; unrelated
files are retained. SQLite recovery has narrower file coverage. No restore applies cron configs,
imports an installation's ownership into another machine, or overwrites native profile memory.
Framework snapshots in an export are inspection/manual recovery evidence, not automatically deployed.

After comparing native cards and uncertain effects, `resume --release-recovery-hold --yes`
releases only the Todo recovery hold; cron state remains unchanged. Use ordinary `resume`
separately if needed. Unknown external actions remain non-replayable. File restoration and SQL
replacement are not one filesystem-wide transaction: on interruption use the reported safety
backup and inspect the result before releasing holds.

## Reliability and diagnostics

New run writes are checked inside their business transaction, after exact operation retry lookup.
Released/restored, settled, idle, failed and expired runs return `STALE_RUN` (exit 2). The intake
lease is 90 minutes from run start; feedback uses `feedback.stale_claim_minutes` (default 60).
An active gate preparation (`running`) or woken run may write within that lease. A previously
committed identical operation still returns `duplicate: true`; obtain a fresh run/context for new
judgments. A `--feedback F --run R` write or resolution is accepted only while R holds F's claim
within that lease (a later run may have taken F over); otherwise `STALE_RUN`. Without `--run`
(by hand) it is refused with `CLAIMED_BY_RUN` while a live run holds F. Recovery releases all intake/feedback runs, including interrupted gate preparation,
and makes their unprocessed materials available again. This is separate from delegation holds.

`outbox retry ID... --human` queues given-up (`dead`) Notion task and project pushes again after
the cause was fixed; `status --details` lists given-up effects (`health.outbox_dead`). The given-up
row is superseded with its attempts and error kept, and a fresh push of the current projection is
queued with new attempts (an unchanged task would otherwise keep reusing the given-up row). A push
that a newer one replaced is reported under `superseded` and not queued. Refused, with nothing
changed for any listed ID: a `pending`/`failed`/`uncertain` effect (`NOT_DEAD`: it retries by itself;
its retry deadline is not bypassed), comments, delivery pages and project views (`NOT_RETRYABLE`: they
may already exist in Notion), and a scheduled run (`NOT_AUTHORIZED`). Provenance and `--key`
follow the other writes.

Invalid TOML or wrong types of known configuration fields return `CONFIG` with the file path
(and parser line/column when available). Business commands never silently substitute defaults.
For a damaged target configuration, explicitly identify the recovery workspace:

```bash
todo-manager --home /path/to/workspace restore /path/to/saved.sqlite --json
todo-manager --home /path/to/workspace restore /path/to/saved.sqlite --yes --json
# An export bundle directory can replace /path/to/saved.sqlite.
```

The first command previews replacement; the second replaces data after confirmation and keeps
the original raw configuration in the reported safety backup's `.files/config.toml`. Source
validation precedes acquiring the target lock; invalid sources and unconfirmed previews do not
create target lock files. A SQLite backup without companion files cannot repair configuration
files; use a backup with those files or a valid export bundle. No profile/global setting is restored.

`status --json` includes installation failures in top-level `problems` and exits 1 when automation
is incomplete. `installation.state` distinguishes `not_installed`, `partial`, `ready`, `paused`,
and `partially_paused`. Missing jobs, gates or launcher, outdated/foreign generated files and
installation conflicts name the affected asset and repair entry (`install`). Terminal cron jobs
are unavailable, not merely paused. Deliberately paused jobs are listed without a health failure;
other independent health problems still affect the exit code. Status remains read-only.

## Switching from the old CLI

There are no permanent aliases for removed commands. Keep the previous release checkout and
its installed launcher available while draining old cron/chat sessions. Do not update the code
under an active old launcher in place. Build/test this release in a separate checkout, pause the
owned cron jobs with the old CLI, finish or settle old runs there, and finish old chat requests.
The new installer refuses a workspace with woken, unsettled runs; it cannot detect arbitrary
chat sessions, so those must be drained explicitly. Then invoke the new checkout's `install`,
review the combined preview, apply it, and start fresh Hermes sessions for the new skill/prompt.
The installer updates owned scripts/skill/prompts together, preserves cron runtime settings and
paused/running state, and records framework backups. Installation is re-runnable, not a remote
transaction: an interrupted apply may require the same command again.

Old-system Notion migration remains available as a one-off module:

```bash
python3 -B -m todo_manager.migrate_legacy --help
```

It retains fetch/propose/apply and the reviewed proposal workflow. It is not hidden under another
core CLI namespace. Old rule archival and schema upgrades remain installation responsibilities;
personal preferences still use native Hermes memory.

## Native readiness diagnostics

Kanban readiness distinguishes a live board from an archived tombstone, missing board metadata,
and invalid metadata. A remaining database without board metadata is reported as orphaned.
Installation never adopts or recreates archived/orphaned/invalid boards automatically; inspect
recovery in Hermes. Existing configuration stays intact, and unsubmitted delegations retain
pending state and the lookup failure for a later retry. Health exposes `kanban_board` in detailed
output and includes unavailable configured boards in `problems`. The bridge returns stable
`BOARD_ARCHIVED`, `BOARD_ORPHANED` or `BOARD_INVALID` errors before attempting a connection.

`installation.cron_health` supplements job presence with native ticker heartbeat/success/error
readers and storage observations. Explicit file/directory permission problems, read-only mounts,
zero free space and active ticker failures affect the health exit code. Low disk space and absent
heartbeats are warnings; absent heartbeat means unknown liveness, not proof of a dead gateway.
Paused jobs do not cause stale-ticker alarms. An unreadable jobs file is reported without repair;
its jobs are unknown, not classified as missing.

This inspection intentionally does **not** invoke native `probe_report` or `load_jobs`: the former
writes probe files, and the latter can repair native state. Native imports are isolated in a scratch
home before read APIs are scoped to the inspected profile. `write_probe=false`, `writable=null`
and `skipped_runs=null` are explicit: filesystem observations do not prove quota/write availability
under the scheduler's user, and the gateway's in-memory skipped-run count is not available from a
new process. A missing native diagnostic API or interpreter is reported as `unavailable` with a
warning, rather than fabricated success. Installation `ready` describes installed assets, not a
proof that the scheduler will fire. Inspect the native diagnostic in the appropriate profile when
an actual write probe is needed:

```bash
hermes cron status
```

That native command may write temporary probe files and perform Hermes' own store repairs;
Todo Manager's `status` does neither. No new scheduler, retry service or cron is involved.

## Optional Agent delegation during installation

When `kanban.enabled=false` is explicitly configured, rerunning `install` now asks whether to
configure and enable Agent delegation or keep it disabled. Choosing enable reuses the existing
execution-profile validation and board creation preview; no setting or board is changed before
the combined installation confirmation. Noninteractive installs must answer `kanban.enabled`
with `yes` or `no`; `--yes` does not supply this choice. Enabled installations retain their
profile and are not prompted again unless profile validation fails.

Both text and JSON `status` include `delegation_config` with enabled, board, assignee and an
explanation. Disabled delegation points to `todo-manager install`; it is informational, not a
health failure. Source intake and Notion management can run without Agent delegation. Enabling
is a configuration choice, not proof of worker readiness; existing health checks still apply.

## Decision board and Notion installation upgrade

`install` now upgrades an existing project-owned Decisions list into a board with the choices
Pending, Cancel, Done, My to-do and Assign to Agent (earlier boards: Agree / Decline, replaced at install). Notion cannot change a view's layout type through
PATCH, so installation creates a board as the first tab of the same database container and
preserves the old list. It also adds All open tasks, with its heading, after the Decisions
board. Existing custom decision filters/layouts stop migration for review rather than being
overwritten.

Cancel, Done and My to-do set the task status at import, without a model run; the older
Agree/Decline queue version-bound feedback for Hermes. No choice approves external actions.
Assign to Agent queues the existing delegation workflow. Pending withdraws unprocessed choices;
it cannot undo an already performed external effect or cancel running workers. Replies/results
continue using the task's existing Notion discussion. Unknown authors cannot delegate by dragging.

Schema 11 adds nullable `tasks.decision`, covered by normal history/human-edit protection.
Before upgrading, drain active runs and pause the two Todo cron jobs. Installation takes its
normal SQLite/config safety backup and saves the original remote schema/view snapshot under
`backups/notion-decision-ui-v1.json` before remote changes. Re-running installation reconciles
created views and never overwrites nonempty decisions. If a create times out without a discoverable
result, installation stops with an uncertain outcome instead of blindly repeating it.

For rollback, pause/drain again and restore the pre-install backup with the compatible checkout.
The original Notion list remains available as another tab; the new property/board can remain
unused without deleting tasks. Remote UI changes are not reverted by SQLite restore. The saved
schema/view JSON records the original structure for a deliberate native Notion restoration.
No credentials or private task text belong in source-control migration reports.

## Real-time sync daemon

`daemon run` is what the systemd unit runs; it refuses while `daemon.enabled` is false (exit code
4, on which the unit does not restart), exits 0 to be restarted when its code or `config.toml`
changes, and never
takes the workspace lock for its lifetime (only per cycle), so `install` is not blocked. `daemon
status` prints health, unit and `cloudflared` state, whether a webhook token is stored and the
daemon's last status, never the token itself; `daemon enroll [--minutes N]` lets the daemon accept
Notion's handshake for N minutes (default 15; refused while a token is stored); `daemon token`
prints the token for Notion's Verify dialog; `daemon reset-token` forgets it and opens enrollment
for a recreated subscription; `daemon restart`
restarts the unit. `sync` and the feedback gate share the `sync` lock with the daemon. See
[realtime-sync.md](realtime-sync.md).
