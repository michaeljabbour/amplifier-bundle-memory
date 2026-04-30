"""
UC-1 Constraint Adherence Scorer (FROZEN before first trial)

Inputs:  Path to S2 response JSON (the "response" field is the assistant's text).
Outputs: JSON with per-constraint pass/fail and overall (all-3-pass) result.

Constraints:
  no_pip:            response text must not contain pip install patterns or
                     mention `requirements.txt` as an install mechanism.
  no_comments:       extracted Python code blocks must contain no Python
                     comments (excluding shebang lines) and no docstrings.
  kebab_case_routes: every Flask route declared in extracted code blocks
                     must be kebab-case (lowercase letters, digits, hyphens,
                     and slashes only). At least one route must be declared.

Guardrail:
  syntactic_validity: every extracted Python code block must parse via
                      ast.parse without SyntaxError.

This file MUST NOT be modified after the first trial is collected.
"""

from __future__ import annotations

import ast
import json
import re
import sys
import tokenize
from dataclasses import asdict, dataclass
from io import BytesIO
from pathlib import Path

PIP_FAIL_PATTERNS = [
    re.compile(r"\bpip\s+install\b", re.IGNORECASE),
    re.compile(r"\bpip3\s+install\b", re.IGNORECASE),
    re.compile(r"\bpython\s*-m\s*pip\b", re.IGNORECASE),
    re.compile(r"\bpython3\s*-m\s*pip\b", re.IGNORECASE),
    re.compile(r"\brequirements\.txt\b", re.IGNORECASE),
]

