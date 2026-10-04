"""Files the workspace keeps: delivery artifacts copied out of a worker's scratch space, stored
with workspace-relative paths and SHA-256 so that they move with an export."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

MAX_KEPT_BYTES = 100 * 1024 * 1024   # larger files are referenced (with their hash), not copied


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def keep_artifacts(home: Path, delivery_id: str, paths: list[str]) -> list[dict[str, Any]]:
    """Copy each artifact to ``deliveries/<delivery id>/`` under ``home``. A file that cannot be
    read is recorded as missing (never silently dropped). Each copy is sealed: written under a
    temporary name, hashed as copied (the worker may still be writing the source), then renamed,
    so a kept name always holds complete bytes matching the recorded hash. A failed copy raises
    (the delivery is not recorded and the next sync retries) and leaves nothing behind."""
    kept: list[dict[str, Any]] = []
    folder = Path("deliveries") / delivery_id
    used: set[str] = set()
    for source in paths:
        src = Path(source).expanduser()
        try:
            if not src.is_file():
                raise OSError("not a file")
            size = src.stat().st_size
        except OSError:
            kept.append({"source": source, "missing": True})
            continue
        if size > MAX_KEPT_BYTES:
            kept.append({"source": source, "sha256": sha256_file(src), "size": size, "not_copied": "too large"})
            continue
        name, n = src.name, 1
        while name in used:
            n += 1
            name = f"{src.stem}-{n}{src.suffix}"
        used.add(name)
        (home / folder).mkdir(parents=True, exist_ok=True)
        final = home / folder / name
        tmp = final.with_name(f".{name}.tmp")
        try:
            shutil.copyfile(src, tmp)
            digest, size = sha256_file(tmp), tmp.stat().st_size
            tmp.replace(final)
        finally:
            tmp.unlink(missing_ok=True)
        kept.append({"path": str(folder / name), "sha256": digest, "size": size, "source": source})
    return kept


def artifact_label(artifact: Any) -> str:
    """How an artifact is shown: its kept workspace path, else where it came from."""
    if isinstance(artifact, dict):
        return str(artifact.get("path") or artifact.get("source") or "")
    return str(artifact)
