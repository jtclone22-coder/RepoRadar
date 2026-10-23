"""Window: every repository, worst first; details, open/terminal/editor, fetch and safe fast-forward."""
from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
import time

from . import __version__, settings
from .cli import summary_line
from .radar import LEVELS, GitError, ago, fast_forward, fast_forward_blocker, fetch, group_worktrees, inspect, scan

COLORS = {"busy": "#b45309", "risk": "#b91c1c", "changes": "#a16207", "behind": "#1d4ed8", "error": "#6b7280",
          "ok": "#15803d"}
VIEWS = {"Needs attention": lambda r: r.level != "ok", "Everything": lambda r: True,
         "At risk only": lambda r: r.level in ("risk", "busy"), "Behind": lambda r: r.behind > 0}


def open_terminal(path: str) -> None:
    terminal = shutil.which("wt")
    if terminal:
        subprocess.Popen([terminal, "-d", path])
    else:
        subprocess.Popen(["cmd.exe", "/k"], cwd=path, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))


def details(repo) -> str:
    lines = [repo.path, ""]
    if repo.error:
        return "\n".join(lines + [f"Couldn't read it: {repo.error}"])
    where = f"on {repo.branch}" if repo.branch else "detached HEAD"
    if repo.upstream:
        where += f", tracking {repo.upstream}" + (" (deleted on the remote)" if repo.upstream_gone else
                                                  f" ({repo.ahead} ahead, {repo.behind} behind)")
    lines.append(where)
    if repo.worktree_of:
        lines.append(f"Worktree of {repo.worktree_of} (its other branches are listed there)")
    lines.append("Remotes: " + (", ".join(repo.remotes) or "none"))
    lines.append(f"Last fetch: {ago(repo.last_fetch)}" + ("" if repo.last_fetch else " (ahead/behind may be old)"))
    if repo.last_commit:
        lines.append(f"Last commit: {ago(repo.last_commit)}: {repo.last_subject}")
    if repo.no_upstream:
        lines.append("Branches never pushed: " + ", ".join(repo.no_upstream))
    if repo.gone:
        lines.append("Branches whose upstream was deleted: " + ", ".join(repo.gone))
    if repo.stashes:
        lines.append(f"Stashes: {repo.stashes} (only on this PC)")
    if repo.files:
        lines += ["", "Changed files:"] + [f"  {f}" for f in repo.files]
        hidden = repo.changed + repo.untracked + repo.conflicts - len(repo.files)
        if hidden > 0:
            lines.append(f"  ... and {hidden} more")
    return "\n".join(lines)


