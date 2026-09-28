"""perf/incremental-fold (part B): hooks-memory-briefing must never arm
(no prefetch, no daemon contact) for automated/non-interactive runs opted
out via AMPLIFIER_MEMORY_CAPTURE or excluded_working_dirs, while leaving a
normal interactive session's default behavior unchanged.
"""

from __future__ import annotations

import asyncio

import pytest

import amplifier_module_hooks_memory_briefing as m

from .test_background_delivery import _FakeClient, _injections, _mounted, fake  # noqa: F401


class TestEnvVarOptOut:
    def test_session_start_does_not_arm_when_opted_out(
        self, fake: _FakeClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AMPLIFIER_MEMORY_CAPTURE", "off")

        async def run() -> list[str]:
            coord = await _mounted()
            await coord.hooks.emit("session:start", {})
            m._wait_for_background(2)
            return _injections(await coord.hooks.emit("prompt:submit", {"prompt": "hi"}))

        assert asyncio.run(run()) == []
        assert fake.calls == []  # never even contacted the daemon


class TestExcludedWorkingDirs:
    def test_matching_cwd_glob_does_not_arm(
        self, fake: _FakeClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AMPLIFIER_MEMORY_CAPTURE", raising=False)
        monkeypatch.setattr(
            "os.getcwd", lambda: "/home/user/dev/afast-ev/bench/experiments/r1"
        )

        async def run() -> list[str]:
            coord = await _mounted(
                {"excluded_working_dirs": ["/home/*/dev/afast-ev/*/experiments/*"]}
            )
            await coord.hooks.emit("session:start", {})
            m._wait_for_background(2)
            return _injections(await coord.hooks.emit("prompt:submit", {"prompt": "hi"}))

        assert asyncio.run(run()) == []
        assert fake.calls == []


class TestDefaultBehaviorUnchanged:
    def test_normal_session_still_arms_and_delivers(self, fake: _FakeClient) -> None:
        async def run() -> list[str]:
            coord = await _mounted()
            await coord.hooks.emit("session:start", {})
            m._wait_for_background(10)
            return _injections(await coord.hooks.emit("prompt:submit", {"prompt": "hi"}))

        assert len(asyncio.run(run())) == 1
        assert fake.calls  # daemon WAS contacted
