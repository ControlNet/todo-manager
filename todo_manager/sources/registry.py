"""Build source adapters from configuration (only sources explicitly enabled and scoped)."""

from __future__ import annotations

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
        from .slack import SlackSource
        token_env = slack.get("token_env", "SLACK_USER_TOKEN")
        env_file = cfg.get("notion.env_file")

        def token() -> str:
            value = read_env_secret(token_env, env_file)
            if not value:
                from .base import SourceError
                raise SourceError(f"{token_env} is not set")
            return value

        for conv in slack.get("conversations", []):
            adapters.append(SlackSource(token=token, team=slack.get("team", ""), conversation=conv,
                                        my_user_ids=ident.get("slack_user_ids", []),
                                        page_size=int(slack.get("page_size", 200))))
    return adapters


def commit_sources(cfg: Config) -> list[Any]:
    commits = cfg.section("commits")
    if not commits.get("enabled"):
        return []
    client = GitHubClient()
    return [CommitSource(client, repo, cfg.get("github.bot_logins", [])) for repo in commits.get("repos", [])]
