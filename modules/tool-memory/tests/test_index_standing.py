"""P3 (T3.1/T3.3, D19) -- L3 index and standing questions at the
``NativeMemoryStore`` level: derived vs curated index, curated-wins-until-
stale, index_set validation, the one-fold read contract, and the standing
question lifecycle including retraction.

Skipped entirely when amplifier-data is not installed (same convention as
the rest of this suite).
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("amplifier_data")

from amplifier_module_tool_memory.store import (
    _INDEX_STALE_AFTER_DEFAULT,
    NativeMemoryStore,
)


def _store() -> NativeMemoryStore:
    return NativeMemoryStore(record_access=False)


def _drawer(store: NativeMemoryStore, content: str, **kw: Any) -> Any:
    return store.file(
        wing=kw.pop("wing", "w"), room=kw.pop("room", "r"), content=content, **kw
    )


def _fact(store: NativeMemoryStore, text: str, source_ref: Any, **kw: Any) -> str:
    result = store.fact_add(
        text=text,
        fact_type=kw.pop("fact_type", "world"),
        source_refs=[source_ref],
        wing=kw.pop("wing", "w"),
        room=kw.pop("room", "r"),
        **kw,
    )
    return result["ref"]


class TestDerivedIndex:
    def test_empty_room_returns_empty_scope(self) -> None:
        store = _store()
        rows = store.index(wing="w", room="empty-room")
        assert rows == [
            {
                "scope": "room:empty-room",
                "abstract": "",
                "overview": "",
                "source": "derived",
                "built_from_count": 0,
                "current_count": 0,
                "pending_changes": 0,
                "built_at": None,
            }
        ]

    def test_no_facts_falls_back_to_latest_drawer_first_line(self) -> None:
        store = _store()
        _drawer(store, "first drawer\nmore content")
        # No filed_at difference guaranteed in the same second; the
        # fallback only needs to pick SOME drawer's first line when there
        # are no facts at all, which we assert here.
        rows = store.index(wing="w", room="r")
        assert len(rows) == 1
        row = rows[0]
        assert row["source"] == "derived"
        assert row["abstract"] in ("first drawer",)

    def test_ordering_by_proof_count_then_recency(self) -> None:
        store = _store()
        d1 = _drawer(store, "evidence about the package manager choice")
        d2 = _drawer(store, "more evidence about the package manager choice")
        weak_ref = _fact(
            store, "The repo uses uv as its package manager for installs.", d1
        )
        strong_ref_result = store.fact_add(
            text="The team standardized on uv over pip for all installs here.",
            fact_type="world",
            source_refs=[d1, d2],
            wing="w",
            room="r",
        )
        assert strong_ref_result["proof_count"] == 2

        rows = store.index(wing="w", room="r")
        assert len(rows) == 1
        abstract = rows[0]["abstract"]
        # The two-source fact (higher proof_count) must be ordered first.
        assert abstract.index("standardized on uv") < abstract.index(
            "uv as its package manager"
        )
        assert weak_ref  # keep referenced

    def test_abstract_and_overview_are_truncated(self) -> None:
        store = _store()
        d1 = _drawer(store, "evidence drawer for a very long fact statement here")
        long_text = "word " * 30 + "which is definitely a long fact about the system"
        store.fact_add(
            text=long_text, fact_type="world", source_refs=[d1], wing="w", room="r"
        )
        rows = store.index(wing="w", room="r")
        assert len(rows[0]["abstract"]) <= 256
        assert len(rows[0]["overview"]) <= 4000

    def test_index_wing_no_room_enumerates_every_room(self) -> None:
        store = _store()
        _drawer(store, "room one content", room="room-a")
        _drawer(store, "room two content", room="room-b")
        rows = store.index(wing="w")
        scopes = {r["scope"] for r in rows}
        assert scopes == {"room:room-a", "room:room-b"}


class TestCuratedVsDerived:
    def test_curated_wins_until_stale_then_derived(self) -> None:
        store = _store()
        d1 = _drawer(store, "evidence for the curated abstract")
        fact_ref = _fact(store, "The system uses a curated navigation index.", d1)

        written = store.index_set(
            scope="room:r",
            abstract="Curated abstract.",
            overview="Curated overview.",
            cites=[fact_ref],
        )
        assert written["built_from_count"] == 2  # 1 fact + 1 drawer

        rows = store.index(wing="w", room="r")
        assert rows[0]["source"] == "curated"
        assert rows[0]["abstract"] == "Curated abstract."
        assert rows[0]["pending_changes"] == 0

        # Add fewer than the stale gate's worth of new drawers: still curated.
        for i in range(_INDEX_STALE_AFTER_DEFAULT - 1):
            _drawer(store, f"minor addition {i}")
        rows = store.index(wing="w", room="r")
        assert rows[0]["source"] == "curated"

        # Cross the stale gate: falls back to derived.
        _drawer(store, "one more addition crossing the stale gate")
        _drawer(store, "another addition crossing the stale gate")
        rows = store.index(wing="w", room="r")
        assert rows[0]["source"] == "derived"


class TestIndexSetValidation:
    def test_abstract_over_length_rejected(self) -> None:
        store = _store()
        with pytest.raises(ValueError, match="abstract must be"):
            store.index_set(scope="room:r", abstract="x" * 257, overview="ok")

    def test_overview_over_length_rejected(self) -> None:
        store = _store()
        with pytest.raises(ValueError, match="overview must be"):
            store.index_set(scope="room:r", abstract="ok", overview="x" * 4001)

    def test_bad_scope_rejected(self) -> None:
        store = _store()
        with pytest.raises(ValueError, match="scope must be"):
            store.index_set(scope="wing:w", abstract="ok", overview="ok")

    def test_secret_shaped_abstract_rejected(self) -> None:
        store = _store()
        secret = "sk-ant-" + "a" * 30
        with pytest.raises(ValueError, match="secret-shaped"):
            store.index_set(scope="room:r", abstract=secret, overview="ok")

    def test_cite_must_be_existing_fact(self) -> None:
        store = _store()
        with pytest.raises(ValueError, match="not an existing fact"):
            store.index_set(
                scope="room:r", abstract="ok", overview="ok", cites=["deadbeef"]
            )

    def test_supersede_never_deletes_old_index_cell(self) -> None:
        store = _store()
        _drawer(store, "evidence")
        first = store.index_set(scope="room:r", abstract="v1", overview="v1")
        second = store.index_set(scope="room:r", abstract="v2", overview="v2")
        assert first["ref"] != second["ref"]
        rows = store.index(wing="w", room="r")
        assert rows[0]["abstract"] == "v2"
        # The old cell is still readable (never deleted) via its own ref.
        assert store._payload_text(first["ref"], None)


class TestOneFoldPerCall:
    def test_index_reads_the_log_exactly_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``index()`` must build exactly ONE :class:`_SearchFold` (which
        itself calls ``kernel.all_events()`` exactly once) regardless of how
        many rooms it enumerates -- never a per-room regenerate/re-fold.
        The Rust kernel's ``all_events`` attribute is read-only, so the
        fold-construction seam (``_fold_snapshot``) is what's counted."""
        store = _store()
        _drawer(store, "room one content", room="room-a")
        _drawer(store, "room two content", room="room-b")
        _drawer(store, "room three content", room="room-c")

        real_fold_snapshot = store._fold_snapshot
        calls = {"n": 0}

        def counting_fold_snapshot() -> Any:
            calls["n"] += 1
            return real_fold_snapshot()

        monkeypatch.setattr(store, "_fold_snapshot", counting_fold_snapshot)

        rows = store.index(wing="w")
        assert len(rows) == 3
        assert calls["n"] == 1

    def test_standing_reads_the_log_exactly_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _store()
        store.standing_add(question="what is our package manager", wing="w")
        store.standing_add(question="what is our test runner", wing="w")

        real_fold_snapshot = store._fold_snapshot
        calls = {"n": 0}

        def counting_fold_snapshot() -> Any:
            calls["n"] += 1
            return real_fold_snapshot()

        monkeypatch.setattr(store, "_fold_snapshot", counting_fold_snapshot)

        rows = store.standing(wing="w")
        assert len(rows) == 2
        assert calls["n"] == 1


