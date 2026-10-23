"""Command line: scan, update (fast-forward clean repos), folders, gui. `scan` exits 1 when work is at risk."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from . import __version__, settings
from .radar import LEVELS, GitError, fast_forward, fast_forward_blocker, plural, scan

COUNT_WORDS = {"busy": "mid-merge or rebase", "risk": "at risk", "changes": "with uncommitted changes",
               "behind": "behind", "error": "unreadable", "ok": "OK"}


def roots_for(args) -> list[str]:
    roots = args.folders or settings.load()["roots"]
    if not roots:
        raise ValueError("No folders to scan. Add one with:  reporadar folders add D:\\code")
    return roots


def run_scan(args, do_fetch: bool):
    live = sys.stderr is not None and sys.stderr.isatty()

    def progress(done, total, repo):
        if live:
            print(f"\r  {done}/{total}  {repo.name[:40]:<40}", end="", file=sys.stderr, flush=True)

    repos = scan(roots_for(args), depth=args.depth or settings.load()["depth"], do_fetch=do_fetch,
                 progress=progress)
    if live:
        print("\r" + " " * 52 + "\r", end="", file=sys.stderr)
    return repos


def summary_line(repos) -> str:
    if not repos:
        return "No git repositories found."
    counts = {level: sum(r.level == level for r in repos) for level in LEVELS}
    parts = [f"{counts[level]} {COUNT_WORDS[level]}" for level in LEVELS if counts[level]]
    line = f"{plural(len(repos), 'repository', 'repositories')}: " + ", ".join(parts) + "."
    if not counts["risk"] and not counts["busy"]:
        line += " Nothing is at risk: every commit is on a remote."
    return line


def cmd_scan(args) -> int:
    repos = run_scan(args, args.fetch)
    if args.json:
        print(json.dumps([dict(asdict(r), level=r.level, summary=r.summary) for r in repos], indent=2))
    else:
        shown = repos if args.all else [r for r in repos if r.level != "ok"]
        width = max((len(r.path) for r in shown), default=0)
        for r in shown:
            branch = r.branch or ("" if r.error else "(detached)")
            print(f"{LEVELS[r.level]:<11} {r.path:<{width}}  {branch:<18} {r.summary}")
        if shown:
            print()
        print(summary_line(repos))
    return 1 if any(r.level in ("risk", "busy") for r in repos) else 0


def cmd_update(args) -> int:
    repos = run_scan(args, not args.no_fetch)
    ready = [r for r in repos if not fast_forward_blocker(r)]
    for r in repos:
        if r.behind and fast_forward_blocker(r):
            print(f"  skip  {r.path}: {fast_forward_blocker(r)}")
    if not ready:
        print("Nothing to update: no clean repository is behind its upstream.")
        return 0
    for r in ready:
        print(f"  ready {r.path}: {r.behind} new from {r.upstream}")
    if not args.yes:
        print(f"\nRun again with --yes to fast-forward these {len(ready)} (only clean branches, nothing is merged).")
        return 0
    failures = 0
    for r in ready:
        try:
            fast_forward(r)
            print(f"  updated {r.path}")
        except GitError as exc:
            failures += 1
            print(f"  FAILED  {r.path}: {exc}")
    return 1 if failures else 0


def cmd_folders(args) -> int:
    if args.action == "add":
        warning = settings.risky(args.path)
        if warning:
            print(f"Note: {warning}")
        roots = settings.add_root(args.path)
    elif args.action == "remove":
        roots = settings.remove_root(args.path)
    else:
        roots = settings.load()["roots"]
    print("\n".join(roots) if roots else "No folders yet. Add one with:  reporadar folders add D:\\code")
    return 0


def cmd_gui(args) -> int:
    from .gui import main as gui_main
    return gui_main()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="reporadar",
                                     description="Every git repository on your PC, and which ones hold work "
                                                 "that exists nowhere else.")
    parser.add_argument("--version", action="version", version=f"RepoRadar {__version__}")
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("scan", help="list repositories that need attention (exit code 1 if work is at risk)")
    p.add_argument("folders", nargs="*", help="folders to scan (default: your saved folders)")
    p.add_argument("--all", action="store_true", help="also list repositories that are fine")
    p.add_argument("--fetch", action="store_true", help="fetch from every remote first (uses the network)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--depth", type=int, help="how many folders deep to look (default 6)")
    p.set_defaults(func=cmd_scan)
    p = sub.add_parser("update", help="fast-forward clean repositories that are behind their upstream")
    p.add_argument("folders", nargs="*")
    p.add_argument("--yes", action="store_true", help="actually update (otherwise just list what would change)")
    p.add_argument("--no-fetch", action="store_true", help="use what was fetched last time")
    p.add_argument("--depth", type=int)
    p.set_defaults(func=cmd_update)
    p = sub.add_parser("folders", help="list, add or remove the folders RepoRadar scans")
    p.add_argument("action", nargs="?", choices=("list", "add", "remove"), default="list")
    p.add_argument("path", nargs="?")
    p.set_defaults(func=cmd_folders)
    sub.add_parser("gui", help="open the window").set_defaults(func=cmd_gui)
    args = parser.parse_args(argv)
    if not args.command:
        return cmd_gui(args)
    if args.command == "folders" and args.action != "list" and not args.path:
        parser.error(f"folders {args.action} needs a folder")
    try:
        return args.func(args)
    except (OSError, ValueError, GitError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
