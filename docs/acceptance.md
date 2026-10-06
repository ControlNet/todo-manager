# Acceptance criteria → implementation → verification

Legend for the evidence column:

- **unit** – automated test against local state and test doubles (`tests/fake_notion.py`
  FakeNotion, `todo_manager.kanban.FakeKanban`, fake runners, synthetic source items and
  synthetic old-Notion pages). Doubles imitate the documented API behaviour; they are not the
  real services.
- **hermes** – automated test against the installed Hermes code (real Kanban database and
  dispatcher logic, real cron prompt scanner) in an isolated temporary `HERMES_HOME`.
- **structural** – guaranteed by how the code is built (no code path exists for the failure).
- **not yet verified** – depends on model judgement or real accounts; needs the real-sample
  evaluation described at the end.

Run everything: `python3 -B -m unittest discover -s tests -t .`.

The current [F01–F05 freshness and recovery report](freshness-reliability-2026-10-06.md) records
feedback revision, project-intent protection, transitive backoff and Gmail request-cost evidence.
The preceding [R01–R16 reliability acceptance report](reliability-audit-2026-10-06.md) records
red-baseline evidence, cross-chains A–H, native contracts and exact split test commands.
Commit-stamped audit tables later in this document describe historical implementations;
removed classifier/coverage rules in those tables are not current guarantees.

## Intake quality

| # | Criterion | Implementation | Evidence |
|---|---|---|---|
| 1 | Relevance reflects user intent and preferences | Hermes model uses native preferences and intake prompt; Todo Manager stores evidence and explicit per-item outcomes. Unhandled items return later; no hard-coded CFP/review classifier or full-run coverage gate | unit: small-command/outcome tests; semantic judgement **not yet verified** |
| 2 | Relevant context is available | Script stdout includes material; long display text is marked and points to full stored content. Reading and interpreting that context remains agent behaviour | unit: context-budget tests; judgement **not yet verified** |
| 3 | Reminders, re-fetches and the same matter across email/Slack/GitHub do not duplicate tasks | Source items are unique per (source, account, external id); thread keys link later messages to existing tasks shown to the model; open tasks are in every intake context | unit: `test_create_and_followup_update_same_matter`, `test_reminder_cannot_reopen_human_cancelled_task`; cross-channel judgement **not yet verified** |
| 4 | Read / expired / silence is not completion; receipts, sent replies, PRs update per done criteria | No code path closes a task on read state or time; closing requires an op with evidence; done criteria are stored per task. Your own PR merged/closed reaches the model only when that PR is evidence of an open task (to update it); otherwise it is context | structural + unit: `test_create_and_followup_update_same_matter`, `test_if_open_task_items_wake_only_for_an_open_linked_task`, `test_tracked_repo_merge_of_my_pr_is_news_only_for_an_open_task`; judgement **not yet verified** |
| 5 | Human business corrections remain protected | Field provenance and task/project versions protect changes, including repository relations. Preferences use native USER/memory, without a Todo rule hash gate | unit: store, feedback-authority and R13 tests |
| 6 | Ignored samples can be audited for misses | Per-item outcomes are stored; prompts guide judgement. No fixed semantic taxonomy, question budget or classifier validator is enforced by Python | unit: small-command outcome tests; judgement **not yet verified** |

## Wake discipline

| # | Criterion | Implementation | Evidence |
|---|---|---|---|
| 7 | No model start without changes, for already-handled feedback or deterministic retries | Gates decide deterministically and print `{"wakeAgent": false}`; outbox retries never wake | unit: `test_no_change_never_wakes`, `test_gate_crash_does_not_wake`, `test_notion_failure_does_not_stop_kanban_and_never_wakes`, `test_comment_wakes_once_with_ids_only_and_reply_lands_in_same_thread`, `test_gate_prints_only_the_wake_line_and_direct_feedback_flow` |
| 8 | Ongoing Slack chat is not re-analysed; ≥ 30 quiet minutes; other sources do not bypass it | Per-batch maturity; Slack quiet time has a floor of 30 minutes and no forced cap | unit: `test_slack_waits_for_30_quiet_minutes_without_forced_cap`, `test_other_source_wake_does_not_pull_immature_slack`, `test_email_alone_wakes_at_most_hourly_but_rides_along` |
| 9 | Commit digest at most daily, skipped without commits; PR pushes do not bypass it | Daily digest batch with bounded retries | unit: `test_commit_digest_once_per_day_and_skips_without_commits`, `test_unfinished_digest_retries_are_bounded` |
| 10 | No reasoning while the board is idle, blocked on you or waiting for review; no manager loop | Card sync is deterministic; only your feedback wakes the model; no orchestrator profile | unit: `test_block_answer_resume_and_stale_answer`; hermes: `test_full_draft_cycle_against_real_kanban` (with `review_dispatch: false`) |

## Feedback and delegation

| # | Criterion | Implementation | Evidence |
|---|---|---|---|
| 11 | Comments distinguish discussion / supplement / delegation / pause / revision; bot replies do not echo | Feedback items carry kind and thread; the model classifies; our bot's comments are never feedback | unit: `test_user_comment_becomes_one_feedback_and_bot_reply_is_ignored`, `test_reply_to_a_delivery_comment_is_tied_to_the_delegation`, `test_comment_from_someone_else_is_marked_not_user`; classification **not yet verified** with the real model |
| 12 | The same feedback / delegation via several entry points never duplicates cards, resumes or deliveries | Feedback unique by (origin, source id, version); delegation messages stored once; one active delegation per task unless explicitly parallel | unit: `test_matrix_direct_feedback_is_recorded_and_applied_once`, `test_double_apply_is_refused`, `test_second_delegation_and_guest_approval_are_refused` |
| 13 | A card blocked on you stays blocked across rounds and restarts, even for a day | Only an answer message unblocks; no timer-based resume | unit: `test_block_answer_resume_and_stale_answer`; hermes: `test_full_draft_cycle_against_real_kanban` |
| 14 | The card keeps question and progress; an old reply cannot clear a newer block | Answers target a block sequence number | unit: `test_late_reply_to_an_earlier_question_cannot_answer_the_new_one`, `test_answer_in_question_thread_unblocks_card` |
| 15 | Review, acceptance and notification have distinct meanings | Native review/decomposition remain enabled according to user settings. Explicit passive routes register native subscriptions; Notion records persistent results. Todo Manager does not disable global orchestration | hermes: native contract probes; real channel delivery **not run** |
| 16 | A finished delivery does not complete the Notion task | Accepting ends the delegation only; task completion is decided separately | unit: `test_review_revision_accept_cycle`, `test_marking_done_accepts_a_delivered_review`, `test_closing_task_through_feedback_accepts_the_delivery` |
| 17 | External actions follow authorisation; approval is bound to the delivery version | Approvals only from you, bound to one action + delivery version, voided by a newer delivery, sent and consumed once; a run that held an approval and failed is parked (`max_retries = 1`), marked unknown and asked about, never respawned automatically. Allowed actions are card instructions the worker model follows: **no tool-call interception** (see limitations) | unit: `test_approval_binds_to_latest_delivery`, `test_second_delegation_and_guest_approval_are_refused`, `ApprovalTests`; hermes: `test_a_failed_run_parks_the_card_instead_of_a_respawn` |

