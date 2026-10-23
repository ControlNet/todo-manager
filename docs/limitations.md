# Known limitations

Concrete, current limitations of this version. See [acceptance.md](acceptance.md) and the
[F01–F05 freshness report](freshness-reliability-2026-10-06.md) and
[R01–R16 reliability report](reliability-audit-2026-10-06.md) for verification and recovery boundaries.

## Hermes-native round (2026-10-06)

- No real model or production service was used in this round. Earlier trial descriptions below
  are historical, not validation of this release's model behavior.
- Child questions and passive native notifications are connected. Child-specific external
  approvals are explicitly refused; root approvals remain version-bound and cannot transfer.
- Pause/resume and ordinary modifications target the root. There is no atomic whole-delegation
  pause; the user is told when descendants are outside that scope. Cancellation has bounded
  ownership re-observation and can remain unconfirmed if workers keep creating new descendants.
- Automatic decomposition receives a shorter native input and does not guarantee copying the
  root protocol/skills. Passing essential constraints is guidance; no execution enforcement
  layer is claimed. Remote workers need access to Todo CLI/full-text materials or must block.
- Missing native subscriptions can be repaired, but past events cannot be replayed through a
  newly registered subscription. Notion retains them; actual channel transport was not tested.
- Generic `api_server` wake delivery is not wired by the passive notification adapter.
- Native assets omitted from exports include profile memory, full Kanban history, notifier
  delivery checkpoints and gateway sessions. The export is not all rules and runtime history.

## Verification

- **Model judgement is not evaluated yet.** One real-model intake run (10 real conversations, in
  the read-only sandbox) was decided and applied; that shows the format works with the real
  model, not that its choices are right. Accuracy of intake (created *and* ignored items) and of
  feedback classification has not been measured. The current small-command interface enforces input validity, identity and versioned
  writes. Items without an outcome remain pending; there is no full-run coverage contract,
  fixed ignore taxonomy or semantic classifier in Python. Relevance and priorities depend
  on the prompts, the workspace `PREFERENCES.md` and native user preferences.
- **Source adapters are verified for fetching, not for judgement.** Gmail, Slack and GitHub are
  covered by recorded-shape tests (`tests/test_sources.py`, fake HTTP) and have been run read-only
  against real accounts (fetch, batching, name resolution, DM discovery and polling cadence,
  context rendering); model decisions were made on one sample run only.
- **Notion writes are tested against a test double.** `tests/fake_notion.py` imitates the
  2026-03-11 API (data sources, property ids, markdown and views endpoints, comments, lost
  responses, 429). The double rounds `last_edited_time` to the minute so tests cover coarse
  timestamps; the code never assumes any particular precision of the real API: every comment,
  managed box and notes stream has its own revision number (content changes, not timestamps),
  and an interpretation claimed by a run is re-checked against a fresh read before it is
  applied (an edit made after the claim but not read yet defers it). Real-API differences can
  surface on the first production setup; it is idempotent and can be re-run.
- **Kanban** is tested both with `FakeKanban` and against the installed Hermes Kanban code in an
  isolated `HERMES_HOME`; the worker is simulated (`tests/integration/hermes_worker_sim.py`),
  not a real model.
- **Not verified against real services:** GitHub notification mail and the same event read
  through the API are de-duplicated by event identity only in tests (the identity is taken from
  the mail's Message-ID suffix or its "view it on GitHub" link; the test mailbox had no GitHub
  conversation mail in the last 365 days, so real mail formats are unverified; a mail whose
  event cannot be identified is kept, never treated as a copy). Real Notion body-edit detection
  and pending-write echo recognition wait for the first production setup.
- **Deleted and resolved Notion comments: the API's answer is assumed.** The comment list
  returns only unresolved comments; a pending comment that is no longer listed is retrieved by id
  (`GET /v1/comments/{id}`). Notion documents 404 for an id that does not exist, not what a
  deleted comment or a comment of a resolved discussion returns. The code takes 404 as deleted
  (feedback withdrawn) and "found but not listed" as resolved (feedback held); any other answer
  holds the feedback as unverified and checks again next run. `FakeNotion` implements this
  assumption; it is unverified on the real API until the first production setup.
