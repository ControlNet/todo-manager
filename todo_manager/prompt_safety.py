"""Text that reaches a Hermes prompt: the checks Hermes applies, so ours never trip them.

Hermes scans two surfaces (checked at hermes-agent 105568c):

* the assembled cron prompt (skill bodies + prompt + pre-run script output): a hit on one of
  ``CRON_PATTERNS`` blocks the whole run (``tools/cronjob_prompt_scan.py``);
* project context files such as the workspace ``AGENTS.md``: a hit on one of the
  ``CONTEXT_PATTERNS`` replaces the WHOLE file by a "BLOCKED" marker (``tools/threat_patterns.py``,
  scope ``context``).

The patterns are re-stated here (same regexes, case-insensitive, NFKC for the context scan) so
that the gate can neutralise source text before printing it and so that templates, migrated
conventions and ``health`` can tell when a file would be blocked. When Hermes changes its lists,
update these and the knowledge file.
"""

from __future__ import annotations

import re
import unicodedata

_FILLER = r"(?:\w+\s+){0,8}"

# tools/cronjob_prompt_scan.py: _CRON_THREAT_PATTERNS[:4] (the assembled-prompt set)
CRON_PATTERNS: list[tuple[str, str]] = [
    (r"ignore\s+(?:\w+\s+)*(?:previous|all|above|prior)\s+(?:\w+\s+)*instructions", "prompt_injection"),
    (r"do\s+not\s+tell\s+the\s+user", "deception_hide"),
    (r"system\s+prompt\s+override", "sys_prompt_override"),
    (r"disregard\s+(your|all|any)\s+(instructions|rules|guidelines)", "disregard_rules"),
]

# tools/threat_patterns.py: scopes "all" + "context" (what a project context file is scanned with)
CONTEXT_PATTERNS: list[tuple[str, str]] = [
    (rf"ignore\s+{_FILLER}(previous|all|above|prior)\s+{_FILLER}instructions", "prompt_injection"),
    (r"system\s+prompt\s+override", "sys_prompt_override"),
    (rf"disregard\s+{_FILLER}(your|all|any)\s+{_FILLER}(instructions|rules|guidelines)", "disregard_rules"),
    (rf"act\s+as\s+(if|though)\s+{_FILLER}you\s+{_FILLER}(have\s+no|don't\s+have)\s+{_FILLER}"
     r"(restrictions|limits|rules)", "bypass_restrictions"),
    (r"<!--[^>]{0,512}(?:ignore|override|system|secret|hidden)[^>]{0,512}-->", "html_comment_injection"),
    (r"<\s*div\s+style\s*=\s*[\"'][^>]{0,2048}display\s*:\s*none", "hidden_div"),
    (r"translate\s+[^\n]{0,512}\s+into\s+\w+(?:[\s-]+\w+){0,2}\s+and\s+(execute|run|eval)\b", "translate_execute"),
    (rf"do\s+not\s+{_FILLER}tell\s+{_FILLER}the\s+user", "deception_hide"),
    (rf"you\s+are\s+{_FILLER}now\s+(?:a|an|the)\s+", "role_hijack"),
    (rf"pretend\s+{_FILLER}(you\s+are|to\s+be)\s+", "role_pretend"),
    (rf"output\s+{_FILLER}(system|initial)\s+prompt", "leak_system_prompt"),
    (rf"(respond|answer|reply)\s+without\s+{_FILLER}(restrictions|limitations|filters|safety)", "remove_filters"),
    (rf"you\s+have\s+been\s+{_FILLER}(updated|upgraded|patched)\s+to", "fake_update"),
    (r"\bname\s+yourself\s+\w+", "identity_override"),
    (r"register\s+(as\s+)?a?\s*node", "c2_node_registration"),
    (r"(heartbeat|beacon|check[\s\-]?in)\s+(to|with)\s+", "c2_heartbeat"),
    (r"pull\s+(down\s+)?(?:new\s+)?task(?:ing|s)?\b", "c2_task_pull"),
    (r"connect\s+to\s+the\s+network\b", "c2_network_connect"),
    (r"you\s+must\s+(?:\w+\s+){0,3}(register|connect|report|beacon)\b", "forced_action"),
    (r"only\s+use\s+one[\s\-]?liners?\b", "anti_forensic_oneliner"),
    (rf"never\s+{_FILLER}(?:create|write)\s+{_FILLER}(?:script|file)\s+{_FILLER}disk", "anti_forensic_disk"),
    (r"unset\s+\w*(?:CLAUDE|CODEX|HERMES|AGENT|OPENAI|ANTHROPIC)\w*", "env_var_unset_agent"),
    (r"\b(?:cobalt\s*strike|sliver|havoc|mythic|metasploit|brainworm)\b", "known_c2_framework"),
    (r"\bc2\s+(?:server|channel|infrastructure|beacon)\b", "c2_explicit"),
    (r"\bcommand\s+and\s+control\b", "c2_explicit_long"),
    (r"curl\s+[^\n]{0,2048}\$\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)S?\b", "exfil_curl"),
    (r"wget\s+[^\n]{0,2048}\$\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)S?\b", "exfil_wget"),
    (r"cat\s+[^\n]{0,2048}(\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)", "read_secrets"),
]

INVISIBLE_CHARS = frozenset("​‌‍⁠⁢⁣⁤﻿"
                            "‪‫‬‭‮⁦⁧⁨⁩")
MAX_SCAN_CHARS = 65_536   # Hermes scans only this much of a context file

_CRON = [(re.compile(p, re.IGNORECASE), pid) for p, pid in CRON_PATTERNS]
_CONTEXT = [(re.compile(p, re.IGNORECASE), pid) for p, pid in CONTEXT_PATTERNS]


def cron_hits(text: str) -> list[str]:
    """Pattern ids that would block a cron run whose prompt contains ``text``."""
    return [pid for rx, pid in _CRON if rx.search(text or "")]


def context_hits(text: str) -> list[str]:
    """Pattern ids that would make Hermes refuse ``text`` as a project context file
    (``AGENTS.md``); invisible characters are reported as ``invisible_unicode_U+XXXX``."""
    if not text:
        return []
    text = text[:MAX_SCAN_CHARS]
    found = [f"invisible_unicode_U+{ord(ch):04X}" for ch in sorted(set(text) & INVISIBLE_CHARS)]
    normalised = unicodedata.normalize("NFKC", text)
    return found + [pid for rx, pid in _CONTEXT if rx.search(normalised)]


def neutralize(text: str, marker: str) -> tuple[str, int]:
    """Replace every span of ``text`` that would block a cron run by ``marker`` (visible, never
    silent). Returns the new text and the number of replacements."""
    count = 0
    for rx, _pid in _CRON:
        text, n = rx.subn(marker, text)
        count += n
    return text, count


def one_line(value: str | None) -> str:
    """A header field (subject, author, title) on one line: newlines and runs of white space
    collapse, so source text cannot start a line of its own."""
    return " ".join((value or "").split())
