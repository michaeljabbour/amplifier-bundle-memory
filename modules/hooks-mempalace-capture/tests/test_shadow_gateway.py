"""
The capture hook's "running shadow": every filed drawer is best-effort
dual-written to amplifier-data through the authed gateway, configured via
shadow_gateway. The palace stays source of truth; shadow never breaks capture.

Tests the shadow path in isolation (the palace write itself needs the mempalace
CLI, which is not assumed here). Skipped when amplifier-data is unavailable.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("amplifier_data")

from amplifier_data import AmplifierStore  # noqa: E402

import amplifier_module_hooks_mempalace_capture as cap  # noqa: E402
from amplifier_module_tool_mempalace.scripts.amplifier_data_gateway import (  # noqa: E402
    make_gateway,
)

_TOKEN = "shadow-token-xyz"


def _job(**over: Any) -> cap._CaptureJob:
    base: dict[str, Any] = {
        "capture_id": "c1",
        "tool_name": "bash",
        "tool_input": {},
        "tool_output": "we decided to ship the manifest",
        "source": "bash",
        "category": "decision",
        "session_id": None,
        "enqueued_at": "now",
        "emit_events": False,
    }
    base.update(over)
    return cap._CaptureJob(**base)


def test_shadow_disabled_by_default() -> None:
    cap._configure_shadow({})
    assert cap._SHADOW_STORE is None
    cap._shadow_job(_job(), "w", "r")  # no-op, must not raise


def test_shadow_writes_through_gateway(tmp_path: Path) -> None:
    backing = AmplifierStore(path=str(tmp_path / "s.ampd"), record_access=False)
    httpd = make_gateway(
        backing, "127.0.0.1", 0, token=_TOKEN, allow_localhost_bypass=False
    )
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    tokfile = tmp_path / "tok"
    tokfile.write_text(_TOKEN, encoding="utf-8")
    try:
        cap._configure_shadow(
            {
                "enabled": True,
                "base_url": f"http://127.0.0.1:{port}",
                "token_file": str(tokfile),
            }
        )
        assert cap._SHADOW_STORE is not None

        cap._shadow_job(_job(), "wing_w", "room_r")

        ref = cap._SHADOW_STORE.filed[-1]["ref"]
        # the drawer landed in the backing store, byte-identical, scoped, with facts
        assert backing.regenerate(ref).payload == b"we decided to ship the manifest"
        labels = {
            backing.regenerate(n).payload.decode()
            for n in backing.graph_neighbors(ref, rel_type="scoped_to")
        }
        assert labels == {"wing:wing_w", "room:room_r"}
        facts = backing.query_facts(subject=ref, predicate="has_category")
        assert facts.success and len(facts.output) == 1
    finally:
        httpd.shutdown()
        cap._configure_shadow({})  # reset module global


def test_shadow_failure_is_swallowed(tmp_path: Path) -> None:
    # point at a dead URL: shadow must fail silently, never raise
    cap._configure_shadow(
        {"enabled": True, "base_url": "http://127.0.0.1:1", "token_file": ""}
    )
    try:
        cap._shadow_job(_job(), "w", "r")  # must not raise despite connection refused
    finally:
        cap._configure_shadow({})
