# Plugins + Tests + Bench + CI — Audit 2026-04-19 (v2.4.6)

## Executive summary

The plugin suite (19 first-party) is structurally sound for the eight
fully-implemented integrations. The three new plugins shipped in 2.4.3
(Goose, OpenCode, Pi) are high quality — idempotent installers, proper
preflight checks, uninstall paths. However, all three contain a silent
misconfiguration: they inject `BRAINCTL_DB` into the MCP server environment
while the MCP server only reads `BRAIN_DB`. Any user with a non-default
brain path gets the wrong database without any error.

The CI gates are substantially tighter post-2.4.6. `continue-on-error` was
correctly dropped from the latency gate. The `retrieval-gate` path filter,
strict budget YAMLs, and per-PR bench matrix comments are well-designed.
Two structural gaps remain: (1) the publish workflow fires on tag push
without waiting for CI, enabling accidental broken releases; (2) there is
no nightly workflow even though one is explicitly called for in the CI
comments. The Dockerfile runs as root with an incomplete `.dockerignore`
that would bundle `brain.db` (user memories) into a local image build.
Seven of 19 plugins are documented placeholders, but this is intended and
clearly flagged in `TRADING_INTEGRATIONS.md`.

Test hygiene is good overall. Two `xfail` tests exist with documented
reasons; neither uses `strict=True`, meaning a silent fix (XPASS) won't
fail the suite — low risk but worth tracking. One test (`test_close_is_idempotent`)
has no `assert`, testing "must not raise" implicitly.

---

## Methodology

- Read all 19 plugin directories (`plugins/*/brainctl/`) for structure, install.py, plugin.yaml, README.
- Read CI workflow files (`.github/workflows/ci.yml`, `publish.yml`) end-to-end.
- Read `pyproject.toml` for extra consistency and dep pinning.
- Read `Dockerfile` and `.dockerignore`.
- Grep source (`src/agentmemory/paths.py`, `mcp_server.py`) to verify env var resolution.
- Surveyed `tests/` for xfail, skip hygiene, missing assertions.
- Read `tests/bench/gate.py`, `tests/bench/run.py`, `tests/bench/budgets/*.yaml`.
- Reviewed `CHANGELOG.md` against `git log` and `git tag`.
- Confirmed commit `1789d94` (drop continue-on-error) landed correctly.
- Spot-checked hook scripts for secret leakage: `plugins/claude-code/brainctl/hooks/post_tool_use.py` caps tool input at 200 chars (`_MAX_INPUT_CHARS = 200`), calls `redact_private()`, and logs only structured summary fields (no full tool results). `session_start.py` truncates memory content to 200 chars (line 59) and surfaces only the top 5 items per category. Both are clean — no credential or full-output exposure risk.

---

## Findings

### [HIGH] F-01: `BRAINCTL_DB` set by Goose/Pi/OpenCode MCP fragments — MCP server ignores it, silently uses wrong DB

**File(s):**
- `plugins/goose/brainctl/install.py:43` (`envs: {BRAINCTL_DB: ...}`)
- `plugins/pi/brainctl/pi-mcp.json.fragment:6` (`"env": {"BRAINCTL_DB": ...}`)
- `plugins/opencode/brainctl/opencode.json.fragment:9` (`"BRAINCTL_DB": ...`)
- `src/agentmemory/paths.py:14` (only reads `BRAIN_DB`)
- `src/agentmemory/mcp_server.py:208` (only checks `BRAIN_DB`/`BRAINCTL_HOME`)

**Claim:** When the MCP server is launched by Goose, Pi, or OpenCode with
`BRAINCTL_DB` in its environment, the server honours the user's configured
brain path.

**Evidence:** `paths.py:get_db_path()` returns
`Path(os.environ.get("BRAIN_DB", ...))`. The env var `BRAINCTL_DB` is never
read anywhere in `src/`. `mcp_server.py:208` only refreshes `DB_PATH` when
`BRAIN_DB` or `BRAINCTL_HOME` is set. The three plugin fragments use
`BRAINCTL_DB` as the env key.

