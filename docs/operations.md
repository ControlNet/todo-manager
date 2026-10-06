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
`~/.hermes/scripts/`, the skill `todo-manager` and two cron jobs. The jobs are created paused,
deliver `local` (kept in Hermes, nothing sent) and run with `TODO_HOME` as their working
directory, so Hermes loads `TODO_HOME/AGENTS.md` (managed framework instructions, unless shadowed by native priority) as their project context:

| Job | Schedule | Pre-run gate |
|---|---|---|
| `todo-manager-intake` | `*/5 * * * *` | fetch sources, batch, wake the model only for mature batches, due follow-ups or the daily commit digest |
| `todo-manager-feedback` | `2-59/5 * * * *` | Notion edits/comments, Kanban results, outbox, home freshness; wakes the model only for feedback that needs interpretation |

`hermes install --dry-run` prints the plan only.

## Upgrade

```bash
cd ~/src/todo-manager && git pull
./bin/todo-manager install            # or: todo-manager hermes install --dry-run, then --yes
```

What an upgrade does with each part (the record is `TODO_HOME/state/hermes/install.json`, the
texts installed last are under `state/hermes/baseline/`):

| Part | Owner after install | Upgrade |
|---|---|---|
| gate scripts, launcher | Todo Manager | replaced (a file there without the Todo Manager mark is left alone and reported) |
| skill `todo-manager`, the two cron prompts, `TODO_HOME/AGENTS.md` | project | known owned files are backed up under `backups/framework/` then replaced; unknown ownership is reported and preserved |
| cron job schedule, deliver, failure_deliver, workdir, skills, model, provider, reasoning_effort | you | all existing values are preserved |
| cron job state (paused / running) | you | never changed by an upgrade |

Hermes' job store is only read: every change is a `hermes cron create|edit` command. A job whose
workdir is not `TODO_HOME` may miss managed instructions; `health` checks native discovery and memory flags.

## Change the automation

Ask Hermes in chat (it uses the skill), or change it yourself:

```bash
todo-manager prefs show                               # managed framework context, not personal memory
todo-manager config set slack.quiet_minutes 60        # e.g. "wait an hour of Slack silence before analysing"
hermes cron edit <job id> --schedule "*/10 * * * *"   # a cron setting: kept by later upgrades
hermes cron list                                      # find the two native job IDs
```

A scheduled run does not change framework files or native cron settings. Authenticated user
feedback may change allowed source settings or explicitly request native USER.md memory updates.
Memory capacity, enable flags and write approval apply; running sessions retain their snapshots.

## Daily checks

