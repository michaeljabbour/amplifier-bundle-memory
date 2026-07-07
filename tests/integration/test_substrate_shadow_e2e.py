"""
Substrate shadow e2e smoke (DTU-gated, §7 of
docs/plans/2026-07-07-substrate-adapter-completion-design.md).

Exercises the completed amplifier-data seam end-to-end as a user would,
inside the memory-bundle-e2e DTU container: file a drawer through the
DualWriteMemoryStore (real palace primary + real amplifier-data shadow),
attach an embedding + facts + a KG triple through the gateway, read it ALL
back from the substrate (palace untouched as read primary), and assert the
shadow never harmed the palace.

Palace-primary interaction note (investigated 2026-07-07): mempalace 3.5.0
has no synchronous single-shot CLI call surface. ``mempalace mcp`` only
prints MCP host setup instructions and exits -- there is no ``--call`` flag
in this or any published mempalace release. The real MCP server is the
separate ``mempalace-mcp`` console script, which speaks newline-delimited
JSON-RPC 2.0 over stdio. This test drives the palace exclusively through
``amplifier_module_tool_mempalace.scripts.memory_store``, which now speaks
that real surface correctly (see ``_call_mcp_tool`` and the fixed
``PalaceMemoryStore.file``) -- so this test exercises production code, not
a test-only reimplementation of the (previously nonexistent) call shape.

Skipped outside the DTU container (see tests/integration/conftest.py's
pytest_collection_modifyitems -- the same sentinel-path gate every test in
this directory uses).

Requires (provisioned by .amplifier/digital-twin-universe/profiles/memory-bundle-e2e.yaml):
  - tool-mempalace installed with the [substrate] extra
  - the amplifier-data gateway running on 127.0.0.1:8799, discovery line at
    /root/gateway.json.log
  - behaviors/mempalace.yaml patched with shadow_gateway: {enabled: true, ...}
    on both hooks-mempalace-capture and tool-mempalace
  - mempalace 3.5.0 (or any release providing the ``mempalace-mcp`` console
    script and the ``mempalace_add_drawer``/``mempalace_kg_add``/
    ``mempalace_search`` MCP tools)
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("amplifier_data")

sys.path.insert(
    0, "/workspace/amplifier-bundle-memory/modules/tool-mempalace"
)  # pragma: no cover - DTU-only path
from amplifier_module_tool_mempalace.scripts.amplifier_data_gateway import (  # noqa: E402
    GatewayClient,
)
from amplifier_module_tool_mempalace.scripts.memory_store import (  # noqa: E402
    AmplifierDataMemoryStore,
    DualWriteMemoryStore,
    PalaceMemoryStore,
    _call_mcp_tool,
)

_GATEWAY_LOG = Path("/root/gateway.json.log")


def _gateway_info() -> dict:
    assert _GATEWAY_LOG.exists(), (
        f"{_GATEWAY_LOG} not found -- the DTU provision step that starts the "
        "amplifier-data gateway (step 6.6) failed or was not run."
    )
    line = _GATEWAY_LOG.read_text(encoding="utf-8").strip().splitlines()[0]
    return json.loads(line)


def test_substrate_shadow_e2e(workspace_dir: Path) -> None:
    info = _gateway_info()
    base_url = info["url"]
    token_file = Path(info["token_file"])
    token = (
        token_file.read_text(encoding="utf-8").strip() if token_file.exists() else None
    )

    wing = "wing_e2e"
    room = "e2e-smoke"
    content = "substrate shadow e2e: verbatim decision content for the smoke test"

    # ---- Stage 1: file a drawer through the REAL DualWriteMemoryStore ----
    # primary=PalaceMemoryStore is the real palace, now correctly speaking MCP
    # stdio to mempalace-mcp (see memory_store._call_mcp_tool's docstring for
    # why the previous `mempalace mcp --call` invocation never worked).
    # shadow=AmplifierDataMemoryStore(base_url=..., token=...) is the real
    # amplifier-data gateway seam. fail_on_shadow_error=True turns a shadow
    # failure into a test failure instead of a silently-swallowed one, since
    # THIS test's whole point is proving the shadow path works.
    dual_store = DualWriteMemoryStore(
        primary=PalaceMemoryStore(added_by="test_substrate_shadow_e2e"),
        shadow=AmplifierDataMemoryStore(base_url=base_url, token=token),
        fail_on_shadow_error=True,
    )
    dual_store.file(
        wing=wing,
        room=room,
        content=content,
        source="test_substrate_shadow_e2e",
    )
    assert not dual_store.shadow_errors, (
        f"shadow write failed: {dual_store.shadow_errors}"
    )

    # Give the palace's chroma index and the shadow write a moment to land.
    time.sleep(1.0)

    client = GatewayClient(base_url, token)
    content_ref = client.write_cell(
        content.encode("utf-8")
    )  # content-addressed; idempotent

    # ---- Stage 2: attach an embedding + facts + KG triple through the gateway ----
    vector = [0.1, 0.2, 0.3, 0.4, 0.5]
    client.add_embedding(content_ref, vector)
    category_ref = client.write_cell(b"decision")
    client.assert_fact(content_ref, "has_category", category_ref)

    # KG triple: filed through the real palace KG tool (mempalace_kg_add, via
    # the same corrected _call_mcp_tool PalaceMemoryStore uses), then shadowed
    # to the substrate exactly like tool-mempalace's `_shadow_kg` does in
    # production (amplifier_module_tool_mempalace/__init__.py) -- calling
    # AmplifierDataMemoryStore.assert_kg directly here avoids standing up the
    # full async Tool.execute plumbing just to prove the same seam call.
    kg_result = _call_mcp_tool(
        "mempalace_kg_add",
        {"subject": "svc-e2e-a", "predicate": "depends_on", "object": "svc-e2e-b"},
    )
    assert not kg_result.get("error"), f"mempalace_kg_add failed: {kg_result}"
    shadow_store = AmplifierDataMemoryStore(base_url=base_url, token=token)
    shadow_store.assert_kg("svc-e2e-a", "depends_on", "svc-e2e-b")
    time.sleep(1.0)

    # ---- Stage 3: read it ALL back from the substrate ----
    assert client.regenerate(content_ref).payload == content.encode("utf-8")

    wing_ref = client.write_cell(f"wing:{wing}".encode())
    neighbors = client.graph_neighbors(content_ref, "scoped_to")
    assert wing_ref in neighbors, (
        "content cell is not scoped_to its wing in the substrate"
    )

    hits = client.query_vector(vector, 1, scope=wing_ref)
    assert hits and hits[0][0] == content_ref, (
        "vector self-retrieval failed through the gateway"
    )

    facts = client.query_facts(subject=content_ref, predicate="has_category")
    assert facts.success and len(facts.output) == 1, (
        "has_category fact missing in the substrate"
    )

    entity_a = client.write_cell(b"entity:svc-e2e-a")
    entity_b = client.write_cell(b"entity:svc-e2e-b")
    kg_facts = client.query_facts(subject=entity_a, predicate="depends_on")
    assert kg_facts.success and any(f.object == entity_b for f in kg_facts.output), (
        "tool-shadowed KG triple not visible via query_facts"
    )

    # ---- Stage 4: assert the shadow never harmed the palace primary ----
    # Real palace read via the same corrected MCP surface -- not faked.
    search_result = _call_mcp_tool(
        "mempalace_search", {"query": "verbatim decision content", "limit": 5}
    )
    assert not search_result.get("error"), f"mempalace_search failed: {search_result}"
    results_text = json.dumps(search_result)
    assert "e2e-smoke" in results_text or "substrate shadow e2e" in results_text, (
        f"palace search does not surface the drawer we just filed: {search_result}"
    )
