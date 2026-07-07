"""
KG-K1 (tool-wiring half) + diary shadow.

Verifies ``_shadow_kg`` / ``_shadow_diary`` in amplifier_module_tool_mempalace:
never raise when unconfigured, land facts/entries through a live gateway when
configured, and swallow a dead-gateway failure. Mirrors the structure of
hooks-mempalace-capture's test_shadow_gateway.py (the same shadow contract).
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

pytest.importorskip("amplifier_data")

from amplifier_data import AmplifierStore  # noqa: E402

import amplifier_module_tool_mempalace as tm  # noqa: E402
from amplifier_module_tool_mempalace.scripts.amplifier_data_gateway import (  # noqa: E402
    make_gateway,
)

_TOKEN = "tool-shadow-token"


def test_shadow_disabled_by_default() -> None:
    tm._configure_shadow({})
    assert tm._SHADOW_STORE is None
    tm._shadow_kg("add", "svc-a", "depends_on", "svc-b")  # no-op, must not raise
    tm._shadow_diary("curator", "an entry", "t")  # no-op, must not raise


def test_shadow_kg_lands_via_gateway(tmp_path: Path) -> None:
    backing = AmplifierStore(path=str(tmp_path / "s.ampd"), record_access=False)
    httpd = make_gateway(
        backing, "127.0.0.1", 0, token=_TOKEN, allow_localhost_bypass=False
    )
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    tokfile = tmp_path / "tok"
    tokfile.write_text(_TOKEN, encoding="utf-8")
    try:
        tm._configure_shadow(
            {
                "enabled": True,
                "base_url": f"http://127.0.0.1:{port}",
                "token_file": str(tokfile),
            }
        )
        assert tm._SHADOW_STORE is not None

        tm._shadow_kg("add", "svc-a", "depends_on", "svc-b")
        facts = tm._SHADOW_STORE.query_kg(subject="svc-a")
        assert ("svc-a", "depends_on", "svc-b") in facts

        tm._shadow_kg("invalidate", "svc-a", "depends_on", "svc-b")
        facts_after = tm._SHADOW_STORE.query_kg(subject="svc-a")
        assert ("svc-a", "depends_on", "svc-b") not in facts_after

        tm._shadow_diary("curator", "diary via tool shadow", "t")
        content_ref = backing.write_cell(b"diary via tool shadow")
        assert backing.regenerate(content_ref).payload == b"diary via tool shadow"
        agent_scope = backing.write_cell(b"agent:curator")
        neighbors = backing.graph_neighbors(content_ref, rel_type="scoped_to")
        assert agent_scope in neighbors
    finally:
        httpd.shutdown()
        tm._configure_shadow({})


def test_shadow_failure_is_swallowed() -> None:
    # point at a dead URL: shadow must fail silently, never raise
    tm._configure_shadow(
        {"enabled": True, "base_url": "http://127.0.0.1:1", "token_file": ""}
    )
    try:
        tm._shadow_kg("add", "svc-a", "depends_on", "svc-b")
        tm._shadow_diary("curator", "entry", "t")
    finally:
        tm._configure_shadow({})
