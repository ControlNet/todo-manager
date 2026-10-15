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
git clone https://github.com/ControlNet/todo-manager.git   # any location; the jobs run from this checkout
cd todo-manager
python3 -B -m tests.run --all                     # parallel; expect "OK" (Hermes integration tests skip without Hermes)
./bin/todo-manager install                        # asks only what it cannot infer; every outside change needs a yes
```

Requirements: see the [README](../README.md#requirements) (Linux, Python ≥ 3.11 with the
standard library only, Hermes with cron and Kanban, a Notion integration, source credentials).

`install` discovers settings in a temporary workspace. It asks about source scope (selected
mailboxes, Slack conversation types, GitHub participation), the Notion home page and identity,
a valid execution profile or disabled delegations, and whether new cron jobs should start.
Previously configured choices are kept. Discovery performs remote reads only. All concrete
writes are shown in one combined preview; confirmation never supplies a missing answer.

Without a terminal it returns structured `needs_answer` or `needs_confirmation` and exit 3.
Repeat the returned command with the requested `--answer KEY=VALUE`. After all answers are
known, use the provided confirmation command with `--yes`. There are no separate workspace,
Hermes, Notion or schedule CLI stages. Source secrets remain in environment/token helpers.

The resulting two jobs are `todo-manager-intake` (`*/5 * * * *`) and
`todo-manager-feedback` (`2-59/5 * * * *`). Installed scripts call fixed Python pre-run entry
points. Hermes owns Script Output injection, skills, project instructions, native memory,
workers and delivery. Missing memory/skill or shadowed project instructions are reported by
`status --details`; no loader or global settings repair is installed.

## Upgrade

Read [the CLI transition procedure](cli.md#switching-from-the-old-cli) before this breaking CLI
release. Drain old callers with the old checkout first; do not pull new code underneath an
active old launcher. Use a separate checkout to test and prepare the new installation. The new
installer refuses woken unsettled runs, but arbitrary main-chat sessions must also be drained.

```bash
./bin/todo-manager install --json       # collect missing answers and inspect combined changes
# Repeat the returned command, including all choices, with --yes only after review.
./bin/todo-manager status --details --json
```

Owned gate/launcher/skill/prompts/AGENTS are backed up then updated. Unknown ownership conflicts
stop the combined installation. User cron schedule, deliver, failure_deliver, workdir, skills,
model, provider, reasoning_effort, and paused/running state are preserved. No native memory is
replaced. A safety backup precedes local upgrade; remote application is re-runnable, not atomic.
Global Hermes switches are read and reported, never modified.

## Change the automation

Ask Hermes in chat (it uses the skill), or change it yourself:

```bash
todo-manager config set slack.quiet_minutes 60 --human        # e.g. "wait an hour of Slack silence before analysing"
hermes cron edit <job id> --schedule "*/10 * * * *"   # a cron setting: kept by later upgrades
hermes cron list                                      # find the two native job IDs
```

A scheduled run does not change framework files or native cron settings. Authenticated user
feedback may change allowed source settings or the workspace `PREFERENCES.md` (Todo conventions;
used from the next run). Edit that file yourself or ask Hermes in chat; `todo-manager status`
shows its path as `preferences_file`, and health reports it when it is longer than the 8000
characters a run sees or contains phrases Hermes refuses in a cron prompt.

## Daily checks

```bash
todo-manager status                         # operational summary, exit 1 for problems
todo-manager status --details --json        # health, installation, paths, metrics and native usage
```

Use `task show ID --history` or `project show ID --history` for object history.


Problems (stale sources, failing channels, given-up writes, quarantined delegations, failed
feedback, a gate that has not run, a run deferred over its context budget, Notion pages a run
could not use) also appear in the Notion freshness box. A problem is identified by what is wrong
(the source scope or channel, the kind of problem; `problem_keys` in `status --details --json`), not by its
text, so a changing error message is still one problem.

todo-manager sends no messages of its own. Results, questions and failures of delegations,
replies to feedback and notices (an unknown approval outcome, a held comment, failed feedback)
are Notion comments on the task page, or on the home page when there is no task page (chat
feedback without a task, a task page in the trash). What a woken cron run reports (its last line) is
delivered by that Hermes cron job's own `deliver` setting: `install` creates the jobs
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
  (`status --details --json` shows `"channel": true`): `notion`/`comments`, `bodies`, `dirty`, `home`;
  `kanban`/`settings`, `messages`, `submit`, `cards`; `gate`/`claims`, `alerts`, `outbox`. A pass
  with a failure (an API error on any page or card, or the step raising) adds a consecutive
  failure with the first error; the next clean pass clears it. A channel that merely had nothing
  to do is never stale. A Notion page that is gone is reported as that page's problem, not as a
  channel failure. None of this creates feedback or wakes the model.

```bash
todo-manager status --details --json | python3 -c 'import json,sys; r=json.load(sys.stdin); [print(s["source"], s["scope"], s["failures"], s["error"]) for s in r["health"]["sources"] if s["failures"]]'
```

Expected when all is well: no output. A failing channel prints e.g. `kanban cards 2 snapshot failed: …`.

Model cost: every run that wakes the model is recorded with its reasons; `metrics` adds
prompt/completion tokens of the two jobs from Hermes' `cron/usage_audit.jsonl`.

## Pause, resume, roll back

```bash
todo-manager pause                # the two cron jobs only; workers may still run
todo-manager sync --json          # deterministic pass; no semantic claims or model
todo-manager resume               # the two cron jobs only; recovery holds remain
```

Before recovery, export the current workspace, stop old business callers and inspect the
replacement with `restore`. Existing data is replaced only with `--yes`, after a safety backup.
A restored workspace keeps delegation holds and unknown external actions non-replayable.
Use `resume --release-recovery-hold --yes` only after comparing the native board and uncertain
effects; this separate action does not resume the cron jobs. See [CLI recovery](cli.md#restore-and-controls).

## Uninstall

```bash
todo-manager uninstall --dry-run      # what would be removed and what stays
todo-manager export                   # optional: a portable copy of everything
todo-manager uninstall --yes
```

Removed: the two cron jobs (`hermes cron remove`), the gate scripts, the launcher, and the
skill only when it is unchanged since install (a skill you or Hermes changed is kept; remove it
in Hermes if you no longer want it). Kept: the workspace (database, `config.toml`, `AGENTS.md`,
`rules-archive.md`, `PREFERENCES.md`, exports, backups), the Notion pages and the Hermes board. Hermes' `USER.md`,
`MEMORY.md`, `SOUL.md` and other skills are never touched. Close open delegations first
(`todo-manager delegate cancel <id>`), otherwise Hermes keeps working on their cards. To delete
the data as well, remove the workspace directory yourself after exporting. `install` again
brings everything back on the same data.

## Export, backup, restore

Two different things:

- **Internal safety backup** (accepted by `restore`): one consistent SQLite file (tasks, history, evidence,
  feedback, delegations, deliveries, outbox, cursors, the retired rule tables) plus a copy of
  `config.toml`, `AGENTS.md` and `rules-archive.md` in `backups/<name>.files/`. A restore puts
  both back (the `pre-restore` safety backup keeps the current ones). Delivery files and run
  context are not in a backup.
- **Portable workspace** (`export` / `restore`): business data and declared framework evidence; native profile assets are excluded. The
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
  earlier `exports/`, `notes/`, `migration/` working files and an actively restored installation. Installation records, baselines, conflict notes and
  prior managed files are exported separately under `framework/` as inspection/recovery evidence.
  History that exists only on the Hermes board (card comments, run logs) stays in Hermes; what
  todo-manager needs from it (questions, answers, deliveries and their files, failures) is
  already copied into its own tables when the card is synced.

```bash
todo-manager export            # exports/<stamp>/: data.json, workspace/, tasks/*.md, projects/*.md, views/, manifest.json
todo-manager export --verify   # also rebuilds a scratch database from data.json, compares every table, checks history
todo-manager restore backups/todo-<stamp>.sqlite --yes   # integrity-checked; exclusive workspace lock
```

`export` prints `"integrity": []` and `"artifact_problems": []` when all is well and exits 1 when
the history check fails (`--verify` also fails then). `restore` (and its preview without
`--yes`) shows `integrity_problems` for the backup; a restore is not refused for them (it is the
way back), an import is.

Moving to a new directory or machine (an absent/empty target is rebuilt directly; an existing target requires preview and --yes.
Nothing is replaced until the bundle has been checked against its manifest and history):

```bash
todo-manager export
todo-manager --home /path/to/new/todo-manager restore /path/to/export-bundle
# Configure credentials privately using the original environment or token helper.
todo-manager --home /path/to/new/todo-manager status --details
todo-manager --home /path/to/new/todo-manager delegate list
todo-manager --home /path/to/new/todo-manager resume --release-recovery-hold --yes   # after checking the board
```

**No replay after an import or restore.** Claims of runs that no longer exist are released;
Notion comments that may already have gone out are marked dead instead of
being sent again (idempotent Notion state pushes stay queued); card submissions and messages to
the board are held. `status --details` shows "delegations held since …" until you have compared the board
(`hermes kanban --board todo-manager list --tenant todo-manager`) with `todo-manager delegate list` and run
`resume --release-recovery-hold --yes`. Old workers are never restarted by todo-manager; the next sync reads the
board as it is.

## Upgrading an existing workspace (schema migrations)

This version uses schema 10. Schema 9 added child-card associations and event history, message
target cards, pinned notification routes and predecessor delegation/delivery IDs. Schema 10
adds persistent child-discovery synchronization responsibility. F01–F05 makes no schema change. Existing task,
feedback, approval and uncertain-action data remain intact. Installation, not inspection, creates
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
| `email.labels` | false | label mail linked to tasks in Gmail (see below) |
| `email.label_root` | `Todo` | top-level Gmail label they go under |

Keys that no longer have an effect (`status --details` names them while `config.toml` still sets them):

| Key | Why |
|---|---|
| `kanban.max_running` | Hermes schedules agent work; its dispatcher limits apply (`hermes config get kanban.max_in_progress`) |
| `wake.over_budget`, `feedback.over_budget` | an over-budget context is clipped with pointers and the run goes ahead |
| `wake.max_questions_per_run` | when to ask is a preference in the workspace `PREFERENCES.md` |

## Gmail labels

With `email.labels` on, every mail linked to a task carries two Gmail labels: its project,
`Todo/<project>`, and the state of its tasks there, `Todo/<project>/<Open|Done|Cancelled>`
(Chinese: 进行中 / 已完成 / 已取消). Open wins while one of the mail's tasks in that project is
open, then Done, then Cancelled. Tasks without a project are under `Todo/Other` (其他); mail of
two projects carries the labels of both. Dismissed mail and mail never read get no label.

The labels follow the tasks: closing a task, moving it to another project or renaming the
project changes them at the next sync (a renamed project renames its labels; the mail keeps
them). They are plain code, no model, and are sent with the sync (daemon or feedback gate);
only differences are sent, nothing when nothing changed. Todo Manager adds and removes only
the labels it created or found under these names; it never archives, marks read or deletes
mail. A label you delete in Gmail comes back with the next change. A failing pass shows as
`gate/gmail_labels` in `status` and is retried by the next sync.

```bash
todo-manager gmail-labels show                        # labels and mail counts; no request to Gmail
todo-manager config set email.labels true --human     # turn on; the next sync labels the mail
todo-manager sync                                     # or do it now
todo-manager config set email.labels false --human    # turn off (labels already set stay)
```

Each mailbox's token needs the `gmail.modify` (or full `mail.google.com`) scope; with a
read-only token the pass fails and `status` shows the Gmail error.

## Real-time sync daemon

Optional and off by default. With it, Notion comments and drags are picked up within seconds
(property events within about a minute), local writes and card events reach Notion at once, and
the feedback gate only claims feedback. Design: [realtime-sync.md](realtime-sync.md).

Turn it on:

```bash
# 1. The unit and the receiver (loopback only); the daemon polls Notion until step 3 is done.
todo-manager config set daemon.enabled true --human
todo-manager install                 # shows and then writes ~/.config/systemd/user/todo-manager-daemon.service
todo-manager daemon status           # expect "healthy": true, "unit": "active", "token": false, "mode": "polling"

