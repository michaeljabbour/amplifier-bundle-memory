"""T2.1/T2.2/T2.7 -- L2 facts (design \u00a72/\u00a74, PROVENANCE.md D12) at the
``NativeMemoryStore`` level: provenance, secret rejection, dedup, supersede,
predicate auto-supersede, conflicts_with tension, the atomic batch path, and
``facts()``/``search()`` listing.

Skipped entirely when amplifier-data is not installed (same convention as
the rest of this suite).
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("amplifier_data")

from amplifier_module_tool_memory.store import NativeMemoryStore

_GOOD_TEXT = (
    "The repository uses uv as its package manager instead of pip for installs."
)


def _store() -> NativeMemoryStore:
    return NativeMemoryStore(record_access=False)


def _drawer(store: NativeMemoryStore, content: str, **kw: Any) -> Any:
    return store.file(
        wing=kw.pop("wing", "w"), room=kw.pop("room", "r"), content=content, **kw
    )


class TestProvenanceRequired:
    def test_missing_source_refs_rejected(self) -> None:
        store = _store()
        with pytest.raises(ValueError, match="at least one source_ref"):
            store.fact_add(text=_GOOD_TEXT, fact_type="world", source_refs=[], wing="w")

    def test_source_ref_must_be_an_existing_drawer(self) -> None:
        store = _store()
        with pytest.raises(ValueError, match="not a known drawer"):
            store.fact_add(
                text=_GOOD_TEXT,
                fact_type="world",
                source_refs=["deadbeefdeadbeef"],
                wing="w",
            )

    def test_a_fact_ref_is_not_a_valid_source_ref(self) -> None:
        store = _store()
        d1 = _drawer(store, "evidence one about uv")
        fact = store.fact_add(
            text=_GOOD_TEXT, fact_type="world", source_refs=[d1], wing="w"
        )
        with pytest.raises(ValueError, match="not a known drawer"):
            store.fact_add(
                text="A second unrelated fact citing a fact instead of a drawer here.",
                fact_type="world",
                source_refs=[fact["ref"]],
                wing="w",
            )


class TestSecretRejection:
    def test_secret_shaped_text_is_rejected(self) -> None:
        store = _store()
        d1 = _drawer(store, "some evidence drawer content here")
        secret_text = (
            "the api key is sk-ant-" + "a" * 30 + " for this integration testing"
        )
        with pytest.raises(ValueError, match="secret-shaped"):
            store.fact_add(
                text=secret_text, fact_type="world", source_refs=[d1], wing="w"
            )


class TestFactTypeAndLengthValidation:
    def test_invalid_fact_type_rejected(self) -> None:
        store = _store()
        d1 = _drawer(store, "some evidence drawer content here")
        with pytest.raises(ValueError, match="fact_type must be one of"):
            store.fact_add(
                text=_GOOD_TEXT, fact_type="bogus", source_refs=[d1], wing="w"
            )

    def test_too_short_text_rejected(self) -> None:
        store = _store()
        d1 = _drawer(store, "some evidence drawer content here")
        with pytest.raises(ValueError, match="3-120 words"):
            store.fact_add(
                text="too short", fact_type="world", source_refs=[d1], wing="w"
            )

    def test_too_long_text_rejected(self) -> None:
        store = _store()
        d1 = _drawer(store, "some evidence drawer content here")
        long_text = " ".join(["word"] * 200)
        with pytest.raises(ValueError, match="3-120 words"):
            store.fact_add(
                text=long_text, fact_type="world", source_refs=[d1], wing="w"
            )


class TestDedup:
    def test_identical_fact_increments_proof_count_not_duplicated(self) -> None:
        store = _store()
        d1 = _drawer(store, "evidence one about uv")
        d2 = _drawer(store, "evidence two about uv")

        first = store.fact_add(
            text=_GOOD_TEXT, fact_type="world", source_refs=[d1], wing="w"
        )
        assert first["deduped"] is False
        assert first["proof_count"] == 1

        second = store.fact_add(
            text=_GOOD_TEXT, fact_type="world", source_refs=[d1, d2], wing="w"
        )
        assert second["ref"] == first["ref"]
        assert second["deduped"] is True
        assert second["proof_count"] == 2

        rows = store.facts(wing="w")
        assert len(rows) == 1
        assert rows[0]["proof_count"] == 2


class TestSupersede:
    def test_explicit_supersede_chain(self) -> None:
        store = _store()
        d1 = _drawer(store, "evidence about test runner")
        old = store.fact_add(
            text="The project's test runner is pytest run via a Makefile target.",
            fact_type="world",
            source_refs=[d1],
            wing="w",
        )
        new = store.fact_add(
            text="The project's test runner switched to nox instead of a raw Makefile.",
            fact_type="world",
            source_refs=[d1],
            wing="w",
            supersedes=old["ref"],
        )
        assert old["ref"] in new["superseded"]

        rows = {r["ref"]: r for r in store.facts(wing="w", current_only=False)}
        assert rows[old["ref"]]["current"] is False
        assert rows[old["ref"]]["superseded_by"] == [new["ref"]]
        assert rows[new["ref"]]["current"] is True

        current_rows = store.facts(wing="w", current_only=True)
        assert [r["ref"] for r in current_rows] == [new["ref"]]

    def test_supersedes_must_reference_an_existing_fact(self) -> None:
        store = _store()
        d1 = _drawer(store, "evidence about test runner")
        with pytest.raises(ValueError, match="not an existing fact"):
            store.fact_add(
                text=_GOOD_TEXT,
                fact_type="world",
                source_refs=[d1],
                wing="w",
                supersedes=d1,
            )


class TestPredicateAutoSupersede:
    def test_same_predicate_same_wing_auto_superseded(self) -> None:
        store = _store()
        d1 = _drawer(store, "evidence about package manager")
        first = store.fact_add(
            text="The repository's package manager is uv for this project.",
            fact_type="world",
            source_refs=[d1],
            wing="w",
            predicate="package_manager",
        )
        second = store.fact_add(
            text="The repository's package manager switched to poetry recently.",
            fact_type="world",
            source_refs=[d1],
            wing="w",
            predicate="package_manager",
        )
        assert first["ref"] in second["superseded"]
        current = store.facts(wing="w", current_only=True)
        assert [r["ref"] for r in current] == [second["ref"]]

    def test_same_predicate_different_wing_not_superseded(self) -> None:
        store = _store()
        d1 = _drawer(store, "evidence in wing one", wing="w1")
        d2 = _drawer(store, "evidence in wing two", wing="w2")
        first = store.fact_add(
            text="Wing one's package manager is uv for all installs.",
            fact_type="world",
            source_refs=[d1],
            wing="w1",
            predicate="package_manager",
        )
        second = store.fact_add(
            text="Wing two's package manager is poetry for all installs.",
            fact_type="world",
            source_refs=[d2],
            wing="w2",
            predicate="package_manager",
        )
        assert second["superseded"] == []
        assert store.facts(wing="w1", current_only=True)[0]["ref"] == first["ref"]
        assert store.facts(wing="w2", current_only=True)[0]["ref"] == second["ref"]


class TestConflictsWith:
    def test_conflicts_with_records_a_tension_both_stay_current(self) -> None:
        store = _store()
        d1 = _drawer(store, "evidence about default branch name")
        a = store.fact_add(
            text="The default branch for this repository is named main.",
            fact_type="world",
            source_refs=[d1],
            wing="w",
        )
        b = store.fact_add(
            text="The default branch for this repository is named trunk.",
            fact_type="world",
            source_refs=[d1],
            wing="w",
            conflicts_with=a["ref"],
        )
        assert b["tension"] is not None

        rows = {r["ref"]: r for r in store.facts(wing="w", current_only=True)}
        assert a["ref"] in rows
        assert b["ref"] in rows

    def test_conflicts_with_must_reference_an_existing_fact(self) -> None:
        store = _store()
        d1 = _drawer(store, "evidence about default branch name")
        with pytest.raises(ValueError, match="not an existing fact"):
            store.fact_add(
                text=_GOOD_TEXT,
                fact_type="world",
                source_refs=[d1],
                wing="w",
                conflicts_with=d1,
            )


class TestAtomicBatchPath:
    def test_fact_add_uses_one_atomic_batch_when_supported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _store()
        d1 = _drawer(store, "evidence for atomic path")
        assert store._supports_atomic_update() is True

        calls: list[str] = []
        real_write_batch = store.store.write_batch  # type: ignore[attr-defined]

        def _spy() -> Any:
            calls.append("write_batch")
            return real_write_batch()

        monkeypatch.setattr(store.store, "write_batch", _spy)  # type: ignore[attr-defined]
        store.fact_add(text=_GOOD_TEXT, fact_type="world", source_refs=[d1], wing="w")
        assert calls == ["write_batch"], (
            "fact_add's fresh-write path must open exactly ONE WriteBatch"
        )


class TestFactsListing:
    def test_query_ranks_by_lexical_match(self) -> None:
        store = _store()
        d1 = _drawer(store, "evidence one")
        store.fact_add(
            text="The build system uses bazel for compiling every target here.",
            fact_type="world",
            source_refs=[d1],
            wing="w",
        )
        store.fact_add(
            text="The deployment pipeline runs entirely through GitHub Actions now.",
            fact_type="world",
            source_refs=[d1],
            wing="w",
        )
        rows = store.facts(wing="w", query="bazel build")
        assert rows[0]["text"].startswith("The build system uses bazel")

    def test_since_until_filters_by_recorded_span(self) -> None:
        store = _store()
        d1 = _drawer(store, "evidence for temporal filter")
        store.fact_add(
            text="A fact recorded for the temporal filter test window here today.",
            fact_type="world",
            source_refs=[d1],
            wing="w",
        )
        # A window entirely before "now" excludes every fact filed at import time.
        rows = store.facts(
            wing="w", since="2000-01-01T00:00:00", until="2000-01-02T00:00:00"
        )
        assert rows == []


class TestSearchLayers:
    def test_search_returns_fact_hits_with_layer_and_derived_from(self) -> None:
        store = _store()
        d1 = _drawer(store, "the repo uses uv not pip for installs")
        fact = store.fact_add(
            text=_GOOD_TEXT, fact_type="world", source_refs=[d1], wing="w", room="r"
        )
        results = store.search(
            None, 5, wing="w", room="r", lexical_query="package manager"
        )
        fact_hits = [r for r in results if r["layer"] == "fact"]
        assert fact_hits, "expected at least one fact-layer hit"
        hit = fact_hits[0]
        assert hit["ref"] == fact["ref"]
        assert hit["derived_from"] == [d1]
        assert "kind" not in hit["content"]  # extracted text, not raw JSON

    def test_layers_drawer_only_excludes_facts(self) -> None:
        store = _store()
        d1 = _drawer(store, "the repo uses uv not pip for installs")
        store.fact_add(
            text=_GOOD_TEXT, fact_type="world", source_refs=[d1], wing="w", room="r"
        )
        results = store.search(
            None,
            5,
            wing="w",
            room="r",
            lexical_query="uv pip",
            layers=["drawer"],
        )
        assert all(r["layer"] == "drawer" for r in results)
        assert any(r["ref"] == d1 for r in results)

    def test_non_current_fact_excluded_from_search(self) -> None:
        store = _store()
        d1 = _drawer(store, "evidence about test runner search visibility")
        old = store.fact_add(
            text="The project's test runner is pytest run via a Makefile target.",
            fact_type="world",
            source_refs=[d1],
            wing="w",
            room="r",
        )
        new = store.fact_add(
            text="The project's test runner switched to nox instead of a raw Makefile.",
            fact_type="world",
            source_refs=[d1],
            wing="w",
            room="r",
            supersedes=old["ref"],
        )
        results = store.search(
            None, 10, wing="w", room="r", lexical_query="test runner"
        )
        refs = {r["ref"] for r in results}
        assert new["ref"] in refs
        assert old["ref"] not in refs
