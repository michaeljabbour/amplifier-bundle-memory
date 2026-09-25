"""perf/startup-latency: the briefing is fetched off the critical path and
delivered where the orchestrator actually honors an ephemeral injection.

amplifier-core discards the HookResult of ``session:start``, so the old
synchronous handler blocked the first turn for seconds and its injection
never reached the model. These tests pin the replacement contract:

* ``mount()`` wires session:start (arm), prompt:submit + provider:request
  (deliver) and starts the prefetch immediately;
* ``session:start`` never blocks on memory I/O;
* the first ``prompt:submit`` waits at most ``deliver_wait_s``; if the
  prefetch is still running the briefing is delivered, non-blocking, at a
  later delivery point -- exactly once per session;
* the delivered text is identical to what the synchronous path builds;
* sub-agent sessions are skipped by default; the per-process cache is reused;
* search-provided importance replaces the per-hit fact lookups.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest

import amplifier_module_hooks_memory_briefing as m


class _Hooks:
    def __init__(self) -> None:
        self.handlers: dict[str, list[tuple[Any, dict[str, Any]]]] = {}

    def register(self, event: str, handler: Any, **kw: Any) -> None:
        self.handlers.setdefault(event, []).append((handler, kw))

    async def emit(self, event: str, data: dict[str, Any]) -> list[Any]:
        return [await h(event, data) for h, _ in self.handlers.get(event, [])]


class _Coordinator:
    def __init__(self) -> None:
        self.hooks = _Hooks()

    def register_contributor(self, *a: Any, **kw: Any) -> None:
        pass


_HITS = [
    {
        "ref": "d1",
        "score": 0.80,
        "content": "alpha memory",
        "room": "r1",
        "importance": 0.5,
    },
    {
        "ref": "d2",
        "score": 0.79,
        "content": "beta memory",
        "room": "r2",
        "importance": 1.0,
    },
    {
        "ref": "d3",
        "score": 0.10,
        "content": "gamma memory",
        "room": "r3",
        "importance": None,
    },
]


class _FakeClient:
    def __init__(
        self, *, delay: float = 0.0, hits: list[dict[str, Any]] | None = None
    ) -> None:
        self.delay = delay
        self.hits = _HITS if hits is None else hits
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def _rec(self, name: str) -> None:
        with self.lock:
            self.calls.append(name)
        if self.delay:
            time.sleep(self.delay)

    def search(self, **kw: Any) -> dict[str, Any]:
        self._rec("search")
        return {"results": [dict(h) for h in self.hits], "degraded": None}

    def kg_query(self, **kw: Any) -> list[tuple[str, str, str]]:
        self._rec("kg_query")
        return [("proj", "uses", "python")]

    def diary_read(self, **kw: Any) -> list[dict[str, Any]]:
        self._rec("diary_read")
        return [{"seq_pos": 7, "entry": "did a thing"}]

    def query_facts(self, **kw: Any) -> Any:  # must NOT be needed any more
        self._rec("query_facts")
        raise AssertionError("per-hit importance lookup should not run")

    def regenerate(self, ref: str) -> Any:
        self._rec("regenerate")
        raise AssertionError("per-hit importance lookup should not run")


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    client = _FakeClient()
    monkeypatch.setattr(m, "ensure_daemon", lambda *a, **kw: client)
    monkeypatch.setattr(m, "_detect_project_name", lambda: "proj")
    monkeypatch.setattr(m, "_find_project_context_dir", lambda: None)
    monkeypatch.setattr(m, "emit_event", lambda *a, **kw: None)
    return client


def _injections(results: list[Any]) -> list[str]:
    return [r.context_injection for r in results if r.action == "inject_context"]


async def _mounted(config: dict[str, Any] | None = None) -> _Coordinator:
    coord = _Coordinator()
    await m.mount(coord, config or {})
    return coord


def test_mount_wires_arm_and_delivery_events(fake: _FakeClient) -> None:
    coord = asyncio.run(_mounted())
    h = coord.hooks.handlers
    assert set(h) >= {"session:start", "prompt:submit", "provider:request"}
    # Runs before hooks-memory-interject (priority 20) on prompt:submit.
    assert h["prompt:submit"][0][1]["priority"] < 20


def test_delivered_on_first_prompt_submit_identical_to_sync(fake: _FakeClient) -> None:
    async def run() -> tuple[list[str], list[str], list[str]]:
        coord = await _mounted()
        start = await coord.hooks.emit("session:start", {"session_id": "s"})
        submit = await coord.hooks.emit("prompt:submit", {"prompt": "hi"})
        later = await coord.hooks.emit("provider:request", {})
        return _injections(start), _injections(submit), _injections(later)

    start, submit, later = asyncio.run(run())
    assert start == []  # session:start never injects (the kernel drops it)
    assert len(submit) == 1 and later == []  # exactly once

    sync = asyncio.run(m.MemoryBriefingHook()("session:start", {}))
    assert submit[0] == sync.context_injection
    # importance rerank used the search-provided values: d2 (imp 1.0) > d1
    assert submit[0].index("beta memory") < submit[0].index("alpha memory")
    assert "query_facts" not in fake.calls and "regenerate" not in fake.calls


def test_session_start_never_blocks_and_wait_is_bounded(
    fake: _FakeClient,
) -> None:
    fake.delay = 0.6  # every daemon call is slow

    async def run() -> dict[str, Any]:
        coord = await _mounted({"deliver_wait_s": 0.1})
        t0 = time.perf_counter()
        await coord.hooks.emit("session:start", {})
        t_start = time.perf_counter() - t0
        t0 = time.perf_counter()
        submit = await coord.hooks.emit("prompt:submit", {"prompt": "hi"})
        t_submit = time.perf_counter() - t0
        early_req = await coord.hooks.emit("provider:request", {})
        m._wait_for_background(10)
        late_req = await coord.hooks.emit("provider:request", {})
        again = await coord.hooks.emit("prompt:submit", {"prompt": "next"})
        return {
            "t_start": t_start,
            "t_submit": t_submit,
            "submit": _injections(submit),
            "early": _injections(early_req),
            "late": _injections(late_req),
            "again": _injections(again),
        }

    r = asyncio.run(run())
    assert r["t_start"] < 0.05
    assert r["t_submit"] < 0.4  # bounded by deliver_wait_s, not the 1.2 s fetch
    assert r["submit"] == [] and r["early"] == []
    assert len(r["late"]) == 1 and "Memory Briefing" in r["late"][0]
    assert r["again"] == []


def test_lookups_run_concurrently(fake: _FakeClient) -> None:
    fake.delay = 0.3
    t0 = time.perf_counter()
    part = m._fetch_memory_part("proj", "", 1500, True, True, 1.0)
    elapsed = time.perf_counter() - t0
    assert len(part.sections) == 3
    assert elapsed < 0.75  # three 0.3 s calls in parallel, not 0.9 s in series


def test_concurrent_fetch_keeps_budget_gating(fake: _FakeClient) -> None:
    fake.hits = [
        {
            "ref": f"d{i}",
            "score": 0.9,
            "content": "x" * 400,
            "room": "r",
            "importance": 0.5,
        }
        for i in range(5)
    ]
    # search section alone is ~390 tokens -> KG and diary gated out at budget 300
    part = m._fetch_memory_part("proj", "", 300, True, True, 1.0)
    assert len(part.sections) == 1 and part.sections[0].startswith("**Recent memories")


def test_subsession_skipped_by_default(fake: _FakeClient) -> None:
    async def run(config: dict[str, Any]) -> list[str]:
        coord = await _mounted(config)
        await coord.hooks.emit("session:start", {"parent_id": "root"})
        m._wait_for_background(10)
        return _injections(await coord.hooks.emit("prompt:submit", {"prompt": "hi"}))

    assert asyncio.run(run({})) == []
    assert len(asyncio.run(run({"brief_subsessions": True}))) == 1


def test_process_cache_reused_across_sessions(fake: _FakeClient) -> None:
    async def session() -> list[str]:
        coord = await _mounted()
        await coord.hooks.emit("session:start", {})
        m._wait_for_background(10)
        return _injections(await coord.hooks.emit("prompt:submit", {"prompt": "hi"}))

    first = asyncio.run(session())
    n_calls = len(fake.calls)
    second = asyncio.run(session())
    assert first == second and len(first) == 1
    assert len(fake.calls) == n_calls  # no new daemon calls within the TTL


def test_older_daemon_without_importance_falls_back(
    fake: _FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.hits = [{k: v for k, v in h.items() if k != "importance"} for h in _HITS]
    looked_up: list[str] = []
    monkeypatch.setattr(
        m, "_query_importance", lambda rid: looked_up.append(rid) or 0.5
    )
    m._fetch_memory_part("proj", "", 1500, False, False, 1.0)
    assert looked_up == ["d1", "d2", "d3"]


def test_daemon_unavailable_delivers_coordination_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    pc = tmp_path / "project-context"
    pc.mkdir()
    (pc / "HANDOFF.md").write_text("next: ship it\n")
    monkeypatch.setattr(m, "ensure_daemon", lambda *a, **kw: None)
    monkeypatch.setattr(m, "_detect_project_name", lambda: "proj")
    monkeypatch.setattr(m, "_find_project_context_dir", lambda: pc)
    monkeypatch.setattr(m, "emit_event", lambda *a, **kw: None)

    async def run() -> list[str]:
        coord = await _mounted()
        await coord.hooks.emit("session:start", {})
        return _injections(await coord.hooks.emit("prompt:submit", {"prompt": "hi"}))

    out = asyncio.run(run())
    assert len(out) == 1
    assert "coordination files only" in out[0] and "ship it" in out[0]


def test_briefing_assembled_bridged_at_delivery(fake: _FakeClient) -> None:
    bridged: list[tuple[str, Any]] = []

    async def bridge(name: str, payload: Any) -> None:
        bridged.append((name, payload))

    async def run() -> None:
        hook = m.MemoryBriefingHook({}, bridge_emit=bridge)
        hook.prefetch()
        await hook.on_session_start("session:start", {})
        await hook.on_deliver("prompt:submit", {"prompt": "hi"})

    asyncio.run(run())
    assert [n for n, _ in bridged] == ["memory:briefing_assembled"]
    assert bridged[0][1]["drawer_ids"] == ["d2", "d1", "d3"]


def test_side_call_provider_request_does_not_consume_delivery(
    fake: _FakeClient,
) -> None:
    async def run() -> tuple[list[str], list[str]]:
        coord = await _mounted({"deliver_wait_s": 0.0})
        await coord.hooks.emit("session:start", {})
        m._wait_for_background(10)
        judge = await coord.hooks.emit("provider:request", {"iteration": 0})
        real = await coord.hooks.emit("provider:request", {"iteration": 2})
        return _injections(judge), _injections(real)

    judge, real = asyncio.run(run())
    assert judge == [] and len(real) == 1
