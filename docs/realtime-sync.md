# Real-time sync daemon

Status: live since 2026-10-13 (real subscription, Cloudflare Tunnel); **off by default**
(`daemon.enabled = false`). Going live: [operations](operations.md#real-time-sync-daemon).

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

Latency the user saw:

| Path | Before |
|---|---|
| Comment on a hot page → noticed | 1–2.5 min |
| Comment on a hot page → first bot reply | 2–7 min |
| Comment on a cold page → noticed | up to `notion.cold_comment_poll_minutes` (60) |
| Drag on a board (Done / Cancel / My to-do / Agent) → applied | up to 5 min |
| Local write → Notion (outbox, drained only by the feedback gate) | median 1 min, p90 4.3 min |
| Card question / delivery → Notion | up to 5 min |

Notion has no "comments since T" query, so without push, comment detection costs one request
per page per run. The first change, independent of the daemon, reads comments every run only
where a conversation is going on (agent work, a user comment in the last week, a user edit in
the last 2 hours); new tasks, change notes and older user edits every 30 minutes. On production
data that is 5 pages every run instead of about 55.

## Shape

One resident process, `todo-manager daemon run` (`todo_manager/daemon.py`), runs the feedback
job's deterministic sync steps as things happen. Hermes cron keeps the model runs and is the
fallback. The daemon never runs a model.

```text
Notion ──HTTPS (Cloudflare Tunnel)──▶ receiver thread (127.0.0.1) ── verify ──▶ event queue
                                                                                   │
main thread, about once a second:                                                  ▼
  events → due work per page (debounced)       PRAGMA data_version changed → local push
  board kanban.db / -wal mtime changed → card read (rate-limited)     timers → claims, home, passes
  every database access, opening included, under the shared `workspace` lock (schema checked
  first), due work also under the `sync` lock, through gate_feedback's steps (a failed read an
  event asked for, judged per page: again after 60 s, 5 times, then every
  `daemon.reconcile_minutes` until it succeeds; timer work is not retried, its timer recurs):
    ├─ Notion → local: pull of that kind (with body reads), comments of the named pages
    ├─ local → board: messages, cards of new delegations, parked starts
    ├─ board → local: card events
    └─ local → Notion: dirty scan + outbox (after every cycle)
  claimable feedback → `hermes cron run <feedback job>` (at most once a minute, never a paused job)
```

### Inbound: Notion → local

Webhook subscription, created by the user in the integration's Webhooks tab (there is no API
for it). Events and the work they cause:

| Event | Work |
|---|---|
| `comment.created` / `updated` / `deleted` | comments of `data.page_id` only (not aggregated: arrives in seconds) |
| `page.properties_updated` / `content_updated` / `created` / `moved` / `deleted` / `undeleted` | `pull` of that page's kind (task or project), which also reads edited bodies (aggregated: usually < 1 min); `deleted` / `undeleted` / `moved` of a known task page also read that page directly (a data source query leaves trashed pages out; a page that is gone is not retried but listed under `page_problems` in `daemon status` and `health`, across restarts, until any read imports it again) |
| anything else | acknowledged and dropped |

Rules:

- An event is a hint only. The payload is reduced to type, page and authors; nothing of it is
  stored or reaches a prompt. The daemon re-reads Notion through the incremental, idempotent
  paths, so a lost event is caught by the next full pass.
- Events whose authors are all bots (our own writes) cause no work; they still count as proof
  that deliveries arrive.
- Pages we do not know are ignored, except `page.created` (looked for in both data sources).
- Debounce per (kind of read, page): `daemon.debounce_seconds`; a burst becomes one read.

### Outbound: local → Notion and the board

The CLI (intake and feedback agents, chat) writes the database. The daemon keeps one connection
open and checks `PRAGMA data_version` every loop; it counts only changes made by other
connections, so the daemon's own writes never trigger it again. A change runs the board's send
steps (new delegations get their card at once) and every cycle ends with the dirty scan and the
outbox: agent replies and new tasks reach Notion seconds after the command.

