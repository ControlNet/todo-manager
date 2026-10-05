# Operations runbook

All runtime data lives in the workspace (`TODO_HOME`, default
`~/.hermes/workspace/todo-manager`), never in the source checkout. Secrets stay in environment
variables (for example `NOTION_API_KEY` loaded from `~/.hermes/.env`); keep any `.env` /
`.env.local` file out of version control.

## Install

Before the first install, in Notion: create an empty top-level page (it becomes the Todo
Manager home page) and add your integration under ··· → Connections. An internal integration
cannot create a top-level page itself.

```bash
git clone <repository url> ~/src/todo-manager
cd ~/src/todo-manager
python3 -m unittest discover -s tests -t .        # expect "OK" (Hermes integration tests skip without Hermes)
./bin/todo-manager install                        # asks only what it cannot infer; every outside change needs a yes
```

Requirements: Python ≥ 3.11 (standard library only), Hermes with cron and Kanban.

`install` runs these steps in order. Each step detects what is already done, so the command is
safe to re-run (to finish an install, or after an upgrade):

| Step | Inferred from | Asked / confirmed |
|---|---|---|
| `workspace` | `hermes config get display.language` and `timezone`, else the system time zone | the time zone, only when Hermes has none and the machine runs on UTC |
| `email` | himalaya accounts with a Gmail token command (`~/.config/himalaya/config.toml`); addresses from the Gmail profile | which mailboxes to read, when there are several |
| `slack` | `~/.hermes/credentials/slack_user_token`, else `SLACK_USER_TOKEN` in Hermes' `.env`; `auth.test` gives your user id and workspace | — (DMs, group DMs and member channels are read; leave some out with `slack.exclude`) |
| `github` | `gh auth token`; `GET /user` gives your login | — |
| `notion` | `NOTION_API_KEY` in Hermes' `.env` (`hermes config env-path`); your user id is the workspace's only person | **the home page, always** (a list of the top-level pages shared with the integration; an id or URL is accepted too); the writes (`notion plan`) need a yes; your user, when the workspace has several people |
| `kanban` | `kanban.review_dispatch` / `kanban.auto_decompose` and the board | a yes for `hermes config set …` (global Hermes settings) and creating the board |
| `hermes` | — | a yes for the launcher, gate scripts, skill and cron jobs (below) |
| `schedule` | — | a yes to start the cron jobs |

What is inferred or chosen is written into `config.toml` key by key; comments and everything
else in the file stay as they are. A key you set yourself is kept: a source whose `enabled` is
in `config.toml` is left as configured, and identities are only added to. No token is written
to `config.toml`, and reading the other systems is read-only (nothing is sent).

Without a terminal (for example from a Hermes chat) `install` stops at the first question or
confirmation, prints it as JSON and exits with status 3; the JSON carries the command to run next:

```bash
./bin/todo-manager install                                   # {"status": "needs_answer", "key": "notion.home_page", "options": [...], "answer_with": "..."}
./bin/todo-manager install --answer notion.home_page=1       # an option number, a page id or a page URL
./bin/todo-manager install --answer notion.home_page=1 --yes notion   # or --no notion; --yes all confirms every step
```

Answers are not remembered between runs until their step is done, so repeat them (the
`answer_with` / `confirm_with` commands do). The separate commands still exist for single steps:
`init` (workspace only), `notion plan|setup`, `hermes install|status|resume|pause`.

The `hermes` step writes a launcher `~/.local/bin/todo-manager`, two pre-run scripts under
`~/.hermes/scripts/`, the skill (only if no skill of that name exists — Hermes may evolve it;
an existing skill is never overwritten, differences are written to
`state/hermes/skill-proposal/` for you to review) and two cron jobs:

| Job | Schedule | Pre-run gate |
|---|---|---|
| `todo-manager-intake` | `*/5 * * * *` | fetch sources, batch, wake the model only for mature batches, due follow-ups or the daily commit digest |
| `todo-manager-feedback` | `2-59/5 * * * *` | Notion edits/comments, Kanban results, outbox, home freshness; wakes the model only for feedback that needs interpretation |

