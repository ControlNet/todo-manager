"""Project templates and exact historical versions used to establish ownership."""

from __future__ import annotations

from importlib import resources

# Commits whose template text is kept under hermes_assets/legacy/<dir>/<stem>.<commit>.md, newest first.
EARLIER = {
    "skill/SKILL.md": ("a820251", "7718c03", "c1df92e", "2052e52", "ec85851", "3bfbb37"),
    "cron/intake.md": ("a820251", "7718c03", "3bfbb37"),
    "cron/feedback.md": ("a820251", "7718c03", "3bfbb37"),
    "workspace/AGENTS.md": ("a820251", "7718c03", "b99b192"),
}
WORKSPACE_AGENTS = {"en": "workspace/AGENTS.md", "zh-CN": "workspace/AGENTS.zh-CN.md"}


def asset(name: str) -> str:
    if name.startswith("cron/"):
        name = "prompts/" + name.split("/", 1)[1]
    return resources.files("todo_manager.hermes_assets").joinpath(name).read_text(encoding="utf-8")


def earlier(name: str) -> tuple[str, ...]:
    """Texts of ``name`` in earlier releases, newest first."""
    folder, _, file = name.rpartition("/")
    stem = file.rsplit(".", 1)[0]
    return tuple(asset(f"legacy/{folder}/{stem}.{commit}.md") for commit in EARLIER.get(name, ()))


def workspace_agents(language: str) -> str:
    return asset(WORKSPACE_AGENTS.get(language, WORKSPACE_AGENTS["en"]))


def cron_prompt(entry: str) -> str:
    """As stored in the Hermes job (the installer passes the text stripped)."""
    return asset(f"cron/{entry}.md").strip()


def earlier_cron_prompts(entry: str) -> tuple[str, ...]:
    return tuple(t.strip() for t in earlier(f"cron/{entry}.md"))
