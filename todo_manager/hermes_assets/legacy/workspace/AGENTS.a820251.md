# My Todo conventions

How I want Hermes to handle my todos. This file is mine: I edit it, and Hermes edits it when I
ask ("from now on ..."). Hermes reads it in every Todo Manager run (the two cron jobs work in this
folder) and before Todo work in a chat. The general defaults it refines are in the
`todo-manager` skill; write here only what is specific to me. One short bullet per convention,
in any language.

## What becomes a task

Who and what I am responsible for beyond the defaults (roles, teams, recurring duties).

## What to dismiss

Material that never needs a task for me. A convention may name a label for the outcome, e.g.
"newsletters from my department: dismiss (label `dept-news`)".

## Questions, reminders and follow-ups

When to ask me instead of deciding, how soon to follow up with others, what to keep in focus.

## Agent work

What agents may do for me without asking, and how I like deliverables.

## Writing

Language, tone and wording of titles, next steps and replies.

## About this folder

- `config.toml`: when and what is read (sources, quiet windows, batch sizes); change values
  with `todo-manager config set <key> <value>`.
- `state/` (database, install records), `runs/`, `exports/`, `backups/`: kept by
  `todo-manager`; do not edit them by hand.
- `rules-archive.md` (after an upgrade): rules of an earlier version that were not active; not
  read by Hermes.
