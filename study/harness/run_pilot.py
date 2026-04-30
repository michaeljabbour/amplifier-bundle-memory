"""
Pilot driver: runs N paired trials interleaved across two DTUs.

For each trial index k in 1..N:
    For each arm in (with-memory, without-memory) (order randomized per pair):
        1. Reset palace if arm=with-memory (idempotent on without-memory)
        2. Make ephemeral workspace inside the DTU
        3. amplifier run S1 (priming) - capture JSON response
        4. Reset workspace (delete ephemeral dir, palace persists)
        5. amplifier run S2 (target) - capture JSON response
        6. Score S2 with frozen scorer
        7. Append to JSONL log

Interleaving is achieved by alternating which DTU we hit first per pair —
this distributes any provider drift evenly across arms.

Outputs:
    trials/results.jsonl   — one line per trial with full per-constraint scoring
    trials/<arm>/sN_<id>.json — raw amplifier response for each session
    trials/<arm>/score_<id>.json — scorer output for each S2

Resumable: if results.jsonl already exists, skip trials whose IDs are present.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
STUDY = HERE.parent
PROMPTS = json.loads((HERE / "prompts.json").read_text())
SCORER = HERE / "scorer.py"

WITH_DTU = "memory-bundle-e2e"
WITHOUT_DTU = "study-without-memory"

ARMS = {
    "with-memory": WITH_DTU,
    "without-memory": WITHOUT_DTU,
}


def dtu_exec(dtu_id: str, cmd: str, timeout: int = 600) -> dict[str, Any]:
    """Run a shell command inside a DTU and return the parsed JSON response."""
    full = [
        "amplifier-digital-twin",
        "exec",
        dtu_id,
        "--",
        "bash",
        "-lc",
        cmd,
    ]
    proc = subprocess.run(
        full, capture_output=True, text=True, timeout=timeout, check=False
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {
            "id": dtu_id,
            "command": cmd,
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "_parse_error": True,
        }


def amplifier_run_inside_dtu(
    dtu_id: str, workspace: str, prompt: str, timeout: int = 240
) -> dict[str, Any]:
    """Run `amplifier run --output-format json` inside the DTU with the given
    prompt. Returns parsed amplifier JSON or the raw exec envelope on failure.

    The shell-quoting strategy: write the prompt to a file inside the DTU, then
    have amplifier read it via xargs/cat.  This avoids any escaping issue with
    quotes/newlines in the prompt body.
    """
    # Encode prompt as base64 to avoid every shell escaping pitfall.
    import base64

    b64 = base64.b64encode(prompt.encode("utf-8")).decode("ascii")
    # --mode single disables tool use and produces a single LLM call. This
    # eliminates exploration / sub-agent delegation as a confound and reduces
    # per-trial wall-clock from ~150s to ~15s.
    cmd = (
        f"set -e; "
        f"cd {workspace}; "
        f"echo {b64} | base64 -d > /tmp/prompt.txt; "
        f"amplifier run --mode single --output-format json \"$(cat /tmp/prompt.txt)\" 2>&1"
    )
    envelope = dtu_exec(dtu_id, cmd, timeout=timeout)
    raw_stdout = envelope.get("stdout", "")
    # The amplifier --output-format json result is the LAST JSON object in
    # stdout. Tool-use modes prepend a lot of progress output (thinking
    # blocks, tool calls, token usage) before the final JSON. The final JSON
    # always has a top-level `"status"` field; locate from the last newline
    # that starts a JSON object containing it.
    candidates = []
    # Cheap heuristic: split on `\n{\n  "status"` markers.
    marker = '\n{\n  "status"'
    idx = raw_stdout.rfind(marker)
    if idx >= 0:
        candidates.append(idx + 1)  # skip the leading newline
    # Fallback: any `\n{\n` (less reliable).
    if not candidates:
        for m in range(len(raw_stdout)):
            if raw_stdout[m : m + 3] == "\n{\n":
                candidates.append(m + 1)

    for start in candidates:
        # Try parsing from `start` to the end. amplifier's JSON is well-formed
        # and ends the file (no trailing content).
        try:
            return json.loads(raw_stdout[start:])
        except json.JSONDecodeError:
            continue
        except Exception:
            continue

    return {
        "amplifier_response_parse_failed": True,
        "envelope": envelope,
    }


def workspace_for(trial_id: str) -> str:
    """Workspace must be under /workspace so the with-memory briefing hook can
    walk up to /workspace/project-context/. The without-memory DTU has no
    project-context dir, so the path is harmless there."""
    return f"/workspace/work_{trial_id}"


def reset_arm_state(arm: str, dtu_id: str, trial_id: str) -> None:
    """Reset palace (with-memory only) and workspace dir."""
    workspace = workspace_for(trial_id)
    if arm == "with-memory":
        # reset-palace is the script installed by the memory-bundle-e2e profile.
        # The seed palace is preserved (it is restored from /root/.mempalace-seed).
        dtu_exec(dtu_id, "reset-palace", timeout=30)
    # Reset workspace (idempotent in both arms). Important: do NOT delete
    # /workspace/project-context/ — the briefing hook depends on it in the
    # with-memory arm.
    dtu_exec(dtu_id, f"rm -rf {workspace} && mkdir -p {workspace}", timeout=30)


def reset_workspace_only(dtu_id: str, trial_id: str) -> str:
    """Delete the trial workspace and recreate empty. Used between S1 and S2
    so the model in S2 cannot read S1's filesystem state. Palace and
    project-context persist."""
    workspace = workspace_for(trial_id)
    dtu_exec(dtu_id, f"rm -rf {workspace} && mkdir -p {workspace}", timeout=30)
    return workspace


def score_response(text: str) -> dict[str, Any]:
    """Pipe the response text into scorer.py and return the result dict."""
    proc = subprocess.run(
        [sys.executable, str(SCORER), "/dev/stdin"],
        input=json.dumps({"response": text}),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        return {
            "_scorer_failed": True,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "returncode": proc.returncode,
        }
    return json.loads(proc.stdout)


def run_one_trial(arm: str, dtu_id: str, trial_id: str, trials_dir: Path) -> dict[str, Any]:
    """Run a single trial in the given arm. Returns a result dict."""
    arm_dir = trials_dir / arm
    arm_dir.mkdir(parents=True, exist_ok=True)

    started_at = time.time()
    s1_prompt = PROMPTS["uc1_constraint_adherence"]["S1"]
    s2_prompt = PROMPTS["uc1_constraint_adherence"]["S2"]

    # 1. Reset arm state and create fresh workspace
    reset_arm_state(arm, dtu_id, trial_id)
    workspace = workspace_for(trial_id)

    # 2. Run S1 (priming). Captured but not scored.
    s1 = amplifier_run_inside_dtu(dtu_id, workspace, s1_prompt, timeout=300)
    (arm_dir / f"s1_{trial_id}.json").write_text(json.dumps(s1, indent=2))

    # 3. Reset workspace (palace persists in with-memory arm)
    reset_workspace_only(dtu_id, trial_id)

    # 4. Run S2 (target). New session id; in-session memory does not span here.
    s2 = amplifier_run_inside_dtu(dtu_id, workspace, s2_prompt, timeout=300)
    (arm_dir / f"s2_{trial_id}.json").write_text(json.dumps(s2, indent=2))

    # 5. Score S2
    s2_text = s2.get("response") if isinstance(s2, dict) else None
    if not isinstance(s2_text, str):
        score = {"_scoring_skipped": True, "reason": "no response field in s2"}
    else:
        score = score_response(s2_text)
    (arm_dir / f"score_{trial_id}.json").write_text(json.dumps(score, indent=2))

    finished_at = time.time()

    # 6. Cleanup workspace inside DTU
    dtu_exec(dtu_id, f"rm -rf {workspace}", timeout=30)

    return {
        "trial_id": trial_id,
        "arm": arm,
        "dtu_id": dtu_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_s": finished_at - started_at,
        "s1_session_id": s1.get("session_id") if isinstance(s1, dict) else None,
        "s2_session_id": s2.get("session_id") if isinstance(s2, dict) else None,
        "s1_model": s1.get("model") if isinstance(s1, dict) else None,
        "s2_model": s2.get("model") if isinstance(s2, dict) else None,
        "s2_response_present": isinstance(s2_text, str),
        "score": score,
    }


def existing_trial_ids(jsonl: Path) -> set[str]:
    if not jsonl.exists():
        return set()
    ids: set[str] = set()
    for line in jsonl.read_text().splitlines():
        if not line.strip():
            continue
        try:
            ids.add(json.loads(line)["trial_id"])
        except (json.JSONDecodeError, KeyError):
            continue
    return ids


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20, help="paired trials")
    ap.add_argument("--start", type=int, default=1, help="first trial index (1-based)")
    ap.add_argument("--label", default="pilot", help="run label, e.g. calibration / pilot")
    ap.add_argument("--seed", type=int, default=42, help="rng seed for arm-order randomization")
    ap.add_argument("--out", type=Path, default=STUDY / "trials")
    ap.add_argument(
        "--arms",
        default="with-memory,without-memory",
        help="Comma-separated arms to run. For parallel pilot, run twice with one arm each.",
    )
    args = ap.parse_args()

    rng = random.Random(args.seed)

    args.out.mkdir(parents=True, exist_ok=True)
    jsonl = args.out / f"results_{args.label}.jsonl"

    existing = existing_trial_ids(jsonl)
    print(f"# existing trials in {jsonl}: {len(existing)}")

    arm_keys = [a.strip() for a in args.arms.split(",") if a.strip()]
    for a in arm_keys:
        if a not in ARMS:
            raise SystemExit(f"unknown arm: {a}")
    print(f"# arms: {arm_keys}")

    with open(jsonl, "a") as f:
        for k in range(args.start, args.start + args.n):
            order = list(arm_keys)
            rng.shuffle(order)
            for arm in order:
                trial_id = f"{args.label}_{k:03d}_{arm}"
                if trial_id in existing:
                    print(f"  [skip] {trial_id} (already in jsonl)")
                    continue
                t0 = time.time()
                print(f"  [run]  {trial_id} ...", flush=True)
                try:
                    result = run_one_trial(arm, ARMS[arm], trial_id, args.out)
                except subprocess.TimeoutExpired as exc:
                    result = {
                        "trial_id": trial_id,
                        "arm": arm,
                        "dtu_id": ARMS[arm],
                        "_timeout": True,
                        "exception": str(exc),
                    }
                except Exception as exc:  # pragma: no cover
                    result = {
                        "trial_id": trial_id,
                        "arm": arm,
                        "dtu_id": ARMS[arm],
                        "_exception": True,
                        "exception": repr(exc),
                    }
                f.write(json.dumps(result) + "\n")
                f.flush()
                dt = time.time() - t0
                pass_str = "?" if "_exception" in result or "_timeout" in result else result.get("score", {}).get("all_three", "?")
                print(f"          done in {dt:.1f}s  all_three_pass={pass_str}")

    print(f"# wrote results to {jsonl}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
