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

Run everything: `python3 -m unittest discover -s tests -t .`

## Intake quality

| # | Criterion | Implementation | Evidence |
|---|---|---|---|
| 1 | Ads, random CFPs, unaccepted review invitations stay out; explicit acceptance is recognised | Model decides with full context; every new item must be linked as evidence or ignored with a category (`advertising`, `cfp`, `review_invitation_unaccepted`, …) and a reason | unit: `test_every_new_item_must_be_covered`, `test_ignore_needs_category_and_reason`; judgement **not yet verified** |
| 2 | No task from a title / IMPORTANT label alone; full context is judged | No keyword or label rules exist; the model receives full message text (quotes trimmed) plus earlier thread messages | structural; judgement **not yet verified** |
| 3 | Reminders, re-fetches and the same matter across email/Slack/GitHub do not duplicate tasks | Source items are unique per (source, account, external id); thread keys link later messages to existing tasks shown to the model; open tasks are in every intake context | unit: `test_create_and_followup_update_same_matter`, `test_reminder_cannot_reopen_human_cancelled_task`; cross-channel judgement **not yet verified** |
| 4 | Read / expired / silence is not completion; receipts, sent replies, PRs update per done criteria | No code path closes a task on read state or time; closing requires an op with evidence; done criteria are stored per task. Your own PR merged/closed reaches the model only when that PR is evidence of an open task (to update it); otherwise it is context | structural + unit: `test_create_and_followup_update_same_matter`, `test_if_open_task_items_wake_only_for_an_open_linked_task`, `test_tracked_repo_merge_of_my_pr_is_news_only_for_an_open_task`; judgement **not yet verified** |
| 5 | Your cancel/done/rule corrections win; genuinely new responsibility is handled | Field-level provenance and `based_on_version`; closed-by-you tasks are never reopened (follow-up tasks instead); rules are versioned and decisions on stale rules are rejected | unit: `test_agent_cannot_overwrite_newer_human_correction`, `test_seen_human_value_needs_explicit_override`, `test_human_closed_task_is_not_reopened_by_agent`, `test_stale_rules_rejected`, `test_stale_human_edit_wins_over_feedback_based_on_older_version` |
| 6 | Ignored samples are checked for misses; no "ignore everything" or "review everything" | Every ignore carries category + rationale and is stored with the run; there is no "needs review" inbox: low-value or unclear noise is ignored with a rationale; an ambiguity that could cost you if missed (direct request, deadline, money, reviews, hiring, an assigned role) becomes one question (`ask`, a `decision` task for you), at most `wake.max_questions_per_run` (3) per run, enforced on apply (`AmbiguityTests`). Whether the model draws this line well is part of the real-sample evaluation | **not yet verified** (needs the real-sample evaluation) |

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
| 15 | Human review waiting is effective; notifications do not wake the creator to re-plan | Requires `kanban.review_dispatch: false`; cards are created without a chat session (no auto-subscribe) | hermes: integration home uses the required settings; **production setting pending your confirmation** |
| 16 | A finished delivery does not complete the Notion task | Accepting ends the delegation only; task completion is decided separately | unit: `test_review_revision_accept_cycle`, `test_marking_done_accepts_a_delivered_review`, `test_closing_task_through_feedback_accepts_the_delivery` |
| 17 | External actions follow authorisation; approval is bound to the delivery version | Approvals only from you, bound to one action + delivery version, voided by a newer delivery, sent and consumed once; a run that held an approval and failed is parked (`max_retries = 1`), marked unknown and asked about, never respawned automatically. Allowed actions are card instructions the worker model follows: **no tool-call interception** (see limitations) | unit: `test_approval_binds_to_latest_delivery`, `test_second_delegation_and_guest_approval_are_refused`, `ApprovalTests`; hermes: `test_a_failed_run_parks_the_card_instead_of_a_respawn` |

## Concurrency and recovery

