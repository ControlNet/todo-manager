# Known limitations

Concrete, current limitations of this version. See [acceptance.md](acceptance.md) for what is
verified and how.

## Verification

- **Model judgement is not evaluated yet.** One real-model intake run (10 real conversations, in
  the read-only sandbox) was decided and applied; that shows the format works with the real
  model, not that its choices are right. Accuracy of intake (created *and* ignored items) and of
  feedback classification has not been measured. The code enforces structure (full coverage of
  new items, categories and reasons for ignores, versioned writes), not accuracy.
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
  (`truncate_markdown_at`); a body the real API cuts marks the item INCOMPLETE (clarify only).
- **Hermes transitions are checked, not atomic.** The bridge checks the Kanban event an answer,
  acceptance or revision is bound to and then calls the Hermes state function; they cannot share
  one transaction (Hermes' `write_txn` refuses nesting and `complete_task` & co. must not run
  under an outer transaction). A dashboard action landing in between (a new question, a new
  version) is detected afterwards from the events and reported to you as "acted on a newer
  question/version" (`raced`); it is not undone automatically.

## Behaviour

- Notion is polled, not pushed: edits are imported within ~5 minutes; comments on pages
  without recent activity are polled round-robin (default hourly), so a comment there can take
  up to that long to be noticed.
- Page body edits wait for a covering read. Notion stamps edits to the minute, so a read in
  the same minute as the last edit does not count as covering it: a body edit or page note is
  claimed (and a claimed one applied) only after a read past the end of that minute, i.e. up to a
  minute plus one gate interval later. Stamps with seconds are taken as precise to the second.
- A Notion comment whose discussion was resolved before its feedback was handled is **held**:
  not handed to the model, not applied, listed by `health` and announced once. It stays held
  until you reopen the discussion (then it is handled) or delete the comment (then it is
  withdrawn). Feedback already applied is never undone by a later delete.
- A Notion text longer than the run's context budget waits (deferred and reported); nothing the
  user wrote is cut. Background (task fields, deliveries, earlier feedback) may be shortened in
  the context with a `…[+N chars]` mark; `todo-manager feedback item <id>` prints a stored item
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
  cannot fit alone is clipped with a pointer (`todo-manager feedback item <id>`) and handled.
- Items the model leaves for later (`todo-manager item defer`) come back at the date it gives,
  at the latest after 7 days, or earlier with the next activity of their conversation. A
  fetched item counts as processed only when a run recorded an outcome for it.
- Health alerts are sent at most once per problem and day. A problem is identified by what is
  wrong (a source scope or channel, a kind of problem), so a changing error text does not
  re-alert, and a list-type problem (e.g. cancelled delegations not confirmed stopped) that gains
  an entry the same day is not alerted again until the next day (`todo-manager health` shows it
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
  GitHub notification emails read through Gmail do not say who opened the PR, so such an email
  still reaches the model (which can ignore it as `automated_notice`).
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
- One active delegation per Task; a second one needs `parallel` (`delegate create --parallel`,
  or `"parallel": true` in a delegate op).
- One user, one machine, one SQLite database. There is no multi-user or multi-host mode.
- The Hermes Kanban switches `review_dispatch` and `auto_decompose` are global in Hermes; a
  human-review board needs both off for all boards. todo-manager only reads them: while either is
  on, new delegations wait and health reports it; turning them off is your decision because it
  affects every board on that Hermes install.
- The migration importer knows one legacy schema (`migration.LEGACY`); other legacy databases
  need that mapping adapted.
- Not supported by design: Zoom transcripts, Notion webhooks, paid Notion features, a model
  call per commit, reading chat logs as cron input.

## Security

- Source content is treated as evidence and the prompts say so, and every Hermes prompt
  surface passes the Hermes prompt scanner in tests, but prompt-injection resistance ultimately
  relies on the model.
- **Allowed actions and approvals are instructions, not a tool-call interceptor.** todo-manager
  does not sit between a worker and its tools: a worker can call whatever tools its Hermes
  profile has (terminal, mail, `gh`, …). What the code guarantees:
  - an approval is recorded only from you, for one action and one delivery id/version; it is
    voided by a newer delivery or a changed requirement before it is sent, re-checked at send
    time, sent at most once (`[tm:<id>]` marker) and consumed once (a replay cannot reopen a
    newer version);
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
