"""Concurrent daemon requests must not race inside the Rust file kernel.

The PyO3 ``RustFileKernel`` raises ``RuntimeError: Already borrowed`` when an
append overlaps an ``all_events`` read on another thread. The daemon serves
requests on a ThreadingHTTPServer and its read tools append (scope / anchor
cells), so overlapping requests used to fail with HTTP 400 (seen ~1.4k times
in live briefing event logs). ``_serialize_kernel_access`` fixes that.
"""

from __future__ import annotations

import threading

import pytest

amplifier_data = pytest.importorskip("amplifier_data")
if not getattr(amplifier_data, "RUST_AVAILABLE", False):  # pragma: no cover
    pytest.skip("durable Rust kernel not built", allow_module_level=True)

from amplifier_data import AmplifierStore  # noqa: E402

from amplifier_module_tool_memory.daemon import (  # noqa: E402
    _SerializedFileKernel,
    _serialize_kernel_access,
)


def _durable_store(tmp_path) -> AmplifierStore:  # noqa: ANN001
    store = AmplifierStore(path=str(tmp_path / "store.log"))
    for i in range(200):
        wb = store.write_batch()
        for j in range(50):
            wb.write_cell(f"cell {i} {j} ".encode() * 5)
        wb.commit()
    return store


def _hammer(store: AmplifierStore) -> list[str]:
    errors: list[str] = []

    def reader() -> None:
        for _ in range(8):
            try:
                store.kernel.all_events()
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

    def writer() -> None:
        for k in range(150):
            try:
                store.write_cell(f"w{k}".encode())
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

    threads = [threading.Thread(target=reader) for _ in range(2)]
    threads.append(threading.Thread(target=writer))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def test_unserialized_kernel_races(tmp_path) -> None:  # noqa: ANN001
    """Documents the underlying hazard (so the fix below is meaningful)."""
    store = _durable_store(tmp_path)
    errors = _hammer(store)
    if not errors:  # pragma: no cover - timing dependent
        pytest.skip("race not reproduced on this machine")
    assert any("Already borrowed" in e for e in errors)


def test_serialized_kernel_does_not_race(tmp_path) -> None:  # noqa: ANN001
    store = _durable_store(tmp_path)
    _serialize_kernel_access(store)
    assert isinstance(store.kernel._fk, _SerializedFileKernel)
    before = len(store.kernel.all_events())
    assert _hammer(store) == []
    assert len(store.kernel.all_events()) == before + 150


def test_serialize_is_idempotent_and_noop_for_memory_kernel(tmp_path) -> None:  # noqa: ANN001
    store = _durable_store(tmp_path)
    _serialize_kernel_access(store)
    wrapped = store.kernel._fk
    _serialize_kernel_access(store)
    assert store.kernel._fk is wrapped

    mem = AmplifierStore(record_access=False)  # in-memory kernel: no _fk
    _serialize_kernel_access(mem)  # must not raise
    mem.write_cell(b"x")


def test_warns_when_durable_kernel_cannot_be_wrapped(caplog) -> None:  # noqa: ANN001
    """Review optional 6: a renamed private attribute must not fail silently."""
    import logging
    import types

    DurableKernel = type("DurableKernel", (), {})  # noqa: N806 - mimics the class name
    store = types.SimpleNamespace(kernel=DurableKernel())
    with caplog.at_level(logging.WARNING):
        _serialize_kernel_access(store)
    assert "NOT serialized" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        _serialize_kernel_access(AmplifierStore(record_access=False))
    assert caplog.text == ""