```bash
todo-manager health            # problems first; exit code 1 when there are problems
todo-manager health --json     # sources, outbox, feedback, delegations, runs
todo-manager metrics --days 7  # runs, wakes, completions, errors, tasks created, model token usage
todo-manager hermes status     # launcher, scripts, skill/prompts/AGENTS.md state, upgrade conflicts, jobs
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
3. Notion pages are not deleted by any command. Move them to the Notion trash yourself if you
   want them gone.

## Uninstall

```bash
todo-manager uninstall --dry-run      # what would be removed and what stays
todo-manager export                   # optional: a portable copy of everything
todo-manager uninstall --yes
```

Removed: the two cron jobs (`hermes cron remove`), the gate scripts, the launcher, and the
skill only when it is unchanged since install (a skill you or Hermes changed is kept; remove it
in Hermes if you no longer want it). Kept: the workspace (database, `config.toml`, `AGENTS.md`,
`rules-archive.md`, exports, backups), the Notion pages and the Hermes board. Hermes' `USER.md`,
`MEMORY.md`, `SOUL.md` and other skills are never touched. Close open delegations first
(`todo-manager delegate cancel <id>`), otherwise Hermes keeps working on their cards. To delete
the data as well, remove the workspace directory yourself after exporting. `install` again
brings everything back on the same data.

## Export, backup, restore

Two different things:

- **Backup** (`backup` / `restore`): one consistent SQLite file (tasks, history, evidence,
  feedback, delegations, deliveries, outbox, cursors, the retired rule tables) plus a copy of
  `config.toml`, `AGENTS.md` and `rules-archive.md` in `backups/<name>.files/`. A restore puts
  both back (the `pre-restore` safety backup keeps the current ones). Delivery files and run
  context are not in a backup.
- **Portable workspace** (`export` / `import`): everything needed to continue elsewhere. The
  database is first copied with SQLite's online backup (one consistent read, safe while gates and
  applies run) and everything is written from that copy: `data.json` (every table, including
  source bodies and history), Markdown views, `workspace/config.toml` with secret-looking values
  redacted (`token_env` / `token_file` names are kept, the secrets themselves stay in your
  environment), `workspace/AGENTS.md`, `workspace/rules-archive.md`, `workspace/runs/<id>` for the runs in the snapshot
  (decision files and manifests) and the `workspace/deliveries/` files its deliveries refer to
  (copied out of the worker's scratch space at sync time, sealed under a temporary name and
  renamed once complete, with workspace-relative paths and SHA-256), plus a `manifest.json` with
  a hash for every file and table, `artifact_problems` (a kept file missing or changed since it
  was recorded) and `integrity` (each task's and project's history holds exactly versions
  1..its version; no dangling reference). Not included: credentials, `backups/`,
  earlier `exports/`, `notes/`, `migration/` working files and `state/hermes/` (the install record,
  baselines and conflict notes of this machine).
  History that exists only on the Hermes board (card comments, run logs) stays in Hermes; what
  todo-manager needs from it (questions, answers, deliveries and their files, failures) is
  already copied into its own tables when the card is synced.

```bash
todo-manager export            # exports/<stamp>/: data.json, workspace/, tasks/*.md, projects/*.md, views/, manifest.json
todo-manager export --verify   # also rebuilds a scratch database from data.json, compares every table, checks history
todo-manager backup --keep 14  # consistent SQLite copy + workspace files in backups/, keeps the newest 14
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

This version uses schema 10. Schema 9 added child-card associations and event history, message
target cards, pinned notification routes and predecessor delegation/delivery IDs. Schema 10
adds persistent child-discovery synchronization responsibility. F01–F05 makes no schema change. Existing task,
feedback, approval and uncertain-action data remain intact. The first database open creates
`backups/pre-schema-v10-from-v<old>-<stamp>.sqlite` using SQLite online backup before migration.

Schema <=7 rule records are preserved verbatim in `rules-archive.md`; no natural-language
classification or automatic memory import occurs. Existing schema-8 AGENTS customizations are
preserved by the framework deployment backup. Ask Hermes explicitly to review that archived
content into the actual profile's native USER.md. Report staged, rejected, ambiguous or
capacity-limited entries as not imported. Program upgrade and preference import are separate.

Use the isolated, executable install/rollback steps in
[hermes-native-verification.md](hermes-native-verification.md). Production deployment was not
performed by this development round. Framework backups contain the previous effective prompt,
skill, AGENTS, conflict/baseline files, installation record and business database. A workspace
restore never restores a whole Hermes profile.

To roll back, first pause the two jobs and close all Todo Manager database connections. The
code supporting only schema 9 refuses a schema-10 database. Preserve the new database and use the pre-schema backup
with the old code; post-upgrade changes remain only in the preserved database/export. Restore
old owned framework files from the matching framework backup, and replay each saved cron prompt
through native `hermes cron edit`. Do not copy jobs.json into place or overwrite native memory.

## Configuration

Values are set with `todo-manager config set <key> <value>` (comments and other keys of
`config.toml` stay as they are) and take effect at the next run. Selected keys:

| Key | Default | Meaning |
|---|---|---|
| `slack.quiet_minutes` | 30 | a Slack conversation is analysed after this many quiet minutes ("wait an hour" = 60) |
| `email.min_wake_interval_minutes` | 60 | mail alone wakes the model at most this often |
| `wake.max_context_chars` | 60000 | rendered context of one intake run; conversations that do not fit wait for the next run, the first always goes |
| `wake.max_evidence_chars` | 40000 | share of it for new material |
| `wake.max_item_chars` | 4000 | one message is shown up to this length, then a line with the command for the rest |
| `feedback.max_context_chars` | 60000 | rendered context of one feedback run; the oldest items that fit are claimed, the rest waits; one item is always taken (clipped with a pointer) |
| `slack.thread_watch_days` | 30 | threads with a reply this recent are watched for new replies |
| `notion.body_reads_per_run` | 10 | page bodies read per feedback run after an edit by someone other than the bot |
| `kanban.max_retries` | 1 | Hermes per-card failure breaker; cards allowed an external action always get 1 |
| `hermes.model`, `hermes.provider`, `hermes.reasoning_effort` | unset (Hermes' defaults) | set on the cron jobs by `install`; a value you change in Hermes is kept |

Keys that no longer have an effect (`health` names them while `config.toml` still sets them):

| Key | Why |
|---|---|
| `kanban.max_running` | Hermes schedules agent work; its dispatcher limits apply (`hermes config get kanban.max_in_progress`) |
| `wake.over_budget`, `feedback.over_budget` | an over-budget context is clipped with pointers and the run goes ahead |
| `wake.max_questions_per_run` | when to ask is a scoped preference in native USER.md |

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

## Recovery after the reliability review

[Review operations and evidence](reliability-review-2026-10-06.md) cover schema 10, exact-question
answers and missing-profile recovery. Configure a valid existing `kanban.assignee`; installation
and health no longer report delegation readiness for an empty/invalid profile. A normal feedback
pass first reconciles remote identity, then repairs only an invalid unsubmitted profile from
configuration, with an audit comment. Existing cards are not reassigned.

`delegate answer` reports pending/sent/superseded/error status, not an assumed delivery receipt.
`delegate show` includes child attention and pending discovery. Notification failures appear
separately in health while Notion result synchronization proceeds. Review the existing install
plan to upgrade the owned Notion focus view; customized views remain unchanged.

## R01–R16 reliability recovery

See [the concentrated audit report](reliability-audit-2026-10-06.md) for exact commands,
compatibility, source contracts and scenario evidence. There is no schema/config/CLI migration.
Back up before upgrading; the normal installer retains user cron settings and refuses another
workspace's profile resources. No production upgrade was run for this patch.

An ordinary answer can target a still-open question after a supplement. Approval cannot reuse
that question's obsolete requirements. `delegate answer --question` also accepts a current
failure delivery; old question/failure identities do not become answers to a newer event.
Ended-delegation residual children require explicit cancellation before follow-up work.

Do not reset failed outbox work to bypass a retry deadline. Dirty scans now reuse the target
projection, including agent-work display changes. Old uncertain writes reconcile before new
pushes. A readable `TM effect` marker identifies new Notion comments; legacy or invisible
uncertain comments need manual inspection, not blind resend. Notification registration health
is separate from delivery: a `history_gap` observation never means historical events were sent.

## F01–F05 reliability patch

See [the repair and verification record](freshness-reliability-2026-10-06.md) before upgrading.
Pending creation/body feedback can be superseded after a complete reread: inspect its successor
and interpret again; a claimed old ID cannot authorize a new mutation. Successful identical
operations remain idempotent. Clearing an unresolved project is now a versioned user edit,
even when the local project column was already NULL. Dirty scans preserve inherited per-entity
Retry-After through repeated projection changes.

Gmail selection and body reads can span ticks. Keep their cursor; `complete:false` is remaining
read work, not evidence that nothing matches. Persistent API errors remain health failures.
The patch accepts old selected-thread queues, but old code cannot resume the new `scan` cursor.
Use an explicit workspace backup before upgrading even though schema remains 10; rollback
requires considering the preserved source checkpoint, not just database schema compatibility.
No production switch or Hermes global-setting change is performed by these fixes.
