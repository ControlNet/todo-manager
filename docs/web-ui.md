# Web UI

A page for desktop, tablet and phone that shows the same tasks as Notion and takes the same
actions. Notion keeps working alongside it; both write to the same SQLite database.

Status: **off by default** (`web.enabled = false`). It needs the [real-time sync
daemon](realtime-sync.md), which serves it.

## Turning it on

```bash
todo-manager config set web.enabled true --human   # the daemon restarts with the new setting
todo-manager web token                              # the sign-in token (created on first use)
```

The page listens on `127.0.0.1:8656` by default. Open `http://127.0.0.1:8656` on the same
machine and paste the token. `todo-manager web token --new` replaces the token and signs every
browser out.

`web.host` and `web.port` choose where it listens; the daemon restarts when they change:

```bash
todo-manager config set web.host 0.0.0.0 --human      # every interface of the machine
todo-manager config set web.host 192.168.1.20 --human # one address, e.g. a LAN or VPN interface
todo-manager config set web.port 8656 --human
```

The page itself speaks plain HTTP, so on any address other than loopback the token and the
session cookie cross the network unencrypted. That is fine on a network only you can reach
(a VPN such as Tailscale or WireGuard); for anything wider, keep the default and publish the
port through a tunnel you control, for example a Cloudflare Tunnel hostname with Cloudflare
Access (or another login) in front of it: the token is then the second lock, not the only one.
The page sets its cookie `Secure` when the tunnel says the request came over HTTPS.

## What it shows

- **Today**: the next thing to do, then tiles for *to decide*, *my to-do*, *agent needs you*,
  *agent working* and *waiting on others* (only follow-ups that are due). Snoozed tasks are
  left out until their date, as on the Notion home page.
- **Decide / Agent / Waiting / All**, and each project's tasks.
- **Task**: fields, the current state as a banner (the agent's question, its latest delivery,
  who you are waiting on), *what changed* with its time for a week after it was written (as the
  Notion view of recent changes; the note itself stays on the task), sources, your Notion page
  notes (read only), the discussion and the change history.
- **Delivery**: each version the agent delivered, its notes (Markdown) and its files (preview and
  download of the copies kept in the workspace).
- **Projects** with their goal, next step, blockers and tasks; **search** over tasks, projects
  and source messages (⌘K / Ctrl+K).
- On a phone: a tab bar, and *Decide* goes through the cards one at a time.

The page checks for changes every 15 seconds while it is visible.

## What the buttons do

Every write is the user's own (history origin `web`), through the same code as the same edit in
Notion (`todo_manager/board.py`):

| Task state | Buttons | Effect |
|---|---|---|
| To decide | Agree, Decline | `decision_choice` feedback for the feedback job, like the decision board |
| To decide | Assign to Agent, My to-do, Done, Cancel | as the decision board's columns |
| To-do | Done, Cancel, Snooze, Wait for… | field changes, applied at once |
| Agent asks / to review / working | Accept (Done), I'll do it (My to-do), Cancel | as the Agent progress board: accept or stop the agent work |
| Agent work | Pause / Resume | `delegate message` pause / resume |
| Waiting | Got it, Still waiting…, Draft a chaser | to-do; a new follow-up date; a comment asking Hermes for a draft |

**Assign to Agent** opens a short dialog whose fields are all optional: a note, when to start and
what counts as done. Confirming it with nothing filled in hands the task over at once; Hermes
writes the brief from the task (or asks one question when it is unclear), exactly as after a drag
to *Assign to Agent* in Notion. What was filled in goes with the request: the note as its text,
the rest as `done_criteria` / `start_after` in its payload. A choice never authorizes an
external action; the agent still asks, and the approval is given in words.

**The input box** writes a comment on the task: feedback with origin `web`, kind `comment`,
which wakes the feedback job like a Notion comment. *To Agent* (shown while the task has agent
work) marks it `to: agent`: Hermes passes it to the agent as the answer to its question, a
supplement or a revision. Each message shows whether it is still waiting, done, or needs more
from you, and Hermes' reply below it.

Every write carries a request id chosen by the page, so a retry after a lost response does not
apply it twice.

## Security

- One secret: the token in `state/web-token` (mode 0600). The session cookie is derived from it
  (HMAC), `HttpOnly`, `SameSite=Lax`, 90 days.
- Writes also need the `X-TM-Request: 1` header, which a page of another site cannot add without
  the browser asking first.
- The page loads nothing from other sites (Content-Security-Policy `default-src 'self'`). Text
  from mail, Slack or the agent is always escaped; delivered files are served as plain text or as
  downloads, never as pages, and only from the workspace's `deliveries/` folder.

## Not yet

- Writing *my notes* (they are the Notion page's own text; edit them there).
- Approving an external action with one tap: the agent's proposed action is free text today, so
  approvals are given in words in the input box.
- Creating tasks from the page (tell Hermes in chat, or create the page in Notion).