Re-running `install` or `hermes install` updates scripts and job settings in place (keeping
paused state) and leaves what is current untouched. `hermes install --dry-run` prints the plan only.

## Daily checks

```bash
todo-manager health            # problems first; exit code 1 when there are problems
todo-manager health --json     # sources, outbox, feedback, delegations, runs
todo-manager metrics --days 7  # runs, wakes, completions, errors, tasks created, model token usage
todo-manager hermes status     # launcher, scripts, skill state, jobs
todo-manager history <task id> # field-level history of one task or project
```

Problems (stale sources, failing channels, given-up writes, quarantined delegations, failed
feedback, a gate that has not run, a run deferred over its context budget, Notion pages a run
could not use) also appear in the Notion freshness box. A problem is identified by what is wrong
(the source scope or channel, the kind of problem; `problem_keys` in `health --json`), not by its
text, so a changing error message is still one problem.

todo-manager sends no messages of its own. Results, questions and failures of delegations,
replies to feedback and notices (an unknown approval outcome, a held comment, failed feedback)
are Notion comments on the task page, or on the home page when there is no task page (chat
feedback without a task, a task page in the trash). What a woken cron run reports (its last line) is
delivered by that Hermes cron job's own `deliver` setting: `hermes install` creates the jobs
with `local` (kept in Hermes, nothing sent) and never changes it afterwards, so to get run
results in a chat set it in Hermes, e.g. `hermes cron edit <job id> --deliver matrix` (the
home channel of that platform). An idle gate (nothing to interpret) wakes no model and
delivers nothing.

- A **source scope** is stale after 30 minutes without a successful fetch, plus its own polling
  interval (quiet Slack conversations are read about every `slack.quiet_poll_minutes`); when more
  than three scopes of one source are stale (e.g. an expired Slack token with all DMs included)
  they are reported as one line. Any failed fetch counts at once.
- A **scheduled scope** (the daily commit digest, `commit`/`commits:<repo>`) carries its next due
  time (`next_due_at`, tomorrow's `commits.digest_time`): it is stale only 30 minutes after that
  without a read, or at once when a due read fails — never between two daily reads.
- A **gate channel** is one of the feedback gate's own steps, kept in the same `cursors` table
  (`health --json` shows `"channel": true`): `notion`/`comments`, `bodies`, `dirty`, `home`;
  `kanban`/`settings`, `messages`, `submit`, `cards`; `gate`/`claims`, `alerts`, `outbox`. A pass
  with a failure (an API error on any page or card, or the step raising) adds a consecutive
  failure with the first error; the next clean pass clears it. A channel that merely had nothing
  to do is never stale. A Notion page that is gone is reported as that page's problem, not as a
  channel failure. None of this creates feedback or wakes the model.

```bash
todo-manager health --json | python3 -c 'import json,sys; r=json.load(sys.stdin); [print(s["source"], s["scope"], s["failures"], s["error"]) for s in r["sources"] if s["failures"]]'
```

Expected when all is well: no output. A failing channel prints e.g. `kanban cards 2 snapshot failed: …`.

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
- **Portable workspace** (`export` / `import`): everything needed to continue elsewhere. The
  database is first copied with SQLite's online backup (one consistent read, safe while gates and
  applies run) and everything is written from that copy: `data.json` (every table, including
  source bodies and history), Markdown views, `workspace/config.toml` with secret-looking values
  redacted (`token_env` / `token_file` names are kept, the secrets themselves stay in your
  environment), `workspace/AGENTS.md`, `workspace/runs/<id>` for the runs in the snapshot
  (decision files and manifests) and the `workspace/deliveries/` files its deliveries refer to
  (copied out of the worker's scratch space at sync time, sealed under a temporary name and
  renamed once complete, with workspace-relative paths and SHA-256), plus a `manifest.json` with
  a hash for every file and table, `artifact_problems` (a kept file missing or changed since it
  was recorded) and `integrity` (each task's and project's history holds exactly versions
  1..its version; no dangling reference). Not included: credentials, `backups/`,
  earlier `exports/`, `notes/`, `migration/` working files and `state/hermes/` skill proposals.
  History that exists only on the Hermes board (card comments, run logs) stays in Hermes; what
  todo-manager needs from it (questions, answers, deliveries and their files, failures) is
  already copied into its own tables when the card is synced.