- **The Notion `truncated` flag of page markdown** is imitated by `FakeNotion`
  (`truncate_markdown_at`); a body the real API cuts blocks business mutation. Automatic
  complete rereads replace incomplete feedback with a new identity, even when the managed
  region text is unchanged. Read/diagnose/clarify remain available while incomplete.
- **Hermes transitions are checked, not atomic.** The bridge checks the Kanban event an answer,
  acceptance or revision is bound to and then calls the Hermes state function; they cannot share
  one transaction (Hermes' `write_txn` refuses nesting and `complete_task` & co. must not run
  under an outer transaction). A dashboard action landing in between (a new question, a new
  version) is detected afterwards from the events and reported to you as "acted on a newer
  question/version" (`raced`); it is not undone automatically.

## Behaviour

- Without the real-time sync daemon (off by default, [realtime-sync.md](realtime-sync.md)),
  Notion is polled, not pushed: edits are imported within ~5 minutes; comments on pages
  without recent activity are polled round-robin (default hourly), so a comment there can take
  up to that long to be noticed. Read every run (one request per page): agent work, a user
  comment in the last 7 days, a user edit of the page in the last 2 hours. Read every 30 minutes:
  within 48 hours an older user edit, a new open task or a change note on an open task.
  Todo Manager's own writes do not count.
- Page body edits wait for a covering read. Notion stamps edits to the minute, so a read in
  the same minute as the last edit does not count as covering it: a body edit or pending creation request is
  claimed (and a claimed one applied) only after a read past the end of that minute, i.e. up to a
  minute plus one gate interval later. Stamps with seconds are taken as precise to the second.
  This also protects pending creation requests; changed body or completeness supersedes the
  old feedback identity, including claimed contexts. Later notes after that creation request
  is resolved remain context, not automatic instructions.
- A Notion comment whose discussion was resolved before its feedback was handled is **held**:
  not handed to the model, not applied, listed by `health` and announced once. It stays held
  until you reopen the discussion (then it is handled) or delete the comment (then it is
  withdrawn). Feedback already applied is never undone by a later delete.
- A Notion text longer than a run's display budget is explicitly clipped with a full-text
  retrieval pointer; the stored content is retained. This display clipping is distinct from
  an incomplete API read, which blocks business mutation. Background (task fields, deliveries,
  earlier feedback) may also be shortened with a `…[+N chars]` mark; `todo-manager feedback show <id>` prints a stored item
  in full.
- The Notion structure language is fixed at setup. Changing `language` later changes replies,
  reminders and new text, but property and option names stay as created (sync keeps working
  because it reads by property id and accepts option names of every language).
- Supported languages: English and Simplified Chinese. Adding one means adding a catalog in
  `todo_manager/locales/` and registering it in `i18n.LANGUAGES`.
- Unthreaded Slack chat is batched per channel/DM, not per topic.
- Slack thread replies: a reply in a thread whose parent is older than the re-read window is
  found by a scan of older parents every `slack.thread_scan_minutes` (default 60, so up to an
  hour later), at most `slack.thread_scan_pages` history pages per run; a pass cut short by that
  cap resumes below the oldest parent it reached in the next run. Coverage is by **parent**
  time: only threads whose parent is within `slack.thread_watch_days` (30) are scanned, at most
  `slack.thread_index_max` (200) indexed per conversation; a reply in an older thread is not
  found. A message is reported deleted only when the listing that contained it was read in full
  in that run.
- Intake context budget: an oversize conversation is processed in segments over several runs
  (in message order); a segment not yet claimed waits while the conversation is active again.
  The first conversation of a run is always taken: a message longer than `wake.max_item_chars`
  is shown clipped with a line naming the command that prints the rest
  (`todo-manager source show <id>`), and a run whose context is still over
  `wake.max_context_chars` (a large fixed part: open tasks, follow-ups) runs anyway and is
  reported by `health`. Nothing waits because it is long, and nothing is cut without a pointer.
- Feedback context budget: a run claims the oldest feedback items whose rendered context fits
  `feedback.max_context_chars` (default 60000); the rest waits for the next run. An item that
  cannot fit alone is clipped with a pointer (`todo-manager feedback show <id>`) and handled.
