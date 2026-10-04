"""Stable identifiers.

Entity IDs are generated exactly once when the entity is first created and never
derived from mutable fields (titles, timestamps of retries). They are sortable by
creation time to keep exports readable.
"""

from __future__ import annotations

import hashlib
import os
import time

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"  # Crockford base32, lower-case

PREFIXES = {
    "task": "tsk",
    "project": "prj",
    "source": "src",
    "batch": "bat",
    "feedback": "fbk",
    "rule": "rul",
    "delegation": "dlg",
    "delivery": "dlv",
    "approval": "apv",
    "decision": "dec",
    "outbox": "obx",
    "run": "run",
    "event": "evt",
}


def _b32(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def new_id(kind: str) -> str:
    prefix = PREFIXES[kind]
    millis = int(time.time() * 1000)
    rand = int.from_bytes(os.urandom(8), "big")
    return f"{prefix}_{_b32(millis, 9)}{_b32(rand, 8)}"


def content_hash(*parts: object) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(repr(part).encode("utf-8"))
        digest.update(b"\x1f")
    return digest.hexdigest()[:24]


def delegation_idempotency_key(delegation_id: str) -> str:
    """The Kanban idempotency key for a delegation. Fixed for its whole lifetime."""
    return f"todo-manager:{delegation_id}"
