"""Find the git repositories under your folders and say which ones hold work that exists only on this PC.

Scanning doesn't change repositories: git runs with --no-optional-locks (no index refresh writes) and with
core.fsmonitor off. Like any `git status`, it can still run programs a repository's own settings name (clean
filters, for example), so scan folders whose repositories you trust, not Downloads or unpacked archives. Only
fetch() and fast_forward() change anything, and only when asked.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".tox", ".mypy_cache", ".pytest_cache",
             ".gradle", ".idea", ".vs", "Intermediate", "DerivedDataCache", "$RECYCLE.BIN",
             "System Volume Information"}
OPERATIONS = (("MERGE_HEAD", "merge"), ("rebase-merge", "rebase"), ("rebase-apply", "rebase"),
              ("CHERRY_PICK_HEAD", "cherry-pick"), ("REVERT_HEAD", "revert"), ("BISECT_LOG", "bisect"))
# Worst first; the order the list is sorted in.
LEVELS = {"busy": "IN PROGRESS", "risk": "AT RISK", "changes": "CHANGES", "behind": "BEHIND", "error": "ERROR",
          "ok": "OK"}
ORDER = list(LEVELS)


class GitError(RuntimeError):
    pass


@dataclass
class Repo:
    path: str
    branch: str = ""
    head: str = ""                 # the commit checked out, to notice changes between scan and update
    detached: bool = False
    empty: bool = False            # no commits yet
    upstream: str = ""
    upstream_gone: bool = False    # set, but deleted on the remote
    ahead: int = 0
    behind: int = 0
    changed: int = 0               # tracked files with edits
    staged: int = 0
    untracked: int = 0
    conflicts: int = 0
    stashes: int = 0
    remotes: list = field(default_factory=list)
    unpushed: int = 0              # commits on the checked-out branch that no remote has
    other_unpushed: int = 0        # same, on the other local branches (counted once per repository)
    detached_commits: int = 0      # commits only reachable from a detached HEAD
    no_upstream: list = field(default_factory=list)   # local branches never pushed
    gone: list = field(default_factory=list)          # local branches whose upstream was deleted
    common: str = ""               # the shared git folder; worktrees of one repository have the same one
    linked: bool = False           # a linked worktree (`git worktree add`), not the main checkout
    worktree_of: str = ""          # for worktrees scanned alongside their main checkout
    operation: str = ""            # merge / rebase / cherry-pick / revert / bisect in progress
    last_commit: float = 0.0
    last_subject: str = ""
    last_fetch: float = 0.0        # 0 = unknown
    files: list = field(default_factory=list)         # first changed paths, "M  src/app.py"
    error: str = ""
    fetch_error: str = ""

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def level(self) -> str:
        return verdict(self)[0]

    @property
    def summary(self) -> str:
        return verdict(self)[1]


def plural(count: int, word: str, many: str = "") -> str:
    return f"{count} {word if count == 1 else many or word + 's'}"


# --- finding repositories --------------------------------------------------------------------------------------

def _is_link(entry: os.DirEntry) -> bool:
    """Symlinks and junctions both: following them would scan the same repos twice, or someone else's disk."""
    if entry.is_symlink():
        return True
    try:
        attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _is_git(entry: os.DirEntry) -> bool:
    """A real .git: a folder with HEAD in it, or a 'gitdir:' pointer to a folder that still exists.
    Empty leftovers and pointers to deleted folders hold nothing, so they don't count."""
    try:
        if entry.is_dir():
            return os.path.isfile(os.path.join(entry.path, "HEAD"))
        with open(entry.path, "rb") as handle:
            text = handle.read(4096).decode("utf-8", "replace").strip()
    except OSError:
        return False
    if not text.startswith("gitdir:"):
        return False
    target = text[7:].strip()
    return os.path.isdir(os.path.join(os.path.dirname(entry.path), target))  # join keeps an absolute target


def find_repos(roots, max_depth: int = 6) -> list[str]:
    """Folders under the roots that contain .git (a folder, or a file for worktrees and submodules).

    The roots themselves may be links; links below them are never followed.
    """
    found: dict[str, str] = {}
    for root in roots:
        stack = [(os.path.abspath(root), 0)]
        while stack:
            folder, depth = stack.pop()
            try:
                with os.scandir(folder) as listing:
                    entries = list(listing)
            except OSError:
                continue
            if any(entry.name == ".git" and _is_git(entry) for entry in entries):
                found.setdefault(os.path.normcase(folder), folder)
            if depth >= max_depth:
                continue
            for entry in entries:
                if entry.name in SKIP_DIRS:
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=False) or _is_link(entry):
                        continue
                except OSError:
                    continue
                stack.append((entry.path, depth + 1))
    return sorted(found.values(), key=str.lower)


