"""Secret scrubbing, shared by hooks-memory-capture and tool-memory (T0.3,
PROVENANCE.md D14; relocated here in P2 so ``fact_add`` can reuse the same
scrubber -- facts must not carry secrets either). ``hooks-memory-capture``
keeps a thin re-export shim at its original import path so its own tests and
callers are unaffected.

``redact(text)`` runs a fixed, ordered set of conservative regexes over
captured tool output and returns ``(scrubbed_text, counts)`` where
``counts`` maps kind -> number of redactions of that kind (kinds with zero
matches are omitted). Nothing here ever raises on ordinary input; the
patterns are intentionally narrow so normal code/prose (docstrings that say
"token" or "password", short example values, etc.) is left untouched.

Order matters: ``anthropic_key`` is applied before ``openai_key`` because
the OpenAI pattern (``sk-`` + 20+ allowed chars) would otherwise also match
an Anthropic key (``sk-ant-...``) and mislabel it. Each pattern is applied
to the text as already redacted by earlier patterns, so a value redacted by
an earlier kind can never be double-counted by a later one.
"""

from __future__ import annotations

import re

__all__ = ["redact"]


def _placeholder_pattern() -> re.Pattern[str]:
    return re.compile(
        r"^(\[REDACTED[^\]]*\]|\$\{.*\}|<.*>|x{3,}|X{3,})$",
    )


_PLACEHOLDER_RE = _placeholder_pattern()

# Ordered (kind, pattern) pairs. Order is significant -- see module docstring.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai_key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}")),
    (
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})\b"),
    ),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    (
        "jwt",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
        ),
    ),
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
        ),
    ),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}")),
]

# NAME must contain one of these secret-shaped substrings, optionally
# surrounded by other identifier chars (e.g. MY_API_KEY, DB_PASSWORD_2).
_ENV_NAME = r"[A-Z0-9_]*(?:API_KEY|APIKEY|SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE_KEY|ACCESS_KEY)[A-Z0-9_]*"

# NAME=value / NAME: value / "NAME": "value" -- lead/trailing quote around
# NAME captured via a backreference so both quoted and bare names match;
# same trick for the value. The value char class excludes whitespace and
# quote characters so it stops at the natural end of the token.
_ENV_ASSIGN_RE = re.compile(
    r'(?P<lead_q>["\'`]?)(?P<name>' + _ENV_NAME + r")(?P=lead_q)"
    r"\s*[:=]\s*"
    r'(?P<val_q>["\'`]?)(?P<value>[^\s"\'`]+)(?P=val_q)',
    re.IGNORECASE,
)


def _apply_pattern(
    text: str, kind: str, pattern: re.Pattern[str], counts: dict[str, int]
) -> str:
    def _sub(_match: re.Match[str]) -> str:
        counts[kind] = counts.get(kind, 0) + 1
        return f"[REDACTED:{kind}]"

    return pattern.sub(_sub, text)


def _apply_env_secret(text: str, counts: dict[str, int]) -> str:
    def _sub(match: re.Match[str]) -> str:
        value = match.group("value")
        if len(value) < 8 or _PLACEHOLDER_RE.match(value):
            return match.group(0)
        counts["env_secret"] = counts.get("env_secret", 0) + 1
        full = match.group(0)
        rel_start = match.start("value") - match.start(0)
        rel_end = match.end("value") - match.start(0)
        return full[:rel_start] + "[REDACTED:env_secret]" + full[rel_end:]

    return _ENV_ASSIGN_RE.sub(_sub, text)


def redact(text: str) -> tuple[str, dict[str, int]]:
    """Scrub known secret shapes from *text*.

    Returns ``(scrubbed_text, counts)``. ``counts`` only contains kinds that
    had at least one redaction (never zero-valued entries), so callers can
    treat a falsy dict as "nothing found" directly.
    """
    if not text:
        return text, {}

    counts: dict[str, int] = {}
    scrubbed = text
    for kind, pattern in _PATTERNS:
        scrubbed = _apply_pattern(scrubbed, kind, pattern, counts)
    scrubbed = _apply_env_secret(scrubbed, counts)
    return scrubbed, counts