```bash
todo-manager export            # exports/<stamp>/: data.json, workspace/, tasks/*.md, projects/*.md, rules/, views/, manifest.json
todo-manager export --verify   # also rebuilds a scratch database from data.json, compares every table, checks history
todo-manager backup --keep 14  # consistent SQLite copy in backups/, keeps the newest 14
todo-manager restore backups/todo-<stamp>.sqlite --yes   # integrity-checked; takes both gate locks
```

`export` prints `"integrity": []` and `"artifact_problems": []` when all is well and exits 1 when
the history check fails (`--verify` also fails then). `restore` (and its preview without
`--yes`) shows `integrity_problems` for the backup; a restore is not refused for them (it is the
way back), an import is.

Moving to a new directory or machine (the target must be absent or empty; nothing is written
until the whole bundle has been checked against its manifest and its history):

```bash
todo-manager export
todo-manager import ~/.hermes/workspace/todo-manager/exports/<stamp> --into /path/to/new/todo-manager
export NOTION_API_KEY=...        # secrets come from your environment again, never from the bundle
todo-manager --home /path/to/new/todo-manager health
todo-manager --home /path/to/new/todo-manager delegate list
todo-manager --home /path/to/new/todo-manager delegate release-hold   # after checking the board
```

**No replay after an import or restore.** Claims of runs that no longer exist are released;
Notion comments that may already have gone out are marked dead instead of
being sent again (idempotent Notion state pushes stay queued); card submissions and messages to
the board are held. `health` shows "delegations held since …" until you have compared the board
(`hermes kanban --board todo-manager list --tenant todo-manager`) with `todo-manager delegate list` and run
`delegate release-hold`. Old workers are never restarted by todo-manager; the next sync reads the
board as it is.

## Upgrading an existing workspace (schema migrations)

This version uses schema v7: v2 binds delegation messages to Kanban events, v3 records pending
Notion writes, v4 tracks page-body reads, v5 adds exactly-once decision application and
follow-up checks, v6 adds a revision per Notion comment / managed box / notes stream, the
`op_key` identity of delegations and messages, and the delivery a message is bound to
(`target_seq`, `target_message_id`); v7 adds where each message's comment sits on the card
(`delegation_messages.card_event_before`, `card_event_id`), why a feedback item is held
(`feedback.hold_reason`) and whether a Notion comment is still listed
(`notion_comments.presence`, `presence_at`). v2–v5, v7 and most of v6 are additive (columns
and indexes; the box fingerprint is seeded from the current box so an unchanged box is not
re-read as an edit; rows written before v7 read as "position unknown, not held, listed"). One
step of v6 rebuilds a table: `delegation_messages` is copied row by row (same ids, same order)
into a table without its old table-level UNIQUE, then renamed; nothing is deleted. You do not
need to delete or rebuild the database.

The migration runs when any command first opens the database with the new code. Before it, a
consistent copy is written to `backups/pre-schema-v7-from-v<old>-<UTC stamp>.sqlite` (one copy
per upgrade, named after the schema the code brings).

```bash
todo-manager hermes pause --yes            # no gate runs during the upgrade
todo-manager export --verify               # optional: a portable copy as well
cd ~/src/todo-manager && git pull     # the new code
todo-manager health                        # opens the database: migrates, then lists problems
ls ~/.hermes/workspace/todo-manager/backups/pre-schema-v7-*
todo-manager hermes install                # refresh scripts; jobs keep their paused state
todo-manager hermes resume --yes
```

Expected: `health` runs without a traceback and a `pre-schema-v7-from-v<old>-*.sqlite` file
exists. To check the schema version:

