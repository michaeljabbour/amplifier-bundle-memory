"""Tests for client.ensure_daemon() -- \u00a75.2 discovery/spawn-race/recovery, and
the killer gates the B1 phase owns: KG-N2 (concurrent cross-process writers),
KG-N6 (daemon lifecycle: kill -9 respawn, stale-lock race), and
version-mismatch respawn (\u00a75.2 step 1c).

These tests spawn REAL subprocesses (the actual ``python -m
amplifier_module_tool_memory.daemon --daemon
--ephemeral`` daemon), so they are slower than the rest of the suite and are
skipped when amplifier-data is not installed. ``--ephemeral`` is used
throughout (D10: durable/production stores require the Rust kernel; these
tests exercise daemon LIFECYCLE, not durability, so ephemeral in-memory
storage is the correct and honest choice here per the design doc's own
carve-out for tests/DTU).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("amplifier_data")

from amplifier_module_tool_memory import client as client_mod  # noqa: E402
from amplifier_module_tool_memory import daemon as daemon_mod  # noqa: E402
from amplifier_module_tool_memory.client import MemoryClient, ensure_daemon  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_unavailable_guard() -> Iterator[None]:
    """ensure_daemon() emits memory:daemon_unavailable at most ONCE per
    process (\u00a75.2 step 3) -- reset the module-level guard between tests so
    each test's assertions about that behavior are independent."""
    client_mod._unavailable_emitted = False  # noqa: SLF001
    yield
    client_mod._unavailable_emitted = False  # noqa: SLF001


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "memory-home"
    home.mkdir()
    return home


def _daemon_pid_from_json(home: Path) -> int:
    info = json.loads((home / "daemon.json").read_text(encoding="utf-8"))
    return int(info["pid"])


def _wait_for_daemon_json_gone(home: Path, *, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not (home / "daemon.json").exists():
            return True
        time.sleep(0.1)
    return False


def _wait_for_daemon_json(home: Path, *, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (home / "daemon.json").exists():
            return True
        time.sleep(0.1)
    return False


def _spawn_ephemeral(home: Path) -> subprocess.Popen:
    return subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-m",
            "amplifier_module_tool_memory.daemon",
            "--daemon",
            "--ephemeral",
            "--embedder-model",
            "none",
            "--home",
            str(home),
        ],
        start_new_session=True,
    )