- Items the model leaves for later (`todo-manager source defer`) come back at the date it gives,
  at the latest after 7 days, or earlier with the next activity of their conversation. A
  fetched item counts as processed only when a run recorded an outcome for it.
- Health alerts are sent at most once per problem and day. A problem is identified by what is
  wrong (a source scope or channel, a kind of problem), so a changing error text does not
  re-alert, and a list-type problem (e.g. cancelled delegations not confirmed stopped) that gains
  an entry the same day is not alerted again until the next day (`todo-manager status --details` shows it
  at once). One failed pass of a channel is enough to alert; there is no grace count.
- A managed box deleted from a page as a whole is not read as an instruction: it is put back at
  the top on the next push of that task; an earlier edit of the box still pending stays pending
  for the model to interpret.
- A Notion property write whose response was lost is recognised as our own echo only while the
  page still shows exactly the values we wrote; if someone changes one of those fields to the
  same value we wrote, that change is indistinguishable from our echo.
- Slack: only conversations the token's user is a member of can be discovered. Bot channels are
  not detected automatically; they are blacklisted with `slack.exclude` (an unlisted one costs only
  API reads: bot messages are context only, never analysed, and do not make a channel "active").
  The Slackbot DM is skipped: the API does not return its history.
- GitHub first read of a matter (or after 14 days without activity, `REACTIVATE_DAYS`): only what
  happened inside the read window (since the source cursor; the lookback on a first run) can
  become news: a person's comment there is batch, bot comments are context, the opening post is
  news when a person wrote it inside the window. Older history is context only (the opening post
  and the last 3 comments before the window): a request made before the window is shown to the
  model but never turned into a todo by itself.
- The "own PR merged/closed is news only for an open task" rule applies to the GitHub API sources.
  A task names a PR by its link, `owner/repo#N`, or `#N` together with the repository or owner
  name; a task that names it differently is found when its due date has passed (the run searches
  the stored material), not when the PR is merged.
  GitHub notification emails read through Gmail do not say who opened the PR, so such an email
  still reaches the model (which can ignore it as `automated_notice`).
- An event the user takes part in (meeting, review, seminar, workshop) is closed at the first intake
  run after its date (the passed-due-date check), not at its end time: its `due_date` is a date.
  It needs that `due_date`; an event task without one stays open until material about it arrives.
- Projects are created and filled by the intake run, by judgement: when a new or updated task and
  at least two related open tasks belong to one ongoing piece of work the user owns. The run is
  told how many open tasks have no project. Tasks with no new material are grouped only when a
  related item arrives, or by hand (`task update <task> --set project_id=<project>`).
- Blacklist semantics: `slack.exclude` filters **discovery only**; a conversation listed
  explicitly in `slack.conversations` is read even if it is also excluded. A discovered
  conversation that leaves the list (blacklisted, left, or its kind turned off while discovery
  stays on) is retired automatically: batches no run has claimed yet are withdrawn (the batch
  fails with the reason, its items stay stored as superseded), batches already claimed finish.
  After a rate limit or an exhausted time budget, the discovered conversations read least
  recently go first in the next run. A scope removed from the configuration
  itself (an email account, an entry of `slack.conversations`, or all Slack discovery turned off)
  keeps its cursor and is reported as stale (one line per source when there are many) until its
  row is deleted from the `cursors` table; there is no command for that yet. The same holds for
  a gate channel (e.g. `kanban`/`cards`) whose system was turned off while it was failing.
- Portability: an export carries the database, non-secret config, run context and delivery
  files; credentials never. Card comments and run logs that exist only on the Hermes board stay
  there. Delivery files larger than 100 MB are recorded by hash only; a file that was
  already gone when the card was synced is recorded as missing. The database part is one
  consistent snapshot; files cannot be snapshotted with it: the run context of a run still in
  progress at export time may contain files written after the snapshot (such runs' claims are
  released on import, so they are context only), and a kept delivery file changed by hand later
  is exported as it is and listed in the manifest's `artifact_problems`.
- Delegation messages: a comment counts for a result when its own card event precedes the
  result event. Hermes hands a comment to the running worker as a steer, but whether the model
  read it before submitting cannot be seen; a comment posted moments before the worker submitted
  counts as received. If the result ignores it, send a revision.
