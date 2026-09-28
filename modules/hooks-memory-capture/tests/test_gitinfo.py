"""Unit tests for subprocess-free git HEAD commit resolution (gitinfo.py).

Builds tiny synthetic .git directories on disk rather than using the real
repo, so behavior is pinned exactly regardless of what branch/commit this
checkout happens to be on.
"""

from __future__ import annotations

from pathlib import Path

from amplifier_module_hooks_memory_capture.gitinfo import resolve_commit


def _make_repo(
    root: Path, *, head_sha: str = "abc1234abc1234abc1234abc1234abc1234abcd"
) -> Path:
    """A normal (non-worktree) repo: root/.git/HEAD -> refs/heads/main."""
    git_dir = root / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "refs" / "heads" / "main").write_text(head_sha + "\n", encoding="utf-8")
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return git_dir


class TestNormalRepo:
    def test_resolves_sha_from_loose_ref(self, tmp_path: Path) -> None:
        sha = "1111111111111111111111111111111111111a"
        repo = tmp_path / "repo"
        repo.mkdir()
        _make_repo(repo, head_sha=sha)

        assert resolve_commit(repo) == sha

    def test_resolves_from_a_subdirectory(self, tmp_path: Path) -> None:
        sha = "2222222222222222222222222222222222222b"
        repo = tmp_path / "repo"
        sub = repo / "a" / "b" / "c"
        sub.mkdir(parents=True)
        _make_repo(repo, head_sha=sha)

        assert resolve_commit(sub) == sha


class TestWorktree:
    def test_resolves_via_gitdir_file(self, tmp_path: Path) -> None:
        sha = "3333333333333333333333333333333333333c"
        main_repo = tmp_path / "main"
        real_git_dir = tmp_path / "main" / ".git-real"
        (real_git_dir / "refs" / "heads").mkdir(parents=True)
        (real_git_dir / "refs" / "heads" / "main").write_text(
            sha + "\n", encoding="utf-8"
        )
        (real_git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        (worktree / ".git").write_text(f"gitdir: {real_git_dir}\n", encoding="utf-8")

        assert resolve_commit(worktree) == sha
        assert main_repo  # keep referenced; not otherwise used


class TestPackedRefs:
    def test_resolves_from_packed_refs_when_loose_ref_absent(
        self, tmp_path: Path
    ) -> None:
        sha = "4444444444444444444444444444444444444d"
        repo = tmp_path / "repo"
        git_dir = repo / ".git"
        git_dir.mkdir(parents=True)
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git_dir / "packed-refs").write_text(
            f"# pack-refs with: peeled fully-peeled sorted\n{sha} refs/heads/main\n",
            encoding="utf-8",
        )

        assert resolve_commit(repo) == sha


class TestDetachedHead:
    def test_resolves_sha_directly_from_head(self, tmp_path: Path) -> None:
        sha = "5555555555555555555555555555555555555e"
        repo = tmp_path / "repo"
        git_dir = repo / ".git"
        git_dir.mkdir(parents=True)
        (git_dir / "HEAD").write_text(sha + "\n", encoding="utf-8")

        assert resolve_commit(repo) == sha


class TestNonRepo:
    def test_returns_none_outside_any_git_repo(self, tmp_path: Path) -> None:
        lonely = tmp_path / "not-a-repo"
        lonely.mkdir()
        assert resolve_commit(lonely) is None


class TestCaching:
    def test_cache_hit_returns_same_value_without_rereading(
        self, tmp_path: Path
    ) -> None:
        sha = "6666666666666666666666666666666666666f"
        repo = tmp_path / "repo"
        repo.mkdir()
        _make_repo(repo, head_sha=sha)

        first = resolve_commit(repo)
        # Mutate the loose ref on disk without touching HEAD's mtime --
        # the cache key is (git_dir, HEAD mtime), so this must still hit
        # the cache and return the ORIGINAL sha (documented behavior, not
        # a correctness bug: same-branch new commits are the accepted
        # staleness window for this per-process cache).
        (repo / ".git" / "refs" / "heads" / "main").write_text(
            "7777777777777777777777777777777777777f\n", encoding="utf-8"
        )
        second = resolve_commit(repo)

        assert first == sha
        assert second == first
