# Operations runbook

All runtime data lives in the workspace (`TODO_HOME`, default
`~/.hermes/workspace/todo-manager`), never in the source checkout. Secrets stay in environment
variables (for example `NOTION_API_KEY` loaded from `~/.hermes/.env`); keep any `.env` /
`.env.local` file out of version control.

## Install

```bash
git clone <repository url> ~/src/todo-manager
cd ~/src/todo-manager
python3 -m unittest discover -s tests -t .        # expect "OK" (Hermes integration tests skip without Hermes)
./bin/todo-manager init --language zh-CN          # or en; default: TODO_MANAGER_LANG / system locale
$EDITOR ~/.hermes/workspace/todo-manager/config.toml   # identities, sources, notify target
./bin/todo-manager notion whoami                  # checks the token (read-only)
./bin/todo-manager notion plan --home-page <page id>   # review the writes
./bin/todo-manager notion setup --home-page <page id> --yes
./bin/todo-manager hermes install                 # shows the plan, asks; jobs are created PAUSED
./bin/todo-manager hermes resume --yes            # start the two cron jobs
```

Requirements: Python ≥ 3.11 (standard library only), Hermes with cron and Kanban, and the
Kanban settings in [board-protocol.md](board-protocol.md).

`hermes install` writes a launcher `~/.local/bin/todo-manager`, two pre-run scripts under
`~/.hermes/scripts/`, the skill (only if no skill of that name exists — Hermes may evolve it;
an existing skill is never overwritten, differences are written to
`state/hermes/skill-proposal/` for you to review) and two cron jobs:

| Job | Schedule | Pre-run gate |
|---|---|---|
| `todo-manager-intake` | `*/5 * * * *` | fetch sources, batch, wake the model only for mature batches, due follow-ups or the daily commit digest |
| `todo-manager-feedback` | `2-59/5 * * * *` | Notion edits/comments, Kanban results, outbox, home freshness; wakes the model only for feedback that needs interpretation |

Re-running `hermes install` updates scripts and job settings in place (keeping paused state)
and is safe to repeat. `hermes install --dry-run` prints the plan only.

## Daily checks

```bash
todo-manager health            # problems first; exit code 1 when there are problems
todo-manager health --json     # sources, outbox, feedback, delegations, runs
todo-manager metrics --days 7  # runs, wakes, completions, errors, tasks created, model token usage
todo-manager hermes status     # launcher, scripts, skill state, jobs
todo-manager history <task id> # field-level history of one task or project
```

Problems (stale sources, given-up writes, quarantined delegations, failed feedback, a gate that
has not run) also appear in the Notion freshness box and, when `notify.matrix` is enabled, as
at most one Matrix reminder per problem per day. A source scope is stale after 30 minutes without
a successful fetch, plus its own polling interval (quiet Slack conversations are read about every
`slack.quiet_poll_minutes`); when more than three scopes of one source are stale (e.g. an expired
Slack token with all DMs included) they are reported as one line.

Model cost: every run that wakes the model is recorded with its reasons; `metrics` adds
prompt/completion tokens of the two jobs from Hermes' `cron/usage_audit.jsonl`.

## Pause, resume, roll back

```bash
todo-manager hermes pause --yes     # stops both cron jobs (nothing runs, nothing is lost)
todo-manager hermes resume --yes
```

Roll back completely:

1. `todo-manager hermes pause --yes`.
2. `todo-manager backup` (keep the current state), then, if needed,
   `todo-manager restore <backups/…sqlite> --yes` to return to an earlier state
   (a `pre-restore` safety copy is taken automatically).