class TestEnsureDaemonSpawnFromScratch:
    def test_spawns_and_returns_working_client(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _home(tmp_path)
        monkeypatch.setattr(
            client_mod, "_spawn_daemon_process", lambda h: _spawn_daemon_ephemeral(h)
        )

        client = ensure_daemon(home)
        try:
            assert client is not None
            ref = client.remember(
                wing="w", room="r", content="hello from ensure_daemon"
            )
            assert ref
            out = client.search("hello", k=5, wing="w")
            assert out["results"]
        finally:
            if client is not None:
                client.shutdown()
            _wait_for_daemon_json_gone(home)

    def test_second_call_discovers_existing_daemon(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _home(tmp_path)
        monkeypatch.setattr(
            client_mod, "_spawn_daemon_process", lambda h: _spawn_daemon_ephemeral(h)
        )

        client1 = ensure_daemon(home)
        assert client1 is not None
        pid1 = _daemon_pid_from_json(home)
        try:
            client2 = ensure_daemon(home)
            assert client2 is not None
            pid2 = _daemon_pid_from_json(home)
            assert pid1 == pid2  # discovery, not a second spawn
        finally:
            client1.shutdown()
            _wait_for_daemon_json_gone(home)


class TestDaemonLifetimeOwnership:
    def test_direct_second_daemon_cannot_replace_active_owner(
        self, tmp_path: Path
    ) -> None:
        home = _home(tmp_path)
        first = _spawn_ephemeral(home)
        assert _wait_for_daemon_json(home), "first daemon did not become discoverable"
        pid1 = _daemon_pid_from_json(home)

        second = _spawn_ephemeral(home)
        try:
            assert second.wait(timeout=10) == 0
            assert _daemon_pid_from_json(home) == pid1
            assert (
                client_mod._health(  # noqa: SLF001
                    json.loads((home / "daemon.json").read_text())["url"], timeout=1.0
                )
                is not None
            )
        finally:
            info = json.loads((home / "daemon.json").read_text(encoding="utf-8"))
            MemoryClient(info["url"], None).shutdown()
            _wait_for_daemon_json_gone(home)
            if first.poll() is None:
                first.terminate()

    def test_discovery_cleanup_never_removes_another_pid(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        path = home / "daemon.json"
        path.write_text(json.dumps({"pid": 222}), encoding="utf-8")

        daemon_mod._remove_daemon_json_if_owned(path, 111)  # noqa: SLF001
        assert json.loads(path.read_text(encoding="utf-8"))["pid"] == 222

        daemon_mod._remove_daemon_json_if_owned(path, 222)  # noqa: SLF001
        assert not path.exists()


def _spawn_daemon_ephemeral(home: Path) -> None:
    """Test double for client._spawn_daemon_process: same subprocess shape but
    forces --ephemeral --embedder-model none so tests never require the Rust
    kernel or a model download."""
    log_path = home / "daemon.log"
    with open(log_path, "a", encoding="utf-8") as log_fh:
        subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                "-m",
                "amplifier_module_tool_memory.daemon",
                "--daemon",
                "--ephemeral",
                "--embedder-model",
                "none",
                "--home",
                str(home),
            ],
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
        )


class TestKGN6DaemonLifecycle:
    """KG-N6: kill -9 -> next remember respawns (new pid, pre-crash drawers
    still readable is NOT exercised here since --ephemeral storage is
    intentionally non-durable -- the durability half of KG-N6 belongs to a
    durable-store variant; this test pins the RESPAWN mechanics, which are
    storage-backend-independent)."""

    def test_kill_9_then_respawn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _home(tmp_path)
        monkeypatch.setattr(
            client_mod, "_spawn_daemon_process", lambda h: _spawn_daemon_ephemeral(h)
        )

        client1 = ensure_daemon(home)
        assert client1 is not None
        pid1 = _daemon_pid_from_json(home)

        os.kill(pid1, signal.SIGKILL)
        # Give the OS a moment to reap/mark the process gone.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                os.kill(pid1, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)

        client2 = ensure_daemon(home)
        try:
            assert client2 is not None
            pid2 = _daemon_pid_from_json(home)
            assert pid2 != pid1
            # The respawned daemon is fully functional.
            ref = client2.remember(wing="w", room="r", content="post-respawn write")
            assert ref
        finally:
            if client2 is not None:
                client2.shutdown()
            _wait_for_daemon_json_gone(home)

    def test_stale_daemon_json_with_dead_pid_is_detected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _home(tmp_path)
        monkeypatch.setattr(
            client_mod, "_spawn_daemon_process", lambda h: _spawn_daemon_ephemeral(h)
        )

        # Fabricate a stale daemon.json pointing at a pid that is guaranteed
        # to be dead (spawn+wait+reap a throwaway subprocess first).
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait(timeout=5)
        stale_pid = dead.pid

        home.mkdir(exist_ok=True)
        (home / "daemon.json").write_text(
            json.dumps(
                {
                    "url": "http://127.0.0.1:1",  # unreachable -- health will fail
                    "port": 1,
                    "pid": stale_pid,
                    "version": "0.0.0-stale",
                    "token_file": str(home / "token"),
                    "started_at": "2020-01-01T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )

        client = ensure_daemon(home)
        try:
            assert client is not None  # stale entry detected, fresh daemon spawned
            pid = _daemon_pid_from_json(home)
            assert pid != stale_pid
        finally:
            if client is not None:
                client.shutdown()
            _wait_for_daemon_json_gone(home)

    def test_concurrent_spawn_race_yields_exactly_one_daemon(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two processes race ensure_daemon() after a kill -- exactly one new
        daemon results (\u00a75.2 step 2's O_EXCL spawn-lock)."""
        import multiprocessing

        home = _home(tmp_path)
        monkeypatch.setattr(
            client_mod, "_spawn_daemon_process", lambda h: _spawn_daemon_ephemeral(h)
        )

        client1 = ensure_daemon(home)
        assert client1 is not None
        pid1 = _daemon_pid_from_json(home)
        os.kill(pid1, signal.SIGKILL)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                os.kill(pid1, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)

        ctx = multiprocessing.get_context("spawn")
        result_queue: multiprocessing.Queue = ctx.Queue()
        procs = [
            ctx.Process(target=_race_worker, args=(str(home), result_queue))
            for _ in range(2)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=20)

        pids = {result_queue.get(timeout=5) for _ in range(2)}
        pids.discard(None)
        assert len(pids) == 1, f"expected exactly one daemon pid, got {pids}"

        info = json.loads((home / "daemon.json").read_text(encoding="utf-8"))
        client = MemoryClient(info["url"], None)
        try:
            client.shutdown()
        finally:
            _wait_for_daemon_json_gone(home)


def _race_worker(home_str: str, result_queue) -> None:  # noqa: ANN001
    """Runs in a separate OS process (multiprocessing 'spawn' context, KG-N2/KG-N6)."""
    import sys as _sys
    from pathlib import Path as _Path

    # A fresh interpreter needs the package importable -- inherited via
    # sys.path from the spawn context's parent env in the common case, but be
    # defensive in case test discovery changed sys.path oddly.
    from amplifier_module_tool_memory import client as _client_mod

    def _spawn_ephemeral(h: _Path) -> None:
        import subprocess as _subprocess

        log_path = h / "daemon.log"
        with open(log_path, "a", encoding="utf-8") as log_fh:
            _subprocess.Popen(  # noqa: S603
                [
                    _sys.executable,
                    "-m",
                    "amplifier_module_tool_memory.daemon",
                    "--daemon",
                    "--ephemeral",
                    "--embedder-model",
                    "none",
                    "--home",
                    str(h),
                ],
                stdout=log_fh,
                stderr=log_fh,
                start_new_session=True,
            )

    _client_mod._spawn_daemon_process = _spawn_ephemeral  # noqa: SLF001
    client = _client_mod.ensure_daemon(_Path(home_str))
    if client is None:
        result_queue.put(None)
        return
    try:
        info = json.loads((_Path(home_str) / "daemon.json").read_text(encoding="utf-8"))
        result_queue.put(int(info["pid"]))
    except Exception:
        result_queue.put(None)


class TestKGN2ConcurrentWriters:
    def test_two_processes_write_concurrently_no_corruption(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import multiprocessing

        home = _home(tmp_path)
        monkeypatch.setattr(
            client_mod, "_spawn_daemon_process", lambda h: _spawn_daemon_ephemeral(h)
        )

        ctx = multiprocessing.get_context("spawn")
        result_queue: multiprocessing.Queue = ctx.Queue()
        procs = [
            ctx.Process(target=_writer_worker, args=(str(home), wid, 50, result_queue))
            for wid in range(2)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=60)

        results = [result_queue.get(timeout=5) for _ in range(2)]
        assert all(r["ok"] for r in results), results

        info = json.loads((home / "daemon.json").read_text(encoding="utf-8"))
        client = MemoryClient(info["url"], None)
        try:
            all_refs: set[str] = set()
            for r in results:
                all_refs.update(r["refs"])
            assert (
                len(all_refs) == 100
            )  # exactly one daemon pid, both workers' writes landed

            # daemon.json is consistent -- same pid both workers observed.
            pids = {r["daemon_pid"] for r in results}
            assert len(pids) == 1

            # (byte-identical regeneration is verified inside each worker
            # process, right after each remember() call, via client.regenerate)
        finally:
            client.shutdown()
            _wait_for_daemon_json_gone(home)


def _writer_worker(home_str: str, wid: int, count: int, result_queue) -> None:  # noqa: ANN001
    """Runs in a separate OS process. Writes `count` unique drawers via
    ensure_daemon(), verifies each regenerates byte-identically, and reports
    the observed daemon pid (KG-N2)."""
    import json as _json
    from pathlib import Path as _Path

    from amplifier_module_tool_memory import client as _client_mod

    def _spawn_ephemeral(h: _Path) -> None:
        import subprocess as _subprocess
        import sys as _sys

        log_path = h / "daemon.log"
        with open(log_path, "a", encoding="utf-8") as log_fh:
            _subprocess.Popen(  # noqa: S603
                [
                    _sys.executable,
                    "-m",
                    "amplifier_module_tool_memory.daemon",
                    "--daemon",
                    "--ephemeral",
                    "--embedder-model",
                    "none",
                    "--home",
                    str(h),
                ],
                stdout=log_fh,
                stderr=log_fh,
                start_new_session=True,
            )

    _client_mod._spawn_daemon_process = _spawn_ephemeral  # noqa: SLF001
    home = _Path(home_str)
    client = _client_mod.ensure_daemon(home)
    if client is None:
        result_queue.put({"ok": False, "refs": [], "daemon_pid": None})
        return

    refs = []
    ok = True
    for i in range(count):
        content = f"w{wid}-d{i}: concurrent drawer content \u4e16\u754c"
        ref = client.remember(wing=f"wing_{wid}", room="concurrency", content=content)
        regen = client.regenerate(ref)
        if regen.payload != content.encode("utf-8"):
            ok = False
        refs.append(ref)

    info = _json.loads((home / "daemon.json").read_text(encoding="utf-8"))
    result_queue.put({"ok": ok, "refs": refs, "daemon_pid": int(info["pid"])})


class TestVersionMismatchRespawn:
    def test_version_mismatch_shuts_down_old_and_spawns_new(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """\u00a75.2 step 1c: a daemon.json advertising a version different from
        the client's own daemon_version() triggers a shutdown-then-respawn."""
        pytest.importorskip("amplifier_data")
        from amplifier_module_tool_memory.daemon import (
            make_daemon,
        )

        from amplifier_data import AmplifierStore

        home = _home(tmp_path)
        store = AmplifierStore(record_access=False)
        httpd = make_daemon(
            store,
            None,
            "127.0.0.1",
            0,
            token="tok",
            version="0.0.0-OLD",
            durable=False,
        )
        import threading

        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        port = httpd.server_address[1]

        home.mkdir(exist_ok=True)
        (home / "token").write_text("tok", encoding="utf-8")
        (home / "daemon.json").write_text(
            json.dumps(
                {
                    "url": f"http://127.0.0.1:{port}",
                    "port": port,
                    "pid": os.getpid(),  # a real, alive pid (this test process)
                    "version": "0.0.0-OLD",
                    "token_file": str(home / "token"),
                    "started_at": "2020-01-01T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )

        monkeypatch.setattr(
            client_mod, "_spawn_daemon_process", lambda h: _spawn_daemon_ephemeral(h)
        )

        client = ensure_daemon(home)
        try:
            assert client is not None
            new_info = json.loads((home / "daemon.json").read_text(encoding="utf-8"))
            assert new_info["port"] != port  # a genuinely new daemon, not the old one
            # the old daemon really did shut down
            deadline = time.monotonic() + 5.0
            stopped = False
            while time.monotonic() < deadline:
                hc = client_mod._health(f"http://127.0.0.1:{port}", timeout=0.5)  # noqa: SLF001
                if hc is None:
                    stopped = True
                    break
                time.sleep(0.2)
            assert stopped
        finally:
            if client is not None:
                client.shutdown()
            _wait_for_daemon_json_gone(home)


class TestRetireOnlyOlderDaemons:
    """A healthy daemon is retired only by a strictly NEWER client -- never
    by a dev checkout / test venv (``0.0.0-dev``) or an older client, which
    used to shut down the user's live daemon and fail to replace it."""

    @pytest.mark.parametrize(
        ("theirs", "mine", "retire"),
        [
            ("0.0.0-OLD", "2.0.2", True),
            ("2.0.1", "2.0.2", True),
            ("2.0.2", "2.0.1", False),  # daemon newer than this client
            ("2.0.1", "0.0.0-dev", False),  # client has no package metadata
            ("garbage", "2.0.2", False),
            ("2.0.1", "garbage", False),
            ("2.0.2", "2.0.2rc1", False),
        ],
    )
    def test_should_retire(self, theirs: str, mine: str, retire: bool) -> None:
        assert client_mod._should_retire(theirs, mine) is retire  # noqa: SLF001

    def test_dev_client_reuses_running_daemon(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("amplifier_data")
        import threading

        from amplifier_data import AmplifierStore

        from amplifier_module_tool_memory.daemon import make_daemon

        home = _home(tmp_path)
        httpd = make_daemon(
            AmplifierStore(record_access=False),
            None,
            "127.0.0.1",
            0,
            token="tok",
            version="2.0.1",
            durable=False,
        )
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        port = httpd.server_address[1]
        home.mkdir(exist_ok=True)
        (home / "token").write_text("tok", encoding="utf-8")
        (home / "daemon.json").write_text(
            json.dumps(
                {
                    "url": f"http://127.0.0.1:{port}",
                    "port": port,
                    "pid": os.getpid(),
                    "version": "2.0.1",
                    "token_file": str(home / "token"),
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(client_mod, "daemon_version", lambda: "0.0.0-dev")
        spawned: list[Path] = []
        monkeypatch.setattr(client_mod, "_spawn_daemon_process", spawned.append)
        try:
            client = ensure_daemon(home)
            assert client is not None and client.base_url.endswith(f":{port}")
            assert spawned == []
            assert client_mod._health(f"http://127.0.0.1:{port}", timeout=1.0)  # noqa: SLF001
        finally:
            httpd.shutdown()
            httpd.server_close()


class TestStaleCodeSameVersionRespawn:
    """§5.2 step 1c-bis: a daemon reporting the SAME version string as the
    client, but whose loaded code is demonstrably older (a reinstalled
    package with no version bump -- the exact incident measured 2026-09-25,
    both processes reporting ``2.0.2``), still gets retired and replaced.
    """

    @pytest.mark.parametrize(
        ("hc", "info", "mine_fp", "expected"),
        [
            # Daemon reports a fingerprint older than ours -> retire.
            ({"code_fingerprint": 100.0}, {}, 200.0, True),
            # Daemon reports a fingerprint newer than (or equal to) ours -> keep.
            ({"code_fingerprint": 300.0}, {}, 200.0, False),
            ({"code_fingerprint": 200.0}, {}, 200.0, False),
            # No code_fingerprint field (pre-fix daemon): fall back to
            # started_at vs our fingerprint. Daemon started BEFORE the
            # newest local .py file was touched -> its code predates ours.
            ({}, {"started_at": "2020-01-01T00:00:00+00:00"}, 2000000000.0, True),
            # Daemon started AFTER our newest file mtime -> its code is not
            # provably stale; keep it.
            ({}, {"started_at": "2099-01-01T00:00:00+00:00"}, 200.0, False),
            # Missing started_at and no code_fingerprint -> fail closed (keep).
            ({}, {}, 200.0, False),
            # Unparsable started_at -> fail closed (keep).
            ({}, {"started_at": "not-a-date"}, 2000000000.0, False),
        ],
    )
    def test_should_retire_stale_code(
        self,
        monkeypatch: pytest.MonkeyPatch,
        hc: dict,
        info: dict,
        mine_fp: float,
        expected: bool,
    ) -> None:
        monkeypatch.setattr(client_mod, "code_fingerprint", lambda: mine_fp)
        assert client_mod._should_retire_stale_code(hc, info) is expected  # noqa: SLF001

    def test_zero_local_fingerprint_never_retires(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A client that can't compute its own fingerprint (scan failure)
        must never use that as grounds to kill a live daemon."""
        monkeypatch.setattr(client_mod, "code_fingerprint", lambda: 0.0)
        assert (
            client_mod._should_retire_stale_code(  # noqa: SLF001
                {"code_fingerprint": 1.0}, {"started_at": "2020-01-01T00:00:00+00:00"}
            )
            is False
        )

    def test_stale_code_same_version_shuts_down_old_and_spawns_new(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Integration: SAME version string, older code_fingerprint on
        /health -> old daemon is retired and a new one spawned."""
        pytest.importorskip("amplifier_data")
        import threading

        from amplifier_data import AmplifierStore

        from amplifier_module_tool_memory.daemon import make_daemon

        home = _home(tmp_path)
        store = AmplifierStore(record_access=False)
        httpd = make_daemon(
            store,
            None,
            "127.0.0.1",
            0,
            token="tok",
            version="2.0.2",
            code_fp=100.0,  # deliberately "old" code
            durable=False,
        )
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        port = httpd.server_address[1]

        home.mkdir(exist_ok=True)
        (home / "token").write_text("tok", encoding="utf-8")
        (home / "daemon.json").write_text(
            json.dumps(
                {
                    "url": f"http://127.0.0.1:{port}",
                    "port": port,
                    "pid": os.getpid(),  # a real, alive pid (this test process)
                    "version": "2.0.2",
                    "token_file": str(home / "token"),
                    "started_at": "2020-01-01T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )

        monkeypatch.setattr(client_mod, "daemon_version", lambda: "2.0.2")
        monkeypatch.setattr(client_mod, "code_fingerprint", lambda: 200.0)
        monkeypatch.setattr(
            client_mod, "_spawn_daemon_process", lambda h: _spawn_daemon_ephemeral(h)
        )

        client = ensure_daemon(home)
        try:
            assert client is not None
            new_info = json.loads((home / "daemon.json").read_text(encoding="utf-8"))
            assert new_info["port"] != port  # a genuinely new daemon, not the old one
            deadline = time.monotonic() + 5.0
            stopped = False
            while time.monotonic() < deadline:
                hc = client_mod._health(f"http://127.0.0.1:{port}", timeout=0.5)  # noqa: SLF001
                if hc is None:
                    stopped = True
                    break
                time.sleep(0.2)
            assert stopped
        finally:
            if client is not None:
                client.shutdown()
            _wait_for_daemon_json_gone(home)

    def test_same_version_fresh_code_reuses_running_daemon(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Integration: SAME version, fresh (newer-or-equal) code_fingerprint
        -> the running daemon is reused, nothing is spawned."""
        pytest.importorskip("amplifier_data")
        import threading

        from amplifier_data import AmplifierStore

        from amplifier_module_tool_memory.daemon import make_daemon

        home = _home(tmp_path)
        httpd = make_daemon(
            AmplifierStore(record_access=False),
            None,
            "127.0.0.1",
            0,
            token="tok",
            version="2.0.2",
            code_fp=200.0,
            durable=False,
        )
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        port = httpd.server_address[1]
        home.mkdir(exist_ok=True)
        (home / "token").write_text("tok", encoding="utf-8")
        (home / "daemon.json").write_text(
            json.dumps(
                {
                    "url": f"http://127.0.0.1:{port}",
                    "port": port,
                    "pid": os.getpid(),
                    "version": "2.0.2",
                    "token_file": str(home / "token"),
                    "started_at": "2020-01-01T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(client_mod, "daemon_version", lambda: "2.0.2")
        monkeypatch.setattr(client_mod, "code_fingerprint", lambda: 200.0)
        spawned: list[Path] = []
        monkeypatch.setattr(client_mod, "_spawn_daemon_process", spawned.append)
        try:
            client = ensure_daemon(home)
            assert client is not None and client.base_url.endswith(f":{port}")
            assert spawned == []
            assert client_mod._health(f"http://127.0.0.1:{port}", timeout=1.0)  # noqa: SLF001
        finally:
            httpd.shutdown()
            httpd.server_close()
