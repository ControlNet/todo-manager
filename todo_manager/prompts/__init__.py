"""Versioned prompt fragments printed by ``todo-manager ... show`` (they win over the skill)."""

from __future__ import annotations

from importlib import resources


def load(name: str) -> str:
    return resources.files(__package__).joinpath(name).read_text(encoding="utf-8")
