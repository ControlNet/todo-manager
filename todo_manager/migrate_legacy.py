"""One-off legacy Notion migration; not part of the core CLI."""
import argparse
from pathlib import Path
from .cli import _open, _print, _fail
def cmd_migrate(args: argparse.Namespace) -> int:
    from . import migration
    cfg, conn = _open(args)
    if args.action == "fetch":
        from .notion.client import NotionClient
        root = migration.fetch_old(NotionClient.from_config(cfg), cfg, args.tasks_ds, args.projects_ds)
        _print({"fetched": str(root), "next": f"python3 -m todo_manager.migrate_legacy propose {root}"})
        return 0
    root = Path(args.dir).expanduser()
    if args.action == "propose":
        _print({**migration.propose(root, cfg, set(args.bot_id or [])), "review": str(root / "summary.md")})
        return 0
    resolver = None
    if args.resolve_threads:
        from .sources.registry import conversation_sources
        mailboxes = [a for a in conversation_sources(cfg) if a.name == "gmail"]

        def resolver(old_id: str) -> str | None:
            for box in mailboxes:
                key = box.thread_key_for(old_id)
                if key:
                    return key
            return None
    try:
        if not args.yes:
            _print({"preview": migration.preview(conn, cfg, root, resolve_thread=resolver),
                    "note": "nothing was written; re-run with --yes to import into the local database"})
            return 0
        _print(migration.apply(conn, cfg, root, dry_run=False, resolve_thread=resolver))
    except ValueError as exc:
        return _fail("INVALID", str(exc))
    return 0



def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--home')
    sub = p.add_subparsers(dest='action', required=True)
    x = sub.add_parser('fetch')
    x.add_argument('--tasks-ds', required=True)
    x.add_argument('--projects-ds', required=True)
    x = sub.add_parser('propose')
    x.add_argument('dir')
    x.add_argument('--bot-id', action='append')
    x = sub.add_parser('apply')
    x.add_argument('dir')
    x.add_argument('--yes', action='store_true')
    x.add_argument('--resolve-threads', action='store_true')
    args = p.parse_args(argv)
    from .locks import file_lock, lock_path
    from . import config
    from .cli import _CONNECTIONS
    try:
        with file_lock(lock_path(config.load(args.home).home, 'workspace'), blocking=True, shared=True):
            return cmd_migrate(args)
    finally:
        while _CONNECTIONS:
            _CONNECTIONS.pop().close()


if __name__ == '__main__':
    raise SystemExit(main())
