#!/usr/bin/env python3
"""Code-level startup-latency bench for hooks-memory-briefing.

Measures what the memory briefing costs a session WITHOUT running
``amplifier`` end-to-end (which would pip-install modules into the shared
Amplifier tool environment). It drives the hook through a minimal fake
coordinator exactly the way the kernel/orchestrator do:

  mount()  ->  emit("session:start")  ->  emit("prompt:submit")

and records how long each emit BLOCKS the caller (that is the latency a
user sees before the first model call), whether a briefing was injected,
and how large it was. It also times every daemon round-trip the briefing
performs (search / kg_query / diary_read / per-hit importance lookups)
directly through ``MemoryClient``.

It runs against a PRIVATE daemon on a COPY of a store log -- never the
user's live ``~/.amplifier/memory`` daemon:

  python scripts/bench_startup.py --store-log /tmp/memperf/store.log.orig \
      --home /tmp/memperf/home --project-dir ~/dev/some-repo --runs 5

Phases:
  cold  -- no daemon running: the first session pays daemon spawn (+ the
           embedder is still loading, so search takes the lexical path).
  warm  -- daemon up and embedder ready (the steady state for every session
           after the first one of the day).
  second-session -- a second mount in the SAME process (sub-agent sessions
           spawned by ``delegate`` mount their own hooks in-process).

Run it with the per-module venv (``uv run --no-sync python scripts/...`` from
modules/hooks-memory-briefing) so the daemon subprocess is spawned from that
venv and the source under test is the checkout, not the tool install.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import statistics
import sys
import time
from pathlib import Path
from typing import Any


class _FakeHooks:
    def __init__(self) -> None:
        self.handlers: dict[str, list[tuple[Any, str]]] = {}

    def register(self, event: str, handler: Any, *, name: str = "", **_: Any) -> Any:
        self.handlers.setdefault(event, []).append((handler, name))
        return lambda: None

    async def emit(self, event: str, data: dict[str, Any]) -> list[Any]:
        out = []
        for handler, _name in self.handlers.get(event, []):
            out.append(await handler(event, data))
        return out


class _FakeCoordinator:
    def __init__(self) -> None:
        self.hooks = _FakeHooks()
        self.contributions: dict[str, Any] = {}

    def register_contributor(self, channel: str, name: str, cb: Any) -> None:
        self.contributions[name] = cb

    def get(self, _name: str) -> Any:
        return None


def _injections(results: list[Any]) -> list[str]:
    return [
        r.context_injection
        for r in results
        if getattr(r, "action", None) == "inject_context"
        and getattr(r, "context_injection", None)
    ]


async def _one_session(
    briefing_mod: Any, config: dict[str, Any], prompt: str, think_s: float
) -> dict[str, Any]:
    """mount -> session:start -> (think) -> prompt:submit, timing each emit."""
    coord = _FakeCoordinator()
    t0 = time.perf_counter()
    await briefing_mod.mount(coord, config)
    t_mount = time.perf_counter() - t0

    t0 = time.perf_counter()
    start_res = await coord.hooks.emit("session:start", {"session_id": "bench"})
    t_start = time.perf_counter() - t0

    if think_s:
        await asyncio.sleep(think_s)

    t0 = time.perf_counter()
    submit_res = await coord.hooks.emit(
        "prompt:submit", {"session_id": "bench", "prompt": prompt}
    )
    t_submit = time.perf_counter() - t0

    # Only prompt:submit / provider:request injections reach the model under
    # the current kernel (session:start's HookResult is discarded by
    # amplifier-core's session.execute) -- record both so the bench is honest
    # about what the OLD code delivered.
    start_inj = _injections(start_res)
    submit_inj = _injections(submit_res)
    delivered = submit_inj
    return {
        "mount_s": t_mount,
        "session_start_block_s": t_start,
        "prompt_submit_block_s": t_submit,
        "critical_path_s": t_mount + t_start + t_submit,
        "session_start_injection_chars": sum(len(x) for x in start_inj),
        "delivered_injection_chars": sum(len(x) for x in delivered),
        "delivered": bool(delivered),
        "text": (delivered or start_inj or [""])[0],
    }


def _time(fn: Any, *a: Any, **kw: Any) -> tuple[float, Any]:
    t0 = time.perf_counter()
    try:
        out = fn(*a, **kw)
    except Exception as exc:  # noqa: BLE001
        out = exc
    return time.perf_counter() - t0, out


def _roundtrips(client: Any, project: str) -> dict[str, float]:
    """Time each daemon call the briefing makes, one by one (sequential)."""
    t: dict[str, float] = {}
    t["search"], res = _time(
        client.search, f"recent work on {project}", k=8, wing=f"wing_{project}"
    )
    hits = res.get("results", []) if isinstance(res, dict) else []
    t["kg_query"], _ = _time(client.kg_query, subject=project, predicate=None)
    t["diary_read"], _ = _time(client.diary_read, agent_name="amplifier", last_n=3)
    imp = 0.0
    for h in hits:
        dt, fr = _time(client.query_facts, subject=h["ref"], predicate="has_importance")
        imp += dt
        if not isinstance(fr, Exception) and fr.success and fr.output:
            dt2, _ = _time(client.regenerate, fr.output[0].object)
            imp += dt2
    t["importance_lookups"] = imp
    t["n_hits"] = float(len(hits))
    return t


def _summ(xs: list[float]) -> str:
    if not xs:
        return "-"
    if len(xs) == 1:
        return f"{xs[0]:.3f}"
    return f"med {statistics.median(xs):.3f} (min {min(xs):.3f}, max {max(xs):.3f}, n={len(xs)})"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--store-log", required=True, help="store.log to COPY (never mutated)")
    ap.add_argument("--home", required=True, help="private daemon home (recreated)")
    ap.add_argument("--project-dir", default=os.getcwd())
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--think-s", type=float, default=0.0,
                    help="simulated gap between session:start and prompt:submit")
    ap.add_argument("--prompt", default="What is 2+2? Answer with just the number please.")
    ap.add_argument("--skip-cold", action="store_true")
    ap.add_argument("--skip-roundtrips", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--show-text", action="store_true")
    args = ap.parse_args(argv)

    home = Path(args.home).expanduser()
    if home.exists():
        # Stop any daemon a previous bench left behind in this private home.
        info = home / "daemon.json"
        if info.exists():
            os.environ["AMPLIFIER_MEMORY_HOME"] = str(home)
            from amplifier_module_tool_memory.client import _discover

            c = _discover(home, allow_recover=False)
            if c is not None:
                c.shutdown()
                time.sleep(1.0)
        shutil.rmtree(home)
    home.mkdir(parents=True)
    shutil.copyfile(Path(args.store_log).expanduser(), home / "store.log")
    os.environ["AMPLIFIER_MEMORY_HOME"] = str(home)
    os.chdir(Path(args.project_dir).expanduser())

    import amplifier_module_hooks_memory_briefing as briefing
    from amplifier_module_tool_memory import client as client_mod

    project = briefing._detect_project_name()
    config = {
        "token_budget": 1500,
        "include_kg": True,
        "include_diary": True,
        "include_project_context": True,
        "ephemeral": True,
        "emit_events": True,
        "briefing_importance_weight": 1.0,
    }
    report: dict[str, Any] = {
        "project": project,
        "store_log_bytes": (home / "store.log").stat().st_size,
        "briefing_module": briefing.__file__,
    }

    def _reset_process_state() -> None:
        # Fresh-process semantics for each simulated top-level session.
        for name in ("_reset_briefing_cache",):
            fn = getattr(briefing, name, None)
            if fn:
                fn()

    # -- cold ---------------------------------------------------------------
    if not args.skip_cold:
        _reset_process_state()
        t_spawn, c = _time(client_mod.ensure_daemon)
        report["cold_ensure_daemon_s"] = t_spawn
        # Kill it again so the hook itself pays the cold spawn inside the session.
        if c is not None:
            c.shutdown()
            time.sleep(1.5)
        _reset_process_state()
        cold = asyncio.run(_one_session(briefing, config, args.prompt, args.think_s))
        cold.pop("text")
        report["cold_session"] = cold
        # background work (new code) must finish before we measure warm
        waiter = getattr(briefing, "_wait_for_background", None)
        if waiter:
            waiter(60.0)

    # -- warm up embedder ----------------------------------------------------
    c = client_mod.ensure_daemon()
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        hc = c.health(timeout=2.0) or {}
        if hc.get("embedder", {}).get("ready"):
            break
        time.sleep(0.5)
    report["embedder_ready"] = bool((c.health() or {}).get("embedder", {}).get("ready"))

    # -- round-trips -----------------------------------------------------------
    if not args.skip_roundtrips:
        rts = [_roundtrips(c, project) for _ in range(max(1, min(args.runs, 3)))]
        report["warm_roundtrips_s"] = {
            k: statistics.median([r[k] for r in rts]) for k in rts[0]
        }

    # -- warm sessions ---------------------------------------------------------
    warm: list[dict[str, Any]] = []
    text = ""
    for _ in range(args.runs):
        _reset_process_state()
        r = asyncio.run(_one_session(briefing, config, args.prompt, args.think_s))
        text = r.pop("text") or text
        warm.append(r)
        waiter = getattr(briefing, "_wait_for_background", None)
        if waiter:
            waiter(60.0)
    report["warm_sessions"] = {
        k: statistics.median([w[k] for w in warm]) for k in warm[0] if k != "delivered"
    }
    report["warm_sessions"]["delivered_all"] = all(w["delivered"] for w in warm)
    report["warm_sessions_raw_critical_path_s"] = [w["critical_path_s"] for w in warm]

    # -- second session in same process (sub-agent) ----------------------------
    r2 = asyncio.run(_one_session(briefing, config, args.prompt, 0.0))
    r2.pop("text")
    report["second_session_same_process"] = r2

    c.shutdown()

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for k, v in report.items():
            if isinstance(v, dict):
                print(f"{k}:")
                for kk, vv in v.items():
                    print(f"    {kk}: {vv:.3f}" if isinstance(vv, float) else f"    {kk}: {vv}")
            elif isinstance(v, list):
                print(f"{k}: {_summ(v)}")
            else:
                print(f"{k}: {v:.3f}" if isinstance(v, float) else f"{k}: {v}")
    if args.show_text:
        print("\n--- injected briefing text ---\n" + text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