**Impact:** A user who stores their brain at a non-default path and installs
Goose/Pi/OpenCode gets the default `~/agentmemory/db/brain.db` silently.
Writes from MCP tools land in a different database than the user expects.
No error is raised.

**Recommended fix:** Change the three MCP env blocks to use `BRAIN_DB` (the
canonical env var). Alternatively, add a `BRAINCTL_DB` alias to `paths.py`:
```python
def get_db_path() -> Path:
    val = os.environ.get("BRAIN_DB") or os.environ.get("BRAINCTL_DB")
    return Path(val or str(get_brain_home() / "db" / "brain.db")).expanduser()
```
The alias approach also repairs the gemini-cli MCP extension (which sets
`BRAINCTL_DB` in `gemini-extension.json`) for users relying on MCP rather
than the hook subprocess path.

---

### [HIGH] F-02: `publish.yml` fires on every `v*` tag push with no dependency on CI — broken release can publish to PyPI

**File(s):** `.github/workflows/publish.yml` (entire file)

**Claim:** A tag push only publishes to PyPI after tests pass.

**Evidence:** `publish.yml` has no `needs:` field and no `workflow_run:`
trigger. It fires immediately and independently of `ci.yml`. The only
safety net is `skip-existing: true` on the PyPI publish action, which
prevents republishing an existing version but does not prevent publishing
a broken new one.

Real scenario: a commit is pushed to `main`, a tag `vX.Y.Z` is pushed
immediately before the `test` / `latency-gate` / `retrieval-quality-gate`
jobs finish. `publish.yml` races ahead and publishes a broken version.

**Impact:** A defective version can reach PyPI and be `pip install`-ed by
users before the CI failure is noticed. `skip-existing: true` makes it
unrecoverable (a new version number is required to publish a fix).

**Recommended fix:** Add a `needs:` block referencing a CI workflow, or add
a `workflow_run:` trigger that waits for `CI` to succeed on the same SHA:
```yaml
on:
  workflow_run:
    workflows: ["CI"]
    types: [completed]
    branches-ignore: []
```
Then gate on `github.event.workflow_run.conclusion == 'success'` and
derive the tag from `github.event.workflow_run.head_sha`. Alternatively,
require branch protection rules to enforce all status checks before
allowing tag creation on commits pushed to `main`.

---

### [MEDIUM] F-03: Dockerfile runs as root, `.dockerignore` misses `brain.db` and sensitive runtime dirs

**File(s):** `Dockerfile`, `.dockerignore`

**Claim:** Docker image is minimal and does not bundle user data.

**Evidence:** `Dockerfile` has no `USER` directive — the container runs
as root. `.dockerignore` excludes `db/` but not `brain.db` (which exists
at the repository root at `/Users/r4vager/agentmemory/brain.db`), nor
`logs/`, `backups/`, `blobs/`, `tmp/`, or `config/`. A `docker build .`
run from the `agentmemory` working directory would include the live
`brain.db` (containing real agent memories), log files, and backup
archives in every image layer. `pip install .[all]` with no
`--no-cache-dir` also runs as root, creating cache files owned by root.

**Impact:** (a) Security: user memory data leaks into Docker images pushed
to a registry. (b) Security: running as root violates container hardening
best-practices; a compromised MCP server process has full container
privileges. (c) Image bloat from bundled runtime data.

**Recommended fix:**
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .[all] \
    && useradd -r -u 1001 -g root brainctl \
    && mkdir -p /data && chown brainctl /data
