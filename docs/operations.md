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
at most one Matrix reminder per problem per day.

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

```bash
todo-manager export            # exports/<stamp>/: tasks/*.md, projects/*.md, rules/, views/home.md, data.json, manifest.json
todo-manager export --verify   # also rebuilds a scratch database from data.json and compares every table
todo-manager backup --keep 14  # consistent SQLite copy in backups/, keeps the newest 14
todo-manager restore backups/todo-<stamp>.sqlite --yes   # integrity-checked; takes both gate locks
```

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
