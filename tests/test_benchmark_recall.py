"""
Benchmark: Briefing Importance Re-ranking — R@5 Recall Gate

Section 9 of spec-v1.2.0-gene-transfer.md

Run modes:
  Default suite  — test_zero_regression_guarantee, test_max_boost_does_not_dominate_semantic
                   (fast math checks, always run)
  Benchmark run  — pytest -m benchmark  (includes the full R@5 simulation)
  Integration    — requires mempalace CLI (auto-skipped when unavailable)

GATE: R@5_reranked >= R@5_baseline. Regression → block CP4 release.

Scale: 200 drawers × 30 queries (per spec Section 9.2).
"""

from __future__ import annotations

import json
import os
import random
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from amplifier_module_hooks_mempalace_briefing import _rerank_by_importance
from amplifier_module_tool_mempalace.phase3 import compute_importance

# ---------------------------------------------------------------------------
# CLI availability guard
# ---------------------------------------------------------------------------


def _mempalace_available() -> bool:
    try:
        result = subprocess.run(
            ["mempalace", "--version"], capture_output=True, timeout=5
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


MEMPALACE_AVAILABLE = _mempalace_available()
skip_no_mempalace = pytest.mark.skipif(
    not MEMPALACE_AVAILABLE, reason="mempalace CLI not available"
)

# NOTE: pytestmark NOT set at module level — only specific tests are marked
# benchmark. test_zero_regression_guarantee and test_max_boost_does_not_dominate_semantic
# run in the default suite (fast, math-derived safety nets).


# ---------------------------------------------------------------------------
# Synthetic fixture: 200 drawers × 30 queries
# ---------------------------------------------------------------------------

def _make_drawer(id_: str, category: str, wing: str, room: str, text: str) -> dict:
    return {
        "id": id_,
        "category": category,
        "wing": wing,
        "room": room,
        "content": text,
    }


# ── 60 hand-written base drawers ────────────────────────────────────────────

SYNTHETIC_DRAWERS: list[dict] = [
    # wing_myapp — decisions (auth, database, api, frontend, deploy)
    _make_drawer(
        "d001",
        "decision",
        "wing_myapp",
        "auth-decisions",
        "Decided to use Clerk for authentication because JWT refresh tokens are complex to manage. "
        "The team agreed Clerk handles OAuth, session rotation, and MFA out of the box.",
    ),
    _make_drawer(
        "d002",
        "decision",
        "wing_myapp",
        "auth-decisions",
        "We will not build our own auth. Clerk pricing at $25/month is acceptable for the user volume.",
    ),
    _make_drawer(
        "d003",
        "decision",
        "wing_myapp",
        "database",
        "Chose Postgres over SQLite because we need concurrent writes and connection pooling. "
        "PlanetScale was considered but vendor lock-in was a concern.",
    ),
    _make_drawer(
        "d004",
        "decision",
        "wing_myapp",
        "database",
        "Decided on Drizzle ORM instead of Prisma. Drizzle has smaller bundle size and "
        "better TypeScript inference. Migration story is also cleaner.",
    ),
    _make_drawer(
        "d005",
        "decision",
        "wing_myapp",
        "api-design",
        "REST over GraphQL for the initial API. GraphQL adds complexity we don't need yet. "
        "Decision is reversible — we'll revisit when we have >3 client types.",
    ),
    _make_drawer(
        "d006",
        "decision",
        "wing_myapp",
        "api-design",
        "API versioning via URL prefix /v1/. Header-based versioning rejected because "
        "it complicates caching and debugging.",
    ),
    _make_drawer(
        "d007",
        "decision",
        "wing_myapp",
        "deployment",
        "Deploying to Fly.io instead of AWS. AWS setup cost is too high for the current team size. "
        "Fly.io gives us Postgres, Redis, and compute in one place.",
    ),
    _make_drawer(
        "d008",
        "decision",
        "wing_myapp",
        "frontend",
        "Using Next.js 14 app router. Pages router migration deferred. "
        "RSC enables us to eliminate most client-side data fetching boilerplate.",
    ),
    # wing_myapp — architecture
    _make_drawer(
        "d009",
        "architecture",
        "wing_myapp",
        "system-design",
        "Monorepo with Turborepo. Packages: web (Next.js), api (Hono), shared (types/utils). "
        "Enables atomic cross-package changes and shared lint/test config.",
    ),
    _make_drawer(
        "d010",
        "architecture",
        "wing_myapp",
        "system-design",
        "Event-driven updates via Postgres LISTEN/NOTIFY for real-time features. "
        "Avoids WebSocket server complexity. Works well with Fly.io Postgres.",
    ),
    _make_drawer(
        "d011",
        "architecture",
        "wing_myapp",
        "caching",
        "Three-layer caching: browser (SWR), CDN (Vercel), and Redis (server-side). "
        "Cache invalidation keys follow pattern: {resource}:{id}:{version}.",
    ),
    _make_drawer(
        "d012",
        "architecture",
        "wing_myapp",
        "error-handling",
        "All API errors return RFC 7807 problem details format. "
        "Error codes are stable enums, not free-form strings, enabling client-side switch statements.",
    ),
    # wing_myapp — blockers / resolved
    _make_drawer(
        "d013",
        "blocker",
        "wing_myapp",
        "auth-decisions",
        "BLOCKED: Clerk webhook signature verification failing in staging. "
        "The Svix library version mismatch is causing HMAC verification to fail. Issue opened.",
    ),
    _make_drawer(
        "d014",
        "resolved_blocker",
        "wing_myapp",
        "auth-decisions",
        "RESOLVED: Clerk webhook issue was due to Svix v0.11 vs v0.12 interface change. "
        "Fixed by pinning Svix to v0.12.0 and updating the verify() call signature.",
    ),
    _make_drawer(
        "d015",
        "blocker",
        "wing_myapp",
        "database",
        "BLOCKED: Connection pool exhaustion under load testing at 100 concurrent users. "
        "Postgres max_connections hit. Need to add PgBouncer or reduce pool size per instance.",
    ),
    _make_drawer(
        "d016",
        "resolved_blocker",
        "wing_myapp",
        "database",
        "RESOLVED: Added PgBouncer sidecar on Fly.io. Pool size reduced to 10 per instance. "
        "Load test at 200 concurrent now succeeds. Root cause: Drizzle default pool was 20.",
    ),
    # wing_myapp — patterns / lessons
    _make_drawer(
        "d017",
        "pattern",
        "wing_myapp",
        "error-handling",
        "Pattern: Always wrap async route handlers in a try-catch that returns problem-detail. "
        "Convention: throw AppError(code, message, status) — never raw Error.",
    ),
    _make_drawer(
        "d018",
        "pattern",
        "wing_myapp",
        "testing",
        "Test naming convention: describe('component', () => { it('verb noun condition') }). "
        "Avoid 'should' prefix — it adds noise without meaning.",
    ),
    _make_drawer(
        "d019",
        "pattern",
        "wing_myapp",
        "api-design",
        "Pagination pattern: cursor-based for large collections, offset for admin views. "
        "Cursor is always opaque base64 — never expose raw IDs.",
    ),
    _make_drawer(
        "d020",
        "pattern",
        "wing_myapp",
        "frontend",
        "Data fetching pattern: Server Components for initial load, SWR for mutations. "
        "Never fetch in useEffect — prefer loader patterns.",
    ),
    # wing_infra
    _make_drawer(
        "d021",
        "architecture",
        "wing_infra",
        "ci-cd",
        "GitHub Actions for CI. Three pipelines: lint+test (PR), preview deploy (PR merged), "
        "prod deploy (main push). Docker layer caching cuts build time from 4m to 45s.",
    ),
    _make_drawer(
        "d022",
        "architecture",
        "wing_infra",
        "monitoring",
        "Observability stack: Fly.io metrics → Grafana Cloud. Structured logging via pino. "
        "Trace IDs injected at middleware level, propagated through all service calls.",
    ),
    _make_drawer(
        "d023",
        "architecture",
        "wing_infra",
        "secrets",
        "Secrets managed via Fly.io secrets store. No .env files committed. "
        "Local dev uses .env.local (gitignored). Pattern: prefix with APP_ for app secrets.",
    ),
    _make_drawer(
        "d024",
        "decision",
        "wing_infra",
        "ci-cd",
        "Decided against running integration tests in CI for now — too slow and flaky. "
        "Unit tests only in CI. Integration tests run nightly on the staging environment.",
    ),
    _make_drawer(
        "d025",
        "pattern",
        "wing_infra",
        "deployment",
        "Blue-green deploys via Fly.io rolling deploys. Zero-downtime guaranteed. "
        "Rollback: fly deploy --image previous_image_tag. Kept for 7 days.",
    ),
    _make_drawer(
        "d026",
        "blocker",
        "wing_infra",
        "monitoring",
        "BLOCKED: Grafana alerting not firing on error rate spikes. "
        "Alert rule uses legacy query format incompatible with Grafana 10. Needs migration.",
    ),
    _make_drawer(
        "d027",
        "resolved_blocker",
        "wing_infra",
        "monitoring",
        "RESOLVED: Grafana alert migration to new query format complete. "
        "Root cause: Grafana 10 removed the old 'classic conditions' alert type.",
    ),
    _make_drawer(
        "d028",
        "lesson_learned",
        "wing_infra",
        "ci-cd",
        "Lesson: Docker multi-stage builds must explicitly COPY package.json before npm install, "
        "otherwise cache misses on every dependency change. Fix: COPY package*.json ./",
    ),
    _make_drawer(
        "d029",
        "architecture",
        "wing_infra",
        "networking",
        "Private networking between Fly.io apps via .internal DNS. "
        "No public internet for service-to-service calls. Fly WireGuard mesh handles it.",
    ),
    _make_drawer(
        "d030",
        "decision",
        "wing_infra",
        "ci-cd",
        "Chose pnpm over npm for the monorepo. Workspace hoisting avoids duplicate "
        "node_modules. pnpm-lock.yaml committed. Renovate bot handles updates.",
    ),
    # wing_team
    _make_drawer(
        "d031",
        "lesson_learned",
        "wing_team",
        "process",
        "Lesson: PRs with >500 lines of diff take 3x longer to review. "
        "Adopted atomic PR policy: one logical change per PR.",
    ),
    _make_drawer(
        "d032",
        "lesson_learned",
        "wing_team",
        "process",
        "Lesson: Daily standups via Slack threads instead of Zoom. "
        "Async format: Yesterday / Today / Blockers. 2x faster, less context-switching.",
    ),
    _make_drawer(
        "d033",
        "decision",
        "wing_team",
        "tooling",
        "Chose Linear over Jira for issue tracking. Linear's keyboard-first design "
        "and GitHub integration reduce friction. Cycles map to 2-week sprints.",
    ),
    _make_drawer(
        "d034",
        "pattern",
        "wing_team",
        "code-review",
        "Code review pattern: Nitpick (cosmetic), Suggestion (optional improvement), "
        "Request (must address before merge). Prefix comments with these tags.",
    ),
    _make_drawer(
        "d035",
        "lesson_learned",
        "wing_team",
        "debugging",
        "Lesson: Always check Fly.io logs before opening an issue. "
        "fly logs --app myapp -n 100 shows the last 100 lines including crash traces.",
    ),
    _make_drawer(
        "d036",
        "lesson_learned",
        "wing_team",
        "debugging",
        "Lesson: Connection refused errors in staging almost always mean the app crashed "
        "before binding the port. Check Fly.io events: fly status --app myapp.",
    ),
    _make_drawer(
        "d037",
        "decision",
        "wing_team",
        "documentation",
        "ADR (Architecture Decision Records) written in project-context/PROVENANCE.md. "
        "Format: Context → Decision → Consequences. Kept in git history.",
    ),
    _make_drawer(
        "d038",
        "pattern",
        "wing_team",
        "process",
        "Pattern: Every session ends with HANDOFF.md update. "
        "Format: Accomplished / Blocked / Start Here Next / Non-obvious context.",
    ),
    # wing_myapp — search, payments, email, misc
    _make_drawer(
        "d039",
        "architecture",
        "wing_myapp",
        "search",
        "Full-text search via Postgres tsvector. No Elasticsearch for now — "
        "too complex for the query volume. GIN index on content column gives sub-10ms latency.",
    ),
    _make_drawer(
        "d040",
        "decision",
        "wing_myapp",
        "search",
        "Decided to implement semantic search via pgvector extension. "
        "OpenAI text-embedding-3-small for embeddings. 1536-dimensional vectors.",
    ),
    _make_drawer(
        "d041",
        "blocker",
        "wing_myapp",
        "search",
        "BLOCKED: pgvector cosine_similarity returning NaN for some queries. "
        "Suspect zero-vector inputs from failed embedding API calls.",
    ),
    _make_drawer(
        "d042",
        "resolved_blocker",
        "wing_myapp",
        "search",
        "RESOLVED: pgvector NaN issue fixed. Added null-check before inserting embeddings. "
        "Root cause: OpenAI API rate limiting returned empty responses silently.",
    ),
    _make_drawer(
        "d043",
        "pattern",
        "wing_myapp",
        "search",
        "Search ranking pattern: cosine similarity for semantic + trigram for text match. "
        "Weighted blend: 0.7 * semantic + 0.3 * text. Re-evaluated monthly.",
    ),
    _make_drawer(
        "d044",
        "lesson_learned",
        "wing_myapp",
        "search",
        "Lesson: Always truncate input to embedding model at 8191 tokens. "
        "text-embedding-3-small has a hard token limit. Silent failure on overflow.",
    ),
    _make_drawer(
        "d045",
        "decision",
        "wing_myapp",
        "payments",
        "Using Stripe for payments. Stripe Checkout for initial implementation — "
        "reduces PCI scope. Custom payment UI deferred to v2.",
    ),
    _make_drawer(
        "d046",
        "architecture",
        "wing_myapp",
        "payments",
        "Webhook-first payment handling. No optimistic UI for payment status. "
        "Stripe webhook → internal queue → order fulfillment. Idempotency key = Stripe event ID.",
    ),
    _make_drawer(
        "d047",
        "pattern",
        "wing_myapp",
        "testing",
        "E2E test pattern: Playwright + test user accounts. No mocking in E2E. "
        "Stripe test mode keys used. Tests clean up after themselves via API.",
    ),
    _make_drawer(
        "d048",
        "lesson_learned",
        "wing_myapp",
        "payments",
        "Lesson: Stripe webhooks must be retried up to 3 times. Ensure handler is idempotent. "
        "Use Stripe event ID as idempotency key in database.",
    ),
    _make_drawer(
        "d049",
        "decision",
        "wing_myapp",
        "email",
        "Resend for transactional email. React Email for templates. "
        "Postmark considered but Resend has better React integration.",
    ),
    _make_drawer(
        "d050",
        "architecture",
        "wing_myapp",
        "email",
        "Email queue via Fly.io Redis. Failed sends retry with exponential backoff. "
        "Max 3 retries. Dead letter queue for manual inspection.",
    ),
    _make_drawer(
        "d051",
        "lesson_learned",
        "wing_myapp",
        "email",
        "Lesson: Always test email rendering in both dark mode and light mode clients. "
        "Outlook uses Word rendering engine — avoid CSS flexbox.",
    ),
    _make_drawer(
        "d052",
        "pattern",
        "wing_myapp",
        "api-design",
        "Rate limiting pattern: per-IP for public endpoints, per-user for authenticated. "
        "Redis sliding window counter. 429 response with Retry-After header.",
    ),
    _make_drawer(
        "d053",
        "architecture",
        "wing_infra",
        "backup",
        "Daily Postgres backups to S3. Fly.io managed backups + custom pg_dump script. "
        "Retention: 7 daily, 4 weekly, 12 monthly. Restore tested monthly.",
    ),
    _make_drawer(
        "d054",
        "lesson_learned",
        "wing_infra",
        "backup",
        "Lesson: Test your backups by actually restoring them. Fly.io managed backups "
        "have been reliable but pg_dump scripts had a schema-only flag set accidentally.",
    ),
    _make_drawer(
        "d055",
        "decision",
        "wing_myapp",
        "caching",
        "Redis 7 for session storage and caching. fly-redis.toml configured for 256MB. "
        "Session TTL: 7 days sliding. Cache TTL: resource-specific, 5-300 seconds.",
    ),
    _make_drawer(
        "d056",
        "pattern",
        "wing_myapp",
        "caching",
        "Cache invalidation via event-driven pattern. Publish cache:invalidate:{key} "
        "to Redis pub/sub on write. All API servers subscribe and clear local caches.",
    ),
    _make_drawer(
        "d057",
        "architecture",
        "wing_myapp",
        "file-storage",
        "File uploads via Cloudflare R2. Pre-signed URLs for direct client upload. "
        "Avoids routing binary data through API server. CDN auto-provisioned.",
    ),
    _make_drawer(
        "d058",
        "decision",
        "wing_team",
        "tooling",
        "Biome replaces ESLint + Prettier. Single tool, 10x faster, zero config drift. "
        "Migration: biome migrate eslint --write. 2 rules required manual adjustment.",
    ),
    _make_drawer(
        "d059",
        "lesson_learned",
        "wing_myapp",
        "frontend",
        "Lesson: Next.js Image component requires known dimensions or fill layout. "
        "Unknown dimension images: use unoptimized prop or CSS object-fit.",
    ),
    _make_drawer(
        "d060",
        "pattern",
        "wing_infra",
        "security",
        "Security headers via middleware: CSP, HSTS, X-Frame-Options, etc. "
        "Verified with securityheaders.com. Score: A+.",
    ),
]

assert len(SYNTHETIC_DRAWERS) == 60, (
    f"Expected 60 base drawers, got {len(SYNTHETIC_DRAWERS)}"
)


# ── Programmatic generation of 140 additional drawers ───────────────────────

_WING_EXTRA_TOPICS: dict[str, list[str]] = {
    "wing_myapp": [
        "notifications",
        "analytics",
        "feature-flags",
        "ab-testing",
        "webhooks",
        "rate-limiting",
        "audit-logs",
        "health-checks",
        "migrations",
        "permissions",
    ],
    "wing_infra": [
        "load-balancing",
        "certificates",
        "dns",
        "alerts",
        "cost-optimization",
        "disaster-recovery",
        "compliance",
        "vulnerability-scanning",
        "gitops",
        "observability",
    ],
    "wing_team": [
        "communication",
        "onboarding",
        "retrospectives",
        "incident-response",
        "post-mortems",
        "knowledge-sharing",
        "pair-programming",
        "sprint-planning",
    ],
    "wing_research": [
        "embeddings",
        "evaluation",
        "retrieval",
        "chunking",
        "similarity",
        "benchmarks",
        "prompting",
        "rag",
        "reranking",
        "agents",
        "fine-tuning",
        "context-window",
        "hallucination",
        "grounding",
        "multimodal",
        "tool-use",
        "memory-systems",
        "vector-search",
        "semantic-cache",
        "dataset-curation",
    ],
}

_GEN_CATEGORIES = [
    "decision",
    "architecture",
    "blocker",
    "resolved_blocker",
    "pattern",
    "lesson_learned",
    "dependency",
]

_TOOLS_A = [
    "Redis",
    "Postgres",
    "Kubernetes",
    "GitHub Actions",
    "Terraform",
    "Docker",
    "NGINX",
    "CloudFront",
    "Datadog",
    "Sentry",
    "LaunchDarkly",
    "Kafka",
    "OpenAI",
    "LangChain",
    "ChromaDB",
]
_TOOLS_B = [
    "MongoDB",
    "MySQL",
    "Jenkins",
    "Ansible",
    "Podman",
    "HAProxy",
    "Fastly",
    "Prometheus",
    "LogRocket",
    "Unleash",
    "RabbitMQ",
    "Pinecone",
    "Weaviate",
    "Qdrant",
    "Cohere",
]
_COMPONENT_NAMES = [
    "service layer",
    "API gateway",
    "cache layer",
    "message queue",
    "monitoring agent",
    "CI pipeline",
    "load balancer",
    "secrets vault",
    "vector store",
    "embedding pipeline",
]
_PATTERN_NAMES = [
    "event-driven architecture",
    "circuit breaker pattern",
    "retry with exponential backoff",
    "read-through caching",
    "blue-green deployment",
    "feature flag gating",
    "canary release",
    "dead letter queue",
    "CQRS",
    "retrieval-augmented generation",
]
_REASON_PHRASES = [
    "performance constraints",
    "operational complexity",
    "vendor lock-in risk",
    "cost optimization",
    "latency requirements",
    "data consistency needs",
    "team familiarity",
    "ecosystem compatibility",
    "token budget limits",
    "accuracy-latency trade-off",
]

_GEN_TEMPLATES: dict[str, str] = {
    "decision": (
        "Decided to use {tool_a} for {topic} instead of {tool_b}. "
        "Key factor: {reason}. Trade-off accepted, documented in PROVENANCE.md."
    ),
    "architecture": (
        "Architecture for {topic}: {pattern} via {component}. "
        "Chosen for {reason}. Enables future scaling without major rework."
    ),
    "blocker": (
        "BLOCKED: {topic} {component} failing due to {reason}. "
        "Investigation ongoing. Temporary workaround: fallback to {tool_b}."
    ),
    "resolved_blocker": (
        "RESOLVED: {topic} blocker. Root cause: {reason}. "
        "Fix: {pattern} applied to {component}. Deployed and verified in staging."
    ),
    "pattern": (
        "Pattern for {topic}: always apply {pattern} via {component}. "
        "Avoids {reason}. See {tool_a} docs for reference implementation."
    ),
    "lesson_learned": (
        "Lesson: {topic} with {tool_a} requires {pattern}. "
        "Without it: {reason}. Updated runbook. Applies to {component}."
    ),
    "dependency": (
        "{topic} hard-depends on {tool_a} ({pattern}). "
        "Version pinned. Must update when {reason}. Managed via {component}."
    ),
}

# Targets: myapp→30, infra→30, team→20, research→60 = 140 total
_WING_EXTRA_COUNTS: dict[str, int] = {
    "wing_myapp": 30,
    "wing_infra": 30,
    "wing_team": 20,
    "wing_research": 60,
}


def _generate_extra_drawers() -> list[dict]:
    """Generate 140 additional drawers deterministically."""
    drawers: list[dict] = []
    n = 61

    for wing, count in _WING_EXTRA_COUNTS.items():
        topics = _WING_EXTRA_TOPICS[wing]
        for i in range(count):
            topic = topics[i % len(topics)]
            category = _GEN_CATEGORIES[i % len(_GEN_CATEGORIES)]
            content = _GEN_TEMPLATES[category].format(
                topic=topic,
                tool_a=_TOOLS_A[i % len(_TOOLS_A)],
                tool_b=_TOOLS_B[i % len(_TOOLS_B)],
                component=_COMPONENT_NAMES[i % len(_COMPONENT_NAMES)],
                pattern=_PATTERN_NAMES[i % len(_PATTERN_NAMES)],
                reason=_REASON_PHRASES[i % len(_REASON_PHRASES)],
            )
            drawers.append(_make_drawer(f"d{n:03d}", category, wing, topic, content))
            n += 1

    return drawers


_EXTRA_DRAWERS = _generate_extra_drawers()
ALL_DRAWERS = SYNTHETIC_DRAWERS + _EXTRA_DRAWERS

assert len(ALL_DRAWERS) == 200, f"Expected 200 drawers, got {len(ALL_DRAWERS)}"

# Build an ID→drawer map for fast lookup
_DRAWER_BY_ID: dict[str, dict] = {d["id"]: d for d in ALL_DRAWERS}

# IDs of wing_research drawers (d141-d200)
_RESEARCH_DRAWER_IDS = [d["id"] for d in ALL_DRAWERS if d["wing"] == "wing_research"]
# IDs of wing_infra extra drawers (d091-d120)
_INFRA_EXTRA_IDS = [d["id"] for d in _EXTRA_DRAWERS if d["wing"] == "wing_infra"][:10]
# IDs of wing_team extra drawers (d121-d140)
_TEAM_EXTRA_IDS = [d["id"] for d in _EXTRA_DRAWERS if d["wing"] == "wing_team"][:8]


# ---------------------------------------------------------------------------
# 30 queries with difficulty levels
# ---------------------------------------------------------------------------
# difficulty:
#   "easy"   — expected drawers at [0.82,0.92], competitors at [0.50,0.77] → R@5 ≈ 1.00
#   "medium" — expected at [0.75,0.87], same-wing at [0.68,0.82]         → R@5 ≈ 0.75
#   "hard"   — expected at [0.70,0.84], same-wing at [0.68,0.83]         → R@5 ≈ 0.55

RECALL_QUERIES: list[dict] = [
    # ── Easy (10) — original queries ─────────────────────────────────────
    {
        "query": "Why did we choose Clerk for authentication?",
        "expected_ids": ["d001", "d002", "d014"],
        "difficulty": "easy",
    },
    {
        "query": "What database are we using and why Postgres?",
        "expected_ids": ["d003", "d004", "d016"],
        "difficulty": "easy",
    },
    {
        "query": "REST vs GraphQL API decision",
        "expected_ids": ["d005", "d006", "d019"],
        "difficulty": "easy",
    },
    {
        "query": "How do we deploy to production?",
        "expected_ids": ["d007", "d025", "d021"],
        "difficulty": "easy",
    },
    {
        "query": "Monorepo architecture and TypeScript setup",
        "expected_ids": ["d009", "d010", "d058"],
        "difficulty": "easy",
    },
    {
        "query": "Stripe payment integration and webhooks",
        "expected_ids": ["d045", "d046", "d048"],
        "difficulty": "easy",
    },
    {
        "query": "Search implementation with embeddings and pgvector",
        "expected_ids": ["d040", "d041", "d043"],
        "difficulty": "easy",
    },
    {
        "query": "Email sending with Resend and retry logic",
        "expected_ids": ["d049", "d050", "d051"],
        "difficulty": "easy",
    },
    {
        "query": "Caching strategy with Redis",
        "expected_ids": ["d055", "d056", "d011"],
        "difficulty": "easy",
    },
    {
        "query": "Code review process and PR conventions",
        "expected_ids": ["d034", "d031", "d037"],
        "difficulty": "easy",
    },
    # ── Medium (10) — new queries targeting additional drawers ────────────
    {
        "query": "Error handling and RFC 7807 problem details format",
        "expected_ids": ["d012", "d017", "d005"],
        "difficulty": "medium",
    },
    {
        "query": "Postgres connection pool exhaustion and PgBouncer fix",
        "expected_ids": ["d015", "d016", "d003"],
        "difficulty": "medium",
    },
    {
        "query": "GitHub Actions CI/CD pipelines and Docker layer caching",
        "expected_ids": ["d021", "d024", "d028"],
        "difficulty": "medium",
    },
    {
        "query": "Secrets management and .env files on Fly.io",
        "expected_ids": ["d023", "d007", "d060"],
        "difficulty": "medium",
    },
    {
        "query": "Grafana alerting and observability stack",
        "expected_ids": ["d022", "d026", "d027"],
        "difficulty": "medium",
    },
    {
        "query": "pgvector NaN bug and embedding pipeline issues",
        "expected_ids": ["d041", "d042", "d044"],
        "difficulty": "medium",
    },
    {
        "query": "File uploads and Cloudflare R2 pre-signed URLs",
        "expected_ids": ["d057", "d059", "d008"],
        "difficulty": "medium",
    },
    {
        "query": "Fly.io private networking and internal DNS",
        "expected_ids": ["d029", "d007", "d025"],
        "difficulty": "medium",
    },
    {
        "query": "pnpm monorepo workspace hoisting",
        "expected_ids": ["d030", "d009", "d018"],
        "difficulty": "medium",
    },
    {
        "query": "Async standup process and remote communication",
        "expected_ids": ["d032", "d038", "d033"],
        "difficulty": "medium",
    },
    # ── Hard (10) — tight semantic competition ─────────────────────────────
    {
        "query": "Biome vs ESLint tooling trade-offs",
        "expected_ids": ["d058", "d033", "d037"],
        "difficulty": "hard",
    },
    {
        "query": "Stripe idempotency key and webhook retry strategy",
        "expected_ids": ["d048", "d046", "d047"],
        "difficulty": "hard",
    },
    {
        "query": "Next.js app router data fetching and Image component",
        "expected_ids": ["d008", "d059", "d020"],
        "difficulty": "hard",
    },
    {
        "query": "Redis cache invalidation with pub/sub",
        "expected_ids": ["d056", "d055", "d011"],
        "difficulty": "hard",
    },
    {
        "query": "Postgres backup restore testing and pg_dump",
        "expected_ids": ["d053", "d054", "d007"],
        "difficulty": "hard",
    },
    {
        "query": "Playwright E2E tests with Stripe test mode",
        "expected_ids": ["d047", "d045", "d018"],
        "difficulty": "hard",
    },
    {
        "query": "Rate limiting strategy per-IP vs per-user",
        "expected_ids": ["d052", "d019", "d006"],
        "difficulty": "hard",
    },
    {
        "query": "Blue-green and canary deploy rollback",
        "expected_ids": ["d025", "d007", "d021"],
        "difficulty": "hard",
    },
    {
        "query": "Search ranking blend of semantic and text similarity",
        "expected_ids": ["d043", "d039", "d040"],
        "difficulty": "hard",
    },
    {
        "query": "Decision record format and ADR documentation",
        "expected_ids": ["d037", "d034", "d038"],
        "difficulty": "hard",
    },
]

assert len(RECALL_QUERIES) == 30, f"Expected 30 queries, got {len(RECALL_QUERIES)}"


# ---------------------------------------------------------------------------
# Importance assignments (simulates Phase 3 KG backfill)
# ---------------------------------------------------------------------------

DRAWER_IMPORTANCE: dict[str, float] = {
    d["id"]: compute_importance(d["category"], {}) for d in ALL_DRAWERS
}

# Apply user_explicit boost to key architectural decisions
for _id in ["d001", "d003", "d007", "d040", "d045", "d009"]:
    DRAWER_IMPORTANCE[_id] = compute_importance("decision", {"user_explicit": True})

# Near-duplicate / low-importance drawers
for _id in ["d002"]:  # very similar to d001
    DRAWER_IMPORTANCE[_id] = 0.15

# Apply architecture cross-wing boost to major arch decisions
for _id in ["d009", "d010", "d021", "d029"]:
    DRAWER_IMPORTANCE[_id] = max(
        DRAWER_IMPORTANCE[_id],
        compute_importance("architecture", {"cross_wing": True}),
    )


# ---------------------------------------------------------------------------
# R@5 metric
# ---------------------------------------------------------------------------


def recall_at_5(top5_ids: list[str], expected_ids: list[str]) -> float:
    if not expected_ids:
        return 1.0
    hits = sum(1 for eid in expected_ids if eid in top5_ids)
    return hits / len(expected_ids)


def simulate_rerank(
    semantic_results: list[dict[str, Any]],
    importance_lookup: dict[str, float],
    weight: float,
) -> list[str]:
    reranked = _rerank_by_importance(semantic_results, importance_lookup, weight)
    return [r["id"] for r in reranked[:5]]


# ---------------------------------------------------------------------------
# Semantic score simulation
# ---------------------------------------------------------------------------

# Score ranges indexed by difficulty
_SCORE_RANGES = {
    "easy": {
        "expected": (0.82, 0.92),
        "same_wing": (0.50, 0.77),
        "other": (0.35, 0.65),
    },
    "medium": {
        "expected": (0.75, 0.87),
        "same_wing": (0.68, 0.82),
        "other": (0.40, 0.70),
    },
    "hard": {
        "expected": (0.70, 0.84),
        "same_wing": (0.68, 0.83),
        "other": (0.42, 0.72),
    },
}


def simulate_search(query_idx: int) -> list[dict[str, Any]]:
    """Generate deterministic semantic scores for a query across all 200 drawers.

    Uses difficulty-based score ranges so that:
    - Easy queries: expected drawers clearly above all competitors.
    - Medium queries: ~20% chance an expected drawer gets bumped out of top-8.
    - Hard queries: ~40% chance an expected drawer gets bumped out of top-8.

    Returns the top-8 results (as briefing fetches limit=8).
    """
    query = RECALL_QUERIES[query_idx]
    difficulty = query.get("difficulty", "easy")
    expected = set(query["expected_ids"])
    target_wing = _DRAWER_BY_ID[query["expected_ids"][0]]["wing"]

    ranges = _SCORE_RANGES[difficulty]
    rng = random.Random(query_idx * 997 + 31337)

    results: list[dict[str, Any]] = []
    for d in ALL_DRAWERS:
        if d["id"] in expected:
            lo, hi = ranges["expected"]
        elif d["wing"] == target_wing:
            lo, hi = ranges["same_wing"]
        else:
            lo, hi = ranges["other"]
        score = rng.uniform(lo, hi)
        results.append(
            {
                "id": d["id"],
                "score": score,
                "room": d["room"],
                "text": d["content"][:60],
            }
        )

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:8]