USER brainctl
ENV BRAIN_DB=/data/brain.db
VOLUME /data
CMD ["brainctl-mcp"]
```
Add to `.dockerignore`:
```
brain.db
logs/
backups/
blobs/
tmp/
config/
benchmarks/
research/
docs/
```

---

### [MEDIUM] F-04: `baseline_p95_ms: null` in both bench budget YAMLs — latency leg of retrieval-gate is advisory only

**File(s):**
- `tests/bench/budgets/locomo.yaml:27` (`baseline_p95_ms: null`)
- `tests/bench/budgets/longmemeval.yaml:29` (`baseline_p95_ms: null`)
- `tests/bench/gate.py:440-444`

**Claim:** The `retrieval-gate` CI job enforces an end-to-end latency
ceiling of p95 <= baseline * 1.15 per the plan envelope.

**Evidence:** Both budget YAMLs have `baseline_p95_ms: null`. In
`gate.py:check_latency()`, when `base <= 0`, the check returns
`informational=True` (line 336-341), meaning it is logged but never fails
the gate. This is documented in the 2.4.6 CHANGELOG as a known follow-up:
"I5 driver couldn't parse per-query timings → `baseline_p95_ms` still
missing in budget YAMLs; p95 leg is advisory until populated."

**Impact:** A commit that doubles end-to-end retrieval latency passes the
`retrieval-gate` job without any CI failure. The latency dimension of the
"top-heavy" plan envelope is unenforced.

**Recommended fix:** Run the bench on a stable machine, extract the p95
latency from the run, and populate `baseline_p95_ms` in each YAML:
```bash
python3 -m tests.bench.run --bench locomo --check-strict --report-json /tmp/loc.json
python3 -c "import json; d=json.load(open('/tmp/loc.json')); print(d['strict']['latency']['current_p95_ms'])"
# Set that value as baseline_p95_ms in locomo.yaml (+ 10% headroom)
```

---

### [MEDIUM] F-05: No nightly CI workflow — Ollama-backed quality gate never runs; cmd-backend regression undetected in CI

**File(s):** `.github/workflows/` (directory — only `ci.yml` and `publish.yml` exist)

**Claim:** `ci.yml` line 87: "Those tiers [Ollama-dependent LOCOMO + LongMemEval] should run on a nightly workflow against a runner with Ollama pre-installed."

**Evidence:** Only `ci.yml` and `publish.yml` exist. The `retrieval-gate`
job uses `--backend brain` (FTS5-only) because stock GitHub runners have
no Ollama. The hybrid `cmd` backend — which is what end users actually
exercise — is never tested in CI. A regression in the vector-fusion or
CE-rerank path that leaves FTS5 unaffected would pass all CI checks
undetected.

**Impact:** The 2.4.6 CHANGELOG highlight ("LoCoMo hybrid: Hit@1 +25.5pp,
MRR +36.2pp vs pre-lift") was measured manually and is not re-gated in CI.
A future PR could silently regress the hybrid path.

**Recommended fix:** Create `.github/workflows/nightly.yml` with a
`schedule: cron: '0 4 * * *'` trigger. Run on a self-hosted runner with
Ollama pre-installed, or use `ollama serve` in CI via the
`jmorganca/setup-ollama` action. Gate `--backend cmd` on both LOCOMO and
LongMemEval with `--check-strict`.

---

### [MEDIUM] F-06: Version gap — `v2.4.4` does not exist in git tags or `CHANGELOG.md`

**File(s):** `CHANGELOG.md`, `git tag` output

**Claim:** `CHANGELOG.md` covers all shipped versions from 2.4.3 to 2.4.6.

**Evidence:** `git tag --sort=-version:refname` shows `v2.4.6, v2.4.3,
v2.4.2 ...` — `v2.4.4` is absent. `CHANGELOG.md` lists `[2.4.6]`,
`[2.4.5]`, `[2.4.3]` with no `[2.4.4]` entry. The `pyproject.toml` at
the `38cbb6e` (2.4.5 release) commit already reads `version = "2.4.5"`,
confirming 2.4.4 was skipped, not just untagged.

**Impact:** Downstream tooling that parses `CHANGELOG.md` or the PyPI
version sequence will see a gap and may flag it as a missing release or
audit anomaly. This is also confusing for contributors tracking what
changed when.

**Recommended fix:** Add a short note in `CHANGELOG.md` above `[2.4.5]`:
```markdown
## [2.4.4] — skipped
Version number not issued; 2.4.3 → 2.4.5 intentional.
```

---

### [MEDIUM] F-07: `test_code_ingest.py` — CE rerank dimension and `BRAINCTL_DISABLE_INTENT_ROUTER` ablation bypass have no test coverage

**File(s):**
- `src/agentmemory/_impl.py:6182` (`BRAINCTL_DISABLE_INTENT_ROUTER`)
- `tests/` — no test file for CE budget gate or intent-router bypass
- `CHANGELOG.md` 2.4.6 Known follow-ups section

**Claim:** The new retrieval controls introduced in 2.4.6 (I2–I4) are
covered by the test suite.

**Evidence:** `grep -r "BRAINCTL_DISABLE_INTENT_ROUTER" tests/` returns
nothing. `grep -r "BRAINCTL_CE_P95_BUDGET_MS" tests/` returns nothing.
`tests/test_topheavy_rollout.py` covers `_resolve_topheavy_rollout` but
not the CE budget fallback path or the intent-router bypass. The CHANGELOG
itself notes: "CE rerank dimension unreachable via bench harness
(`args.rerank` not populated by `locomo_eval.py` / `longmemeval_eval.py`). Wire it to measure CE in the calibration matrix."

**Impact:** A regression in CE rerank fallback logic (e.g., the budget
gate misfires and always falls back to RRF ordering) would not be caught
by the test suite. Similarly, `BRAINCTL_DISABLE_INTENT_ROUTER=1` could be
broken without CI noticing.

**Recommended fix:** Add unit tests for:
1. CE fallback: mock a reranker that takes > `_CE_P95_BUDGET_MS` ms, assert
   the result is the input ordering (RRF pass-through).
2. Intent-router bypass: set `BRAINCTL_DISABLE_INTENT_ROUTER=1`, verify
   `cmd_search` skips intent classification and uses the default profile.
3. Wire `--rerank` into `locomo_eval.py` and `longmemeval_eval.py` so the
   calibration matrix can measure the CE dimension.

---

### [LOW] F-08: 7 of 19 plugins are undocumented placeholders counted in "19 first-party plugins" headline

**File(s):**
- `plugins/coinbase-agentkit/brainctl/plugin.yaml:4` (`status: placeholder`)
- `plugins/hummingbot/brainctl/plugin.yaml:4` (`status: placeholder`)
- `plugins/nautilustrader/brainctl/plugin.yaml:4` (`status: placeholder`)
- `plugins/octobot/brainctl/plugin.yaml:4` (`status: placeholder`)
- `plugins/rig/brainctl/plugin.yaml:4` (`status: placeholder`)
- `plugins/virtuals-game/brainctl/plugin.yaml:4` (`status: placeholder`)
- `plugins/zerebro/brainctl/plugin.yaml:4` (`status: placeholder`)

**Claim:** brainctl ships "19 first-party plugins" (2.4.3 CHANGELOG).

**Evidence:** 7 of the 19 plugin directories contain `status: placeholder`
in `plugin.yaml` with no implementation — only a README describing the
planned shape. `TRADING_INTEGRATIONS.md:195` documents the placeholder
convention accurately, but the headline number in the CHANGELOG and README
counts these as "first-party" without qualification.

**Impact:** External users counting on, say, a NautilusTrader or OctoBot
integration from the "19 plugins" headline will find an empty stub.
Misleading marketing.

**Recommended fix:** Qualify the count in public-facing copy: "19
first-party plugin directories (12 implemented, 7 on roadmap)." Or
filter placeholders from the count. The internal `TRADING_INTEGRATIONS.md`
already documents this clearly; the issue is in the CHANGELOG and
`README.md` headline numbers.

---

### [LOW] F-09: `test_close_is_idempotent` has no `assert` — "must not raise" is implicit

**File(s):** `tests/test_connection_lifecycle.py:167`

**Claim:** Tests verify the expected post-condition explicitly.

**Evidence:**
```python
def test_close_is_idempotent(db_file):
    brain = Brain(db_path=db_file, agent_id="idemp")
    brain.remember("hi")
    brain.close()
    brain.close()  # must not raise
    brain.close()  # still fine
