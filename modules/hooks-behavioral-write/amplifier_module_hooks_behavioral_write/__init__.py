"""amplifier-module-hooks-behavioral-write — T1-HOOK-1 (Step-2 integrator).

The behavioral write hook is the memory bundle's half of the behavioral
plasticity loop. It is CO-MOUNTED by the memory bundle and fires on
``orchestrator:complete``. End to end it does:

    CI outcome (read events.jsonl)  ->  salience gate (T1-GATE-1)
        ->  outcome->importance recompute (T1-MEM-1 signal, phase3 rubric)
        ->  durable, reversible UPDATE via the amplifier-data seam (T1-MEM-2)

Hard constraints honoured here (the constellation's prime directives):

* **Off the hot path.** ``__call__`` returns immediately; the recompute runs in
  a detached daemon thread. No per-iteration mutation, no ``project()`` forward
  pass — learning is post-session only.
* **Default: don't learn.** Every candidate write must clear the salience gate,
  which rejects by default.
* **Peer isolation.** This module reads the raw ``events.jsonl`` substrate
  directly to derive the outcome. It does NOT import the context-intelligence
  bundle or the survey bundle (peers must not import each other). It imports
  only within the memory bundle (tool-mempalace) and amplifier-core.
* **Full mutation contract.** Every write carries provenance, causal
  interaction id, reversible delta, timestamp, source outcome, confidence, and
  a rollback handle (see ``scripts/mutation.py``), and is appended to an audit
  ledger so it can be reversed.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from amplifier_module_tool_mempalace.phase3 import compute_importance
from amplifier_module_tool_mempalace.salience import (
    SalienceConfig,
    SalienceInput,
    evaluate_salience,
)
from amplifier_module_tool_mempalace.scripts.mutation import MutationRecord

try:
    from amplifier_core import HookResult  # type: ignore
except ImportError:  # pragma: no cover - exercised outside Amplifier

    class HookResult:  # type: ignore
        def __init__(self, *, action: str = "continue", **kwargs: Any) -> None:
            self.action = action
            for k, v in kwargs.items():
                setattr(self, k, v)


PROVENANCE = "hooks-behavioral-write:orchestrator:complete"

# Memory-side default gate. Deliberately looser than the gate's own strict
# default (0.6) because this is a coarse pre-filter; the conductor
# (behavioral-plasticity) owns the authoritative thresholds and may override
# this config when it co-mounts. Still default-rejects a neutral session.
DEFAULT_SALIENCE = SalienceConfig(threshold=0.25)

# Event whose presence marks a drawer touched this session (emitted by the
# capture hook's drain worker). We read it from the substrate, not from CI.
_DRAWER_EVENT_NAMES = {"drawer_filed", "memory-mempalace:drawer_filed"}


@dataclass(frozen=True)
class TouchedDrawer:
    """A drawer written during the session, plus its captured outcome."""

    subject: str
    category: str | None
    tool_success: bool


# ---------------------------------------------------------------------------
# Substrate reading (no CI import — we parse the raw JSONL ourselves)
# ---------------------------------------------------------------------------


def _read_events(events_path: str | Path) -> list[dict[str, Any]]:
    """Read a session events.jsonl. Robust: skips malformed lines, never raises.

    Each large line is parsed independently; a single bad line cannot abort the
    learning pass. Returns [] if the file is missing.
    """
    out: list[dict[str, Any]] = []
    p = Path(events_path)
    if not p.is_file():
        return out
    try:
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except (ValueError, TypeError):
                    continue
    except OSError:
        return out
    return out


def _event_name(ev: dict[str, Any]) -> str:
    return str(ev.get("event") or ev.get("event_name") or ev.get("name") or "")


def _touched_drawers(events: Iterable[dict[str, Any]]) -> list[TouchedDrawer]:
    """Extract the drawers touched this session from drawer_filed events.

    Subject resolution order: explicit ``drawer_id`` -> ``source`` -> ``capture_id``.
    The drawer_filed event carries ``tool_success`` (added in T1-MEM-1) and
    ``category``; absence of tool_success defaults to success (outcome-blind).
    """
    seen: dict[str, TouchedDrawer] = {}
    for ev in events:
        if _event_name(ev) not in _DRAWER_EVENT_NAMES:
            continue
        data = ev.get("data") or {}
        subject = str(
            data.get("drawer_id") or data.get("source") or data.get("capture_id") or ""
        )
        if not subject:
            continue
        td = TouchedDrawer(
            subject=subject,
            category=data.get("category"),
            tool_success=bool(data.get("tool_success", True)),
        )
        # Last write wins for a given subject within the session.
        seen[subject] = td
    return list(seen.values())


def _session_failure_rate(touched: list[TouchedDrawer]) -> float:
    """Fraction of touched drawers whose capture failed. 0.0 if none touched."""
    if not touched:
        return 0.0
    failures = sum(1 for t in touched if not t.tool_success)
    return failures / len(touched)


def _salience_for(
    td: TouchedDrawer, delta: float, failure_rate: float
) -> SalienceInput:
    """Derive (novelty, reward, surprise) for one drawer.

    Heuristic (memory-side default; the conductor may replace it):
    * novelty  — magnitude of the importance change, plus a bump if the drawer's
                 own capture failed (a failure is intrinsically novel signal).
    * reward   — high for a failed drawer (failures are instructive); otherwise
                 the session-wide failure rate.
    * surprise — prediction error: maximal for a failed drawer, else the
                 session failure rate.
    """
    drawer_failed = 0.0 if td.tool_success else 1.0
    novelty = abs(delta) + 0.5 * drawer_failed
    reward = 1.0 if not td.tool_success else failure_rate
    surprise = 1.0 if not td.tool_success else failure_rate
    return SalienceInput(novelty=novelty, reward=reward, surprise=surprise)


# ---------------------------------------------------------------------------
# Core (synchronous, dependency-injected, deterministic — directly testable)
# ---------------------------------------------------------------------------


def process_session(
    events_path: str | Path,
    store: Any,
    *,
    query_importance: Callable[[str], float | None],
    config: SalienceConfig = DEFAULT_SALIENCE,
    audit: Callable[[MutationRecord], None] | None = None,
) -> list[MutationRecord]:
    """Run one post-session learning pass. Returns the applied mutations.

    Pure-ish: all side effects go through the injected ``store`` (the
    amplifier-data seam or a RecordingMemoryStore) and the optional ``audit``
    sink. No threads, no I/O beyond reading the events file. Safe to call
    directly from a test.
    """
    events = _read_events(events_path)
    touched = _touched_drawers(events)
    failure_rate = _session_failure_rate(touched)
    records: list[MutationRecord] = []

    for td in touched:
        old = query_importance(td.subject)
        signals = {"unresolved": not td.tool_success}
        new = compute_importance(td.category, signals)
        baseline = 0.0 if old is None else float(old)
        delta = new - baseline

        # Salience gate FIRST — default rejects. No write unless it clears.
        decision = evaluate_salience(_salience_for(td, delta, failure_rate), config)
        if not decision.write:
            continue
        # Skip no-op writes (importance unchanged) — nothing to learn.
        if old is not None and abs(new - float(old)) < 1e-9:
            continue

        record = store.update_importance(
            td.subject,
            old_importance=old,
            new_importance=new,
            provenance=PROVENANCE,
            source_outcome=(
                f"tool_success={td.tool_success};"
                f"failure_rate={round(failure_rate, 4)};"
                f"salience={decision.salience}"
            ),
            confidence=decision.salience,
            interaction_id=None,
        )
        records.append(record)
        if audit is not None:
            audit(record)
    return records


# ---------------------------------------------------------------------------
# The hook (thin async wrapper — dispatches the core off the hot path)
# ---------------------------------------------------------------------------


class BehavioralWriteHook:
    name = "hooks-behavioral-write"
    events = ["orchestrator:complete"]

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        store_factory: Callable[[], Any] | None = None,
        query_importance: Callable[[str], float | None] | None = None,
    ) -> None:
        self.config = config or {}
        self.enabled: bool = bool(self.config.get("enabled", True))
        threshold = float(
            self.config.get("salience_threshold", DEFAULT_SALIENCE.threshold)
        )
        self.salience_config = SalienceConfig(threshold=threshold)
        self._store_factory = store_factory
        self._query_importance = query_importance
        # Ledger of every mutation this hook has applied (reversible audit).
        self.audit_log: list[MutationRecord] = []

    def _run(self, events_path: str, store: Any) -> None:
        try:
            process_session(
                events_path,
                store,
                query_importance=self._query_importance or (lambda _s: None),
                config=self.salience_config,
                audit=self.audit_log.append,
            )
        except Exception:
            # Learning must NEVER destabilise the session. Swallow + drop.
            pass

    async def __call__(self, event: str, data: dict[str, Any]) -> HookResult:
        """Hot-path handler: dispatch the learning pass to a daemon thread.

        Returns immediately. If learning is disabled, no store is available, or
        no events path is known, it is a silent no-op (default: don't learn).
        """
        if not self.enabled or self._store_factory is None:
            return HookResult(action="continue")
        events_path = data.get("events_path") or data.get("events_jsonl")
        if not events_path:
            return HookResult(action="continue")
        try:
            store = self._store_factory()
        except Exception:
            return HookResult(action="continue")

        threading.Thread(
            target=self._run,
            args=(str(events_path), store),
            name="behavioral-write",
            daemon=True,
        ).start()
        return HookResult(action="continue")


async def mount(
    coordinator: Any, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Co-mount the behavioral write hook on ``orchestrator:complete``.

    The store factory and current-value query are intentionally left for the
    integrating bundle (or the conductor) to inject via config wiring; absent
    them the hook mounts as a safe no-op so merely installing the module never
    starts mutating memory on its own.
    """
    hook = BehavioralWriteHook(config or {})
    for event in hook.events:
        coordinator.hooks.register(event, hook, name=hook.name)
    return {"hook": hook}