# ---------------------------------------------------------------------------
# Eval doc writer
# ---------------------------------------------------------------------------

_EVAL_DOC = (
    Path(__file__).parent.parent / "docs" / "eval" / "briefing-rerank-benchmark.md"
)


def _write_eval_doc(
    r5_baseline: float,
    r5_reranked: float,
    fixture_desc: str,
    run_mode: str,
    per_difficulty: dict[str, tuple[float, float]],
) -> None:
    """Append a benchmark run entry to docs/eval/briefing-rerank-benchmark.md."""
    _EVAL_DOC.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).isoformat()
    delta = r5_reranked - r5_baseline
    verdict = "✅ PASS" if r5_reranked >= r5_baseline else "❌ REGRESSION"

    lines = [
        f"\n## Run: {ts}",
        "",
        f"**Mode**: {run_mode}  ",
        f"**Fixture**: {fixture_desc}  ",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| R@5 baseline (weight=0.0) | {r5_baseline:.3f} |",
        f"| R@5 reranked (weight=1.0) | {r5_reranked:.3f} |",
        f"| Delta | {delta:+.3f} |",
        f"| Verdict | {verdict} |",
        "",
        "**Per-difficulty breakdown:**",
        "",
        "| Difficulty | Baseline | Reranked | Δ |",
        "|---|---|---|---|",
    ]
    for diff, (b, t) in sorted(per_difficulty.items()):
        lines.append(f"| {diff} | {b:.3f} | {t:.3f} | {t - b:+.3f} |")

    content = "\n".join(lines) + "\n"

    # Ensure header exists in file
    if not _EVAL_DOC.exists():
        header = "# Briefing Re-ranking Benchmark\n\nR@5 recall gate for CP4 importance re-ranking.\n"
        _EVAL_DOC.write_text(header, encoding="utf-8")

    with _EVAL_DOC.open("a", encoding="utf-8") as f:
        f.write(content)


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------