## Concurrency and recovery

| # | Criterion | Implementation | Evidence |
|---|---|---|---|
| 18 | Concurrent creation of one delegation ends in exactly one card | `kanban-submit` file lock + lookup before create + derived idempotency key | unit: `test_concurrent_submitters_create_one_card`, `test_capacity_one_queues_second_delegation_locally` |
| 19 | Crash after create but before saving the card id recovers the card; uncertain lookup never re-creates; archived cards reconcile | `reconcile` state before create; lookup includes archived cards; multiple matches quarantine | unit: `test_lost_create_response_is_reconciled_not_duplicated`, `test_failed_lookup_never_creates`, `test_archived_card_is_found_and_not_recreated`, `test_multiple_matches_are_quarantined`; hermes: `test_archived_card_found_by_lookup` |
| 20 | Interruptions after fetch / after decision / after an external write with lost response recover | Items stay pending until covered by applied decisions; effects are queued in the same transaction; uncertain writes are looked up before retry | unit: `test_double_apply_is_refused`, `test_unapplied_claim_is_retried_then_failed_with_notification`, `test_lost_create_response_does_not_duplicate_page`, `test_lost_comment_response_is_not_posted_twice` |
| 21 | 429, unavailable sources, invalid cursors, large pagination are observable and recover; failure is never "no changes" | Retries with `Retry-After`; fetch errors recorded per source (health + freshness box); Gmail falls back from an expired history id to a windowed list; full pagination | unit: `test_rate_limit_is_retried`, `test_fetch_failure_is_recorded_not_treated_as_empty`, `test_expired_history_falls_back_to_a_paginated_window`, `test_other_errors_are_failures_not_empty_results`, `test_threads_pagination_edits_and_deletions`, `test_api_error_is_a_failure`, `test_rate_limit_defers_the_rest_of_that_source_only`, `test_discovered_scopes_are_read_once_and_survive_a_failed_listing`, `test_many_failing_scopes_of_one_source_are_reported_once`, `test_scopes_dropped_from_a_listing_are_retired_not_stale`, `test_a_rate_limited_tail_is_read_first_next_time`, `test_pending_data_of_a_blacklisted_conversation_is_withdrawn_visibly`, `SlackThreadTests`, `GitHubFetchFailureTests`; real read-only Gmail, Slack (every member conversation discovered; blacklisted bot channels retired) and GitHub notification fetches in a sandbox workspace |
| 22 | Overlapping schedules, simultaneous corrections and late board results never overwrite newer versions | Per-gate non-blocking locks; version checks on every write; delegation-driven status changes yield to later human edits | unit: `test_human_status_change_after_delegation_is_respected`, `test_stale_human_edit_wins_over_feedback_based_on_older_version`, `test_push_reads_before_write_and_keeps_concurrent_human_edit` |
| 23 | Recovery checks existing results and side effects first; no duplicate emails or replies | Outbox reconciliation; comments carry markers; nothing is sent outside Notion and the board | unit: `test_lost_comment_response_is_not_posted_twice`, `test_lost_create_response_is_reconciled_not_duplicated` |
| 24 | Rules, states, evidence, feedback, delegations and deliveries export and rebuild; backups restore | `export` (data, non-secret config, run context, delivery files with hashes), `export --verify`, `import --into` an empty directory, `backup`, `restore`; after import/restore nothing is replayed and card work is held until `delegate release-hold` | unit: `test_export_verify_backup_restore`, `ExportImportTests`, `NoReplayTests`, `DeliveryFileTests`; real: export → import round trip of a scratch copy of the sandbox database (row counts equal) |
| 25 | Your Notion notes survive; logs do not clutter Notion; source freshness is visible | One managed callout per page; edits inside it are interpreted, not overwritten; freshness box rewritten only on change | unit: `test_managed_region_updates_and_user_notes_survive`, `test_user_edit_inside_region_is_held_for_interpretation`, `test_deleted_region_is_reinserted_at_top`, `test_home_freshness_written_only_when_content_changes` |

## Reliability round (baseline 89cf930)

Each item: problem → root cause → fix → tests → commit. Tests marked *hermes* run against the
installed Hermes code (105568c155) in an isolated `HERMES_HOME`; all others use test doubles.

