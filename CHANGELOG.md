# Changelog

## 0.1.0 (2026-10-22)

- Finds every git repository under your folders, including worktrees and submodules. It skips symlinks, junctions, `node_modules`, virtual environments, Unreal caches and empty `.git` leftovers.
- Each repository gets one verdict, worst first: in the middle of a merge or rebase, at risk (unpushed commits, no remote, stashes, commits on a detached HEAD), uncommitted changes, behind, or OK.
- Worktrees of one repository are counted once. Each worktree reports its own branch, and the main checkout reports the rest.
- Read-only scanning: git runs without optional locks and with `core.fsmonitor` off. (Like any `git status`, it can run filters a repository itself names; adding Downloads or a temp folder shows a warning.) Git's ownership check (`safe.directory`) is respected, and RepoRadar shows the exact command to run instead.
- Optional fetch from every remote. It never stops to ask for a password; repositories that need one are skipped and named.
- Safe bulk update: fast-forwards only clean branches with no commits of their own.
- A command line (`scan` exits with code 1 when work is at risk) and a window with details, Open folder, Terminal, VS Code and Copy path.
- Update re-reads each repository just before fast-forwarding and leaves it alone if it changed since the scan. Fetching only switches SSH to batch mode when you haven't set up your own (GIT_SSH, GIT_SSH_COMMAND or core.sshCommand, such as plink or Windows OpenSSH).