class TestBenchmarkRecallPurePython:
    """Pure-Python benchmark: deterministic simulation over 200 drawers × 30 queries.

    test_zero_regression_guarantee and test_max_boost_does_not_dominate_semantic
    are FAST math checks that run in the default suite (no benchmark mark).
    test_rerank_improves_high_importance_results is the full recall gate and
    is marked @pytest.mark.benchmark (excluded from default runs).
    """

    def test_zero_regression_guarantee(self) -> None:
        """When all importance=0.5 (no KG facts), weight=1.0 == weight=0.0 on every query."""
        neutral = {d["id"]: 0.5 for d in ALL_DRAWERS}

        for i in range(len(RECALL_QUERIES)):
            raw = simulate_search(i)
            baseline = simulate_rerank(raw, neutral, weight=0.0)
            treatment = simulate_rerank(raw, neutral, weight=1.0)
            assert baseline == treatment, (
                f"Query {i}: zero-regression violated.\n"
                f"  Baseline: {baseline}\n  Treatment: {treatment}"
            )

    def test_max_boost_does_not_dominate_semantic(self) -> None:
        """Max importance boost (+0.04) cannot overcome a semantic gap > 0.04."""
        high_imp = {
            "id": "d001",
            "score": 0.85,
            "room": "auth",
            "text": "auth decision",
        }
        high_sem = {
            "id": "d999",
            "score": 0.92,
            "room": "other",
            "text": "other content",
        }
        lookup = {**DRAWER_IMPORTANCE, "d999": 0.5}
        reranked = _rerank_by_importance([high_sem, high_imp], lookup, weight=1.0)
        assert reranked[0]["id"] == "d999", (
            "Semantic dominance violated: high-importance + low-semantic jumped above high-semantic"
        )

    @pytest.mark.benchmark
    def test_rerank_improves_high_importance_results(self) -> None:
        """Full R@5 recall gate over 200 drawers × 30 queries.

        Verifies: R@5_reranked >= R@5_baseline (no regression gate).
        Writes results to docs/eval/briefing-rerank-benchmark.md.
        """
        baseline_by_diff: dict[str, list[float]] = {
            "easy": [],
            "medium": [],
            "hard": [],
        }
        treatment_by_diff: dict[str, list[float]] = {
            "easy": [],
            "medium": [],
            "hard": [],
        }

        neutral = {d["id"]: 0.5 for d in ALL_DRAWERS}

        for i, query in enumerate(RECALL_QUERIES):
            diff = query.get("difficulty", "easy")
            raw = simulate_search(i)
            r5_b = recall_at_5(
                simulate_rerank(raw, neutral, weight=0.0), query["expected_ids"]
            )
            r5_t = recall_at_5(
                simulate_rerank(raw, DRAWER_IMPORTANCE, weight=1.0),
                query["expected_ids"],
            )
            baseline_by_diff[diff].append(r5_b)
            treatment_by_diff[diff].append(r5_t)

        def avg(lst: list[float]) -> float:
            return sum(lst) / len(lst) if lst else 0.0

        r5_baseline = avg([s for scores in baseline_by_diff.values() for s in scores])
        r5_reranked = avg([s for scores in treatment_by_diff.values() for s in scores])

        per_difficulty = {
            diff: (avg(baseline_by_diff[diff]), avg(treatment_by_diff[diff]))
            for diff in ["easy", "medium", "hard"]
        }

        print("\nPure-Python benchmark (200 drawers × 30 queries):")
        print(f"  R@5_baseline  = {r5_baseline:.3f}")
        print(f"  R@5_reranked  = {r5_reranked:.3f}")
        print(f"  Delta         = {r5_reranked - r5_baseline:+.3f}")
        for diff, (b, t) in sorted(per_difficulty.items()):
            print(f"  {diff:8s}: baseline={b:.3f}  reranked={t:.3f}  Δ={t - b:+.3f}")

        _write_eval_doc(
            r5_baseline=r5_baseline,
            r5_reranked=r5_reranked,
            fixture_desc="200 drawers × 30 queries (pure-Python simulation)",
            run_mode="pure-Python (no real palace; simulated semantic scores)",
            per_difficulty=per_difficulty,
        )

        assert r5_reranked >= r5_baseline, (
            f"BENCHMARK REGRESSION: R@5 dropped from {r5_baseline:.3f} to {r5_reranked:.3f}.\n"
            "Block CP4. See spec Section 9.4 for mitigations."
        )


