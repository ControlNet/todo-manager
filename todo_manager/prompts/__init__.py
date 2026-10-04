"""Versioned prompt fragments printed by ``todo-manager ... show`` (they win over the skill).

Prompts are English; ``{language}`` is replaced with the configured user language.
"""

from __future__ import annotations

from importlib import resources

from .. import i18n


def load(name: str) -> str:
    text = resources.files(__package__).joinpath(name).read_text(encoding="utf-8")
    return text.replace("{language}", i18n.language_name())