| Item | Root cause | Fix | Tests | Commit |
|---|---|---|---|---|
| R01 cancellation not enforced everywhere | each entry point handled a closed Task itself; submit did not re-check the Task; archive success was assumed | one lifecycle function for every entry; submit re-checks; uncertain creates looked up; archive confirmed from Hermes' termination event; late results kept as history only | `CancellationTests` | c8f5507 |
| R02 stale answer unblocks a newer question | answers were matched by a local sequence number, retries re-sent blindly | messages bound to the Kanban event they answer; check-and-apply in one bridge process (`apply` / `already` / `mismatch`) | `StaleTransitionTests`; *hermes* `test_retried_unblock_is_already_done_and_leaves_the_next_question` | c8f5507 |
| R03 replies in old Slack threads missed | only threads whose parent was in the re-read window were read | bounded thread index + periodic scan of older parents; never-seen threads bring context; unread ≠ deleted | `SlackThreadTests` (FakeSlack) | bec6fe1 |
| R04 decisions applied twice | no record of an applied run; duplicates inside a file accepted | `applied_runs` with decisions hash, ownership checked in the same transaction; identical ops / reused refs / repeated feedback ids rejected | `ApplyOnceTests`, `test_duplicate_feedback_items_are_rejected` | aa5d53a, d3c8054 |
| R05 resumes exceed `max_running` | capacity counted only at submit | one reservation for submit and every resume-type message, under the submit lock | `CapacityTests` | c8f5507 |
| R06 follow-up handled before success | marked surfaced at wake | `followup_checks` claimed at wake, done on apply, bounded retries, explicit `followup_reviewed`, new date = new check | `FollowupTests` | aa5d53a |
| R07 author not an authorization boundary | missing identity defaulted to the user; checks per op | unverified feedback is evidence only, one check for every mutating op; property edits by others are evidence | `MissingIdentityTests`, `NonUserFeedbackTests`, `PropertyAuthorTests` | d3c8054 |
| R08 revoked approval still sent | message not linked to its approval; no send-time check | approval id on the message, re-validated at send; newer version / requirement change voids; used once; unknown outcome held | `ApprovalTests` | c8f5507 |
| R09 body-only Notion edit ignored | body read only when we pushed | bounded body reads after edits by others; notes vs managed box; new page body is part of its task | `BodyDetectionTests`, `BodyFeedbackTests` | 57c533f |
| R10 same-timestamp comment edit lost | version = `last_edited_time` only | version includes a content hash; supersedes the old one; claimed old version re-checked | `CommentVersionTests` | d3c8054 |
| R11 lost write overrides a newer correction | our unconfirmed write looked like a human edit | expected values stored before the write; exact echo recognised on pull | `UnconfirmedWriteTests` | d3c8054 |
| R12 GitHub context too thin | only events inside the window were stored | per-matter cache: body + earlier discussion on first sight / reactivation, status and participation line, body edits by content digest, mail/API copy detection | `RepoContextTests`, `NotificationContextTests`, `MailApiDuplicateTests` (FakeGitHubApi) | e7af7ca |
| R13 (fixed before this round) | — | not modified; regression tests added; 404/410 now stated in the item | `GitHubFetchFailureTests` | e862d24 |
| R14 one batch exceeds the budget | the first batch was always admitted; only evidence estimated | rendered context checked before waking; oversize conversations split in order; fair share per source | `BudgetTests` | 346b878 |
| R15 old delivery bound to the latest run | deliveries read the card's current run | each review event reads its own run; event id unique per delivery | `DeliveryRunTests` | c8f5507 |
| R16 export not portable | delivery files lived in worker scratch space; absolute paths only | files kept under `deliveries/` with hashes; export bundle with workspace; `import --into`; no replay after restore | `DeliveryFileTests`, `ExportImportTests`, `NoReplayTests` | fd62963 |
| A ambiguity → always ignore | prompt rule "when unsure, prefer ignore" | noise ignored; a few high-impact ambiguities become one question each, capped per run | `AmbiguityTests` | 7e80c9b |
| B Hermes preconditions | global switches on by default (on here) | read-only check; no new cards while unsafe; health names the command; transition semantics re-verified | `HermesSettingsTests`; *hermes* settings test | cc21eba |
| C `False` treated as success | bridge returned the raw bool | `transition` op distinguishes done / already / mismatch, reconciled before marking sent | `StaleTransitionTests`, `test_lost_archive_response_is_confirmed_not_repeated` | c8f5507 |
| D approval scope over-stated; respawn could repeat an action | Hermes respawns failed runs before our gate pauses them | cards created with `max_retries = 1`; docs state what is enforced vs model-followed | `test_a_crash_after_approval_is_not_respawned_before_the_gate_runs`; *hermes* `test_a_failed_run_parks_the_card_instead_of_a_respawn` | f1c65ea |
| E discovery/rate-limit fairness, blacklist semantics | discovered scopes read in fixed order; explicit config could be retired by discovery; blacklisted pending batches lingered | least recently read first; explicit config wins; unclaimed batches of retired scopes withdrawn visibly | `test_a_rate_limited_tail_is_read_first_next_time`, `test_an_explicit_conversation_is_read_even_when_discovery_drops_it`, `test_pending_data_of_a_blacklisted_conversation_is_withdrawn_visibly` | 516aff9 |

## Reliability round 2 (baseline 4b2791d)

