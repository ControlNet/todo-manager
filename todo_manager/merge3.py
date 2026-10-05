"""Line-based three-way merge (diff3 style), stdlib only.

Used to upgrade files the user may edit (the Hermes skill, the cron prompts, the workspace
``AGENTS.md``): ``base`` is the text Todo Manager installed, ``ours`` what the user has now,
``theirs`` the new template. A region changed on one side only takes that side; the same change
on both sides is taken once; different changes to the same region are a conflict, rendered with
markers for the user to resolve (the caller keeps the user's file unchanged in that case).
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

OURS_MARK = "<<<<<<< yours (current file)"
BASE_MARK = "||||||| installed before"
SPLIT_MARK = "======="
THEIRS_MARK = ">>>>>>> new template"


@dataclass
class MergeResult:
    text: str          # merged text; with conflict markers when ``conflicts`` > 0
    conflicts: int

    @property
    def clean(self) -> bool:
        return self.conflicts == 0


def _lines(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def _mapping(base: list[str], other: list[str]) -> dict[int, int]:
    out: dict[int, int] = {}
    for i, j, n in SequenceMatcher(None, base, other, autojunk=False).get_matching_blocks():
        for k in range(n):
            out[i + k] = j + k
    return out


def _ensure_newline(chunk: list[str]) -> list[str]:
    return [*chunk[:-1], chunk[-1] if chunk[-1].endswith("\n") else chunk[-1] + "\n"] if chunk else chunk


def merge3(base: str, ours: str, theirs: str) -> MergeResult:
    b, o, t = _lines(base), _lines(ours), _lines(theirs)
    in_ours, in_theirs = _mapping(b, o), _mapping(b, t)
    # Base lines kept unchanged on both sides, in order, are the synchronisation points.
    sync = [(i, in_ours[i], in_theirs[i]) for i in range(len(b)) if i in in_ours and i in in_theirs]
    sync.append((len(b), len(o), len(t)))
    out: list[str] = []
    conflicts = 0
    pb = po = pt = 0
    last_o = last_t = -1
    for i, jo, jt in sync:
        if jo <= last_o or jt <= last_t:
            continue            # not monotonic on one side (a moved line): leave it to the next point
        base_chunk, ours_chunk, theirs_chunk = b[pb:i], o[po:jo], t[pt:jt]
        if ours_chunk == base_chunk:
            out.extend(theirs_chunk)
        elif theirs_chunk == base_chunk or ours_chunk == theirs_chunk:
            out.extend(ours_chunk)
        else:
            conflicts += 1
            out.append(OURS_MARK + "\n")
            out.extend(_ensure_newline(ours_chunk))
            out.append(BASE_MARK + "\n")
            out.extend(_ensure_newline(base_chunk))
            out.append(SPLIT_MARK + "\n")
            out.extend(_ensure_newline(theirs_chunk))
            out.append(THEIRS_MARK + "\n")
        if i < len(b):
            out.append(o[jo])
        pb, po, pt = i + 1, jo + 1, jt + 1
        last_o, last_t = jo, jt
    return MergeResult("".join(out), conflicts)
