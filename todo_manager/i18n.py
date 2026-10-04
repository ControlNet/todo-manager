"""User-facing text in the configured language.

The language comes from ``language`` in the workspace ``config.toml`` (set by
``todo-manager init``, which defaults to the system locale), and can be overridden with the
``TODO_MANAGER_LANG`` environment variable. Catalogs live in ``todo_manager/locales/<lang>.json``;
a key missing from a catalog falls back to English.

Text written into Notion structure (property and option names) uses the language that was
active at ``notion setup`` (stored in the local state), so switching languages later never
breaks the mapping; parsing accepts labels and markers of every known language.

Model-facing prompts stay in English and name the user's language explicitly.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from importlib import resources

DEFAULT = "en"
LANGUAGES = {"en": "English", "zh-CN": "Simplified Chinese (简体中文)"}
_ALIASES = {"zh": "zh-CN", "zh-cn": "zh-CN", "zh-hans": "zh-CN", "zh-sg": "zh-CN", "zh_cn": "zh-CN",
            "en-us": "en", "en-gb": "en", "en-au": "en", "c": "en", "posix": "en"}
_current = DEFAULT


def normalize(lang: str | None) -> str | None:
    """Map 'zh_CN.UTF-8', 'zh-Hans', 'en_AU' ... to a supported language, else None."""
    if not lang:
        return None
    code = lang.split(".")[0].split("@")[0].replace("_", "-").strip()
    if code in LANGUAGES:
        return code
    low = code.lower()
    if low in _ALIASES:
        return _ALIASES[low]
    base = low.split("-")[0]
    return _ALIASES.get(base) or (base if base in LANGUAGES else None)


def from_environment() -> str:
    """Best guess from the process locale (used as the ``init`` default)."""
    for var in ("TODO_MANAGER_LANG", "LC_ALL", "LC_MESSAGES", "LANG"):
        lang = normalize(os.environ.get(var))
        if lang:
            return lang
    return DEFAULT


def set_language(lang: str | None) -> str:
    global _current
    _current = normalize(lang) or DEFAULT
    return _current


def language() -> str:
    return _current


def language_name(lang: str | None = None) -> str:
    return LANGUAGES[normalize(lang) or _current]


@lru_cache(maxsize=None)
def catalog(lang: str) -> dict[str, str]:
    text = resources.files("todo_manager.locales").joinpath(f"{lang}.json").read_text(encoding="utf-8")
    return json.loads(text)


def t(key: str, lang: str | None = None, **values: object) -> str:
    lang = normalize(lang) or _current
    text = catalog(lang).get(key)
    if text is None:
        text = catalog(DEFAULT)[key]   # KeyError for an unknown key is a bug worth surfacing
    return text.format(**values) if values else text


def variants(key: str) -> set[str]:
    """The text of ``key`` in every language (for recognising our own markers and labels)."""
    return {catalog(lang)[key] for lang in LANGUAGES if key in catalog(lang)}


def prefixed(prefix: str, lang: str | None = None) -> dict[str, str]:
    """``{suffix: text}`` for all keys starting with ``prefix.`` (e.g. option label groups)."""
    lang = normalize(lang) or _current
    merged = {**catalog(DEFAULT), **catalog(lang)}
    return {k[len(prefix) + 1:]: v for k, v in merged.items() if k.startswith(prefix + ".")}
