"""Build source adapters from configuration (only sources explicitly enabled and scoped)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import Config, read_env_secret
from .github import CommitSource, GitHubClient, GitHubNotificationSource, GitHubRepoSource


def conversation_sources(cfg: Config) -> list[Any]:
    adapters: list[Any] = []
    ident = cfg.section("identity")
    email = cfg.section("email")
    if email.get("enabled"):
        from .gmail import GmailSource, TokenProvider
        github_api = set(cfg.get("github.repos", [])) if cfg.get("github.enabled") else set()
        for account in email.get("accounts", []):
            adapters.append(GmailSource(
                account=account["name"], address=account["address"],
                token=TokenProvider(account["token_command"]), my_addresses=ident.get("emails", []),
                query=email.get("query", ""), page_size=int(email.get("page_size", 100)),
                overlap_minutes=int(email.get("overlap_minutes", 120)),
                initial_lookback_days=int(email.get("initial_lookback_days", 14)),
                github_api_repos=github_api))
    gh = cfg.section("github")
    if gh.get("enabled"):
        client = GitHubClient()
        for repo in gh.get("repos", []):
            adapters.append(GitHubRepoSource(client, repo, ident.get("github_logins", []), gh.get("bot_logins", []),
                                             overlap_minutes=int(gh.get("overlap_minutes", 30))))
        if gh.get("notifications", True):
            adapters.append(GitHubNotificationSource(client, set(gh.get("repos", [])), ident.get("github_logins", []),
                                                     bot_logins=gh.get("bot_logins", []),
                                                     overlap_minutes=int(gh.get("overlap_minutes", 30))))
    slack = cfg.section("slack")
    if slack.get("enabled"):
        from .slack import SlackDirectory, SlackSource
        token_env = slack.get("token_env", "SLACK_USER_TOKEN")
        token_file = slack.get("token_file") or ""
        env_file = cfg.get("notion.env_file")

        def token() -> str:
            from .base import SourceError
            if token_file:   # e.g. a 0600 file under ~/.hermes/credentials/
                path = Path(token_file).expanduser()
                try:
                    value = path.read_text(encoding="utf-8").strip()
                except OSError:
                    raise SourceError(f"Slack token file {path} is not readable") from None
                if not value:
                    raise SourceError(f"Slack token file {path} is empty")
                return value
            value = read_env_secret(token_env, env_file)
            if not value:
                raise SourceError(f"{token_env} is not set")
            return value

        names: dict[str, str | None] = {}   # one users.info lookup per member per gate run

        def make(conversation: str, peer: str | None = None, title: str | None = None) -> SlackSource:
            return SlackSource(token=token, team=slack.get("team", ""), conversation=conversation,
                               my_user_ids=ident.get("slack_user_ids", []), page_size=int(slack.get("page_size", 200)),
                               user_names=names, peer=peer, title=title,
                               active_hours=int(slack.get("active_hours", 168)),
                               quiet_poll_minutes=int(slack.get("quiet_poll_minutes", 30)))

        for conv in slack.get("conversations", []):
            adapters.append(make(conv))
        types = [kind for kind, key in (("im", "direct_messages"), ("mpim", "group_direct_messages"),
                                        ("public_channel", "channels"), ("private_channel", "channels"))
                 if slack.get(key)]
        if types:   # every DM / group DM / member channel, discovered each run (explicit ids above are not read twice)
            adapters.append(SlackDirectory(token=token, types=types, make=make, exclude=slack.get("exclude", []),
                                           page_size=int(slack.get("page_size", 200))))
    return adapters


def commit_sources(cfg: Config) -> list[Any]:
    commits = cfg.section("commits")
    if not commits.get("enabled"):
        return []
    client = GitHubClient()
    return [CommitSource(client, repo, cfg.get("github.bot_logins", [])) for repo in commits.get("repos", [])]
