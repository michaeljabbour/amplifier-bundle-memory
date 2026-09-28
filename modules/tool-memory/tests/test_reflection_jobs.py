"""T2.5/D10 -- durable reflection job queue at the ``NativeMemoryStore``
level: add -> list -> done lifecycle, idempotency, span redaction, and the
done-twice error.

Skipped entirely when amplifier-data is not installed (same convention as
the rest of this suite).
"""

from __future__ import annotations

import pytest

pytest.importorskip("amplifier_data")

from amplifier_module_tool_memory.store import NativeMemoryStore


def _store() -> NativeMemoryStore:
    return NativeMemoryStore(record_access=False)


class TestAddListDoneLifecycle:
    def test_full_lifecycle(self) -> None:
        store = _store()
        job = store.reflection_job_add(
            span_text="the user said to use uv instead of pip going forward",
            session_id="sess-1",
            trigger="context:compaction",
            wing="w",
        )
        assert job["state"] == "pending"
        assert job["span_ref"]
        assert job["job_ref"]

        pending = store.reflection_jobs(wing="w")
        assert [j["job_ref"] for j in pending] == [job["job_ref"]]
        assert pending[0]["span_text"] == (
            "the user said to use uv instead of pip going forward"
        )
        assert pending[0]["session_id"] == "sess-1"
        assert pending[0]["trigger"] == "context:compaction"

        done = store.reflection_job_done(job_ref=job["job_ref"], fact_refs=["deadbeef"])
        assert done["state"] == "done"
        assert done["fact_refs"] == ["deadbeef"]

        assert store.reflection_jobs(state="pending", wing="w") == []
        done_rows = store.reflection_jobs(state="done", wing="w")
        assert [j["job_ref"] for j in done_rows] == [job["job_ref"]]

    def test_noop_done_records_note(self) -> None:
        store = _store()
        job = store.reflection_job_add(
            span_text="nothing durable happened in this span at all really",
            session_id=None,
            trigger="session:end",
            wing="w",
        )
        done = store.reflection_job_done(
            job_ref=job["job_ref"], noop=True, note="nothing worth recording"
        )
        assert done["noop"] is True
        assert done["fact_refs"] == []


class TestIdempotency:
    def test_same_span_session_trigger_yields_same_job_no_state_reset(self) -> None:
        store = _store()
        first = store.reflection_job_add(
            span_text="identical span text for idempotency check",
            session_id="s1",
            trigger="session:end",
            wing="w",
        )
        second = store.reflection_job_add(
            span_text="identical span text for idempotency check",
            session_id="s1",
            trigger="session:end",
            wing="w",
        )
        assert first["job_ref"] == second["job_ref"]
        assert first["span_ref"] == second["span_ref"]
        assert second["state"] == "pending"

        store.reflection_job_done(job_ref=first["job_ref"])
        # Re-adding after done must NOT resurrect it as pending.
        third = store.reflection_job_add(
            span_text="identical span text for idempotency check",
            session_id="s1",
            trigger="session:end",
            wing="w",
        )
        assert third["job_ref"] == first["job_ref"]
        assert third["state"] == "done"


class TestSpanRedaction:
    def test_span_text_is_redacted_before_storage(self) -> None:
        store = _store()
        secret = "sk-ant-" + "a" * 30
        job = store.reflection_job_add(
            span_text=f"the key is {secret} please remember it",
            session_id=None,
            trigger="session:end",
            wing="w",
        )
        rows = store.reflection_jobs(wing="w")
        assert secret not in rows[0]["span_text"]
        assert job["redactions"].get("anthropic_key") == 1


class TestDoneTwiceError:
    def test_done_twice_raises(self) -> None:
        store = _store()
        job = store.reflection_job_add(
            span_text="a span that will be closed exactly once please",
            session_id=None,
            trigger="session:end",
            wing="w",
        )
        store.reflection_job_done(job_ref=job["job_ref"])
        with pytest.raises(ValueError, match="not pending"):
            store.reflection_job_done(job_ref=job["job_ref"])
