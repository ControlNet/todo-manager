"""Kanban backends: the real Hermes board through ``kanban_bridge`` and an in-memory fake.

Every operation names the board explicitly; nothing relies on the CLI's current board.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Protocol

from .config import Config

MARK = "@@TM@@"
BRIDGE = Path(__file__).with_name("kanban_bridge.py")


# Transitions bound to the Kanban event they answer. ``anchors``: event kinds that put the
# card in the state the transition leaves; ``done_by``: events after the anchor that mean the
# transition already happened (e.g. our earlier call whose response was lost); ``final``:
# statuses that already are the target. Mirrored in ``kanban_bridge.py`` (which cannot
# import this package); the integration tests run both against the installed Hermes code.
TRANSITIONS: dict[str, dict[str, tuple[str, ...] | None]] = {
    "unblock": {"anchors": ("blocked", "block_loop_detected", "gave_up"), "from": ("blocked", "triage"),
                "done_by": ("unblocked", "specified"), "final": ()},
    "reopen_review": {"anchors": ("review_requested",), "from": ("review",), "done_by": ("review_reopened",),
                      "final": ()},
    "complete": {"anchors": ("review_requested",), "from": ("review",), "done_by": ("completed",),
                 "final": ("done",)},
    "archive": {"anchors": (), "from": None, "done_by": ("archived",), "final": ("archived",)},
}


def transition_verdict(status: str, events: list[dict[str, Any]], action: str, anchor: int | None) -> str:
    """``apply`` | ``already`` | ``mismatch`` for ``action`` bound to event ``anchor``.

    ``already``: the transition happened after the anchor (by us or by someone else), or the
    card is in the target state. ``mismatch``: the anchor is no longer the card's current
    question/review, or the card is not in a state the transition applies to."""
    spec = TRANSITIONS[action]
    if status in (spec["final"] or ()):
        return "already"
    if anchor is not None:
        later = [e for e in events if int(e["id"]) > int(anchor)]
        if any(e["kind"] in spec["done_by"] for e in later):
            return "already"
        latest = max((int(e["id"]) for e in events if e["kind"] in spec["anchors"]), default=None)
        if latest != int(anchor):
            return "mismatch"
    if spec["from"] is not None and status not in spec["from"]:
        return "mismatch"
    return "apply"


class KanbanError(Exception):
    def __init__(self, message: str, code: str | None = None, uncertain: bool = False):
        super().__init__(message)
        self.code = code
        self.uncertain = uncertain


class Kanban(Protocol):
    board: str

    def find(self, idem_key: str, marker: str) -> list[dict[str, Any]]: ...
    def create(self, *, title: str, body: str, idem_key: str, assignee: str | None, tenant: str,
               workspace: str | None = None) -> str: ...
    def snapshot(self, card_id: str, after_event: int = 0, after_comment: int = 0) -> dict[str, Any]: ...
    def comment(self, card_id: str, body: str, author: str = "todo-manager") -> int: ...
    def block(self, card_id: str, reason: str, kind: str | None = None) -> bool: ...
    def unblock(self, card_id: str) -> bool: ...
    def respecify(self, card_id: str) -> bool: ...
    def reopen_review(self, card_id: str) -> bool: ...
    def complete(self, card_id: str, summary: str) -> bool: ...
    def archive(self, card_id: str) -> bool: ...
    def transition(self, card_id: str, action: str, anchor: int | None = None,
                   summary: str | None = None) -> dict[str, Any]: ...
    def counts(self) -> dict[str, int]: ...


class BridgeKanban:
    """Runs ``kanban_bridge.py`` under the Hermes interpreter (one process per call)."""

    def __init__(self, board: str, python: str, repo: str, hermes_home: str | None = None, timeout: float = 90.0):
        if not board or board == "default":
            raise KanbanError("an explicit non-default board is required")
        self.board = board
        self.python = str(Path(python).expanduser())
        self.repo = str(Path(repo).expanduser())
        self.hermes_home = hermes_home
        self.timeout = timeout

    @classmethod
    def from_config(cls, cfg: Config) -> "BridgeKanban":
        k = cfg.section("kanban")
        return cls(k.get("board", "todo-manager"), k.get("hermes_python", "~/.hermes/hermes-agent/venv/bin/python"),
                   k.get("hermes_repo", "~/.hermes/hermes-agent"), k.get("hermes_home") or None)

    def call(self, op: str, mutating: bool = True, **args: Any) -> Any:
        env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_KANBAN_")}
        if self.hermes_home:
            env["HERMES_HOME"] = str(Path(self.hermes_home).expanduser())
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        payload = json.dumps({"op": op, "board": self.board, "hermes_repo": self.repo, **args})
        try:
            proc = subprocess.run([self.python, str(BRIDGE)], input=payload, capture_output=True, text=True,
                                  timeout=self.timeout, env=env, check=False)
        except subprocess.TimeoutExpired:
            raise KanbanError(f"bridge timeout during {op}", uncertain=mutating) from None
        except OSError as exc:
            raise KanbanError(f"bridge could not start: {exc}") from None
        lines = [ln for ln in proc.stdout.splitlines() if ln.startswith(MARK)]
        if not lines:
            raise KanbanError(f"bridge returned no result for {op} (exit {proc.returncode}): "
                              f"{(proc.stderr or '')[-300:]}", uncertain=mutating)
        response = json.loads(lines[-1][len(MARK):])
        if not response.get("ok"):
            raise KanbanError(response.get("error", "bridge error"), code=response.get("code"))
        return response.get("result")

    def ping(self) -> dict[str, Any]:
        return self.call("ping", mutating=False)

    def create_board(self, name: str, description: str) -> dict[str, Any]:
        return self.call("create_board", name=name, description=description)

    def find(self, idem_key: str, marker: str) -> list[dict[str, Any]]:
        return self.call("find", mutating=False, idem_key=idem_key, marker=marker)

    def create(self, *, title: str, body: str, idem_key: str, assignee: str | None, tenant: str,
               workspace: str | None = None) -> str:
        kind, _, path = (workspace or "").partition(":")
        return self.call("create", title=title, body=body, idem_key=idem_key, assignee=assignee, tenant=tenant,
                         created_by="todo-manager", workspace_kind=kind or None, workspace_path=path or None)

    def snapshot(self, card_id: str, after_event: int = 0, after_comment: int = 0) -> dict[str, Any]:
        return self.call("snapshot", mutating=False, task_id=card_id, after_event=after_event,
                         after_comment=after_comment)

    def comment(self, card_id: str, body: str, author: str = "todo-manager") -> int:
        return self.call("comment", task_id=card_id, body=body, author=author)

    def block(self, card_id: str, reason: str, kind: str | None = None) -> bool:
        return self.call("block", task_id=card_id, reason=reason, kind=kind)

    def unblock(self, card_id: str) -> bool:
        return self.call("unblock", task_id=card_id)

    def respecify(self, card_id: str) -> bool:
        return self.call("respecify", task_id=card_id)

    def reopen_review(self, card_id: str) -> bool:
        return self.call("reopen_review", task_id=card_id)

    def complete(self, card_id: str, summary: str) -> bool:
        return self.call("complete", task_id=card_id, summary=summary, result=summary)

    def archive(self, card_id: str) -> bool:
        return self.call("archive", task_id=card_id)

    def transition(self, card_id: str, action: str, anchor: int | None = None,
                   summary: str | None = None) -> dict[str, Any]:
        """Check-and-apply in one bridge process: ``{"outcome": "done"|"already"|"mismatch",
        "status": ..., "termination": <archive_worker_termination payload or None>}``."""
        return self.call("transition", task_id=card_id, action=action, anchor=anchor, summary=summary)

    def counts(self) -> dict[str, int]:
        return self.call("counts", mutating=False)


class FakeKanban:
    """In-memory board mirroring the Hermes semantics this project relies on:
    idempotency lookup excludes archived cards, blocks are sticky until unblocked, a
    second same-kind block after an unblock routes to triage, review waits for a human."""

    def __init__(self, board: str = "todo-manager"):
        self.board = board
        self.cards: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.comments: list[dict[str, Any]] = []
        self.runs: dict[str, list[dict[str, Any]]] = {}
        self.fail_next: dict[str, Exception] = {}
        self.lose_response: set[str] = set()
        self.terminate_ok = True  # outcome of terminating a running worker on archive
        self._n = 0

    def _event(self, card_id: str, kind: str, payload: dict | None = None, run_id: int | None = None) -> None:
        self.events.append({"id": len(self.events) + 1, "task_id": card_id, "kind": kind, "payload": payload or {},
                            "created_at": 0, "run_id": run_id})

    def _current_run(self, card_id: str) -> int | None:
        runs = self.runs.get(card_id) or []
        return runs[-1]["id"] if runs and runs[-1]["status"] == "running" else None

    def _maybe_fail(self, op: str) -> None:
        if op in self.fail_next:
            raise self.fail_next.pop(op)

    def find(self, idem_key: str, marker: str) -> list[dict[str, Any]]:
        self._maybe_fail("find")
        return [{"id": c["id"], "status": c["status"], "title": c["title"], "idempotency_key": c["idem_key"]}
                for c in self.cards.values() if c["idem_key"] == idem_key or marker in c["body"]]

    def create(self, *, title: str, body: str, idem_key: str, assignee: str | None, tenant: str,
               workspace: str | None = None) -> str:
        self._maybe_fail("create")
        for card in self.cards.values():
            if card["idem_key"] == idem_key and card["status"] != "archived":
                return card["id"]
        self._n += 1
        cid = f"t_{self._n:04d}"
        self.cards[cid] = {"id": cid, "title": title, "body": body, "idem_key": idem_key, "status": "ready",
                           "assignee": assignee, "tenant": tenant, "block_kind": None, "block_recurrences": 0}
        self._event(cid, "created")
        if "create" in self.lose_response:
            self.lose_response.discard("create")
            raise KanbanError("bridge timeout during create", uncertain=True)
        return cid

    def snapshot(self, card_id: str, after_event: int = 0, after_comment: int = 0) -> dict[str, Any]:
        self._maybe_fail("snapshot")
        card = self.cards.get(card_id)
        if card is None:
            raise KanbanError("card not found", code="NO_CARD")
        return {"task": {"id": card_id, "status": card["status"], "block_kind": card["block_kind"],
                         "block_recurrences": card["block_recurrences"]},
                "events": [e for e in self.events if e["task_id"] == card_id and e["id"] > after_event],
                "comments": [c for c in self.comments if c["task_id"] == card_id and c["id"] > after_comment],
                "runs": self.runs.get(card_id, []), "attachments": []}

    def comment(self, card_id: str, body: str, author: str = "todo-manager") -> int:
        self._maybe_fail("comment")
        cid = len(self.comments) + 1
        self.comments.append({"id": cid, "task_id": card_id, "author": author, "body": body})
        self._event(card_id, "commented")
        if "comment" in self.lose_response:
            self.lose_response.discard("comment")
            raise KanbanError("bridge timeout during comment", uncertain=True)
        return cid

    # ----- worker simulation helpers (tests) -----
    def worker_start(self, card_id: str) -> None:
        card = self.cards[card_id]
        assert card["status"] == "ready", card["status"]
        card["status"] = "running"
        self.runs.setdefault(card_id, []).append({"id": len(self.runs.get(card_id, [])) + 1, "status": "running",
                                                  "summary": None, "metadata": None})
        self._event(card_id, "claimed", run_id=self.runs[card_id][-1]["id"])

    def worker_block(self, card_id: str, reason: str, kind: str = "needs_input") -> None:
        self.block(card_id, reason, kind, _from_worker=True)

    def worker_review(self, card_id: str, summary: str, metadata: dict | None = None) -> None:
        card = self.cards[card_id]
        assert card["status"] == "running"
        card["status"] = "review"
        run = self.runs[card_id][-1]
        run.update(status="done", summary=summary, metadata=metadata or {})
        self._event(card_id, "review_requested", {"summary": summary}, run_id=run["id"])

    def worker_crash(self, card_id: str) -> None:
        """The worker process died: the dispatcher returns the card to ready for a retry."""
        card = self.cards[card_id]
        assert card["status"] == "running"
        run = self.runs[card_id][-1]
        run.update(status="crashed")
        card["status"] = "ready"
        self._event(card_id, "crashed", {"error": "worker exited"}, run_id=run["id"])

    def dispatch_tick(self) -> list[str]:
        """Which cards a dispatcher would start: only ready ones (blocked/review/triage wait)."""
        return [c["id"] for c in self.cards.values() if c["status"] == "ready"]

    # ----- state operations -----
    def block(self, card_id: str, reason: str, kind: str | None = None, _from_worker: bool = False) -> bool:
        self._maybe_fail("block")
        card = self.cards[card_id]
        if card["status"] not in ("running", "ready"):
            return False
        run_id = self._current_run(card_id)
        if run_id is not None:
            self.runs[card_id][-1]["status"] = "blocked"
        recurrences = card["block_recurrences"] + 1 if card["block_kind"] == kind else 1
        card["block_kind"], card["block_recurrences"] = kind, recurrences
        if recurrences >= 2:
            card["status"] = "triage"
            self._event(card_id, "block_loop_detected", {"reason": reason, "kind": kind}, run_id=run_id)
        else:
            card["status"] = "blocked"
            self._event(card_id, "blocked", {"reason": reason, "kind": kind}, run_id=run_id)
        return True

    def unblock(self, card_id: str) -> bool:
        self._maybe_fail("unblock")
        card = self.cards[card_id]
        if card["status"] != "blocked":
            return False
        card["status"] = "ready"
        self._event(card_id, "unblocked")
        return True

    def respecify(self, card_id: str) -> bool:
        card = self.cards[card_id]
        if card["status"] != "triage":
            return False
        card["status"] = "ready"
        self._event(card_id, "specified")
        return True

    def reopen_review(self, card_id: str) -> bool:
        card = self.cards[card_id]
        if card["status"] != "review":
            return False
        card["status"] = "ready"
        self._event(card_id, "review_reopened")
        return True

    def complete(self, card_id: str, summary: str) -> bool:
        card = self.cards[card_id]
        if card["status"] not in ("running", "ready", "blocked", "review"):
            return False
        card["status"] = "done"
        card["block_kind"], card["block_recurrences"] = None, 0
        self._event(card_id, "completed", {"summary": summary})
        return True

    def archive(self, card_id: str) -> bool:
        card = self.cards[card_id]
        if card["status"] == "archived":
            return False
        was_running = card["status"] == "running"
        card["status"] = "archived"
        self._event(card_id, "archived")
        if was_running:
            self._event(card_id, "archive_worker_termination",
                        {"host_local": True, "termination_attempted": True, "terminated": self.terminate_ok})
        return True

    def transition(self, card_id: str, action: str, anchor: int | None = None,
                   summary: str | None = None) -> dict[str, Any]:
        self._maybe_fail("transition")
        card = self.cards.get(card_id)
        if card is None:
            raise KanbanError("card not found", code="NO_CARD")
        events = [e for e in self.events if e["task_id"] == card_id]
        verdict = transition_verdict(card["status"], events, action, anchor)
        outcome = verdict
        if verdict == "apply":
            if action == "unblock":
                ok = self.respecify(card_id) if card["status"] == "triage" else self.unblock(card_id)
            else:
                ok = getattr(self, action)(card_id, *((summary or "",) if action == "complete" else ()))
            after = [e for e in self.events if e["task_id"] == card_id]
            outcome = "done" if ok else (
                "already" if transition_verdict(card["status"], after, action, anchor) == "already" else "mismatch")
        termination = None
        if action == "archive":
            term = [e for e in self.events if e["task_id"] == card_id and e["kind"] == "archive_worker_termination"]
            termination = term[-1]["payload"] if term else None
        if outcome == "done" and action in self.lose_response:
            self.lose_response.discard(action)
            raise KanbanError(f"bridge timeout during transition {action}", uncertain=True)
        return {"outcome": outcome, "status": card["status"], "termination": termination}

    def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for card in self.cards.values():
            result[card["status"]] = result.get(card["status"], 0) + 1
        return result