| # | Criterion | Implementation | Evidence |
|---|---|---|---|
| 18 | Concurrent creation of one delegation ends in exactly one card | `kanban-submit` file lock + lookup before create + derived idempotency key | unit: `test_concurrent_submitters_create_one_card`, `test_capacity_one_queues_second_delegation_locally` |
| 19 | Crash after create but before saving the card id recovers the card; uncertain lookup never re-creates; archived cards reconcile | `reconcile` state before create; lookup includes archived cards; multiple matches quarantine | unit: `test_lost_create_response_is_reconciled_not_duplicated`, `test_failed_lookup_never_creates`, `test_archived_card_is_found_and_not_recreated`, `test_multiple_matches_are_quarantined`; hermes: `test_archived_card_found_by_lookup` |
| 20 | Interruptions after fetch / after decision / after an external write with lost response recover | Items stay pending until covered by applied decisions; effects are queued in the same transaction; uncertain writes are looked up before retry | unit: `test_double_apply_is_refused`, `test_unapplied_claim_is_retried_then_failed_with_notification`, `test_lost_create_response_does_not_duplicate_page`, `test_lost_comment_response_is_not_posted_twice` |
| 21 | 429, unavailable sources, invalid cursors, large pagination are observable and recover; failure is never "no changes" | Retries with `Retry-After`; fetch errors recorded per source (health + freshness box); Gmail falls back from an expired history id to a windowed list; full pagination | unit: `test_rate_limit_is_retried`, `test_fetch_failure_is_recorded_not_treated_as_empty`, `test_expired_history_falls_back_to_a_paginated_window`, `test_other_errors_are_failures_not_empty_results`, `test_threads_pagination_edits_and_deletions`, `test_api_error_is_a_failure`, `test_rate_limit_defers_the_rest_of_that_source_only`, `test_discovered_scopes_are_read_once_and_survive_a_failed_listing`, `test_many_failing_scopes_of_one_source_are_reported_once`, `test_scopes_dropped_from_a_listing_are_retired_not_stale`; real read-only Gmail, Slack (every member conversation discovered; blacklisted bot channels retired) and GitHub notification fetches in a sandbox workspace |
| 22 | Overlapping schedules, simultaneous corrections and late board results never overwrite newer versions | Per-gate non-blocking locks; version checks on every write; delegation-driven status changes yield to later human edits | unit: `test_human_status_change_after_delegation_is_respected`, `test_stale_human_edit_wins_over_feedback_based_on_older_version`, `test_push_reads_before_write_and_keeps_concurrent_human_edit` |
| 23 | Recovery checks existing results and side effects first; no duplicate emails or replies | Outbox reconciliation; Matrix reminders are at-most-once; comments carry markers | unit: `test_notifier_appends_link_and_is_at_most_once`, `test_lost_comment_response_is_not_posted_twice`, `test_lost_create_response_is_reconciled_not_duplicated` |
| 24 | Rules, states, evidence, feedback, delegations and deliveries export and rebuild; backups restore | `export` (Markdown + JSON + manifest hashes), `export --verify`, `backup`, `restore` | unit: `test_export_verify_backup_restore` |
| 25 | Your Notion notes survive; logs do not clutter Notion; source freshness is visible | One managed callout per page; edits inside it are interpreted, not overwritten; freshness box rewritten only on change | unit: `test_managed_region_updates_and_user_notes_survive`, `test_user_edit_inside_region_is_held_for_interpretation`, `test_deleted_region_is_reinserted_at_top`, `test_home_freshness_written_only_when_content_changes` |

## Still to verify with real data

1. **Real-sample intake evaluation** (criteria 1–4, 6, 11): run the intake gate against an
   isolated workspace with the confirmed source scope (read-only), let the model decide, and
   review created tasks *and* a sample of ignored items for misses. Nothing is written to
   Notion during this evaluation.
2. **Longer real runs** (criterion 21): Gmail, Slack and GitHub have been fetched read-only for real
   once; incremental cursors, Gmail history expiry and Slack edits/deletions over days are so far
   covered only by recorded-shape tests.
3. **Delegation dry run on production Hermes** (criteria 10, 13–17): an email-draft delegation
   on the real `todo-manager` board after the Kanban settings are confirmed; nothing is sent.
4. **First production Notion setup and migration** require your confirmation (see the rollout
   plan in the workspace notes).