```
No `assert` statement. The intent ("must not raise") is sound, but pytest
reports this as green even if `brain.close()` silently swallows an
exception internally.

**Impact:** Low — the test is correct in spirit. If `close()` raises, the
test will fail naturally. The risk is a future refactor wrapping the
exception internally.

**Recommended fix:**
```python
def test_close_is_idempotent(db_file):
    brain = Brain(db_path=db_file, agent_id="idemp")
    brain.remember("hi")
    for _ in range(3):
        brain.close()  # must not raise
    assert brain._db is None  # or whatever the closed-state sentinel is
```

---

### [LOW] F-10: Two `xfail` tests without `strict=True` — a passing XPASS goes unreported

**File(s):**
- `tests/test_integration.py:337`
- `tests/test_init.py:75`

**Evidence:**
```python
@pytest.mark.xfail(reason="FTS5 content-external table index-build timing issue on some SQLite versions — known issue, does not affect production (Brain.search works)")
```
Neither uses `strict=True`. If the FTS5 timing issue is resolved (e.g., by
the 2.4.6 `Brain.search` → `cmd_search` unification), the tests will
silently XPASS without triggering a warning that the `xfail` annotation
should be removed.

**Impact:** Stale xfail markers. Low severity — no functionality hidden.

**Recommended fix:** Add `strict=True` so an unexpected pass becomes a
test failure that prompts removing the marker; or remove the marker if the
timing issue is believed fixed by the 2.4.6 unification:
```python
@pytest.mark.xfail(
    strict=True,
    reason="FTS5 content-external table index-build timing issue..."
)
```

---

### [LOW] F-11: `latency-gate` CI job baseline is `darwin`, runs on `ubuntu-latest` — cross-platform skip only covers 5 ops

**File(s):**
- `tests/bench/baselines/latency.json` (`"platform": "darwin"`)
- `tests/test_latency_regression.py:109-115` (`SUBPROCESS_BOUND_OPS`)
- `.github/workflows/ci.yml:66-81`

**Claim:** Cross-platform latency gate is accurate after commit `6f21946`.

**Evidence:** The baseline was generated on darwin/M-class hardware (per
`latency.json:"platform": "darwin"`). CI runs on `ubuntu-latest`. The
`SUBPROCESS_BOUND_OPS` frozenset correctly skips 5 CLI-subprocess ops when
`baseline.platform != fresh.platform`. The comment says "Library-level ops
(`brain_search_*`, `brain_remember_*`, `vec_*`) stay gated cross-platform
because sqlite3 + FTS5 are deterministic enough."

This reasoning is sound for FTS5, but `vec_*` ops involve `sqlite-vec`
which uses platform-compiled SIMD intrinsics. On arm64 macOS vs x86_64
ubuntu, SIMD throughput differs significantly. The 50% threshold may be
wide enough to absorb this, but it is an assumption, not a calibration.

**Impact:** UNCERTAIN — the 50% threshold may be sufficient. If `sqlite-vec`
SIMD differences push ubuntu results >50% above darwin baseline on vec ops,
the gate will produce false positives. Confirming experiment: run
`BRAINCTL_RUN_BENCH=1 pytest tests/test_latency_regression.py -v` on an
ubuntu machine and compare `vec_*` results against the darwin baseline.

**Recommended fix:** Either add `vec_*` ops to `SUBPROCESS_BOUND_OPS` when
cross-platform (or document the measured darwin/ubuntu ratio on `vec_*` in
the test module docstring to justify keeping them gated).

---

### [LOW] F-12: `test_search_quality_bench.py` runs the full bench pipeline in the main `test` CI job (3 × Python versions)

**File(s):** `tests/test_search_quality_bench.py`, `.github/workflows/ci.yml:29`

**Claim:** The main CI `test` job runs unit + integration tests only.

**Evidence:** `ci.yml` runs `pytest tests/ -v` which recurses into all
test files including `tests/test_search_quality_bench.py`. That file has
no `BRAINCTL_RUN_BENCH` guard and calls `bench_eval.run(pipeline="cmd")`
unconditionally. The bench seeds a 30-memory temporary DB and runs 20
queries — fast (~1-2s) but not a unit test. It runs on all 3 Python
versions (3.11, 3.12, 3.13) on every push to `main` and every PR.

**Impact:** Low — 20 queries is fast. But it is a structural inconsistency:
the dedicated `retrieval-quality-gate` job exists precisely to gate this
benchmark. Having it also run in the main test matrix means any noise in
this bench (Thompson-sampling RNG drift despite `random.seed(42)`) can
cause a test failure that looks like a unit test failure. The `random.seed(42)`
fixture mitigates drift but relies on call-order stability.

**Recommended fix:** Add a `BRAINCTL_RUN_BENCH` guard to
`test_search_quality_bench.py` consistent with `test_locomo_bench.py` and
`test_latency_regression.py`, and run it only in the dedicated
`retrieval-quality-gate` job.

---

### [LOW] F-13: Three third-party CI Actions pinned by mutable tag, not commit SHA

**File(s):** `.github/workflows/ci.yml:145,222`, `.github/workflows/publish.yml`

**Evidence:**
- `dorny/paths-filter@v3` (retrieval-gate step 1)
- `actions/github-script@v7` (PR comment step)
- `pypa/gh-action-pypi-publish@release/v1`

All three are referenced by mutable version tag rather than an immutable
commit SHA. A supply-chain compromise or tag reassignment in any of these
upstream repositories would silently execute arbitrary code in the
brainctl CI pipeline with `id-token: write` (publish.yml) or
`pull-requests: write` (retrieval-gate) permissions — privileges that
can write to PyPI and post PR comments.

First-party Actions (`actions/checkout@v4`, `actions/setup-python@v5`,
`actions/cache@v4`, `actions/upload-artifact@v4`) carry the same risk but
are lower priority because they are maintained by GitHub itself and receive
independent security scanning.

**Impact:** Supply-chain attack surface. Low current likelihood but
high-impact if exploited (PyPI package poisoning or CI token abuse).

**Recommended fix:** Pin each to a commit SHA:
```yaml
# dorny/paths-filter v3.0.2 — 2024-11-11
- uses: dorny/paths-filter@de90cc6fb38fc0963ad72b210f1f284cd68cea36
# actions/github-script v7.0.1 — 2024-03-15
- uses: actions/github-script@60a0d83039c74a4aee543508d2ffcb1c3799cdea
# pypa/gh-action-pypi-publish v1.12.3 — 2025-01-30
- uses: pypa/gh-action-pypi-publish@76f52bc884231f62b9a034ebfe128415bbaabf9f
```
Or adopt Dependabot (`dependabot.yml`) with `ecosystem: github-actions`
to keep SHA pins up-to-date automatically.

---

## Changes Made

None — audit only. No code was modified.

---

## Summary Table

| ID | Severity | Area | Title |
|----|----------|------|-------|
| F-01 | HIGH | Plugins | `BRAINCTL_DB` ignored by MCP server — silent wrong-DB for Goose/Pi/OpenCode users |
| F-02 | HIGH | CI/Release | `publish.yml` fires on tag push with no CI dependency |
| F-03 | MEDIUM | Dockerfile | Runs as root; `.dockerignore` misses `brain.db` and runtime dirs |
| F-04 | MEDIUM | CI/Bench | `baseline_p95_ms: null` — latency gate is advisory only |
| F-05 | MEDIUM | CI | No nightly workflow; cmd-backend quality never CI-gated |
| F-06 | MEDIUM | Release | Version `2.4.4` skipped with no CHANGELOG entry |
| F-07 | MEDIUM | Tests | CE rerank + intent-router bypass have no test coverage |
| F-08 | LOW | Plugins | 7/19 plugins are placeholders; "19 first-party" headline inflated |
| F-09 | LOW | Tests | `test_close_is_idempotent` has no `assert` |
| F-10 | LOW | Tests | Two `xfail` without `strict=True` — silent XPASS goes unreported |
| F-11 | LOW | CI/Bench | `vec_*` ops gated cross-platform with 50% threshold, no darwin/ubuntu calibration |
| F-12 | LOW | Tests/CI | `test_search_quality_bench.py` runs bench pipeline in main test job (3 × Python) |
| F-13 | LOW | CI/Supply-chain | Third-party Actions pinned by mutable tag, not commit SHA |
