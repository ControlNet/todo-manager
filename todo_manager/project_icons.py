"""Project page icons, so a project is easy to spot wherever its tasks show it.

A project's ``icon`` is stored as text:

- ``name:color``: a built-in Notion icon (icon picker, "Icons" tab) in one of its colours;
- an emoji;
- ``custom_emoji:<id>`` or ``external:<url>``: what the user may pick in Notion, written back as is;
- ``file``: an image the user uploaded; Notion's link to it expires, so it is never written back.

The agent picks from ``NAMES`` (checked on 2026-10-12 against Notion's public icon set,
``https://www.notion.so/icons/<name>_<color>.svg``: an unknown name or colour is a validation error
in the API) and a colour no other active project uses yet.
"""

from __future__ import annotations

import re
from typing import Any

# Order of preference for new projects: distinct, readable colours first, the greys last.
COLORS = ("blue", "green", "red", "purple", "orange", "yellow", "brown", "pink", "gray", "lightgray")

NAMES = (
    "airplane", "alarm", "anchor", "apple", "archive", "battery", "bell", "bicycle", "book", "book-closed",
    "bookmark", "brain", "briefcase", "bug", "bullseye", "cake", "calendar", "calendar-day", "camera", "car",
    "chart", "chart-line", "chart-pie", "chat", "check", "checklist", "checkmark", "clock", "cloud", "code",
    "coffee", "comment", "compass", "computer", "credit-card", "database", "dna", "document", "fire", "flag",
    "folder", "gear", "gem", "gift", "glasses", "globe", "graduate", "hammer", "headphones", "heart", "home",
    "hourglass", "inbox", "key", "keyboard", "laptop", "layers", "leaf", "library", "light-bulb", "link",
    "location", "lock", "mail", "map", "map-pin", "megaphone", "microphone", "microscope", "mobile", "moon",
    "movie", "music", "network", "news", "package", "pen", "pencil", "people", "phone", "pin", "pizza",
    "puzzle", "receipt", "robot", "rocket", "school", "science", "search", "send", "server", "shield",
    "slideshow", "star", "sun", "tablet", "tag", "target", "telescope", "ticket", "train", "tree", "trophy",
    "user", "video-camera", "wifi", "wrench",
)


def _name(name: str) -> str:
    """Notion treats case, spaces, underscores and hyphens in icon names alike."""
    return re.sub(r"[\s_-]+", "-", name.strip().lower())


def normalize(value: Any) -> str | None:
    if value is None or not str(value).strip():
        return None
    value = str(value).strip()
    kind, _, rest = value.partition(":")
    if kind in ("custom_emoji", "external") or value == "file" or not rest:
        return value
    return f"{_name(kind)}:{rest.strip().lower()}"


def check(value: Any) -> str | None:
    """A value given on the command line: a known built-in icon in a known colour, or an emoji."""
    value = normalize(value)
    if value is None:
        return None
    name, sep, color = value.partition(":")
    if not sep:
        if value.isascii():
            raise ValueError(f"icon {value!r}: give <name>:<color> (see `todo-manager project icons`) or an emoji")
        return value
    if name not in NAMES:
        raise ValueError(f"unknown icon name {name!r}; names: `todo-manager project icons`")
    if color not in COLORS:
        raise ValueError(f"unknown icon colour {color!r}; colours: {', '.join(COLORS)}")
    return value


def render(value: Any) -> dict[str, Any] | None:
    """The Notion icon object, or None (no icon)."""
    value = normalize(value)
    if value is None or value == "file":
        return None
    kind, sep, rest = value.partition(":")
    if not sep:
        return {"type": "emoji", "emoji": value}
    if kind == "custom_emoji":
        return {"type": "custom_emoji", "custom_emoji": {"id": rest}}
    if kind == "external":
        return {"type": "external", "external": {"url": rest}}
    return {"type": "icon", "icon": {"name": kind, "color": rest}}


def from_page(icon: dict[str, Any] | None) -> str | None:
    """The stored form of a page's icon."""
    if not icon:
        return None
    kind = icon.get("type")
    if kind == "emoji":
        return icon.get("emoji") or None
    if kind == "icon":
        native = icon.get("icon") or {}
        return normalize(f"{native.get('name')}:{native.get('color') or 'gray'}") if native.get("name") else None
    if kind == "custom_emoji":
        return f"custom_emoji:{(icon.get('custom_emoji') or {}).get('id')}"
    if kind == "external":
        return f"external:{(icon.get('external') or {}).get('url')}"
    return "file"


def writable(value: Any) -> bool:
    """Whether the value can be written to a page (an uploaded image cannot)."""
    return normalize(value) != "file"