def default_roots() -> list[str]:
    """Where repositories usually live on Windows, if those folders exist."""
    home = Path.home()
    candidates = [home / "source" / "repos", home / "repos", home / "code", home / "src", home / "dev",
                  home / "projects", home / "Projects", home / "Documents" / "GitHub", home / "git"]
    seen, roots = set(), []
    for path in candidates:
        key = os.path.normcase(str(path))
        if path.is_dir() and key not in seen:
            seen.add(key)
            roots.append(str(path))
    return roots


# --- talking to git --------------------------------------------------------------------------------------------

@lru_cache(maxsize=1)
def git_exe() -> str:
    return shutil.which("git") or "git"


def git_env(batch_ssh: bool = False) -> dict:
    """Never stop to ask for a password: fail instead, so a scan or fetch can't hang on one repository.
    batch_ssh also makes plain ssh fail instead of asking for a passphrase (only used when you haven't set up
    your own SSH command, such as plink or Windows OpenSSH)."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", LC_ALL="C", LANGUAGE="C")
    if batch_ssh:
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
    return env


def git(repo: str, *args: str, runner=subprocess.run, timeout: float = 30.0, batch_ssh: bool = False) -> str:
    command = [git_exe(), "-C", str(repo), "--no-optional-locks", "-c", "core.quotepath=off",
               "-c", "core.fsmonitor=false", *args]
    try:
        done = runner(command, capture_output=True, timeout=timeout, env=git_env(batch_ssh),
                      creationflags=NO_WINDOW)
    except FileNotFoundError:
        raise GitError("Git isn't installed: https://git-scm.com/download/win") from None
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]} took longer than {timeout:.0f} s") from None
    if done.returncode != 0:
        message = (done.stderr or b"").decode("utf-8", "replace").strip()
        if "dubious ownership" in message:
            message = ("Git doesn't trust this folder's owner. If it's yours, run: "
                       f'git config --global --add safe.directory "{Path(repo).as_posix()}"')
        raise GitError(message or f"git {args[0]} failed")
    return done.stdout.decode("utf-8", "replace")


def git_dirs(repo: str) -> tuple[Path, Path]:
    """(this checkout's git folder, the shared one). For worktrees and submodules .git is a file pointing away."""
    dot = Path(repo) / ".git"
    gitdir = dot
    if dot.is_file():
        text = dot.read_text(encoding="utf-8", errors="replace").strip()
        if text.startswith("gitdir:"):
            gitdir = Path(text[7:].strip())
            if not gitdir.is_absolute():
                gitdir = (Path(repo) / gitdir).resolve()
    common = gitdir
    pointer = gitdir / "commondir"
    if pointer.is_file():
        common = Path(pointer.read_text(encoding="utf-8", errors="replace").strip())
        if not common.is_absolute():
            common = (gitdir / common).resolve()
    return gitdir, common


def parse_status(text: str, repo: Repo, keep_files: int = 25) -> None:
    """`git status --porcelain=v2 --branch --show-stash -z` into the repo's fields."""
    records = text.split("\0")
    has_counts = False
    i = 0
    while i < len(records):
        record = records[i]
        i += 1
        if not record:
            continue
        if record.startswith("# "):
            key, _, value = record[2:].partition(" ")
            if key == "branch.oid":
                repo.empty, repo.head = value == "(initial)", value
            elif key == "branch.head":
                repo.detached = value == "(detached)"
                repo.branch = "" if repo.detached else value
            elif key == "branch.upstream":
                repo.upstream = value
            elif key == "branch.ab":
                ahead, behind = value.split()
                repo.ahead, repo.behind = abs(int(ahead)), abs(int(behind))
                has_counts = True
            elif key == "stash":
                repo.stashes = int(value)
            continue
        kind = record[0]
        if kind == "?":
            repo.untracked += 1
            code, path = "??", record[2:]
        elif kind == "u":
            repo.conflicts += 1
            code, path = "UU", record.split(" ", 10)[-1]
        elif kind in "12":
            code = record[2:4]
            repo.changed += 1
            repo.staged += code[0] != "."
            path = record.split(" ", 8 if kind == "1" else 9)[-1]
            if kind == "2":
                i += 1  # a rename: the old path comes next
        else:
            continue
        if len(repo.files) < keep_files:
            repo.files.append(f"{code.replace('.', ' ')} {path}")
    repo.upstream_gone = bool(repo.upstream) and not has_counts


def count(path: str, *revisions: str, runner=subprocess.run) -> int:
    return int(git(path, "rev-list", "--count", *revisions, runner=runner).strip() or 0)


def inspect(path: str, runner=subprocess.run) -> Repo:
    repo = Repo(path)
    try:
        parse_status(git(path, "status", "--porcelain=v2", "--branch", "--show-stash", "-z", "--untracked-files=normal",
                         runner=runner), repo)
        for line in git(path, "for-each-ref", "--format=%(refname:short)%00%(upstream:short)%00%(upstream:track)",
                        "refs/heads", runner=runner).splitlines():
            branch, upstream, track = (line.split("\0") + ["", ""])[:3]
            if not upstream:
                repo.no_upstream.append(branch)
            elif "gone" in track:
                repo.gone.append(branch)
        repo.remotes = git(path, "remote", runner=runner).split()
        if not repo.empty:
            if repo.detached:
                repo.detached_commits = count(path, "HEAD", "--not", "--branches", "--remotes", runner=runner)
            else:
                repo.unpushed = count(path, "HEAD", "--not", "--remotes", runner=runner)
            repo.other_unpushed = count(path, "--branches", "--not", "--remotes", "HEAD", runner=runner)
            stamp, _, subject = git(path, "log", "-1", "--format=%ct%x00%s", runner=runner).strip().partition("\0")
            repo.last_commit, repo.last_subject = float(stamp or 0), subject
    except GitError as exc:
        repo.error = str(exc)
    except (OSError, ValueError) as exc:
        repo.error = f"couldn't read this repository: {exc}"
    try:
        gitdir, common = git_dirs(path)
        repo.common, repo.linked = str(common), os.path.normcase(str(gitdir)) != os.path.normcase(str(common))
        for marker, operation in OPERATIONS:
            if (gitdir / marker).exists():
                repo.operation = operation
                break
        fetched = [p.stat().st_mtime for p in (gitdir / "FETCH_HEAD", common / "FETCH_HEAD") if p.exists()]
        repo.last_fetch = max(fetched, default=0.0)
    except OSError:
        pass
    return repo


def verdict(repo: Repo) -> tuple[str, str]:
    """(level, one-line explanation). Levels are LEVELS' keys."""
    if repo.error:
        return "error", repo.error.splitlines()[0][:300]
    busy, risk, changes, notes = [], [], [], []
    if repo.operation:
        busy.append(f"{repo.operation} in progress")
    if repo.conflicts:
        busy.append(plural(repo.conflicts, "conflicted file"))
    if not repo.remotes and not repo.empty:
        risk.append("no remote: every commit is only on this PC")
    else:
        if repo.unpushed:
            risk.append(f"{plural(repo.unpushed, 'commit')} on {repo.branch} not on any remote")
        if repo.other_unpushed:
            risk.append(f"{plural(repo.other_unpushed, 'commit')} on other branches not on any remote")
    if repo.detached_commits:
        risk.append(f"{plural(repo.detached_commits, 'commit')} only on a detached HEAD (on no branch)")
    if repo.stashes:
        risk.append(f"{plural(repo.stashes, 'stash', 'stashes')} (stashes are never pushed)")
    if repo.changed or repo.untracked:
        parts = []
        if repo.changed:
            parts.append(plural(repo.changed, "changed file") + (f" ({repo.staged} staged)" if repo.staged else ""))
        if repo.untracked:
            parts.append(f"{repo.untracked} untracked")
        changes.append(", ".join(parts))
    if repo.behind:
        notes.append(f"{plural(repo.behind, 'commit')} behind {repo.upstream}")
    if repo.upstream_gone:
        notes.append(f"{repo.upstream} was deleted on the remote")
    if repo.detached and not repo.detached_commits:
        notes.append("detached HEAD")
    if repo.empty:
        notes.append("no commits yet")
    if repo.fetch_error:
        notes.append(f"fetch failed: {repo.fetch_error.splitlines()[0][:120]}")
    level = "busy" if busy else "risk" if risk else "changes" if changes else "behind" if repo.behind else "ok"
    return level, "; ".join(busy + risk + changes + notes) or "clean, everything pushed"


def sort_key(repo: Repo):
    return ORDER.index(repo.level), repo.path.lower()


# --- the two actions that change something ----------------------------------------------------------------------

def own_ssh_setup(path: str, runner=subprocess.run) -> bool:
    """True when you've chosen how git runs SSH (GIT_SSH, GIT_SSH_COMMAND or core.sshCommand): leave it alone."""
    if os.environ.get("GIT_SSH") or os.environ.get("GIT_SSH_COMMAND"):
        return True
    try:
        return bool(git(path, "config", "--get", "core.sshCommand", runner=runner).strip())
    except GitError:  # not set: git exits 1
        return False


def fetch(path: str, runner=subprocess.run) -> None:
    """Download what's new from every remote (no merge, no prune). Needs the network and may need a login."""
    git(path, "fetch", "--all", "--quiet", runner=runner, timeout=180,
        batch_ssh=not own_ssh_setup(path, runner=runner))


def fast_forward_blocker(repo: Repo) -> str:
    """Why a fast-forward isn't safe, or '' when it is."""
    if repo.error:
        return "couldn't be read"
    if repo.detached:
        return "detached HEAD"
    if not repo.upstream or repo.upstream_gone:
        return "no upstream branch"
    if repo.operation or repo.conflicts:
        return f"{repo.operation or 'merge'} in progress"
    if not repo.behind:
        return "already up to date"
    if repo.ahead:
        return "has its own commits too (needs a merge or rebase, not a fast-forward)"
    if repo.changed:
        return "has uncommitted changes"
    return ""


def fast_forward(repo: Repo, runner=subprocess.run) -> Repo:
    """Move the current branch up to its upstream, only when nothing local can be lost. Returns the new state.
    The repository is read again first: if it changed since the scan (edits, commits, another branch), it's
    left alone."""
    blocker = fast_forward_blocker(repo)
    if blocker:
        raise GitError(f"Not updated: {blocker}.")
    now = inspect(repo.path, runner=runner)
    if (now.branch, now.head) != (repo.branch, repo.head):
        raise GitError("Not updated: it changed since the scan (another branch or new commits). Scan again.")
    blocker = fast_forward_blocker(now)
    if blocker:
        raise GitError(f"Not updated: {blocker} (changed since the scan).")
    git(repo.path, "merge", "--ff-only", "--quiet", "@{upstream}", runner=runner, timeout=120)
    return inspect(repo.path, runner=runner)


def scan(roots, *, depth: int = 6, do_fetch: bool = False, workers: int = 4, progress=None,
         runner=subprocess.run) -> list[Repo]:
    """Inspect every repository under the roots, a few at a time. progress(done, total, repo) is optional."""
    paths = find_repos(roots, depth)

    def one(path: str) -> Repo:
        fetch_error = ""
        if do_fetch:
            try:
                fetch(path, runner=runner)
            except GitError as exc:
                fetch_error = str(exc)
        repo = inspect(path, runner=runner)
        repo.fetch_error = fetch_error
        return repo

    repos = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for repo in pool.map(one, paths):
            repos.append(repo)
            if progress:
                progress(len(repos), len(paths), repo)
    group_worktrees(repos, runner=runner)
    return sorted(repos, key=sort_key)


def group_worktrees(repos, runner=subprocess.run) -> None:
    """Worktrees share one repository: report its other branches once, on the main checkout, and leave out the
    branches that are checked out in a scanned worktree (each of those reports its own branch)."""
    groups: dict[str, list] = {}
    for repo in repos:
        if repo.common and not repo.error:
            groups.setdefault(os.path.normcase(repo.common), []).append(repo)
    for members in groups.values():
        if len(members) < 2:
            continue
        main = next((r for r in members if not r.linked), None) or min(members, key=lambda r: len(r.path))
        elsewhere = [f"refs/heads/{r.branch}" for r in members if r is not main and r.branch]
        for repo in members:
            if repo is not main:
                repo.other_unpushed, repo.no_upstream, repo.gone = 0, [], []
                repo.worktree_of = main.path
        if elsewhere and not main.empty:
            try:
                main.other_unpushed = count(main.path, "--branches", "--not", "--remotes", "HEAD", *elsewhere,
                                            runner=runner)
            except GitError:
                pass
            checked_out = {ref[len("refs/heads/"):] for ref in elsewhere}
            main.no_upstream = [b for b in main.no_upstream if b not in checked_out]


def ago(stamp: float, now: float | None = None) -> str:
    if not stamp:
        return "unknown"
    seconds = max(0.0, (now or time.time()) - stamp)
    for size, unit in ((86400 * 365, "year"), (86400 * 30, "month"), (86400 * 7, "week"), (86400, "day"),
                       (3600, "hour"), (60, "minute")):
        if seconds >= size:
            return f"{plural(int(seconds // size), unit)} ago"
    return "just now"
