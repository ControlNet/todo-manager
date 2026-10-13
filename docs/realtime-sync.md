# Real-time Notion sync daemon (proposal)

Status: **proposal**, agreed in direction with the user on 2026-10-13; not implemented. When it
lands, `architecture.md` ("no resident coordinator") and `limitations.md` ("Notion webhooks: not
supported by design", "Notion is polled, not pushed") change with it.

## Why

Intake polling is fine: conversations wait for a quiet window anyway, so push would buy nothing.
The feedback side is what hurts. Production, 300 feedback gate runs (2026-10-12 00:37 to
2026-10-13 01:47 UTC):

| Measure | Value |
|---|---|
| Runs that woke the model | 8 of 300 (292 idle) |
| Gate duration | median 26.7 s, p90 61 s, max 115 s |
| `list_comments` calls | 21,584 (about 72 pages per run) for **7** new user comments |
| Where those comments were | all on 3 pages with agent work; 6 of 7 after a bot comment there |
| Property pulls | 2 queries per run, 21 edits found; cheap |
| Kanban | 1 active delegation; negligible today, linear in active cards |

Latency the user sees today:

| Path | Today |
|---|---|
| Comment on a hot page → noticed | 1–2.5 min |
| Comment on a hot page → first bot reply | 2–7 min |
| Comment on a cold page → noticed | up to `notion.cold_comment_poll_minutes` (60) |
| Drag on a board (Done / Cancel / My to-do / Agent) → applied | up to 5 min |
| Local write → Notion (outbox, drained only by the feedback gate) | median 1 min, p90 4.3 min |
| Card question / delivery → Notion | up to 5 min |

Notion has no "comments since T" query, so without push, comment detection costs one request
per page per run.

## Shape

One resident process, `todo-manager daemon`, runs the deterministic sync on events. Hermes cron
keeps the model runs and becomes the fallback and reconciliation path. The daemon never runs a
model.

```text
Notion ──HTTPS (Tailscale Funnel)──▶ receiver (127.0.0.1) ── verify ──▶ event queue
local DB writes (CLI, agents)  ── PRAGMA data_version, every 1–2 s ──▶ event queue
board kanban.db / -wal mtime   ── stat, every 1–2 s ─────────────────▶ event queue
                                                                          │
                                       one worker thread, debounced ◀─────┘
                                       ├─ Notion → local: pull, page comments, body reads
                                       ├─ local → Notion: dirty scan + outbox
                                       ├─ Kanban: sync changed cards, send messages
                                       └─ feedback needs judgement → `hermes cron run todo-manager-feedback`
```

### Inbound: Notion → local

Webhook subscription (created by the user in the integration's Webhooks tab; there is no API
for it). Events and the action they cause:

| Event | Action |
|---|---|
| `comment.created` / `updated` / `deleted` | poll comments of `data.page_id` only (not aggregated: arrives in seconds) |
| `page.properties_updated` / `page.created` / `page.deleted` / `page.undeleted` / `page.moved` | data source pull of that kind (existing `pull`, aggregated: usually < 1 min) |
| `page.content_updated` | body read of that page (`check_bodies` for one page) |
| `data_source.schema_updated` | schema check (existing setup verification) |

Rules:

- An event is a hint only. The payload is never stored as data and never reaches a prompt; the
  daemon re-reads Notion through the existing incremental, idempotent paths. A lost event is
  caught by the reconciliation sweep.
- Events whose `authors` are only our integration are dropped (our own writes), after being
  counted as liveness (see Health).
- Unknown page IDs are ignored except `page.created` under our data sources.
- Debounce per (action, page): 3 s; a burst becomes one read.

### Outbound: local → Notion

The CLI (intake and feedback agents, chat) writes the database; today those writes reach Notion
at the next feedback gate. The daemon keeps one connection open and checks
`PRAGMA data_version` every 1–2 s; a change runs `enqueue_dirty` and drains the outbox. Agent
replies and new tasks appear in Notion seconds after the command.

### Kanban

The board has its own database (`~/.hermes/kanban/boards/todo-manager/kanban.db`). A changed
mtime of it or its WAL runs `sync_cards` and `send_messages`; the existing event cursors decide
what is new. No Hermes table is read or written by the daemon itself; the bridge stays the only
access path.

### Waking the model

After a sync, if feedback is claimable the daemon calls `hermes cron run todo-manager-feedback`
at most once per minute. Hermes picks it up on its next cron tick (60 s by default) and the gate
claims as today. Scheduled times (follow-ups, due dates, parked cards) stay with cron.

### Who talks to Notion

`RateLimiter` is per process, so two writers could exceed Notion's ~3 requests/s together. While
the daemon is healthy it is the only Notion client: the feedback gate skips its Notion steps
(pull, comments, dirty, outbox, home) and only claims feedback, except a reconciliation pass every
`daemon.reconcile_minutes`. When the daemon is unhealthy the gate does everything as today.
Both sides take the existing `feedback` / `outbox` / `kanban-submit` file locks.

## Endpoint and authentication

Security bar agreed with the user: no unauthenticated access; no further hardening.

- `ThreadingHTTPServer` on `127.0.0.1:<daemon.port>`, `POST /notion` only, body cap 1 MiB.
- Public URL through Tailscale Funnel (1.102.2 on this host supports `--set-path`), run by the
  user because it is outward-facing; `install` prints the exact command and `health` checks
  `tailscale funnel status --json`.
- Handshake: while no token is stored, the first POST carrying `verification_token` is accepted
  and the token saved (`state/notion-webhook-token`, mode 0600, excluded from exports and logs);
  `todo-manager daemon status` shows it for the user to paste into Notion's Verify dialog.
  `todo-manager daemon reset-token` clears it for a recreated subscription.
- Every other request must carry `X-Notion-Signature: sha256=<hex HMAC-SHA256 of the raw body,
  keyed with the token>` (constant-time compare); otherwise 401.
- Event `id` is deduplicated (Notion may retry up to 8 times over ~24 h).

## Staying up

1. `systemd --user` unit `todo-manager-daemon.service`, `Restart=always`, `RestartSec=5`. The
   user has linger enabled (as for `hermes-gateway.service`). `install` writes and enables it;
   `uninstall` removes it. Tokens come from an `EnvironmentFile`, never the unit text.
2. Code reload: the daemon checks the mtimes of its package files every minute and exits on a
   change; systemd starts the new code. Gates already import from the checkout.
3. Hang detection: `WatchdogSec=60` with `sd_notify` over `NOTIFY_SOCKET` (stdlib socket).
4. The feedback gate is the outer watchdog: the daemon stamps `daemon.alive_at` every 30 s; a
   stale stamp makes the gate try `systemctl --user restart todo-manager-daemon`, report it in
   `health` and the home callout, and take over the Notion steps.
5. End to end: our own Notion writes produce events. Writes in the last
   `daemon.webhook_silence_minutes` with no event at all mean the Funnel or the subscription is
   broken; reported, and the gate takes over comment polling.

Down for minutes: Notion retries, nothing is lost. Down for longer: the gate's polling covers it.

## Settings (proposed)

| Setting | Default | Effect |
|---|---|---|
| `daemon.enabled` | false | Turned on by `install` once the subscription is verified |
| `daemon.port` | 8655 | Loopback port of the receiver |
| `daemon.local_poll_seconds` | 2 | `data_version` and board mtime checks |
| `daemon.debounce_seconds` | 3 | Per (action, page) |
| `daemon.reconcile_minutes` | 30 | Full Notion pass by the gate while the daemon is healthy |
| `daemon.webhook_silence_minutes` | 30 | Own writes without any event this long → webhook reported broken |

## Phases

1. **Smaller hot set** (independent, also the fallback): every run only pages with an active
   delegation, an open agent question or a user comment in the last 7 days; new tasks, user
   edits and change notes move to a warm interval. Open: warm interval (proposed 30 min) and
   whether a user edit stays hot for a short window (proposed 2 h).
2. **Daemon v1**: receiver, handshake, signature, event → targeted reads, outbox on
   `data_version`, waking feedback, gate hand-over, systemd unit, watchdogs.
3. **Daemon v2**: Kanban mtime watch; the cron schedule relaxed once v1 has run for a while.
4. **Acknowledgement** (needs a board decision): show "received / queued" on the task as soon
   as a comment or drag that needs the agent arrives.

## Verification

- Unit tests (stdlib, fake Notion): signature accept/reject, handshake only without a token,
  event → action mapping, own-author drop, unknown page ignore, debounce, dedupe by event id,
  `data_version` trigger, gate skips Notion steps while healthy and takes over when stale,
  wake rate limit.
- A real webhook check uses a separate test integration and test pages, never the production
  workspace.
- Targets after phase 2: drag applied ≤ ~1 min (Notion aggregation), comment noticed ≤ ~10 s,
  agent reply visible ≤ ~10 s after the agent's command, idle feedback gate a few seconds.