# ---------------------------------------------------------------------------
# Full integration benchmark (requires mempalace CLI)
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
@skip_no_mempalace
class TestBenchmarkRecallIntegration:
    """
    Full integration benchmark using a real MemPalace instance.
    SKIPPED unless mempalace CLI is installed.
    """

    @pytest.fixture(scope="class")
    def palace_dir(self, tmp_path_factory: Any) -> Path:
        palace = tmp_path_factory.mktemp("benchmark_palace")
        result = subprocess.run(
            ["mempalace", "init", str(palace)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            pytest.skip(f"mempalace init failed: {result.stderr}")
        return palace

    def _mcp(self, palace_dir: Path, tool: str, args: dict) -> dict:
        payload = json.dumps({"tool": tool, "arguments": args})
        env = {**os.environ, "MEMPALACE_DIR": str(palace_dir)}
        result = subprocess.run(
            ["mempalace", "mcp", "--call", payload],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        if result.returncode != 0:
            return {"error": result.stderr}
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"result": result.stdout.strip()}

    def test_benchmark_recall_r5_no_regression(self, palace_dir: Path) -> None:
        """Full integration benchmark: seed real palace, run semantic search, verify R@5."""
        from typing import Any  # noqa: F401 (used in fixture type hint)

        # Seed palace with all 200 drawers
        for d in ALL_DRAWERS:
            self._mcp(
                palace_dir,
                "mempalace_add_drawer",
                {
                    "wing": d["wing"],
                    "room": d["room"],
                    "content": d["content"],
                    "added_by": "benchmark",
                },
            )

        # Run queries — no importance backfill for baseline
        baseline_scores: list[float] = []
        treatment_scores: list[float] = []

        for query in RECALL_QUERIES:
            raw = self._mcp(
                palace_dir,
                "mempalace_search",
                {
                    "query": query["query"],
                    "wing": "wing_myapp",
                    "limit": 8,
                },
            ).get("results", [])
            if not raw:
                baseline_scores.append(0.0)
                treatment_scores.append(0.0)
                continue

            top5_b = [r.get("id", "") for r in raw[:5]]

            imp_lookup: dict[str, float] = {}
            for r in raw:
                rid = r.get("id", "")
                if rid:
                    kg = self._mcp(
                        palace_dir, "mempalace_kg_query", {"entity": f"drawer:{rid}"}
                    )
                    for fact in kg.get("facts", []):
                        if fact.get("predicate") == "has_importance":
                            try:
                                imp_lookup[rid] = float(fact["object"])
                            except (ValueError, KeyError):
                                pass

            reranked = _rerank_by_importance(raw, imp_lookup, weight=1.0)
            top5_t = [r.get("id", "") for r in reranked[:5]]

            baseline_scores.append(recall_at_5(top5_b, query["expected_ids"]))
            treatment_scores.append(recall_at_5(top5_t, query["expected_ids"]))

        r5_b = sum(baseline_scores) / max(len(baseline_scores), 1)
        r5_t = sum(treatment_scores) / max(len(treatment_scores), 1)

        _write_eval_doc(
            r5_baseline=r5_b,
            r5_reranked=r5_t,
            fixture_desc="200 drawers × 30 queries (real MemPalace integration)",
            run_mode="integration (real palace + real embeddings)",
            per_difficulty={"all": (r5_b, r5_t)},
        )

        print(
            f"\nIntegration benchmark: R@5_baseline={r5_b:.3f}, R@5_reranked={r5_t:.3f}"
        )
        assert r5_t >= r5_b, f"REGRESSION: {r5_b:.3f} → {r5_t:.3f}"