3. To uninstall: `hermes cron list`, then `hermes cron remove <job id>` for both jobs; delete
   `~/.hermes/scripts/todo_manager_gate_*.py` and `~/.local/bin/todo-manager` if you no longer need them.
   The skill stays (it may contain Hermes' own learning); remove it only on purpose.
4. Notion pages are not deleted by any command. Move them to the Notion trash yourself if you
   want them gone.

## Export, backup, restore

Two different things:

- **Database snapshot** (`backup` / `restore`): one consistent SQLite file. It is the state of
  this workspace (tasks, history, evidence, rules, feedback, delegations, deliveries, outbox,
  cursors) but not the files next to it (delivery files, run context, config).
- **Portable workspace** (`export` / `import`): everything needed to continue elsewhere:
  `data.json` (every table, including source bodies and history), `workspace/config.toml` with
  secret-looking values redacted (`token_env` / `token_file` names are kept, the secrets
  themselves stay in your environment), `workspace/AGENTS.md`, `workspace/runs/` (decision files
  and manifests) and `workspace/deliveries/` (delivery files copied out of the worker's scratch
  space at sync time, with workspace-relative paths and SHA-256), plus Markdown views and a
  `manifest.json` with a hash for every file and table. Not included: credentials, `backups/`,
  earlier `exports/`, `notes/`, `migration/` working files and `state/hermes/` skill proposals.
  History that exists only on the Hermes board (card comments, run logs) stays in Hermes; what
  todo-manager needs from it (questions, answers, deliveries and their files, failures) is
  already copied into its own tables when the card is synced.

```bash
todo-manager export            # exports/<stamp>/: data.json, workspace/, tasks/*.md, projects/*.md, rules/, views/, manifest.json
todo-manager export --verify   # also rebuilds a scratch database from data.json and compares every table
todo-manager backup --keep 14  # consistent SQLite copy in backups/, keeps the newest 14
todo-manager restore backups/todo-<stamp>.sqlite --yes   # integrity-checked; takes both gate locks
```

Moving to a new directory or machine (the target must be absent or empty; nothing is written
until the whole bundle has been checked against its manifest):

```bash
todo-manager export
todo-manager import ~/.hermes/workspace/todo-manager/exports/<stamp> --into /path/to/new/todo-manager
export NOTION_API_KEY=...        # secrets come from your environment again, never from the bundle
todo-manager --home /path/to/new/todo-manager health
todo-manager --home /path/to/new/todo-manager delegate list
todo-manager --home /path/to/new/todo-manager delegate release-hold   # after checking the board
```

**No replay after an import or restore.** Claims of runs that no longer exist are released;
Matrix reminders and Notion comments that may already have gone out are marked dead instead of
being sent again (idempotent Notion state pushes stay queued); card submissions and messages to
the board are held. `health` shows "delegations held since …" until you have compared the board
(`hermes kanban --board todo-manager list --tenant todo-manager`) with `todo-manager delegate list` and run
`delegate release-hold`. Old workers are never restarted by todo-manager; the next sync reads the
board as it is.

## Upgrading an existing workspace (schema migrations)

This version uses schema v5 (v1 is the previous release): v2 binds delegation messages to Kanban
events, v3 records pending Notion writes, v4 tracks page-body reads, v5 adds exactly-once
decision application and follow-up checks. Every step is additive: no table or column is
dropped and existing rows are kept; v5 marks follow-ups that were already surfaced as done so
they are not shown again. You do not need to delete or rebuild the database.

The migration runs when any command first opens the database with the new code. Before it, a
consistent copy is written to `backups/pre-schema-v5-from-v<old>-<UTC stamp>.sqlite`.

```bash
todo-manager hermes pause --yes            # no gate runs during the upgrade
todo-manager export --verify               # optional: a portable copy as well
cd ~/src/todo-manager && git pull     # the new code
todo-manager health                        # opens the database: migrates, then lists problems
ls ~/.hermes/workspace/todo-manager/backups/pre-schema-v5-*
todo-manager hermes install                # refresh scripts; jobs keep their paused state
todo-manager hermes resume --yes
```

Expected: `health` runs without a traceback and a `pre-schema-v5-from-v1-*.sqlite` file exists.

Rolling back to the previous code (verified on a scratch v1 workspace): the old code refuses a
newer database ("database schema 5 is newer than this code"), and `restore` with the new code
would migrate again, so the file is swapped while the jobs are paused. Changes made after the
upgrade are lost (they stay in the set-aside file and the export).

```bash
todo-manager hermes pause --yes
H=~/.hermes/workspace/todo-manager
mv "$H/state/todo.sqlite" "$H/backups/todo-v5-set-aside.sqlite"   # keep, do not delete
ls "$H/state"                       # no todo.sqlite-wal / -shm left (all connections closed)
cp "$H"/backups/pre-schema-v5-from-v1-<stamp>.sqlite "$H/state/todo.sqlite"
cd ~/src/todo-manager && git checkout <previous commit>
todo-manager health                 # the previous code reads it
```

## Configuration added in this version

| Key | Default | Meaning |
|---|---|---|
| `wake.max_evidence_chars` | 40000 | share of `wake.max_context_chars` for new evidence; the rest is rules, open tasks, follow-ups, format |
| `wake.max_questions_per_run` | 3 | at most this many `ask` decisions (questions to you) per intake run |
| `slack.thread_watch_days` | 30 | threads with a reply this recent are watched for new replies |
| `slack.thread_index_max` | 200 | at most this many watched threads per conversation |
| `slack.thread_scan_minutes` | 60 | how often older thread parents are scanned |
| `slack.thread_scan_pages` | 5 | history pages per scan (the rest waits for the next scan) |
| `notion.body_reads_per_run` | 10 | page bodies read per feedback run after an edit by someone other than the bot |
| `kanban.max_retries` | 1 | Hermes per-card failure breaker: a failed run parks the card for you instead of a respawn |

## Migrating an older Notion todo system

The migration is proposal-based and read-only on the old pages:

```bash
todo-manager migrate fetch --tasks-ds <old tasks data source> --projects-ds <old projects data source>
todo-manager migrate propose ~/.hermes/workspace/todo-manager/migration/<stamp>
# edit tasks.csv / projects.csv: action = keep | history | drop (resolve every "review")
todo-manager migrate apply ~/.hermes/workspace/todo-manager/migration/<stamp>          # preview
todo-manager migrate apply ~/.hermes/workspace/todo-manager/migration/<stamp> --yes --resolve-threads
```

`keep` rows become open tasks (pushed to Notion on the next sync); `history` rows become closed
tasks that never appear in Notion but stop old reminders from re-creating work you already
closed; `drop` rows stay only in the old system. Nothing old is modified or deleted.
`migration.LEGACY` describes the old schema; adapt it for a differently named database.

## Recovering from failures

- A gate never crashes the cron job: failures are recorded per step, the wake line is still
  printed, and one failing system (Notion, Kanban, a source) does not stop the others.
- Outbound writes go through the outbox with retries and back-off; an uncertain write is
  reconciled (looked up) before any retry. Given-up writes show in `health`; after fixing the
  cause, the next dirty scan re-queues the push.
- Feedback that the model claimed but never applied is retried (bounded), then marked failed
  with a reminder.
- Overlapping runs are prevented by per-gate locks; a restore takes both locks.
