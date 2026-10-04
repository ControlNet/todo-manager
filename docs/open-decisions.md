# Open decisions and proposed defaults

Values marked `PROPOSED` in `todo_manager/config.py` are engineering defaults chosen during
implementation. They work, but they encode preferences only the user can confirm. Override any
of them in `config.toml`. (Slack's 30 quiet minutes is a requirement, not a proposal: lower values
are raised to 30.)

## Proposed defaults

| Setting | Default | Effect |
|---|---|---|
| `email.initial_lookback_days` | 14 | Backfill window on the first Gmail run |
| `email.quiet_minutes` | 15 | An email thread must be quiet this long before analysis |
| `email.min_wake_interval_minutes` | 60 | Email alone wakes the model at most hourly (mature email joins any other wake) |
| `github.quiet_minutes` / `min_wake_interval_minutes` | 10 / 60 | Same shape as email |
| `commits.digest_time` | 21:00 local | Daily commit digest (project progress only) |
| `commits.max_attempts_per_day` | 2 | Model attempts for one day's digest |
| `notion.cold_comment_poll_minutes` | 60 | Comment polling interval for open tasks without recent activity (active pages: every run) |
| `notion.cold_comment_pages_per_run` | 30 | Round-robin budget of cold pages per run |
| `notion.history_poll_hours` | 24 | Sweep of recently closed pages for late comments |
| `wake.max_context_chars` / `max_items_per_run` | 60000 / 60 | Evidence rendered into one intake run; the rest waits for the next run. Batches are taken round-robin across sources, so an email backlog cannot crowd out chat. When more than one run's worth is waiting, the email/GitHub wake interval is skipped so a backlog drains every 5 minutes |
| `feedback.max_items_per_run` | 20 | Feedback items per model run |
| `feedback.max_attempts` | 3 | Model runs before an unapplied feedback item is marked failed (with a reminder) |
| `kanban.max_running` | 1 | Delegations executing at once |
| `notify.failure_alert_after_minutes` | 60 | A gate that has not run this long is reported |

## Agreed defaults

| Setting | Default | Effect |
|---|---|---|
| `slack.quiet_minutes` | 30 | A Slack conversation matures only after 30 quiet minutes (floor) |
| `slack.active_hours` / `slack.quiet_poll_minutes` | 168 / 30 | A Slack conversation with a human (non-bot) message in the last 7 days is read every run; quieter ones about every 30 minutes (staggered), so all DMs and channels stay within Slack's rate limits. A new message in a long-quiet conversation is seen within ~30 minutes, before the 30 quiet minutes of batching |
| GitHub: your own PR merged/closed | context unless an open task is about it | A merge/close of a PR you opened (with no newer comment by someone else) wakes the model only when that PR is evidence of an open task, so the task gets updated |

## Decisions that belong to the user

- **Source scope**: which email accounts, Slack conversations and GitHub repositories are read.
  For Slack, `slack.direct_messages` / `group_direct_messages` / `channels` include every DM,
  group DM or member channel (new ones too), minus the `slack.exclude` blacklist (ids, DM user
  ids or channel name globs such as `"#alerts-*"`); `slack.conversations` lists single ones.
- **Identities** (`identity.*`): your own addresses, logins and user ids. Never guessed; a
  message counts as yours only when it matches.
- **Reminder target** (`notify.matrix`, `notify.matrix_target`): whether and where short Matrix
  reminders go (important deliveries, questions, failures).
- **Hermes Kanban settings** (`review_dispatch`, `auto_decompose`): global switches, see
  [board-protocol.md](board-protocol.md).
- **Model** for the two cron jobs (`hermes cron edit <id> --model … --provider …`); the jobs use
  the Hermes default unless set.
- **Notion home page** and **migration** of an older system: both are production writes that
  are shown as a plan first (`notion plan`, `migrate propose` / `migrate apply` preview).
