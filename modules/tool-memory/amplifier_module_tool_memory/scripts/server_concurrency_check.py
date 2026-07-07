"""
server_concurrency_check — verify the single-writer companion server under real
multi-PROCESS contention (the blocker memory raised: many sessions -> one store).

Coordinator: starts an AmplifierStore-backed companion server, spawns N worker
SUBPROCESSES that each write M unique drawers + 1 shared drawer through a
``RemoteStore``, then verifies integrity:

  * all N*M unique writes landed (no lost writes under contention),
  * the shared drawer converged to ONE ref across all workers (concurrent,
    content-addressed dedup is safe under the single-writer lock),
  * every ref regenerates byte-for-byte equal to the content that produced it.

Run:
    memory-daemon-concurrency-check [--workers 6] [--per-worker 25]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

_SHARED = "SHARED-DRAWER-shared-content-for-dedup"


def _worker(url: str, wid: int, count: int) -> int:
    from amplifier_module_tool_memory.store import (
        NativeMemoryStore,
    )

    store = NativeMemoryStore(base_url=url)
    pairs: list[tuple[str, str]] = []
    for i in range(count):
        content = f"w{wid}-d{i}: decided to keep drawer {wid}.{i} verbatim 世界"
        store.file(
            wing=f"wing_{wid}", room="concurrency", content=content, category="decision"
        )
        pairs.append((store.filed[-1]["ref"], content))  # type: ignore[arg-type]
    store.file(wing="wing_shared", room="concurrency", content=_SHARED)
    shared_ref = store.filed[-1]["ref"]
    sys.stdout.write(json.dumps({"wid": wid, "pairs": pairs, "shared_ref": shared_ref}))
    return 0


def _run_coordinator(workers: int, per_worker: int) -> dict[str, object]:
    import tempfile

    from amplifier_data import AmplifierStore
    from amplifier_data import server as srv
    from amplifier_data.client import RemoteStore

    tmp = tempfile.mkdtemp()
    backing = AmplifierStore(path=str(Path(tmp) / "srv.ampd"), record_access=False)
    httpd = srv.make_server(backing, "127.0.0.1", 0)
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    env = dict(os.environ)
    mod_dir = str(Path(__file__).resolve().parents[2])  # modules/tool-memory
    env["PYTHONPATH"] = mod_dir + os.pathsep + env.get("PYTHONPATH", "")

    procs = []
    for wid in range(workers):
        procs.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "amplifier_module_tool_memory.scripts.server_concurrency_check",
                    "--worker",
                    "--url",
                    url,
                    "--wid",
                    str(wid),
                    "--count",
                    str(per_worker),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
            )
        )

    all_pairs: list[tuple[str, str]] = []
    shared_refs: set[str] = set()
    failures: list[str] = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        if p.returncode != 0:
            failures.append(f"worker rc={p.returncode}: {err.strip()[:200]}")
            continue
        data = json.loads(out)
        all_pairs.extend((r, c) for r, c in data["pairs"])
        shared_refs.add(data["shared_ref"])

    # verify byte-identity of every landed write via a fresh RemoteStore client
    verify = RemoteStore(url)
    unique_refs = {r for r, _ in all_pairs}
    byte_ok = 0
    for ref, content in all_pairs:
        if verify.regenerate(ref).payload == content.encode("utf-8"):
            byte_ok += 1

    httpd.shutdown()

    expected_unique = workers * per_worker
    return {
        "workers": workers,
        "per_worker": per_worker,
        "expected_unique_writes": expected_unique,
        "landed_unique_writes": len(unique_refs),
        "byte_identical": byte_ok,
        "shared_ref_count": len(shared_refs),  # MUST be 1 (concurrent dedup)
        "worker_failures": failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memory-daemon-concurrency-check")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--url")
    parser.add_argument("--wid", type=int, default=0)
    parser.add_argument("--count", type=int, default=25)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--per-worker", type=int, default=25)
    args = parser.parse_args(argv)

    if args.worker:
        return _worker(args.url or "", args.wid, args.count)

    report = _run_coordinator(args.workers, args.per_worker)
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    ok = (
        report["landed_unique_writes"] == report["expected_unique_writes"]
        and report["byte_identical"] == report["expected_unique_writes"]
        and report["shared_ref_count"] == 1
        and not report["worker_failures"]
    )
    sys.stdout.write("PASS\n" if ok else "FAIL\n")
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