### Kanban

The board has its own database (`~/.hermes/kanban/boards/<board>/kanban.db`). A changed mtime of
it or its WAL reads the cards (`sync_cards`), at most every `daemon.kanban_min_interval_seconds`
because running workers write it constantly; and every `daemon.kanban_poll_minutes` regardless.
The daemon reads no Hermes table itself; the bridge stays the only access path.

### Waking the model

After a cycle, if feedback is claimable the daemon runs `hermes cron run <job id>` at most once a
minute. Hermes starts it on its next cron tick (60 s by default) and the gate claims as before.
`cron run` would resume a paused job, so the daemon skips a job the user paused. Scheduled times
(follow-ups, due dates) stay with cron.

### One syncer at a time

Notion's `RateLimiter` is per process, so two syncers would exceed Notion's ~3 requests/s
together. While the daemon is healthy it is the only one: the feedback gate skips all of its sync
steps and only claims feedback. The daemon does its own full Notion pass every
`daemon.reconcile_minutes`, or every `notion.active_poll_minutes` while it polls (no verified
subscription yet, or the webhook went silent). The gate, the daemon and `todo-manager sync` take
the `sync` lock; a gate that finds it taken skips its sync for that run.

### Local cost

Measured on this host (2026-10-13): `PRAGMA data_version` 5 µs, `stat` of the board DB and its
WAL 7 µs; an idle stdlib process is about 11 MB RSS. What the checks trigger is what costs:

- The Kanban bridge is a Hermes Python process per call (about 0.6 s), so board changes are
  rate-limited (above).
- The connection is in autocommit and keeps no read transaction open between checks, so WAL
  checkpoints are not starved.

### The feedback cron job with the daemon

| Gate step (`gate_feedback.py`) | With a healthy daemon |
|---|---|
| Expire exhausted claims | daemon, every minute |
| Notion pull, body reads, comment poll | daemon on events, full pass every `daemon.reconcile_minutes` |
| Kanban messages, card creation, parked starts, card sync | daemon on local and board changes and timers |
| Dirty scan + outbox | daemon after every cycle |
| Home freshness callout | daemon, every 5 minutes |
| Problem reports (`[CRON_FAILURE]`) | gate (wakes the model) |
| Claim feedback, build context, wake the model | gate, started on demand by the daemon |

The job keeps three roles: the only entry point for model runs (Hermes owns prompt, skills,
model, delivery and at-most-once claim), the safety net (watchdog, full takeover), and incident
reports. An idle run with a healthy daemon is a local check, so the `*/5` schedule stays: it
bounds the latency when the daemon is down.

## Endpoint and authentication

Webhook text is never trusted: an event only says which page to re-read through the normal API
paths, so a forged event can at most cause extra reads. Events are authenticated by Notion's
signature; the handshake that delivers the signing key is only accepted while enrollment is open.

- `ThreadingHTTPServer` on `daemon.host:daemon.port` (127.0.0.1:8655), `POST daemon.path`
  (`/notion`) only, body cap 1 MiB, no request logging (`todo_manager/webhook.py`).
- Public URL through a Cloudflare Tunnel, set up by the user because it is outward-facing:
  `cloudflared` as its own system service (its token stays in its unit, never in this repo) and a
  public hostname with path `^/notion$` → `http://127.0.0.1:8655`. No Cloudflare Access in front
  (Notion cannot authenticate to it); the signature is the key. `daemon status` shows whether
  `cloudflared` is active. Tailscale Funnel (`--set-path`) is the alternative.
