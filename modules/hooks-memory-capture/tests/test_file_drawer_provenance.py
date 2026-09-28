"""Unit tests for _file_drawer's session_id/commit passthrough and its
TypeError fallback (T0.2 hook-side half + T0.3 daemon compatibility).

The store's file()/remember() path is being extended elsewhere (concurrent
work) with keyword-only session_id/commit params. _file_drawer must work
against BOTH the old and new signatures: pass the extras when accepted,
retry without them on a plain TypeError otherwise.
"""

from __future__ import annotations

from typing import Any

import amplifier_module_hooks_memory_capture as m


class _NewStyleClient:
    """Accepts session_id/commit (the post-upgrade daemon)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def remember(
        self,
        *,
        wing: str,
        room: str,
        content: str,
        source: str,
        category: str | None,
        session_id: str | None = None,
        commit: str | None = None,
    ) -> str:
        self.calls.append(
            {
                "wing": wing,
                "room": room,
                "content": content,
                "source": source,
                "category": category,
                "session_id": session_id,
                "commit": commit,
            }
        )
        return "ref-1"


class _OldStyleClient:
    """Does NOT accept session_id/commit (the pre-upgrade daemon) --
    passing them must raise TypeError, exactly like a real Python call
    against a narrower keyword-only signature."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def remember(
        self,
        *,
        wing: str,
        room: str,
        content: str,
        source: str,
        category: str | None,
    ) -> str:
        self.calls.append(
            {
                "wing": wing,
                "room": room,
                "content": content,
                "source": source,
                "category": category,
            }
        )
        return "ref-1"


def test_new_style_client_receives_session_id_and_commit(monkeypatch: Any) -> None:
    client = _NewStyleClient()
    monkeypatch.setattr(m, "ensure_daemon", lambda: client)

    m._file_drawer(
        "wing_x",
        "room_y",
        "content",
        "source",
        "decision",
        session_id="sess-9",
        commit="cafefeedcafefeedcafefeedcafefeedcafefeed",
    )

    assert len(client.calls) == 1
    assert client.calls[0]["session_id"] == "sess-9"
    assert client.calls[0]["commit"] == "cafefeedcafefeedcafefeedcafefeedcafefeed"


def test_old_style_client_falls_back_without_extras(monkeypatch: Any) -> None:
    client = _OldStyleClient()
    monkeypatch.setattr(m, "ensure_daemon", lambda: client)

    # Must not raise -- the TypeError from the first (kwargs-including)
    # attempt is caught and a second call is made without the extras.
    m._file_drawer(
        "wing_x",
        "room_y",
        "content",
        "source",
        "decision",
        session_id="sess-10",
        commit="deadbeef",
    )

    assert len(client.calls) == 1
    assert client.calls[0]["wing"] == "wing_x"
    assert client.calls[0]["content"] == "content"


def test_daemon_unavailable_raises_runtime_error(monkeypatch: Any) -> None:
    monkeypatch.setattr(m, "ensure_daemon", lambda: None)

    try:
        m._file_drawer("w", "r", "c", "s", None)
    except RuntimeError as exc:
        assert "daemon" in str(exc).lower()
    else:  # pragma: no cover - defensive
        raise AssertionError("expected RuntimeError when daemon is unavailable")