```bash
python3 -c "import sqlite3,os; c=sqlite3.connect(os.path.expanduser('~/.hermes/workspace/todo-manager/state/todo.sqlite')); print(c.execute(\"SELECT value FROM meta WHERE key='schema_version'\").fetchone()[0])"
```

Expected: `7`.

Rolling back to the previous code (verified 2026-10-05 for v7 on a copy of a v6 workspace:
the 8cf3da5 code refuses the migrated file with "database schema 7 is newer than this code
(6)" and reads the pre-migration copy after the swap below; verified 2026-10-04 the same way
for v6 from v5 with 4b2791d; a task created after the upgrade stays only in the set-aside
file). `restore` with the new code would migrate again, so the file is swapped while the jobs
are paused. Changes made after the upgrade are lost (they stay in the set-aside file and the
export).

```bash
todo-manager hermes pause --yes
H=~/.hermes/workspace/todo-manager
mv "$H/state/todo.sqlite" "$H/backups/todo-v7-set-aside.sqlite"   # keep, do not delete
ls "$H/state"                       # no todo.sqlite-wal / -shm left (all connections closed)
cp "$H"/backups/pre-schema-v7-from-v6-<stamp>.sqlite "$H/state/todo.sqlite"
cd ~/src/todo-manager && git checkout <previous commit>    # e.g. 8cf3da5 for v6
todo-manager health                 # the previous code reads it
```

Changes in behaviour you will see after the v7 upgrade (round 3):

- An accept is refused until a version has been delivered and is in review; a legacy accept
  queued before any version is not sent (you are told: nothing to accept).
- A second active delegation for the same Task is refused unless it is explicitly parallel:
  `todo-manager delegate create … --parallel` (a delegate op takes `"parallel": true`).
- A cancel goes out before anything else queued for that delegation and voids what has not
  taken effect (one notice); while it is pending, answers, revisions, resumes, accepts and
  approvals are refused.
- Several approvals for one version are delivered together with one reopen of the review.
- `todo-manager delegate list` and `task show` say "pause requested" while a pause has not taken effect;
  "paused" only once our pause blocks the card.
- Notion page notes, box edits and new-page bodies are stored in full; long items may take
  longer to fit the feedback budget (deferred and reported, never cut). `todo-manager feedback
  item <id>` prints a stored item in full (JSON).
- A body edit is claimed only after a read past the minute of the edit (about a minute later).
- A Notion comment deleted before it was handled is withdrawn; one in a resolved discussion is
  held and listed by `health` until reopened or deleted.
- GitHub: the first read of a matter (or after 14 idle days) turns a person's comment in the
  read window into news even when a bot commented after it.
- For scripts using the Kanban bridge: the `comment` op now returns
  `{"comment_id": …, "event_id": …}` instead of the bare comment id.

Changes in behaviour you will see after the v6 upgrade:

- Direct requests from chat need an identity: `todo-manager feedback request-id` prints a fresh
  `req_…` id; pass it as `--source-id` to `feedback apply-direct` / `feedback record` (a Matrix
  event id works too). Reusing an id for the same text is a retry (applied once); for other text
  it is refused (`REQUEST_ID_REUSED`). The skill tells Hermes to do this.
- An intake or feedback run whose context does not fit even with one item is deferred and
  reported instead of run over budget (see the configuration table).

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
| `wake.over_budget` | `"defer"` | intake run whose context does not fit even with one conversation: `"defer"` (no model run, health alert) or `"proceed"` (run over budget, reported) |
| `feedback.max_context_chars` | 60000 | rendered context of one feedback run; the oldest items that fit are claimed, the rest waits |
| `feedback.over_budget` | `"defer"` | as `wake.over_budget`, for a feedback item that cannot fit alone |

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
  (a comment on its task page; `health` lists it).
- Overlapping runs are prevented by per-gate locks; a restore takes both locks.
- A message to the board that Hermes applied to a newer question or version than the one you
  replied to (someone acted on the dashboard at the same moment) is kept as sent, marked
  `raced` on the message (`todo-manager task show <task>`), and you get a task comment; check
  the card and send a correction if needed.
