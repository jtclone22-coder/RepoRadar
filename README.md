# RepoRadar

**Is all my work pushed?** RepoRadar answers that for every git repository on your PC at once, before you wipe a drive, replace a computer, or just end the day.

It finds every repository under your folders and gives each one a single verdict, worst first:

| Verdict | Meaning |
|---|---|
| **IN PROGRESS** | Stuck in a merge, rebase, cherry-pick or bisect, or has conflicted files |
| **AT RISK** | Work that exists only on this PC: unpushed commits on any branch, no remote at all, stashes, or commits on a detached HEAD |
| **CHANGES** | Uncommitted edits or untracked files |
| **BEHIND** | The remote has commits you don't have yet |
| **OK** | Clean, and everything is pushed |

Select a repository to see why: which branches were never pushed, which upstreams were deleted, the changed files and the last commit. Buttons open the folder, a terminal or VS Code there, or copy the path.

- **Read-only.** Scanning never changes a repository. Git runs without optional locks, so the index isn't touched, and with `core.fsmonitor` off. Like any `git status`, a scan can still run programs a repository's own settings name (such as clean filters), so point RepoRadar at your own code folders, not at Downloads or unpacked archives. It warns if you add one of those.
- **Worktrees counted once.** Each `git worktree` reports its own branch, and the main checkout reports the rest, so the same commits never show up five times.
- **Safe bulk update.** "Update clean repos" fast-forwards only branches with no uncommitted changes and no commits of their own. Nothing is merged or rebased.
- **Never hangs on a password.** Fetching runs with prompts turned off. Repositories that need a login are skipped and named.
- **Respects Git's safety check.** If git doesn't trust a folder's owner (`safe.directory`), RepoRadar shows the exact command to run rather than going around it.
- **Nothing to install** besides Git for Windows. Plain Python 3.9+, or the standalone .exe.

## Download

Get **RepoRadar-0.1.0-windows.zip** from [Releases](../../releases/latest), unzip it anywhere and double-click **RepoRadar.exe** in the RepoRadar folder. Nothing to install, and no Python needed.

Windows may say "Windows protected your PC" because the app is new: click **More info**, then **Run anyway**.

Prefer Python? With Python 3.9 or newer, download this repository and run `py -m reporadar` in its folder.

## Command line

```
py -m reporadar folders add D:\code      # where your repositories live (saved)
py -m reporadar scan                     # what needs attention; exit code 1 if work is at risk
py -m reporadar scan --all               # every repository, including the fine ones
py -m reporadar scan --fetch             # fetch from every remote first
py -m reporadar scan D:\work --json      # one-off folder, machine-readable
py -m reporadar update                   # fetch, then list clean repos that can fast-forward
py -m reporadar update --yes             # ...and do it
```

The exit code makes it a pre-flight check in scripts. This one stops a wipe while anything is unpushed:

```
py -m reporadar scan || echo Not everything is pushed. Stop here.
```

## Tests

```
py -m unittest discover -s tests
```

The tests build real repositories in a temp folder, with a bare repository standing in for the remote. They cover worktrees, stashes, detached HEADs, a merge conflict, junctions and leftover `.git` folders, and they run with an empty git config so your own settings can't change the results.

MIT License.