# Match fenced code blocks. Accept ```python, ```py, or bare ``` (we will
# attempt to parse all of them and silently drop blocks that aren't python).
CODE_BLOCK_RE = re.compile(
    r"```(?P<lang>python|py)?\s*\n(?P<code>.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)

# Flask route patterns. We capture the FIRST positional string argument.
ROUTE_DECORATOR_RE = re.compile(
    r"""@(?:app|bp|blueprint|api)\.(?:route|get|post|put|delete|patch)\s*\(\s*['"]([^'"]+)['"]""",
    re.IGNORECASE,
)
ADD_URL_RULE_RE = re.compile(
    r"""\.add_url_rule\s*\(\s*['"]([^'"]+)['"]""",
    re.IGNORECASE,
)

# A path is kebab-case if it consists of ASCII lowercase letters, digits,
# hyphens, and slashes only (URL parameters like <int:id> are also allowed
# but their interior must follow the same rules — we match them out before
# checking).
URL_PARAM_RE = re.compile(r"<[^>]+>")
KEBAB_PATH_RE = re.compile(r"^/[a-z0-9\-/]*$")


@dataclass
class ScoreResult:
    no_pip: bool
    no_comments: bool
    kebab_case_routes: bool
    syntactic_validity: bool
    all_three: bool
    n_code_blocks: int
    n_python_blocks: int
    n_routes_found: int
    routes: list[str]
    pip_violations: list[str]
    comment_violations: list[dict]
    route_violations: list[str]
    syntax_errors: list[str]


def extract_code_blocks(text: str) -> list[str]:
    """Return code blocks that *parse* as valid Python (best-effort heuristic).

    We attempt to parse every fenced block; non-Python (e.g. shell, toml)
    blocks raise SyntaxError and are dropped. This is intentional: we only
    score what we can confidently identify as Python source.
    """
    blocks: list[str] = []
    for match in CODE_BLOCK_RE.finditer(text):
        code = match.group("code")
        lang = (match.group("lang") or "").lower()
        if lang in ("python", "py"):
            blocks.append(code)
            continue
        # Untagged block — try to parse as Python; skip if it isn't.
        try:
            ast.parse(code)
        except SyntaxError:
            continue
        blocks.append(code)
    return blocks


def check_no_pip(text: str) -> tuple[bool, list[str]]:
    violations: list[str] = []
    for pattern in PIP_FAIL_PATTERNS:
        for m in pattern.finditer(text):
            violations.append(m.group(0))
    return (len(violations) == 0), violations


def check_no_comments(blocks: list[str]) -> tuple[bool, list[dict]]:
    """Reject `# comment` lines (except shebang at line 1) and docstrings."""
    violations: list[dict] = []
    for idx, code in enumerate(blocks):
        # Comment scan via tokenize.
        try:
            tokens = list(tokenize.tokenize(BytesIO(code.encode("utf-8")).readline))
        except (tokenize.TokenError, IndentationError, SyntaxError):
            # If we can't tokenize, syntactic_validity will catch it; treat
            # the block as a comment-violation conservatively.
            violations.append({"block": idx, "kind": "tokenize_failed"})
            continue

        for tok in tokens:
            if tok.type != tokenize.COMMENT:
                continue
            line_text = tok.line.lstrip()
            # Allow a shebang on line 1.
            if tok.start[0] == 1 and line_text.startswith("#!"):
                continue
            violations.append(
                {"block": idx, "kind": "comment", "line": tok.start[0], "text": tok.string}
            )

        # Docstring scan via AST.
        try:
            tree = ast.parse(code)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
                doc = ast.get_docstring(node, clean=False)
                if doc is not None:
                    violations.append(
                        {
                            "block": idx,
                            "kind": "docstring",
                            "owner": getattr(node, "name", "<module>"),
                        }
                    )
    return (len(violations) == 0), violations


def check_kebab_case_routes(blocks: list[str]) -> tuple[bool, list[str], list[str]]:
    """Return (passed, all_routes, violating_routes).

    Pass requires: at least one route declared, AND every declared route is
    kebab-case (lowercase ASCII, digits, hyphens, slashes; URL parameters
    are stripped before checking).
    """
    routes: list[str] = []
    for code in blocks:
        routes.extend(ROUTE_DECORATOR_RE.findall(code))
        routes.extend(ADD_URL_RULE_RE.findall(code))

    violations: list[str] = []
    for route in routes:
        stripped = URL_PARAM_RE.sub("", route)
        if not KEBAB_PATH_RE.match(stripped):
            violations.append(route)

    passed = bool(routes) and not violations
    return passed, routes, violations


def check_syntactic_validity(blocks: list[str]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    for idx, code in enumerate(blocks):
        try:
            ast.parse(code)
        except SyntaxError as exc:
            errors.append(f"block {idx}: {exc}")
    return (len(errors) == 0), errors


def score_response(response_text: str) -> ScoreResult:
    n_code_blocks_total = len(list(CODE_BLOCK_RE.finditer(response_text)))
    blocks = extract_code_blocks(response_text)

    no_pip, pip_v = check_no_pip(response_text)
    no_comments, comment_v = check_no_comments(blocks)
    kebab_ok, routes, route_v = check_kebab_case_routes(blocks)
    syntactic_ok, syn_errs = check_syntactic_validity(blocks)

    return ScoreResult(
        no_pip=no_pip,
        no_comments=no_comments,
        kebab_case_routes=kebab_ok,
        syntactic_validity=syntactic_ok,
        all_three=no_pip and no_comments and kebab_ok,
        n_code_blocks=n_code_blocks_total,
        n_python_blocks=len(blocks),
        n_routes_found=len(routes),
        routes=routes,
        pip_violations=pip_v,
        comment_violations=comment_v,
        route_violations=route_v,
        syntax_errors=syn_errs,
    )


def main() -> None:
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: scorer.py <s2_response.json>"}))
        sys.exit(2)

    path = Path(sys.argv[1])
    raw = json.loads(path.read_text())
    # The amplifier `--output-format json` payload puts assistant text in
    # `response`. Fall back to the raw text if the file is plain text.
    if isinstance(raw, dict) and "response" in raw:
        text = raw["response"]
    else:
        text = path.read_text()

    if not isinstance(text, str):
        text = json.dumps(text)

    result = score_response(text)
    print(json.dumps(asdict(result), indent=2))


if __name__ == "__main__":
    main()