def main() -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title(f"RepoRadar {__version__}")
    root.geometry("1120x680")
    messages: queue.Queue = queue.Queue()
    state = {"repos": [], "rows": {}, "busy": False}

    def background(task, kind) -> None:
        state["busy"] = True

        def work():
            try:
                messages.put((kind, task()))
            except Exception as exc:  # shown in the status bar
                messages.put(("error", str(exc)))
        threading.Thread(target=work, daemon=True).start()

    bar = ttk.Frame(root, padding=(10, 10, 10, 0))
    bar.pack(fill="x")
    view = tk.StringVar(value="Needs attention")
    needle = tk.StringVar()
    columns = ("level", "name", "branch", "summary", "last")
    tree = ttk.Treeview(root, columns=columns, show="headings", selectmode="browse")
    for column, title, width in (("level", "Status", 100), ("name", "Repository", 200), ("branch", "Branch", 140),
                                 ("summary", "What needs doing", 520), ("last", "Last commit", 110)):
        tree.heading(column, text=title)
        tree.column(column, width=width, anchor="w", stretch=column == "summary")
    for level, color in COLORS.items():
        tree.tag_configure(level, foreground=color)
    lower = ttk.Frame(root, padding=(10, 0, 10, 0))
    info = tk.Text(lower, height=11, wrap="word", relief="flat", background=root.cget("background"))
    actions = ttk.Frame(lower)
    status = ttk.Label(root, text="", anchor="w", padding=(10, 4))

    def selected():
        chosen = tree.selection()
        return state["rows"].get(chosen[0]) if chosen else None

    def fill() -> None:
        tree.delete(*tree.get_children())
        state["rows"] = {}
        keep = VIEWS[view.get()]
        text = needle.get().strip().lower()
        for repo in state["repos"]:
            if not keep(repo) or (text and text not in repo.path.lower() and text not in repo.summary.lower()):
                continue
            branch = repo.branch or ("" if repo.error else "(detached)")
            row = tree.insert("", "end", values=(LEVELS[repo.level], repo.name, branch,
                                                 repo.summary, ago(repo.last_commit) if repo.last_commit else ""),
                              tags=(repo.level,))
            state["rows"][row] = repo
        show_details()

    def show_details(*_) -> None:
        repo = selected()
        info.configure(state="normal")
        info.delete("1.0", "end")
        info.insert("1.0", details(repo) if repo else "Select a repository to see the details.")
        info.configure(state="disabled")
        for button in repo_buttons:
            button.configure(state="normal" if repo and not state["busy"] else "disabled")
        update_one.configure(state="normal" if repo and not state["busy"] and not fast_forward_blocker(repo)
                             else "disabled")

    def run_scan(do_fetch: bool = False) -> None:
        roots = settings.load()["roots"]
        if not roots:
            status.configure(text="Add a folder that holds your repositories (Folders...).")
            return
        if do_fetch and not messagebox.askyesno(
                "Fetch everything?", "Fetch from every remote first? This uses the network, may take a while, "
                                     "and skips repositories that would need a password.", parent=root):
            return
        status.configure(text="Fetching and scanning..." if do_fetch else "Scanning...")
        progress = lambda done, total, repo: messages.put(("progress", (done, total, repo.name)))  # noqa: E731
        background(lambda: scan(roots, depth=settings.load()["depth"], do_fetch=do_fetch, progress=progress), "scanned")

    def replace(repo) -> None:
        state["repos"] = [repo if r.path == repo.path else r for r in state["repos"]]
        group_worktrees([r for r in state["repos"] if r.common and os.path.normcase(r.common) == os.path.normcase(repo.common)])
        state["repos"].sort(key=lambda r: (list(LEVELS).index(r.level), r.path.lower()))

    def fetch_selected() -> None:
        repo = selected()
        status.configure(text=f"Fetching {repo.name}...")

        def task():
            fetch(repo.path)
            return inspect(repo.path)
        background(task, "one")

    def update_selected() -> None:
        repo = selected()
        if messagebox.askyesno("Fast-forward?", f"Move {repo.name} ({repo.branch}) forward by {repo.behind} commit(s) "
                                                f"from {repo.upstream}? Nothing local is changed or merged.",
                               parent=root):
            status.configure(text=f"Updating {repo.name}...")
            background(lambda: fast_forward(repo), "one")

    def update_all() -> None:
        ready = [r for r in state["repos"] if not fast_forward_blocker(r)]
        if not ready:
            messagebox.showinfo("Nothing to update", "No clean repository is behind its upstream. "
                                                     "Use Fetch all & scan to check the remotes first.", parent=root)
            return
        names = "\n".join(f"  {r.name}: {r.behind} new" for r in ready[:20]) + ("\n  ..." if len(ready) > 20 else "")
        if not messagebox.askyesno("Update clean repositories?", f"Fast-forward these {len(ready)}?\n\n{names}\n\n"
                                   "Only clean branches with no commits of their own are moved.", parent=root):
            return

        def task():
            results, failures = [], []
            for repo in ready:
                try:
                    results.append(fast_forward(repo))
                except GitError as exc:
                    failures.append(f"{repo.name}: {exc}")
            return results, failures
        status.configure(text=f"Updating {len(ready)} repositories...")
        background(task, "many")

    def manage_folders() -> None:
        window = tk.Toplevel(root)
        window.title("Folders to scan")
        window.geometry("560x320")
        window.transient(root)
        listing = tk.Listbox(window)
        listing.pack(fill="both", expand=True, padx=10, pady=10)
        for folder in settings.load()["roots"]:
            listing.insert("end", folder)
        row = ttk.Frame(window, padding=(10, 0, 10, 10))
        row.pack(fill="x")

        def add() -> None:
            folder = filedialog.askdirectory(parent=window, title="A folder that holds repositories")
            if folder:
                warning = settings.risky(folder)
                if warning and not messagebox.askyesno("Add this folder?", warning + "\n\nAdd it anyway?",
                                                       parent=window):
                    return
                listing.delete(0, "end")
                for item in settings.add_root(folder):
                    listing.insert("end", item)

        def remove() -> None:
            chosen = listing.curselection()
            if chosen:
                settings.remove_root(listing.get(chosen[0]))
                listing.delete(chosen[0])

        ttk.Button(row, text="Add folder...", command=add).pack(side="left")
        ttk.Button(row, text="Remove", command=remove).pack(side="left", padx=6)
        ttk.Button(row, text="Done, scan", command=lambda: (window.destroy(), run_scan())).pack(side="right")

    ttk.Button(bar, text="Folders...", command=manage_folders).pack(side="left")
    ttk.Button(bar, text="Scan", command=run_scan).pack(side="left", padx=(6, 0))
    ttk.Button(bar, text="Fetch all & scan", command=lambda: run_scan(True)).pack(side="left", padx=6)
    ttk.Button(bar, text="Update clean repos...", command=update_all).pack(side="left")
    ttk.Entry(bar, textvariable=needle, width=22).pack(side="right")
    ttk.Label(bar, text="Find").pack(side="right", padx=(10, 6))
    chooser = ttk.Combobox(bar, textvariable=view, values=list(VIEWS), state="readonly", width=16)
    chooser.pack(side="right")
    chooser.bind("<<ComboboxSelected>>", lambda e: fill())
    needle.trace_add("write", lambda *a: fill())
    tree.pack(fill="both", expand=True, padx=10, pady=8)
    tree.bind("<<TreeviewSelect>>", show_details)
    lower.pack(fill="x")
    info.pack(side="left", fill="both", expand=True)
    actions.pack(side="right", fill="y", padx=(10, 0))
    code = shutil.which("code")
    repo_buttons = [
        ttk.Button(actions, text="Open folder", command=lambda: os.startfile(selected().path)),
        ttk.Button(actions, text="Terminal here", command=lambda: open_terminal(selected().path)),
        ttk.Button(actions, text="Copy path", command=lambda: (root.clipboard_clear(),
                                                               root.clipboard_append(selected().path))),
        ttk.Button(actions, text="Fetch this one", command=fetch_selected),
    ]
    if code:
        repo_buttons.insert(2, ttk.Button(actions, text="Open in VS Code", command=lambda: subprocess.Popen(
            [code, selected().path], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))))
    update_one = ttk.Button(actions, text="Fast-forward", command=update_selected)
    for button in repo_buttons + [update_one]:
        button.pack(fill="x", pady=2)
    status.pack(fill="x")

    def poll() -> None:
        try:
            while True:
                kind, payload = messages.get_nowait()
                if kind == "progress":
                    done, total, name = payload
                    status.configure(text=f"Scanning {done}/{total}: {name}")
                    continue
                state["busy"] = False
                if kind == "scanned":
                    state["repos"] = payload
                    status.configure(text=summary_line(payload) + f"  (scanned {time.strftime('%H:%M')})")
                elif kind == "one":
                    replace(payload)
                    status.configure(text=f"{payload.name}: {payload.summary}")
                elif kind == "many":
                    results, failures = payload
                    for repo in results:
                        replace(repo)
                    status.configure(text=f"Updated {len(results)}. " + summary_line(state["repos"]))
                    if failures:
                        messagebox.showwarning("Some weren't updated", "\n".join(failures[:20]), parent=root)
                else:
                    status.configure(text=f"Error: {payload}")
                fill()
        except queue.Empty:
            pass
        root.after(150, poll)

    show_details()
    root.after(150, poll)
    root.after(300, run_scan)
    root.mainloop()
    return 0
