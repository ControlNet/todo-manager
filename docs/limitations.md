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
- **Matrix reminders** (`hermes send`) are tested with a fake runner only.
- **Not verified against real services:** GitHub notification mail and the same event read
  through the API are de-duplicated by event identity only in tests (the identity is taken from
  the mail's Message-ID suffix or its "view it on GitHub" link; the test mailbox had no GitHub
  conversation mail in the last 365 days, so real mail formats are unverified; a mail whose
  event cannot be identified is kept, never treated as a copy). Real Notion body-edit detection
  and pending-write echo recognition wait for the first production setup.
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
  If even one conversation does not fit (the fixed part — rules, open tasks, follow-ups,
  format — is too large), the run is **deferred**: nothing is claimed, the model is not woken,
  `health` reports it and a Matrix alert goes out once a day, until you shrink the fixed part,
  raise `wake.max_context_chars`, or set `wake.over_budget = "proceed"` (then it runs over
  budget, reported). Nothing is cut silently.
- Feedback context budget: a run claims the oldest feedback items whose rendered context fits
  `feedback.max_context_chars` (default 60000); the rest waits for the next run. An item that
  cannot fit alone defers the run the same way (`feedback.over_budget`).
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
- Matrix reminders are at-most-once: a send that timed out is not retried, because the outcome
  cannot be looked up. After an import or restore, reminders that may already have gone out are
  dropped (marked dead), not re-sent.
- Portability: an export carries the database, non-secret config, run context and delivery
  files; credentials never. Card comments and run logs that exist only on the Hermes board stay
  there. Delivery files larger than 100 MB are recorded by hash only; a file that was
  already gone when the card was synced is recorded as missing. The database part is one
  consistent snapshot; files cannot be snapshotted with it: the run context of a run still in
  progress at export time may contain files written after the snapshot (such runs' claims are
  released on import, so they are context only), and a kept delivery file changed by hand later
  is exported as it is and listed in the manifest's `artifact_problems`.
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
    an approval and ended without a result is marked `unknown`, paused and reported to you;
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