class TestStandingLifecycle:
    def test_unanswered_question_is_stale(self) -> None:
        store = _store()
        added = store.standing_add(question="what is our package manager", wing="w")
        rows = store.standing(wing="w")
        assert len(rows) == 1
        assert rows[0]["question_ref"] == added["ref"]
        assert rows[0]["answer"] is None
        assert rows[0]["stale"] is True

    def test_idempotent_add(self) -> None:
        store = _store()
        a = store.standing_add(question="what is our package manager", wing="w")
        b = store.standing_add(question="what is our package manager", wing="w")
        assert a["ref"] == b["ref"]

    def test_answer_must_cite_existing_fact(self) -> None:
        store = _store()
        added = store.standing_add(question="q", wing="w")
        with pytest.raises(ValueError, match="not an existing fact"):
            store.standing_answer(
                question_ref=added["ref"], answer="an answer", cites=["deadbeef"]
            )

    def test_answer_rejects_secret_shaped_text(self) -> None:
        store = _store()
        added = store.standing_add(question="q", wing="w")
        secret = "sk-ant-" + "a" * 30
        with pytest.raises(ValueError, match="secret-shaped"):
            store.standing_answer(question_ref=added["ref"], answer=secret)

    def test_answered_question_is_not_stale(self) -> None:
        store = _store()
        d1 = _drawer(store, "evidence for package manager choice")
        fact_ref = _fact(store, "The repo uses uv as its package manager.", d1)
        added = store.standing_add(question="what is our package manager", wing="w")
        store.standing_answer(question_ref=added["ref"], answer="uv", cites=[fact_ref])
        rows = store.standing(wing="w")
        assert rows[0]["answer"] == "uv"
        assert rows[0]["stale"] is False
        assert rows[0]["cites"] == [fact_ref]

    def test_answer_supersede_never_deletes(self) -> None:
        store = _store()
        d1 = _drawer(store, "evidence")
        fact_ref = _fact(store, "The repo uses uv as its package manager.", d1)
        added = store.standing_add(question="q", wing="w")
        first = store.standing_answer(
            question_ref=added["ref"], answer="uv", cites=[fact_ref]
        )
        second = store.standing_answer(
            question_ref=added["ref"], answer="uv (confirmed)", cites=[fact_ref]
        )
        assert first["ref"] != second["ref"]
        rows = store.standing(wing="w")
        assert rows[0]["answer"] == "uv (confirmed)"
        assert store._payload_text(first["ref"], None)  # old cell still readable

    def test_retraction_marks_stale(self) -> None:
        store = _store()
        d1 = _drawer(store, "evidence for package manager choice")
        fact_result = store.fact_add(
            text="The repo uses uv as its package manager.",
            fact_type="world",
            source_refs=[d1],
            wing="w",
            room="r",
        )
        fact_ref = fact_result["ref"]
        added = store.standing_add(question="what is our package manager", wing="w")
        store.standing_answer(question_ref=added["ref"], answer="uv", cites=[fact_ref])

        # Supersede the cited fact -- it is no longer current.
        store.fact_add(
            text="The repo now uses pip as its package manager instead.",
            fact_type="correction",
            source_refs=[d1],
            wing="w",
            room="r",
            supersedes=fact_ref,
        )

        rows = store.standing(wing="w")
        assert rows[0]["stale"] is True
        assert fact_ref in rows[0]["stale_reasons"]
