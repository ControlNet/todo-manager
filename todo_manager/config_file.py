"""Targeted edits of ``config.toml``: set a few keys, keep everything else as written.

``todo-manager install`` records what it infers and what the user chose here, key by key. The
rest of the file (comments, order, values the user set) is left byte for byte. Only the subset of
TOML that config.toml uses is handled: top-level keys and ``[table]`` headers, values on one line
or arrays/inline tables spanning lines; anything else (array-of-tables headers, multi-line
strings) is refused with ``ConfigEditError`` and the file is not touched. Every edit is checked by
re-reading the result with ``tomllib``: the set keys have their new values and nothing else changed.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_KEY = re.compile(r'[ \t]*([A-Za-z0-9_.-]+|"[^"\n]*")[ \t]*=')
_BARE = re.compile(r"[A-Za-z0-9_-]+")


class ConfigEditError(ValueError):
    pass


def explicit(path: Path) -> dict[str, Any]:
    """What the file itself sets (no defaults); empty when there is no file."""
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def dumps_value(value: Any, indent: str = "") -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)   # a valid TOML basic string
    if isinstance(value, dict):
        items = ", ".join(f"{k if _BARE.fullmatch(k) else json.dumps(k)} = {dumps_value(v)}" for k, v in value.items())
        return "{" + items + "}"
    if isinstance(value, (list, tuple)):
        if any(isinstance(v, dict) for v in value):
            return "[\n" + "".join(f"{indent}  {dumps_value(v)},\n" for v in value) + indent + "]"
        return "[" + ", ".join(dumps_value(v) for v in value) + "]"
    raise ConfigEditError(f"cannot write a {type(value).__name__} value")


@dataclass
class _Table:
    start: int                 # just after the header line (0 for the top level)
    end: int                   # just after the last key line of the table
    keys: dict[str, tuple[int, int]] = field(default_factory=dict)   # key -> value span


def _string_end(text: str, i: int) -> int:
    quote = text[i]
    if text.startswith(quote * 3, i):
        raise ConfigEditError("multi-line strings are not supported")
    j = i + 1
    while j < len(text):
        c = text[j]
        if c == "\n":
            break
        if quote == '"' and c == "\\":
            j += 2
            continue
        if c == quote:
            return j + 1
        j += 1
    raise ConfigEditError("unterminated string")


def _value_end(text: str, i: int) -> int:
    """Index just past the value that starts at ``text[i]``."""
    depth = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in "\"'":
            i = _string_end(text, i)
            if depth == 0:
                return i
            continue
        if depth == 0 and c in " \t\r\n#":
            return i
        if c == "#":                          # a comment inside a multi-line array
            nl = text.find("\n", i)
            i = n if nl == -1 else nl
            continue
        if c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    if depth:
        raise ConfigEditError("unterminated array or inline table")
    return i


def _scan(text: str) -> tuple[dict[str | None, _Table], int | None]:
    """Tables with their key spans, and the offset of the first header (None: no table)."""
    tables: dict[str | None, _Table] = {None: _Table(0, 0)}
    current: str | None = None
    first_header: int | None = None
    i, n = 0, len(text)
    while i < n:
        nl = text.find("\n", i)
        eol = n if nl == -1 else nl
        stripped = text[i:eol].strip()
        if not stripped or stripped.startswith("#"):
            i = eol + 1
            continue
        if stripped.startswith("["):
            if stripped.startswith("[[") or "]" not in stripped:
                raise ConfigEditError(f"unsupported table header: {stripped[:40]}")
            current = stripped[1:stripped.index("]")].strip()
            if current in tables:
                raise ConfigEditError(f"table [{current}] appears twice")
            first_header = i if first_header is None else first_header
            tables[current] = _Table(eol + 1, eol + 1)
            i = eol + 1
            continue
        m = _KEY.match(text, i)
        if not m:
            raise ConfigEditError(f"cannot read the line: {stripped[:40]}")
        start = m.end()
        while start < n and text[start] in " \t":
            start += 1
        end = _value_end(text, start)
        if end == start:
            raise ConfigEditError(f"missing value for {m.group(1)}")
        tables[current].keys[m.group(1).strip('"')] = (start, end)
        nl = text.find("\n", end)
        i = n if nl == -1 else nl + 1
        tables[current].end = i
    return tables, first_header


def _set(text: str, dotted: str, value: Any) -> str:
    table, _, key = dotted.rpartition(".")
    table = table or None
    tables, first_header = _scan(text)
    rendered = dumps_value(value)
    t = tables.get(table)
    if t is not None and key in t.keys:
        start, end = t.keys[key]
        return text[:start] + rendered + text[end:]
    line = f"{key} = {rendered}\n"
    if t is None:
        if text and not text.endswith("\n"):
            text += "\n"
        return text + ("\n" if text else "") + f"[{table}]\n" + line
    at = t.end
    if table is None and not t.keys:
        at = 0 if first_header is None else first_header
    if at > 0 and text[at - 1] != "\n":
        text, at = text[:at] + "\n" + text[at:], at + 1
    return text[:at] + line + text[at:]


def _get(data: dict[str, Any], dotted: str) -> Any:
    node: Any = data
    for part in dotted.split("."):
        node = node[part]
    return node


def _without(data: dict[str, Any], dotted_keys: list[str]) -> dict[str, Any]:
    out = json.loads(json.dumps(data, default=str))
    for dotted in dotted_keys:
        table, _, key = dotted.rpartition(".")
        node = out.get(table, {}) if table else out
        node.pop(key, None)
        if table and not node:
            out.pop(table, None)
    return out


def set_values(path: Path, values: dict[str, Any]) -> list[str]:
    """Set ``values`` (``"table.key"`` or ``"key"``) in the file; returns the keys that changed."""
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        before = tomllib.loads(original)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigEditError(f"{path} is not valid TOML: {exc}") from None
    changed = []
    text = original
    for dotted, value in values.items():
        try:
            if _get(before, dotted) == value:
                continue
        except (KeyError, TypeError):
            pass
        text = _set(text, dotted, value)
        changed.append(dotted)
    if not changed:
        return []
    try:
        after = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigEditError(f"the edit would make {path} invalid: {exc}") from None
    if any(_get(after, k) != values[k] for k in changed) or _without(after, changed) != _without(before, changed):
        raise ConfigEditError(f"the edit of {path} did not give the expected values; the file was not changed")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config.", suffix=".toml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return changed