- A cancel takes priority: messages queued for the delegation that have not taken effect are
  voided (one notice lists them), and while it is pending answers, revisions, resumes, accepts
  and approvals are refused. An accept that completed the card before the cancel ran stands.
  A cancel does not show that an approved external action was not done: an approval whose run
  was still going (or waiting for an answer) when the card was archived is `unknown` and you are
  told; only when no run claimed the card after the approval is it `not_run`.
- Approvals are per delivered version and go out together with one reopen; one that arrives
  after that version was sent back (or while others for it are in flight) is refused, so
  approve again for the next version.
- One unfinished delegation per Task includes known active descendants and pending discovery
  after root completion. An intentionally separate concurrent delegation requires
  `delegate create --parallel`. Cancelling a Task stops proven owned descendants through
  native archive calls and keeps unconfirmed termination visible.
- A later start (`delegate create --start-after`) is honoured by the feedback gate, so the work
  starts within one gate interval (about 5 minutes) after the time, and only while that cron
  job runs. A pause before the time holds the start; a resume lets the time apply again.
- One user, one machine, one SQLite database. There is no multi-user or multi-host mode.
- Hermes `review_dispatch` and `auto_decompose` remain native orchestration settings.
  Todo Manager neither disables them nor blocks all submission while they are on. Native
  review is not user acceptance, and native decomposition does not guarantee complete
  inheritance of the root protocol. Proven child ownership and supported question transfer
  are handled by the existing feedback sync; there is no custom task-graph executor.
- The migration importer knows one legacy schema (`migration.LEGACY`); other legacy databases
  need that mapping adapted.
- Not supported by design: Zoom transcripts, paid Notion features, a model call per commit,
  reading chat logs as cron input.
- The daemon's Notion webhook is tested against the fake Notion only. That our own writes produce
  deliveries (its liveness signal), payload fields beyond the documented examples and the
  Cloudflare Tunnel mapping are checked only at go-live. Property and page events are aggregated
  by Notion (usually under a minute, up to 5); comment events arrive in seconds. A webhook
  reaches only pages the integration can access. While the subscription is unverified or
  silent the daemon polls Notion like the gate.
- Work the daemon owes for an acknowledged event (a failed read) is retried from memory: after
  a daemon restart only the timers and full passes remain, and a full pass does not see a page
  deleted in Notion. A project page deleted in Notion is not imported (as in the gate); task
  pages are.

## Security

- Source content is treated as evidence and the prompts say so, and every Hermes prompt
  surface passes the Hermes prompt scanner in tests, but prompt-injection resistance ultimately
  relies on the model.
- **Allowed actions and approvals are instructions, not a tool-call interceptor.** todo-manager
  does not sit between a worker and its tools: a worker can call whatever tools its Hermes
  profile has (terminal, mail, `gh`, …). What the code guarantees:
  - an approval is recorded only from you, for one action and one delivery id/version; it is
    voided by a newer delivery or a changed requirement before it is sent, re-checked at send
    time (also on the retry of an uncertain attempt while the version still waits in review: a
    voided one whose comment landed is withdrawn on the card with a `WITHDRAWN` comment), sent
    at most once (`[tm:<id>]` marker) and consumed once (a replay cannot reopen a newer
    version; a retry counts it only when its comment is on the card);
  - cards are created with Hermes' per-card breaker `max_retries = 1` (`kanban.max_retries`):
    a crashed, timed-out or stale run parks the card instead of a respawn, and a run that held
    an approval and ended without a result is marked `unknown`, paused and reported to you -
    also when the reopen's response was lost and the approval is confirmed only after the sync
    read the run's end (then one gate tick later; a card with retries left could be restarted
    by Hermes in that tick, as it can between a crash and the next tick in the normal case);
  - the approval record is an audit trail of what you allowed, not proof of what a worker did.
  Whether your words grant an external action at all (a `delegate` op with `send_email` in
  `allowed_actions`) is the feedback model's reading of what you wrote; only actions from the
  known list are accepted and only from your own feedback, but nothing checks the grant against
  your exact words.
  What relies on the worker model following the card protocol: staying within the allowed
  actions, acting only on an `APPROVED` comment for the delivered version, not repeating an
  action whose outcome it cannot confirm, and reporting `external_actions_done` truthfully.
  Hermes requeues a run killed by a provider rate limit without counting it as a failure; that
  next run is covered only by the protocol rule. Hard enforcement would mean a Hermes profile
  for the board's assignee without write tools for mail/GitHub/publishing — a Hermes
  configuration you decide on.
