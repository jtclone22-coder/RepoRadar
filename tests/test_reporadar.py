"""RepoRadar tests: real git repositories in a temp folder, with a bare repository standing in for the remote.

Git runs with an empty global config (GIT_CONFIG_GLOBAL) so the developer's own settings can't change results.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reporadar import cli, radar  # noqa: E402
from reporadar.radar import Repo, parse_status, verdict  # noqa: E402


def sh(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def commit(repo: Path, name: str, text: str = "x\n", message: str = "") -> None:
    (repo / name).write_text(text, encoding="utf-8")
    sh(repo, "add", name)
    sh(repo, "commit", "-q", "-m", message or f"add {name}")


class ParseTests(unittest.TestCase):
    def test_porcelain_v2(self) -> None:
        text = "\0".join([
            "# branch.oid 1234", "# branch.head main", "# branch.upstream origin/main", "# branch.ab +2 -3",
            "# stash 1",
            "1 .M N... 100644 100644 100644 aaa bbb src/app file.py",
            "1 A. N... 000000 100644 100644 000 ccc new.txt",
            "2 R. N... 100644 100644 100644 ddd ddd R100 renamed name.py", "old name.py",
            "u UU N... 100644 100644 100644 100644 e f g both.py",
            "? notes/todo.md", ""])
        repo = Repo("x")
        parse_status(text, repo)
        self.assertEqual((repo.branch, repo.upstream, repo.ahead, repo.behind, repo.stashes), ("main", "origin/main", 2, 3, 1))
        self.assertEqual((repo.changed, repo.staged, repo.untracked, repo.conflicts), (3, 2, 1, 1))
        self.assertEqual(repo.files, [" M src/app file.py", "A  new.txt", "R  renamed name.py", "UU both.py",
                                      "?? notes/todo.md"])
        self.assertFalse(repo.upstream_gone)

    def test_gone_upstream_and_detached(self) -> None:
        repo = Repo("x")
        parse_status("# branch.oid 1\0# branch.head main\0# branch.upstream origin/old\0", repo)
        self.assertTrue(repo.upstream_gone)
        repo = Repo("y")
        parse_status("# branch.oid 1\0# branch.head (detached)\0", repo)
        self.assertEqual((repo.detached, repo.branch), (True, ""))

    def test_verdict_wording(self) -> None:
        self.assertEqual(verdict(Repo("a", remotes=["origin"])), ("ok", "clean, everything pushed"))
        self.assertEqual(verdict(Repo("a", branch="main", remotes=["origin"], unpushed=1, stashes=2, changed=1,
                                      untracked=4)),
                         ("risk", "1 commit on main not on any remote; 2 stashes (stashes are never pushed); "
                                  "1 changed file, 4 untracked"))
        self.assertEqual(verdict(Repo("a"))[0], "risk")  # commits but no remote at all
        self.assertEqual(verdict(Repo("a", empty=True)), ("ok", "no commits yet"))
        self.assertEqual(verdict(Repo("a", remotes=["o"], operation="rebase", conflicts=2))[1],
                         "rebase in progress; 2 conflicted files")
        self.assertEqual(radar.ago(1000, now=1000 + 3 * 86400 + 5), "3 days ago")


class GitTests(unittest.TestCase):
    """One remote, a handful of clones in different states, scanned the way a user would."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        base = Path(cls.temp.name)
        (base / "gitconfig").write_text("", encoding="utf-8")
        cls.env = mock.patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": str(base / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.com", "REPORADAR_HOME": str(base / "home")})
        cls.env.start()
        cls.root = base / "code"
        cls.root.mkdir()
        remote = base / "remote.git"
        sh(base, "init", "-q", "--bare", "-b", "main", str(remote))

        def clone(name: str) -> Path:
            sh(cls.root, "clone", "-q", str(remote), name)
            return cls.root / name

        clean = clone("clean")
        commit(clean, "readme.md", "hello\n")
        commit(clean, "shared.txt", "line 1\n")
        sh(clean, "push", "-q", "-u", "origin", "main")
        ahead = clone("ahead")
        commit(ahead, "local.txt")
        (ahead / "readme.md").write_text("edited\n", encoding="utf-8")
        (ahead / "staged.txt").write_text("s\n", encoding="utf-8")
        sh(ahead, "add", "staged.txt")
        (ahead / "scratch.txt").write_text("u\n", encoding="utf-8")
        behind = clone("behind")
        stash = clone("stash")
        (stash / "readme.md").write_text("half done\n", encoding="utf-8")
        sh(stash, "stash", "-q")
        feature = clone("feature")
        sh(feature, "switch", "-q", "-c", "feature")
        commit(feature, "feature.txt")
        sh(feature, "switch", "-q", "main")
        detached = clone("detached")
        sh(detached, "switch", "-q", "--detach")
        commit(detached, "lost.txt")
        merging = clone("merging")
        sh(merging, "switch", "-q", "-c", "other")
        commit(merging, "shared.txt", "theirs\n")
        sh(merging, "switch", "-q", "main")
        commit(merging, "shared.txt", "ours\n")
        subprocess.run(["git", "merge", "-q", "other"], cwd=merging, capture_output=True)  # conflicts on purpose
        local = cls.root / "nested" / "local-only"
        local.mkdir(parents=True)
        sh(local, "init", "-q", "-b", "main")
        commit(local, "a.txt")
        sh(cls.root / "nested", "init", "-q", "-b", "main", "empty")
        sh(clean, "worktree", "add", "-q", str(cls.root / "clean-hotfix"), "-b", "hotfix")
        commit(cls.root / "clean-hotfix", "fix.txt")  # reported by the worktree, not again by "clean"
        (cls.root / "leftover" / ".git").mkdir(parents=True)  # an empty .git folder isn't a repository
        (cls.root / "orphan").mkdir()
        (cls.root / "orphan" / ".git").write_text("gitdir: ../.git/modules/deleted\n", encoding="utf-8")  # nor this
        modules = cls.root / "web" / "node_modules" / "pkg"
        modules.mkdir(parents=True)
        sh(modules, "init", "-q")  # inside node_modules: skipped
        outside = base / "elsewhere" / "repo"
        outside.mkdir(parents=True)
        sh(outside, "init", "-q")
        import _winapi
        _winapi.CreateJunction(str(base / "elsewhere"), str(cls.root / "link-to-elsewhere"))  # never followed
        commit(clean, "news.txt", message="news for the others")  # behind/... don't have this yet
        sh(clean, "push", "-q")
        cls.index_mtime = (ahead / ".git" / "index").stat().st_mtime_ns
        cls.repos = {Path(r.path).name: r for r in radar.scan([str(cls.root)])}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.env.stop()
        cls.temp.cleanup()

    def test_finds_repos_but_not_through_links_or_node_modules(self) -> None:
        self.assertEqual(sorted(self.repos), ["ahead", "behind", "clean", "clean-hotfix", "detached", "empty",
                                              "feature", "local-only", "merging", "stash"])

    def test_states(self) -> None:
        r = self.repos
        self.assertEqual(r["clean"].level, "ok")
        self.assertEqual(r["ahead"].summary,
                         "1 commit on main not on any remote; 2 changed files (1 staged), 1 untracked")
        self.assertEqual((r["ahead"].ahead, r["ahead"].branch, r["ahead"].upstream), (1, "main", "origin/main"))
        self.assertEqual(r["stash"].summary, "1 stash (stashes are never pushed)")
        self.assertEqual((r["feature"].level, r["feature"].unpushed, r["feature"].other_unpushed,
                          r["feature"].no_upstream), ("risk", 0, 1, ["feature"]))
        self.assertEqual(r["feature"].summary, "1 commit on other branches not on any remote")
        self.assertEqual(r["detached"].summary, "1 commit only on a detached HEAD (on no branch)")
        self.assertEqual((r["merging"].level, r["merging"].operation, r["merging"].conflicts), ("busy", "merge", 1))
        self.assertEqual(r["local-only"].summary, "no remote: every commit is only on this PC")
        self.assertEqual((r["empty"].level, r["empty"].summary), ("ok", "no commits yet"))
        hotfix = r["clean-hotfix"]
        self.assertEqual((hotfix.summary, hotfix.linked, hotfix.worktree_of),
                         ("1 commit on hotfix not on any remote", True, str(self.root / "clean")))
        self.assertEqual((r["clean"].other_unpushed, r["clean"].no_upstream), (0, []))
        self.assertEqual(r["behind"].behind, 0)  # nothing fetched yet, so it can't know
        self.assertEqual([radar.ORDER.index(x.level) for x in self.repos.values()],
                         sorted(radar.ORDER.index(x.level) for x in self.repos.values()))

    def test_scanning_writes_nothing(self) -> None:
        index = self.root / "ahead" / ".git" / "index"
        self.assertEqual(index.stat().st_mtime_ns, self.index_mtime)
        self.assertFalse((self.root / "ahead" / ".git" / "index.lock").exists())

    def test_fetch_then_fast_forward_only_when_safe(self) -> None:
        behind = str(self.root / "behind")
        radar.fetch(behind)
        repo = radar.inspect(behind)
        self.assertEqual((repo.level, repo.behind, repo.summary), ("behind", 1, "1 commit behind origin/main"))
        self.assertGreater(repo.last_fetch, 0)
        self.assertEqual(radar.fast_forward(repo).level, "ok")
        self.assertTrue((self.root / "behind" / "news.txt").exists())
        ahead = str(self.root / "ahead")
        radar.fetch(ahead)
        repo = radar.inspect(ahead)
        self.assertEqual((repo.ahead, repo.behind), (1, 1))
        self.assertIn("has its own commits", radar.fast_forward_blocker(repo))
        with self.assertRaises(radar.GitError):
            radar.fast_forward(repo)

    def test_update_refuses_when_the_repo_changed_after_the_scan(self) -> None:
        path = self.root / "late-change"
        sh(self.root, "clone", "-q", str(Path(self.temp.name) / "remote.git"), "late-change")
        sh(self.root / "clean", "pull", "-q")
        commit(self.root / "clean", "later.txt")
        sh(self.root / "clean", "push", "-q")
        radar.fetch(str(path))
        scanned = radar.inspect(str(path))
        self.assertEqual(radar.fast_forward_blocker(scanned), "")
        commit(path, "mine.txt")  # work done between the scan and clicking Update
        with self.assertRaises(radar.GitError) as caught:
            radar.fast_forward(scanned)
        self.assertIn("changed since the scan", str(caught.exception))
        self.assertTrue((path / "mine.txt").exists())

    def test_ssh_is_left_to_your_own_setup(self) -> None:
        clean = str(self.root / "clean")
        self.assertFalse(radar.own_ssh_setup(clean))
        self.assertEqual(radar.git_env(batch_ssh=True)["GIT_SSH_COMMAND"], "ssh -o BatchMode=yes")
        self.assertNotIn("GIT_SSH_COMMAND", radar.git_env())  # scans never touch SSH at all
        sh(clean, "config", "core.sshCommand", "plink -batch")
        self.addCleanup(sh, clean, "config", "--unset", "core.sshCommand")
        self.assertTrue(radar.own_ssh_setup(clean))
        with mock.patch.dict(os.environ, {"GIT_SSH": r"C:\Program Files\PuTTY\plink.exe"}):
            self.assertTrue(radar.own_ssh_setup(str(self.root / "ahead")))

    def test_cli_scan_json_exit_code_and_update_dry_run(self) -> None:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = cli.main(["scan", str(self.root), "--json"])
        self.assertEqual(code, 1)  # work at risk
        levels = {Path(r["path"]).name: r["level"] for r in json.loads(out.getvalue())}
        self.assertEqual(levels["merging"], "busy")
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            self.assertEqual(cli.main(["scan", str(self.root / "nested" / "empty")]), 0)
        self.assertIn("1 repository: 1 OK. Nothing is at risk", out.getvalue())
        out = io.StringIO()
        with mock.patch("sys.stdout", out):  # alone, "clean" owns the hotfix worktree's unpushed commit
            self.assertEqual(cli.main(["scan", str(self.root / "clean")]), 1)
        self.assertIn("1 commit on other branches not on any remote", out.getvalue())
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            self.assertEqual(cli.main(["update", str(self.root / "stash"), "--no-fetch"]), 0)
        self.assertIn("Nothing to update", out.getvalue())

    def test_folder_settings(self) -> None:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            cli.main(["folders", "add", str(self.root)])
            cli.main(["folders"])
        self.assertIn(str(self.root), out.getvalue())
        with mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(cli.main(["folders", "remove", str(self.root / "nope")]), 1)


class GitMissingTests(unittest.TestCase):
    def test_clear_message_without_git(self) -> None:
        def runner(*args, **kwargs):
            raise FileNotFoundError

        repo = radar.inspect(".", runner=runner)
        self.assertEqual(repo.level, "error")
        self.assertIn("git-scm.com", repo.summary)


if __name__ == "__main__":
    unittest.main()