Same layout. Schema v6 (B01–B05) is additive except the rebuild of `delegation_messages`;
migration and rollback are in [operations.md](operations.md#upgrading-an-existing-workspace-schema-migrations).
Doubles named: FakeNotion, FakeKanban, FakeSlack, FakeHttp (Gmail), FakeGitHubApi, FakeSource.

| Item | Root cause | Fix | Tests | Commit |
|---|---|---|---|---|
| B01 Notion comment edited A→B→A in one timestamp | a version was `last_edited_time` + content; A's old row was reused, B stayed pending | a persistent revision per comment; each content change supersedes the older pending/claimed rows | `CommentRevisionTests`, `CommentRunTests` | d3a7f0c |
| B02 managed box / page notes edits piled up | each read appended an interpretation; a revert to our text left the edit pending | revision per box and per notes stream: a new box edit replaces the pending one, a revert withdraws it; a pending note stays while its lines hold, otherwise superseded with the remainder carried over; an edit not yet read defers the claimed interpretation | `BoxRevisionTests`, `NotesRevisionTests`, `EditAfterClaimTests`; migration `test_v5_box_edits_and_comments_get_revisions` | d3a7f0c |
| B03 accept/revision/resume hit a newer version | the target was resolved when sent (latest delivery / any block) | bound at enqueue to the delivery replied to (or explicit `delivery`, else the latest then) and its review event; a newer version makes it unused (user told), whichever of send and sync runs first; a resume lifts its own pause | `AcceptBindingTests`, `RevisionBindingTests`, `ResumeBindingTests` | 86cc371 |
| B04 second delegate/message op of one feedback swallowed | identity = feedback id (+ kind) | `op_key = <feedback id>#<op index>` (unique); retries return what they created | `OperationIdentityTests`; migration `test_v5_messages_survive_the_rebuild_in_order` | c1df92e |
| B05 retried direct request applied twice | no identity: a fresh timestamp per call | `--source-id` required (Matrix event id or `feedback request-id`); reuse with other text refused (`REQUEST_ID_REUSED`); no dedupe by text | `DirectRequestTests`, `DirectRequestCliTests` | c1df92e |
| B06 status/next_actor half applied | the pair was normalised before user-set fields were protected | pair checked after protection; an inconsistent half is rejected with a reason | `StatePairTests` | 0b3acbf |
| B07 older Slack threads starved | a page-capped scan was recorded as complete | the pass resumes below the oldest parent reached; the watermark advances only after a complete pass | `ThreadScanProgressTests` (FakeSlack) | db7cc08 |
| B08 segment tail claimed during new activity | the quiet period did not apply to a split backlog | a mature batch waits while its conversation has an accumulating unforced batch | `QuietSegmentTests` | 66ad8a2 |
| B09 tail of a long mail lost | body cut to 20000 chars at storage; parts by `attachmentId` skipped | full body stored (2M guard recorded); parts by reference read; transient failure fails the fetch, permanent one is recorded as incomplete; context clips with a `source show` pointer | `GmailBodyTests`, `GmailContextTests` (FakeHttp) | e6e27be |
| B10 GitHub mail/API copies over-merged or lost | copies matched by matter + actor + time; tracked-repo mail routed away at normalisation | dedupe by event identity (`github_event`); unknown identity is never a copy | `MailApiDuplicateTests`, `TrackedRepoMailTests` (FakeGitHubApi) | 685b42d |
| B11 notification comments dropped | only the last 10 comments, no edits, no overlap; a bot's last comment hid the request | every comment since the last view (10-minute overlap, repeats dropped by id/version); a person's comment is news | `NotificationContextTests` (FakeGitHubApi) | d78d2eb |
| B12 sync failures invisible / not alerted | comment, body and Kanban failures and step exceptions only reached the run summary; `notion.problems` was never alerted | channel health in `cursors` (`notion`/`comments`, `bodies`, `dirty`, `home`; `kanban`/`settings`, `messages`, `submit`, `cards`; `gate`/`claims`, `alerts`, `outbox`): a failure counts, the next clean pass clears; a gone page is a page problem; Notion problems kept until the next run and alerted; no feedback or wake | `ChannelHealthTests` | 3b8248b |
| B13 daily commit source stale after 31 min; alert identity unstable | threshold = 30 min + poll interval for a once-a-day read; alert key hashed the text with digits replaced | the digest stamps `next_due_at` (tomorrow's digest time, wall clock); stale only after it or when a due read fails; alerts keyed by source scope / problem kind | `CommitCadenceTests` (incl. a 25-hour DST day), `AlertIdentityTests` | 3b8248b |
| B14 export not a consistent snapshot | Markdown, tables and files read live, table by table; all of `runs/` and `deliveries/` copied | online-backup snapshot first, everything from it; only the snapshot's runs and referenced delivery files; kept files sealed (temp copy, hash, rename); history/reference check on export, rebuild, backup check, restore (reported) and import (refused) | `ConsistentSnapshotTests`, `SealedArtifactTests`, `HistoryIntegrityTests` | 6d30093 |

Boundaries checked separately in this round:

| Boundary | Result | Evidence | Commit |
|---|---|---|---|
| 1 Context budget | intake over budget even for one conversation: deferred (claims undone, no wake), health + alert; `wake.over_budget = "proceed"` opts in. Feedback: `feedback.max_context_chars` claims the oldest items that fit; one item that cannot fit defers (`feedback.over_budget`) | `IntakeOverBudgetTests`, `FeedbackBudgetTests` | bb8c9a3 |
| 2 Hermes transitions | check and state function cannot share a transaction (`write_txn` refuses nesting; `complete_task` & co. must not run under an outer transaction); a dashboard action in between is detected from the events and reported as `raced` (user told) | `TransitionRaceTests` (FakeKanban); *hermes* `test_an_answer_unblocking_a_newer_question_is_reported_as_raced`, `test_an_accept_completing_a_newer_version_is_reported_to_the_user` (real bridge, real Hermes calls interleaved by `tests/integration/hermes_race_bridge.py`) | 31a38be |
| 3 Installed skill and runtime AGENTS.md | nothing installed (no skill, no cron job); runtime AGENTS.md was identical to the template before this round; templates now forbid changing the program, skill, cron jobs, config.toml or Hermes settings and say to stop and report | `SelfModificationRuleTests`; *hermes* prompt scanner test | 7718c03 |
| 4 Permissions and real model | `allowed_actions`: unknown actions refused, non-user feedback cannot delegate or approve, approvals bound to one delivery and used once. Hermes `review_dispatch` / `auto_decompose` read as true (unchanged; no cards are created while unsafe). Real-model read-only trial: see below | existing `ApprovalTests`, `NonUserFeedbackTests`, `HermesSettingsTests`; `EvidenceRoleFormatTests` | 1822899 |

Real-model read-only trial (2026-10-04): the evaluation sandbox (Notion, Kanban and Matrix
off) ran one intake gate on real Gmail/Slack/GitHub data read read-only; the rendered context
(25.9k chars, 10 conversations) plus the skill went to the configured Hermes model one-shot
with only the `clarify` tool. First answer: refused as a whole (evidence roles not in the
format, fixed in 1822899). Second answer after the fix: applied in the sandbox, 9 tasks, 1
project, 29 items ignored, every task with a responsibility sentence. One sample of 10
conversations: it shows the format works with the real model, not that its judgement holds.

## Reliability round 3 (baseline 8cf3da5)

Same layout. Schema v7 is additive (columns only: `delegation_messages.card_event_before` /
`card_event_id`, `feedback.hold_reason`, `notion_comments.presence` / `presence_at`); migration,
backup and rollback in [operations.md](operations.md#upgrading-an-existing-workspace-schema-migrations).
Every item was reproduced by a failing test before the fix. Doubles named: FakeNotion,
FakeKanban, FakeGitHubApi; *hermes* = the installed Hermes Kanban in an isolated `HERMES_HOME`
with the simulated worker (`tests/integration/hermes_worker_sim.py`). No real model ran in this
round.

| Item | Root cause | Fix | Tests | Commit |
|---|---|---|---|---|
| C01 revision after the worker submitted | a revision was routed by our last sync ("running" → steer only), and a delivery's requirement version counted every message *sent before the sync* | the revision checks the card: a version already in review is reopened; a delivery counts only messages whose own comment event precedes its result event (card body = position 0); comment posts record the event before and the comment's own event (bridge `comment` returns `{comment_id, event_id}`) | `RevisionOnTheCardTests`, `SupplementPlacementTests`; *hermes* `test_c01_a_revision_sent_after_the_worker_submitted_reopens_that_version`, `test_c01_a_revision_steered_into_the_running_worker_counts_for_its_result` | c55555d |
| C02 same-minute body edit applied an old interpretation | apply compared edit time and last read by minute; body re-read used a separate one-minute slack | one predicate `notion.body.edit_unread` for re-read, claim and apply; no precision assumed (whole-minute stamp covered only by a read after that minute; a stamp with seconds is precise to the second); body_edit/page_notes items stay unclaimed while the body is unread | `CoarseTimestampTests`, `PreciseTimestampTests`, `PredicateTests` | f434c50 |
| C03 long Notion text cut silently | notes cut to 3000/1500 chars, a new page's body to 4000, the notes baseline to 50000; the model saw a cut text as whole and a cut baseline invented "added" lines | stored in full (`payload.complete`); context shows each item's own text in full, background cut with `…[+N chars]`, earlier items named by id for `todo-manager feedback show <id>`; text the API cut (or an older version stored cut) is marked INCOMPLETE and only takes `clarify`; an oversized item is clipped with a full-text pointer; API-incomplete input remains protected | `StoredInFullTests`, `DisplayCutTests`, `IncompleteTextTests` | a609421 |
| C04 accept without a delivered version | `add_message` accepted it; legacy rows (`target_seq` NULL) resolved "latest" at send time | an accept needs a delivered version in review (CLI, Matrix and scheduled feedback share the check); legacy accepts bind to the latest version received strictly before they were queued; a tie or `target_seq` 0 is unused (`nothing_to_accept`) | `AcceptNeedsADeliveryTests` | c55555d |
| C05 two active delegations for one Task | the rule was only checked in decision validation against the database, so ops in one batch, the CLI and concurrent writers each created one | checked in `delegation.create` inside the insert transaction (after the `op_key` lookup) unless `parallel`; validation counts delegations planned earlier in the batch; CLI `delegate create --parallel` | `SharedCreatePathTests` (incl. an interleaved writer), `SameGateRunTests` | 95b55c5 |
| C06 retried accept turned into "failed" | the retry checked the delegation status, which a sync in between had set to done | a retried accept checks its bound review event on the card first (`already` → sent) | `AcceptRetryTests` | c55555d |
| C07 cancel lost to queued messages | messages went out in queue order (an answer unblocked the card before the archive); a queued accept suppressed the cancel of a cancelled Task | cancels first; other messages that have not taken effect are void (approvals revoked) with one notice; one in flight is checked on the card and kept as history when it landed; an accept that already completed the card makes the cancel unused; answer/revision/resume/accept/approval refused while a cancel is pending | `CancelFirstTests`, `NotionStatusCancelTests`; *hermes* `test_c07_a_queued_answer_is_not_sent_before_the_cancel` | 164a288 |
| C08 lost create + pause never reconciled | `submit_pending` skipped `pause_requested` rows, so the read-only lookup never ran | the lookup always runs; a found card is linked and the pause delivered at once; confirmed not created → `pending_submit` (nothing created until resumed); "paused" means the newest block is our pause | `PauseAfterLostCreateTests`, `PauseRequestedIsNotPausedTests` | e8229fe |
| C09 second approval lost | one message per feedback + kind merged them; otherwise the first approval's reopen voided the second as `not_in_review` | own identity per approval (`op_key`); identical unsent approval is the same one; all valid approvals of one version are posted before one reopen; an approval that cannot reach that run is refused when received; a run ending without a report marks all its approvals unknown | `ApprovalGroupTests`; *hermes* `test_c09_two_approvals_reach_one_run` | 0488245 |
| C10 deleted Notion comment applied | comments were only ever added or edited | after a complete list, each pending comment no longer listed is retrieved by id: 404 → deleted (all revisions superseded); found → resolved (held, user told once, health lists it; released when listed again); other answer → held, rechecked next run; applied feedback is never undone | `DeletedCommentTests`, `UnlistedCommentTests` | bf86ae4 |
| C11 GitHub first read hid a person's request | on first sight / reactivation everything before the notification was context; a bot's latest comment made the notification context too | split at the read window (cursor `since`): a person's comment inside it is batch, bots context; the opening post is batch when a person wrote it inside the window; before the window: body + last 3 comments as context; delivered comments not repeated; a context row is promoted into a batch once | `FirstReadTests`, `PromotionTests` | 3d7fd9f |

Interactions checked: C07 × C09 (a cancel revokes queued approvals; approvals refused while a
cancel is pending), C07 × C04/C06 (an accept that landed before the cancel ends it as done, the
cancel is unused), C08 × C07 (a pause/resume on a delegation that ended is unused), C01 × C08
(a paused delegation's revision waits for the resume), C02 × C03 × C10 (a held or unread item is
neither claimed nor applied; an INCOMPLETE item can only be clarified), C05 × B04 (a retried op
returns its delegation instead of being refused).

Migration v6 → v7 (2026-10-05) on a copy of the read-only evaluation sandbox (schema v6, 9
tasks, 387 source items; copied with the online-backup API): `health` migrated it, integrity
and foreign keys ok, counts unchanged, new columns present; `backups/pre-schema-v7-from-v6-*`
is v6; the 8cf3da5 code refuses the migrated file ("database schema 7 is newer than this code
(6)") and reads the backup after the file swap in operations.md; `restore <backup> --yes` with
the new code restores and migrates again with a new backup. `test_v6_workspace_keeps_its_messages_and_feedback`
covers the same in the suite. The production workspace (schema v1) was not touched.

Full suite: 423 tests, 0 failures, 0 skips (`python3 -m unittest discover -s tests -t .`,
including the 13 *hermes* integration tests).

## Follow-up: approvals confirmed after their run ended (baseline dc1479b)

| Item | Root cause | Fix | Tests | Commit |
|---|---|---|---|---|
| Late-confirmed approval never settled | an approval's outcome was settled only by the sync, at the run's end or result event, and only for approvals already confirmed (`consumed_at`). When the reopen's response was lost, the retry confirmed the approval one tick later, after the sync had read the run's end and moved its cursor past it: the outcome stayed NULL (no `unknown`, no pause; the ordinary failure notice was still sent) or, after a result, never became `reported` | the confirmation (`_sent_transition`) settles the approval in the same transaction from the card's events up to the sync cursor: the first run end or result after the reopen of the approved version's review and after the approval's comment. Run end → `unknown` (notice, pause, nothing replayed); result → `reported`. Ends not read yet stay with the sync. A pause queued meanwhile is not doubled; with a cancel pending the outcome and notice are recorded, the archive replaces the pause | `test_approval_late_confirmation` (FakeKanban: crash, result, two approvals, idempotent ticks and a reopened database, earlier runs and approvals, cancel, pause, sync-not-yet-read orderings); *hermes* `test_late_confirmed_approval_whose_run_failed_is_unknown_and_paused`, `test_late_confirmed_approval_whose_run_delivered_is_reported` (installed Hermes; only the lost response is simulated) | c3ed05e |

No schema change: the approval's bound delivery (`deliveries.source_event_id`), the comment's
card event (`delegation_messages.card_event_id`) and the card's own event log identify the run;
the delegation's `event_cursor` tells which events the sync has read.

Full suite: 439 tests, 0 failures, 0 skips, including 15 *hermes* integration tests (installed
Hermes 0.21.5 in an isolated `HERMES_HOME`).

## Follow-up: a cancel settles approvals already handed to a run (baseline 2480453)

| Item | Root cause | Fix | Tests | Commit |
|---|---|---|---|---|
| Cancel left handed approvals unsettled | the cancel is processed first; after the archive it read only the events after the sync cursor and applied them `quiet`, which skipped the unknown hold for a run end; cancelled delegations are not synced again. An approval delivered normally (`consumed_at` set) whose run crashed, or was still going, before the sync read it kept `outcome` NULL with no notice (a result already became `reported`) | after the archive the cancel reads all of the card's events and settles every handed approval whose outcome is open, for the run it went to (same matching as the late confirmation): result → `reported`; run end without one, or a run still going or waiting for an answer when archived → `unknown` (notice once, no pause, nothing resumed or replayed); no run claimed the card after the approval reached it → `not_run`. Settled outcomes are kept. If the card cannot be read after the archive, the cancel is finished by the retry (archive is idempotent). A run end read while a cancel is pending also settles approvals. The notice for a stopped delegation no longer says it was paused | `test_cancel_settles_approvals` (FakeKanban through `sync_all`: crash, result with the Task cancelled, two approvals, a run still going or blocked, never claimed, earlier failed run, settled outcome kept, repeated ticks and a reopened database, lost archive response, unreadable card after the archive, process killed after the archive, queued pause); *hermes* `test_cancel_after_an_unread_crash_settles_the_approval_unknown`, `test_cancel_after_an_unread_result_settles_the_approval_reported`, `test_cancel_while_the_run_is_going_settles_the_approval_unknown`, `test_cancel_before_any_run_claimed_the_card_is_not_run` | a3101d0 |

Hermes archive behaviour (installed 0.21.5, isolated `HERMES_HOME`): archiving closes the active
run as `reclaimed` and names it on the `archived` event (`run_id`; None when no run was
active); there is no separate run-end event; a worker is only started after a `claimed` event.
No schema change: `approvals.outcome` gains the value `not_run` (free text column).

Full suite: 457 tests, 0 failures, 0 skips, including 19 *hermes* integration tests.

## Follow-up: no notification channel of our own; GitHub bots by GitHub's marking (baseline 47c58f5)

| Item | Root cause | Fix | Tests | Commit |
|---|---|---|---|---|
| Own Matrix reminder channel | todo-manager sent reminders itself (`hermes send -t <notify.matrix_target>`) and asked the user for a target, although Hermes delivers what a cron run reports through the job's `deliver` setting; `hermes install` also reset `--deliver local` (and a failure target) on every re-install | the reminder outbox, `notify.*`, the alerts step and the `notifier` wiring are removed; every notice is a Notion comment (task page; home page when there is no task page or it is in the trash); problems stay in `health` and the freshness box; `hermes install` sets `--deliver local` only when it creates a job and leaves delivery to Hermes afterwards | `test_delivery_is_left_to_the_hermes_job`, `test_unapplied_claim_is_retried_then_failed_and_the_task_page_says_so`, `test_a_reply_to_page_notes_is_a_new_comment_on_the_task_page`, `test_a_reply_about_a_page_in_the_trash_goes_to_the_home_page`, `test_health_channels` (nothing queued outside Notion/board), `ProblemIdentityTests`, `NoReplayTests` (a pending comment is held after a restore) | 5d1558c |
| Hard-coded GitHub bot names | `github.bot_logins` listed dependabot/github-actions/renovate/codecov because the repo source ignored the `[bot]` suffix and the commit digest ignored `user.type` | one check `sources.github.is_bot` (type Bot, `[bot]` login, or `github.bot_logins`, now empty by default) for every GitHub source and the digest | `GitHubBotTests` | cc99992 |

The GitHub repository has no Dependabot configuration, pull requests or branches; Dependabot
alerts and automated security fixes are off (checked 2026-10-05), so nothing was removed there.

Full suite: 462 tests, 0 failures, 0 skips, including the *hermes* integration tests.

## Follow-up: one install command (baseline e8f06e4)

| Item | Root cause | Fix | Tests | Commit |
|---|---|---|---|---|
| Install was six manual commands plus hand-editing config.toml | identities, mailboxes, tokens and the Notion page id had to be copied into config.toml by hand, although Hermes already holds them | `todo-manager install`: workspace, email, slack, github, notion, kanban, hermes, schedule; values inferred from Hermes (`hermes config get`, himalaya Gmail accounts + Gmail profile, Slack `auth.test`, `gh` + `GET /user`, Notion users); asked: several mailboxes, the Notion home page (always, from the top-level pages shared with the integration), several Notion people, the time zone on a UTC machine; Notion writes, global Kanban switches, the Hermes install and starting the jobs each need a yes; without a terminal JSON + exit 3 with the next command | `tests/test_install.py` (27) | 259d843 |
| config.toml could only be rewritten whole | no TOML writer (standard library only) | `config_file.set_values`: key-by-key edits keeping comments and order, checked with `tomllib` before the atomic replace; a value the user set is kept | `tests/test_config_file.py` (10) | 259d843 |
| A re-install always edited both cron jobs | `job_action` re-applied unchanged definitions | an up-to-date job is a `noop` (prompt, script, interpreter, schedule, skills, workdir, model settings compared) | `test_fresh_install_creates_paused_jobs_scripts_skill_and_launcher` (re-plan is all `noop`), `test_delivery_is_left_to_the_hermes_job` | 259d843 |

Read-only check against the real Hermes environment (temp workspace, every step declined,
mutating Hermes commands blocked): mailbox, Slack workspace and user, GitHub login and language
match the hand-written production config; the time zone is asked (Hermes has none, the machine
runs on UTC).

Full suite: 499 tests, 0 failures, 0 skips, including the *hermes* integration tests.

## Hermes-native refactor (baseline 232afdd, Hermes 0.21.5 / hermes-agent 105568c)

Commits: 453bf8f (design and migration plan), f4d1e5e (3-way merge, Hermes prompt-scan
checks), 72149a5 (direct context, small commands, conventions in `AGENTS.md`), 8c8227c (agent
work as its own state, native worker tools, closing cancels), afc6978 (install / upgrade /
uninstall with ownership, settings authority, backups with workspace files).

### Required scenarios

| Scenario | Verified by | Kind |
|---|---|---|
| No new material: no model run | `test_intake.IntakeWakeTests.test_no_change_never_wakes`, `test_feedback.GateTests.test_notion_failure_does_not_stop_kanban_and_never_wakes` | unit |
| The model gets the material directly, no `show` call on the normal path | `test_cli.CliTests.test_gate_context_and_feedback_commands`, `test_context_budget.IntakeOverBudgetTests`, `test_feedback.GateTests.test_comment_wakes_once_with_its_context_and_reply_lands_in_same_thread`; the assembled prompt passes Hermes' own scanner: `integration/test_hermes_prompts` | unit, hermes |
| An oversized item is clipped with a pointer and handled, never stuck | `test_context_budget` (both classes), `test_gmail_body.test_a_clipped_item_in_the_context_says_how_to_read_the_rest`, `test_full_feedback_text.test_over_the_context_budget_it_is_clipped_and_handled_never_waits`, `test_intake_budget.test_an_oversize_fixed_context_is_reported_not_silent_and_never_waits` | unit |
| Fetched is not processed; deferral is bounded | `test_intake.RecordTests.test_items_without_an_outcome_stay_unprocessed_and_come_back`, `test_deferred_material_returns_with_new_activity_or_at_its_date`, `test_unhandled_material_fails_visibly_after_bounded_runs` | unit |
| Idle = success; a failing source degrades visibly; a gate failure is a Hermes script error | `test_intake.IntakeWakeTests.test_gate_failure_is_reported_not_turned_into_idle`, `test_a_source_down_for_an_hour_is_reported_once_a_day` | unit |
| A convention changed in chat or by a Notion comment applies from the next run | `test_prefs.NextRunTests.test_a_convention_changed_in_chat_or_from_a_notion_comment_applies_from_the_next_run`, `test_intake.RecordTests.test_dismissal_refused_when_conventions_changed_during_the_run` | unit |
| "Slack 安静一小时再分析" changes the collection setting | `test_prefs.NextRunTests.test_a_setting_changed_on_request_is_used_by_the_next_gate` (`config set slack.quiet_minutes 60`), `test_intake.IntakeWakeTests.test_a_shorter_slack_quiet_window_is_honoured`, `test_hermes_assets.ScheduledRunAuthorityTests` | unit |
| A scheduled run never changes the program or widens permissions | `test_prefs.SettingAuthorityTests` (all three), `test_answer_approvals.test_a_scheduled_run_cannot_approve_and_unknown_actions_are_refused`, `test_hermes_assets.ScheduledRunAuthorityTests` | unit |
| An upgrade keeps the user's prompts, skill, `AGENTS.md` and cron settings | `test_hermes_install.UserFileUpgradeTests` (4), `CronSettingTests` (5), `test_install.FreshInstallTests.test_an_upgrade_keeps_the_users_prompt_and_cron_settings`, `test_merge3` | unit |
| New users get no academic conventions | `test_prefs.NewWorkspaceTests.test_a_new_user_gets_no_academic_conventions`, `test_hermes_install.FreshInstallTests.test_a_new_workspace_starts_from_the_template_in_its_language` | unit |
| A Notion delegation becomes one native card | `test_notion.test_assigning_agent_asks_the_model_once`, `test_delegation.test_submit_creates_one_card_and_hands_the_task_to_the_agent`, `integration/test_hermes_kanban.test_full_draft_cycle_against_real_kanban` | unit, hermes |
| A late answer goes to the original card | `test_delegation.test_block_answer_resume_and_stale_answer`, `test_answer_approvals`, `integration/test_hermes_kanban.test_an_approval_given_with_an_answer_reaches_the_worker_once` | unit, hermes |
| Sync down, then caught up | `test_delegation_reliability.test_offline_recovery_and_replay_do_not_duplicate`, `test_notion_body.test_body_reads_are_bounded_and_catch_up` | unit |
| Lost responses and restarts never duplicate | `test_delegation.test_lost_create_response_is_reconciled_not_duplicated`, `test_notion.test_lost_create_response_does_not_duplicate_page`, `test_lost_comment_response_is_not_posted_twice`, `test_intake_reliability.ApplyOnceTests`, `test_delegation_reliability.test_lost_archive_response_is_confirmed_not_repeated` | unit |
| The agent's draft is done ≠ the personal task is done; closing cancels, never accepts | `test_delegation.TaskLinkTests.test_card_events_never_change_the_status`, `test_notion.test_marking_done_stops_the_agent_work_and_never_accepts`, `integration/test_hermes_kanban.test_a_worker_completing_natively_ends_the_work_but_not_the_task` | unit, hermes |
| Old workspace and delegations migrate | `test_prefs.RulesMigrationTests` (3), `test_db.test_v7_delegations_keep_working_without_the_capacity_hold`; rollback to 232afdd checked on a scratch workspace (operations.md) | unit, manual |
| Uninstall keeps exportable data | `test_hermes_install.UninstallTests` (4) | unit |
| Backups and exports carry the conventions | `test_cli.test_a_backup_keeps_the_conventions_and_settings_and_a_restore_brings_them_back`, `test_prefs.RulesMigrationTests.test_the_export_carries_both_files`, `test_portability` | unit |

### Tests removed or rewritten, by reason

499 tests at 232afdd, 551 now (75 test functions gone or renamed, 127 new or renamed). No
reliability guarantee lost its test: where the interface changed, the scenario was ported.

| Reason | Tests at 232afdd | Now |
|---|---|---|
| Ported: decision files (`intake apply`, `feedback apply`) replaced by small commands; the same guarantee is checked through the commands | `test_intake.ApplyTests` (6), `test_intake_reliability.ApplyOnceTests` (4), `FollowupTests` (2), `test_feedback.GateTests` (2), `test_feedback_authority.DecisionFileTests`, `test_single_delegation.SharedCreatePathTests` (3), `test_approval_grouping` retried op, `test_direct_requests` (3), `test_cli` gate test, `test_feedback.DirectAndReplyTests` direct request, `test_intake_format` | `test_intake.RecordTests` (11), `test_intake_reliability.ApplyOnceTests` (4) / `FollowupTests` (4), `test_feedback.GateTests` (2), `test_feedback_authority.OneFeedbackManyCommandsTests`, `test_single_delegation.SharedCreatePathTests` (3), retried-command tests, `test_direct_requests` (3), `test_cli.test_gate_context_and_feedback_commands`, `test_intake_format.test_the_command_help_lists_every_evidence_role` |
| Requirement removed the mechanism: coverage check, fixed ignore categories, question budget | `test_every_new_item_must_be_covered`, `test_ignore_needs_category_and_reason`, `AmbiguityTests` (3) | `test_items_without_an_outcome_stay_unprocessed_and_come_back`, `test_dismiss_takes_a_generic_outcome_and_an_optional_label`, `test_any_number_of_questions_is_accepted` |
| Requirement removed the Slack 30-minute floor | `test_slack_waits_for_30_quiet_minutes_without_forced_cap` | `test_slack_waits_for_its_quiet_window_without_forced_cap`, `test_a_shorter_slack_quiet_window_is_honoured` |
| Requirement: no infinite defer; over-budget context is clipped and runs | `test_context_budget` (3), `test_intake_budget` (2), `test_full_feedback_text.test_over_the_context_budget_it_waits_instead_of_being_cut` | `test_context_budget` (2), `test_intake_budget` (2), `test_over_the_context_budget_it_is_clipped_and_handled_never_waits` |
| Requirement: Hermes schedules the board (no `max_running`, no switch gating) | `CapacityTests` (2), `test_capacity_one_queues_second_delegation_locally`, `HermesSettingsTests` (3) | `SchedulingTests` (2), `test_a_second_delegation_is_submitted_at_once_hermes_schedules_both`, `RemovedSettingTests`, `HermesSettingsTests` (3, switches explained, never gating) |
| Requirement: closing a task cancels, never accepts | `test_closing_task_through_feedback_accepts_the_delivery`, `test_marking_done_accepts_a_delivered_review`, `test_submit_creates_one_card_and_updates_task` | `test_closing_task_through_feedback_stops_the_agent_work_and_keeps_the_delivery`, `test_marking_done_stops_the_agent_work_and_never_accepts`, `test_an_explicit_accept_before_marking_done_is_still_carried_out`, `TaskLinkTests` (3) |
| Requirement: page notes are context, not feedback | notes revision / supersede / carry-over (`test_feedback_revisions` 6, `test_body_edit_timing` 4, `test_notion_body` 3, `test_full_feedback_text` 7, `test_feedback` notes reply) | box-edit timing (3), `NotesAsContextTests` (3), `test_the_notes_are_always_the_latest_text_and_never_feedback`, full-text tests on comments, box edits and new pages (5), `test_a_reply_to_a_page_edit_is_a_new_comment_on_the_task_page` |
| Requirement: install by 3-way merge instead of "never overwrite the skill" | `test_cli.HermesInstallTests` (5) | `test_hermes_install` (16) with a cron double that follows Hermes' `cron create/edit/remove` semantics |
| Wording of model-facing text changed | `test_hermes_assets.test_every_model_facing_text_forbids_changing_the_system_itself`, `test_i18n.test_prompts_stay_english_and_name_the_language` | `ScheduledRunAuthorityTests` (3), `test_model_facing_text_stays_english_and_names_the_language` |

Full suite: 551 tests, 0 failures (unit), plus 22 *hermes* integration tests against
the installed Hermes code in a temporary `HERMES_HOME`.

## Still to verify with real data

1. **Real-sample intake evaluation** (criteria 1–4, 6, 11): one sandbox run was decided by the
   real model (above). Still to do: several runs over the remaining backlog with you reviewing
   the created tasks *and* a sample of ignored items for misses, and feedback runs (comment
   classification, criterion 11). Nothing is written to Notion during this evaluation.
2. **Longer real runs** (criterion 21): Gmail, Slack and GitHub have been fetched read-only for real
   once; incremental cursors, Gmail history expiry and Slack edits/deletions over days are so far
   covered only by recorded-shape tests.
3. **Delegation dry run on production Hermes** (criteria 10, 13–17): an email-draft delegation
   on the real `todo-manager` board; nothing is sent. With `kanban.review_dispatch` on (the
   current setting), Hermes' review worker may handle the delivery before you see it; decide
   whether to keep it (`health` explains it).
5. **Production switch to this version**: not done in this round (no change to the real Notion,
   the production workspace, the existing cron jobs or Hermes' global configuration). Steps:
   pause the owned jobs, back up/export, update the checkout, run `todo-manager status --details`,
   then `todo-manager install` (review the plan) and resume only when ready. Schema remains v10;
   native memory is not migrated by this reliability patch
   (see operations.md, "Upgrading an existing workspace").
4. **First production Notion setup and migration** require your confirmation (see the rollout
   plan in the workspace notes). `todo-manager install` does the setup after you have created
   the new top-level home page and added the integration to it; migrating the old system stays
   the separate `migrate` command.

## Reliability review after 8e177d3 (2026-10-06)

The six initial regressions each failed before implementation. Their causes, repairs, cross-case
coverage, schema-10 migration and separate Fake/native test evidence are recorded in
[the reliability acceptance record](reliability-review-2026-10-06.md). Earlier acceptance records
above remain historical; they do not override the current prompt ownership or native execution
boundaries.

## CLI redesign acceptance (0.2)

See [the command contract](cli.md) and `tests/test_cli_redesign.py`. Synthetic tests exercise:

- exactly 15 public commands; missing/old workspace reads preserve files/schema;
- explicit write provenance, one request text record, stable retry and no later duplicate queue;
- JSON business lists/history and delegation brief files with explicit authority/parallel work;
- evidence-only updates without a fabricated content version;
- atomic run confirmation ownership/date checks, explicit digest review, unchanged retry sets;
- linked clarification, wrong/newer-body refusal and unrelated-answer protection;
- complete discovery before confirmation, no remote writes on unanswered installation, preserved
  user cron workdir/delivery/pause, and refusal to switch under unsettled old runs;
- validated bundle replacement, SQLite safety backup, unconfirmed recovered run coverage,
  writer exclusion, and independent cron/recovery-hold controls;
- fixed pre-run idle stdout and nonzero failure.

Existing reliability tests remain; deleted CLI names in fixtures were migrated to supported
boundaries (internal install initialization/safety backup or the new CLI). The full-text tests
now assert `feedback show` pointers, not removed aliases. Historical machine op-log command
identities remain unchanged so upgrades do not turn old retries into fresh effects.