- Tokens are read from environment variables; the workspace must not be committed or shared.

The six issues reviewed at `8e177d3` are addressed in
[the reliability review](reliability-review-2026-10-06.md). Restore hold now also prevents staging
release through notification repair. Child discovery retries survive root completion/restart,
and channel registration failure does not hide business results. These protections do not stop
already running native workers. Ended delegations retain child visibility without accepting new
authority or reopening the entry. Passive notification repair still cannot replay missed events.

## Reliability recovery boundaries

- Answers bind to a card and its currently open question/failure event. A supplement does not
  close that event, but it invalidates old external authorization. Child external approvals
  remain unsupported. An ended root's residual questions are explicitly identified as such;
  cancel residual owned work before creating a follow-up, rather than reopening it by reply.
- A terminal subscription failure remains eligible for registration repair (up to 12 consecutive
  failures, then explicit manual intervention). Registration recovery and native delivery are
  separate; `health` retains a `history_gap` timestamp when history could not be replayed.
- Notion comments carry a readable effect identifier. Recovery requires identity and target
  agreement. Uncertain legacy/unlisted sends remain uncertain and are not automatically
  replayed; the unresolved-only listing API cannot prove that an absent send never happened.
- Repeated dirty scans preserve attempts, permanent failures and retry deadlines for the same
  projection. A changed projection still waits behind an older uncertain write and its delay.
- Unresolved project relations are preserved pending resolution, never converted into a user
  clear. Duplicate project pages are reported without rebinding the original page.
- Only one Todo workspace can own a given set of profile cron/skill/launcher resources. A
  conflicting second installation is refused; an owning user's changed workdir is preserved.
- Gmail history query filtering is evaluated by Gmail through a paginated search, then matched
  thread context is hydrated. It adds API reads; existing spam/trash/draft/chat exclusions
  still define the supported source scope.
- Daily commit reads may span ticks. Skipped, failed or paginated reads do not consume the
  day's completion marker or model attempt, and completed repos do not need rereading that day.

## Gmail incremental selection cost

History candidates are filtered by Gmail's configured query AND Message-IDs from all messages
in each changed thread, then selected threads are hydrated. Typical cost scales with changed
threads, not mailbox size. Missing/unusual Message-ID headers require a resumable broad-search
fallback; a nonmatch can still scan the matching mailbox across ticks. Each adapter call has
40-request / 180-second checking limits and also receives the remaining gate budget. An HTTP
read timeout/token helper is not a hard process deadline. Pagination, text-part hydration and
failed-request checkpoints retain the old history watermark until complete. Live Gmail search
behaviour is not newly validated by this round; see the linked cost tests and official API
references in the F01–F05 report.

## CLI 0.2 transition and recovery boundaries

- No permanent compatibility aliases. Old scheduled/chat sessions must finish against their
  previous checkout before switching. The installer detects unsettled woken run records;
  arbitrary Hermes main-chat sessions require operator coordination.
- `--human` is explicit provenance, not an authentication or tool-isolation boundary.
- The installer previews from a temporary workspace and applies after one confirmation.
  Notion/Hermes writes are individually recoverable, not one atomic distributed transaction.
- Restore excludes supported CLI/pre-run writers with a workspace lock. Old releases and
  external direct Python/SQL writers do not follow that lock and must be stopped.
- SQLite safety backups have narrower file coverage than exports. Neither includes native
  USER/MEMORY/SOUL, full Kanban history or native notification/session state; inspect the manifest.
  Framework snapshots are recovery evidence, never silently deployed by restore.
- Follow-up/digest review and the meaning of a natural-language brief remain model judgments.
  Code enforces claim ownership, settlement identity, explicit action scope and body revisions.
