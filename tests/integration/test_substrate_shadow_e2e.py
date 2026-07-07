"""
Substrate shadow e2e smoke (DTU-gated, \u00a77 of
docs/plans/2026-07-07-substrate-adapter-completion-design.md).

Exercises the completed amplifier-data seam end-to-end as a user would,
inside the memory-bundle-e2e DTU container: file a drawer through the
shadow, attach an embedding + facts + a KG triple through the gateway, read
it ALL back from the substrate (palace untouched as read primary), and
assert the shadow never harmed the palace.

Skipped outside the DTU container (see tests/integration/conftest.py's
pytest_collection_modifyitems -- the same sentinel-path gate every test in
this directory uses).

Requires (provisioned by .amplifier/digital-twin-universe/profiles/memory-bundle-e2e.yaml):
  - tool-mempalace installed with the [substrate] extra
  - the amplifier-data gateway running on 127.0.0.1:8799, discovery line at
    /root/gateway.json.log
  - behaviors/mempalace.yaml patched with shadow_gateway: {enabled: true, ...}
    on both hooks-mempalace-capture and tool-mempalace
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("amplifier_data")

sys.path.insert(
    0, "/workspace/amplifier-bundle-memory/modules/tool-mempalace"
)  # pragma: no cover - DTU-only path
from amplifier_module_tool_mempalace.scripts.amplifier_data_gateway import (  # noqa: E402
    GatewayClient,
)

_GATEWAY_LOG = Path("/root/gateway.json.log")


def _gateway_info() -> dict:
    assert _GATEWAY_LOG.exists(), (
        f"{_GATEWAY_LOG} not found -- the DTU provision step that starts the "
        "amplifier-data gateway (step 6.6) failed or was not run."
    )
    line = _GATEWAY_LOG.read_text(encoding="utf-8").strip().splitlines()[0]
    return json.loads(line)


def _mcp_call(tool: str, arguments: dict) -> dict:
    payload = json.dumps({"tool": tool, "arguments": arguments})
    result = subprocess.run(
        ["mempalace", "mcp", "--call", payload],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"mempalace mcp --call {tool} failed (rc={result.returncode}).\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"raw": result.stdout}


def test_substrate_shadow_e2e(workspace_dir: Path) -> None:
    info = _gateway_info()
    base_url = info["url"]
    token_file = Path(info["token_file"])
    token = token_file.read_text(encoding="utf-8").strip() if token_file.exists() else None
    client = GatewayClient(base_url, token)

    wing = "wing_e2e"
    content = "substrate shadow e2e: verbatim decision content for the smoke test"

    # ---- Stage 1: file a drawer through the shadow (palace primary write) ----
    add_result = _mcp_call(
        "mempalace_add_drawer",
        {
            "wing": wing,
            "room": "e2e-smoke",
            "content": content,
            "source_file": "test_substrate_shadow_e2e",
            "added_by": "test_substrate_shadow_e2e",
        },
    )
    assert not add_result.get("error"), f"mempalace_add_drawer failed: {add_result}"

    # Give the async shadow (drain thread on the capture path, or immediate on
    # the direct add_drawer path if wired) a moment to land.
    import time

    time.sleep(1.0)

    content_ref = client.write_cell(content.encode("utf-8"))  # content-addressed; idempotent

    # ---- Stage 2: attach an embedding + facts + KG triple through the gateway ----
    vector = [0.1, 0.2, 0.3, 0.4, 0.5]
    client.add_embedding(content_ref, vector)
    category_ref = client.write_cell(b"decision")
    client.assert_fact(content_ref, "has_category", category_ref)

    kg_result = _mcp_call(
        "mempalace_kg_add",
        {"subject": "svc-e2e-a", "predicate": "depends_on", "object": "svc-e2e-b"},
    )
    assert not kg_result.get("error"), f"mempalace_kg_add failed: {kg_result}"
    time.sleep(1.0)

    # ---- Stage 3: read it ALL back from the substrate ----
    assert client.regenerate(content_ref).payload == content.encode("utf-8")

    wing_ref = client.write_cell(f"wing:{wing}".encode())
    neighbors = client.graph_neighbors(content_ref, "scoped_to")
    assert wing_ref in neighbors, "content cell is not scoped_to its wing in the substrate"

    hits = client.query_vector(vector, 1, scope=wing_ref)
    assert hits and hits[0][0] == content_ref, "vector self-retrieval failed through the gateway"

    facts = client.query_facts(subject=content_ref, predicate="has_category")
    assert facts.success and len(facts.output) == 1, "has_category fact missing in the substrate"

    entity_a = client.write_cell(b"entity:svc-e2e-a")
    entity_b = client.write_cell(b"entity:svc-e2e-b")
    kg_facts = client.query_facts(subject=entity_a, predicate="depends_on")
    assert kg_facts.success and any(
        f.object == entity_b for f in kg_facts.output
    ), "tool-shadowed KG triple not visible via query_facts"

    # ---- Stage 4: assert the shadow never harmed the primary ----
    search_result = _mcp_call("mempalace_search", {"query": "verbatim decision content", "limit": 5})
    assert not search_result.get("error"), f"mempalace_search failed: {search_result}"
    results_text = json.dumps(search_result)
    assert "e2e-smoke" in results_text or "substrate shadow e2e" in results_text, (
        f"palace search does not surface the drawer we just filed: {search_result}"
    )