# 2. Public entry (outward-facing: run it yourself). Cloudflare Tunnel connector:
#    install cloudflared and `sudo cloudflared service install <TUNNEL_TOKEN>`, then add a public
#    hostname with path ^/notion$ -> http://127.0.0.1:8655
systemctl is-active cloudflared      # expect: active

# 3. Notion: integration settings -> Webhooks -> Create subscription
#    URL https://<your hostname>/notion; events: comment created/updated/deleted, page created /
#    properties updated / content updated / moved / deleted / undeleted (the integration needs
#    the "read comments" capability). Open enrollment first: only then does the daemon accept
#    the verification token Notion sends (15 minutes; press Resend in Notion if it ran out):
todo-manager daemon enroll
#    ... create the subscription in Notion, then:
todo-manager daemon token            # paste it into Verify
todo-manager daemon status           # expect "token": true, "mode": "webhook"; comment on a task:
                                     # "accepted" grows and the comment is read within seconds
```

Turn it off: `todo-manager config set daemon.enabled false --human && todo-manager install` (the daemon
stops by itself within about 20 s of the edit; `install` removes the unit; the gate syncs again at
its next run). `pause` stops the two cron jobs only:
the daemon keeps syncing but no longer starts the feedback job; stop it as well with
`systemctl --user stop todo-manager-daemon`.

Checks: `todo-manager daemon status` (state, mode, last delivery, bad signatures, last error);
`journalctl --user -u todo-manager-daemon` (start-up line and crashes; requests are not logged).
`health` and the home callout report the `daemon` channels: `alive` (the gate found the daemon
stale, on other code or waiting for install, and synced itself) and `webhook` (our writes
produced no deliveries: tunnel or subscription broken; the daemon polls meanwhile). After code
updates and `config.toml` edits (`config set` or by hand) the daemon restarts itself within about
20 s; after a schema upgrade it waits for `todo-manager install`.

## Migrating an older Notion todo system

The migration is proposal-based and read-only on the old pages:

```bash
python3 -B -m todo_manager.migrate_legacy fetch --tasks-ds <old tasks data source> --projects-ds <old projects data source>
python3 -B -m todo_manager.migrate_legacy propose ~/.hermes/workspace/todo-manager/migration/<stamp>
# edit tasks.csv / projects.csv: action = keep | history | drop (resolve every "review")
python3 -B -m todo_manager.migrate_legacy apply ~/.hermes/workspace/todo-manager/migration/<stamp>          # preview
python3 -B -m todo_manager.migrate_legacy apply ~/.hermes/workspace/todo-manager/migration/<stamp> --yes --resolve-threads
```

`keep` rows become open tasks (pushed to Notion on the next sync); `history` rows become closed
tasks that never appear in Notion but stop old reminders from re-creating work you already
closed; `drop` rows stay only in the old system. Nothing old is modified or deleted.
`migration.LEGACY` describes the old schema; adapt it for a differently named database.

## Recovering from failures

- Recoverable source/sync failures are recorded per step without pretending inputs are complete.
  A gate-level exception exits nonzero. Hermes may inject Script Error and wake the agent; the
  prompt requires [CRON_FAILURE] with no business mutation.
- Outbound writes go through the outbox with retries and back-off; an uncertain write is
  reconciled (looked up) before any retry. Given-up writes show in `status --details`; after fixing the
  cause, unchanged projections retain their retry/exhaustion state; a real projection change
  uses the existing replacement/reconciliation path without resetting active Retry-After, and a
  newer task or project push replaces a given-up one.
- Feedback that the model claimed but never applied is retried (bounded), then marked failed
  (a comment on its task page; `status --details` lists it).
- Overlapping runs are prevented by per-gate locks; a restore takes the shared writers’ workspace lock exclusively.
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

## Installing the decision board (schema 11)

Use the same `install` preview/confirmation as other upgrades. It migrates the Notion UI,
not just the local database. Schema 11 adds the nullable Decision field; pre-schema and
pre-install backups retain the old data. Remote schema/view snapshots are saved separately
in `backups/notion-decision-ui-v1.json`. See the current [CLI migration contract](cli.md#decision-board-and-notion-installation-upgrade).

```bash
todo-manager pause
# Let any already running intake/feedback finish before changing its checkout.
todo-manager install --json
# Review the returned concrete changes and use its confirmation command.
todo-manager status --details
todo-manager resume
```

Installation preserves the paused state. Resume only jobs that were running before maintenance.
Do not edit a live launcher's checkout while a model turn is still using its previous schema.