- Handshake: Notion's first request carries `verification_token` and no signature, so anyone
  who reaches the public URL first could offer their own. It is accepted only while no token is
  stored **and** the operator has opened enrollment: `todo-manager daemon enroll [--minutes N]`
  (default 15; `state/notion-webhook-enroll`). Otherwise 403, counted as `refused_handshakes`.
  The accepted handshake saves the token (`state/notion-webhook-token`, mode 0600, not printed by
  `status` or logs) and closes enrollment; `todo-manager daemon token` prints it for Notion's
  Verify dialog. Once stored, a second handshake is refused; `todo-manager daemon reset-token`
  clears it and opens enrollment for a recreated subscription. Token and enrollment change
  under one lock shared by the daemon and the CLI (`state/locks/webhook-token.lock`). If a token you did not expect was
  stored (Notion's Verify rejects it), run `daemon reset-token` and press Resend in Notion.
- Every other request must carry `X-Notion-Signature: sha256=<hex HMAC-SHA256 of the raw body,
  keyed with the token>` (constant-time compare); otherwise 401, counted in the status.
- Event ids are deduplicated (Notion retries up to 8 times over ~24 h).

## Staying up

1. `systemd --user` unit `todo-manager-daemon.service` (`Type=notify`, `Restart=always`,
   `RestartSec=10`, no start limit, `WatchdogSec=120`), running the `todo-manager` launcher
   (paths quoted for systemd).
   `install` writes, enables and (re)starts it while `daemon.enabled`; stops and removes it when
   turned off and on `uninstall`. A unit not written by todo-manager is left alone. The user has
   linger enabled (as for `hermes-gateway.service`). The Notion token comes from
   `notion.env_file`, as for the gates.
2. Status file `state/daemon.json`, stamped every 15 s by a pulse thread that also pings the
   systemd watchdog: pid, fingerprint, state, mode, webhook counters, last error. The pulse goes
   on while the main thread makes progress (a loop, a sync step, a Notion request), so a long
   pass stays healthy; after 5 minutes without progress it stops, systemd kills the process and
   the gate covers. It is a file, not a database row, so it needs no schema and works while the
   daemon waits for `install`.
3. The feedback gate is the outer watchdog: a status older than 90 s, from other code or
   settings, or not `running` makes the gate sync itself, record the `daemon`/`alive` channel
   (shown by `health` and the home callout) and restart an installed unit at most every 10
   minutes.
4. End to end: our own Notion writes produce events (assumed from the documented payloads, to be
   confirmed at go-live). The oldest write of ours still without any delivery after it, older
   than `daemon.webhook_silence_minutes`, means the tunnel or the subscription is broken (later
   writes do not move that deadline): the daemon polls Notion as the gate did and records the
   `daemon`/`webhook` channel until a delivery arrives again. With `daemon.webhook = false` it
   always polls.
5. SIGTERM (`systemctl stop`) leaves through the cleanup; an interrupted transaction rolls back.

Down for minutes: Notion retries and the gate covers. Down for longer: the gate's polling covers.

## Updates and production safety

Production imports this checkout directly (the cron pre-run scripts and the `todo-manager`
launcher put it on `sys.path`), so a code change here is live at the next cron run. The daemon
follows the same model: its unit runs the checkout, and it picks up new code by restarting.

- **Code or settings change**: every 10 s the daemon fingerprints its package files and the
  workspace `config.toml` (path, mtime, size); it reads `config.toml` only when it starts. On a
  change it waits until the files have been stable for 10 s (a `git pull` in progress is not
  loaded half-way), stamps `restarting` and exits 0; systemd starts it with the new code and
  settings. With `daemon.enabled` turned off it then refuses to run with exit code 4, which the
  unit's `RestartPreventExitStatus` keeps down until `install` removes the unit.
- **Version skew**: the status carries the fingerprint. A gate whose own fingerprint differs
  (other code or other settings) syncs itself for that run, so an old daemon and a new gate
  never split the work.
- **Broken new code**: systemd retries every 10 s without limit, so a fixed checkout recovers by
  itself; meanwhile the gate syncs. No outage.
- **Schema upgrade**: the daemon never migrates. When the code expects another schema it stays
  up as `waiting_install`, does nothing and tries again every minute; after `todo-manager
  install` migrates (with its backup) it continues. While `install` or `restore` holds the
  workspace lock the daemon does not touch the database at all; afterwards it checks the schema
  again before any access, so a restore of an older backup also puts it in `waiting_install`.
- **Unit change**: `todo-manager install` rewrites the unit when it differs, reloads systemd and
  restarts the daemon (it also starts a stopped one); `todo-manager daemon restart` restarts it
  alone.

Merging the branch is the deployment. After the merge the daemon is still off: the gate then
behaves as before except for the smaller set of pages read every run.

## Settings

| Setting | Default | Effect |
|---|---|---|
| `daemon.enabled` | false | `install` writes and starts the unit; the gate leaves the sync to a healthy daemon |
| `daemon.webhook` | true | Serve the Notion webhook (false: local changes and Kanban only, Notion polled) |
| `daemon.host` / `port` / `path` | 127.0.0.1 / 8655 / `/notion` | Where the receiver listens |
| `daemon.debounce_seconds` | 3 | Per kind of read and page |
| `daemon.kanban_min_interval_seconds` | 15 | Board changes read the cards at most this often |
| `daemon.kanban_poll_minutes` | 5 | Cards read on a timer as well |
| `daemon.reconcile_minutes` | 5 | Full Notion pass while webhooks arrive: what the events miss (a body edit read within Notion's edit minute, a partly failed read) waits no longer than with the gate |
| `daemon.webhook_silence_minutes` | 10 | Our write without any delivery this long: webhook reported broken, Notion polled |
| `daemon.unit_dir` | `~/.config/systemd/user` | Where `install` writes the unit (tests); `systemctl --user enable` finds it only in a directory systemd searches |

## Verification

- `tests/test_comment_polling.py`: which pages are read every run, every warm interval, round-robin.
- `tests/test_webhook.py`: handshake only during open enrollment (refused before, after expiry,
  after the accepted one; a reset during a handshake keeps its window), owner-only token, a second handshake refused (also two at once), signature
  accept/reject, tampered body, retries deduplicated, bot-only marking, HTTP surface (path,
  method, body cap) on an ephemeral loopback port.
- `tests/test_daemon.py`: a comment reads only its page and wakes once; a burst is one read; own
  writes are not work; property edits and pages created in Notion are pulled; unknown pages
  ignored; a comment event due with a full pass is not dropped; a page deleted in Notion is read
  directly, again slowly while its read keeps failing, reported when it is gone until it reads
  again (a home refresh does not clear it); a project deletion is left to the pull; a failed
  event read is retried without another event, only that read, and timer work is not; the
  status is read safely while the main thread records retries; a failing pulse does not end
  the pulse thread; no
  database access (not even opening) while install holds the workspace; a schema changed
  underneath → waiting for install and back, the event taken meanwhile kept; a delivery during
  the work answers only the writes before it; a local write reaches Notion
  and a new delegation gets its card without the gate; a commit by another process during a
  cycle is not swallowed; idle means no request; board changes rate-limited; wake rate limit;
  webhook silence → polling and back (steady writes do not postpone it, only a delivery ends it;
  receiver off → polling); no token → polling; waiting for install; a long cycle with progress
  stays healthy, a stuck one stops the heartbeat; unit paths quoted; gate hand-over (healthy, stale, other code,
  disabled, restart at most every 10 minutes, a config edit); status file; fingerprint of code and
  config.toml; exit after a settled source or config change; the unit stays down on exit code 4.
- `tests/test_daemon_install.py`: unit written/started/left alone/stopped/removed by `install` and
  `uninstall` with a fake systemctl; the `daemon` command; one real `daemon run` process on a
  temporary workspace answering the handshake over HTTP and stopping cleanly on SIGTERM.
- Verified at go-live (2026-10-13) with the real Notion: the subscription and handshake, the
  Cloudflare Tunnel path mapping, a comment read and claimed within seconds, and that our own
  writes produce deliveries.
