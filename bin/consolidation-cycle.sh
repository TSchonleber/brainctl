#!/usr/bin/env bash
# consolidation-cycle.sh — Daily maintenance job for brain.db
#
# Invoked by run_cognitive_cycle.sh (via cron or launchd).
# Uses importlib to load digit-prefixed research modules without a package layout.
#
# Usage:
#   AGENTMEMORY=$HOME/agentmemory bash consolidation-cycle.sh [--dry-run] [--dream-pass]
#
set -euo pipefail

AGENTMEMORY="${AGENTMEMORY:-$HOME/agentmemory}"
PYTHON="${AGENTMEMORY}/.venv/bin/python3"

if [ ! -x "$PYTHON" ]; then
  echo "ERROR: Python not found at $PYTHON" >&2
  exit 1
fi

DRY_RUN="False"
DREAM_PASS="False"
for arg in "$@"; do
  case "$arg" in
    --dry-run)    DRY_RUN="True" ;;
    --dream-pass) DREAM_PASS="True" ;;
  esac
done

exec "$PYTHON" - "$DRY_RUN" "$DREAM_PASS" <<'PYEOF'
import importlib.util, sys, os, json, subprocess

dry_run_flag  = sys.argv[1] if len(sys.argv) > 1 else "False"
dream_flag    = sys.argv[2] if len(sys.argv) > 2 else "False"
DRY_RUN       = dry_run_flag == "True"
RUN_DREAM     = dream_flag == "True"

AGENTMEMORY = os.environ.get("AGENTMEMORY", os.path.expanduser("~/agentmemory"))
sys.path.insert(0, AGENTMEMORY)


def _load(alias, rel_path):
    """Load a module by file path and register it under the given alias."""
    full_path = os.path.join(AGENTMEMORY, rel_path)
    spec = importlib.util.spec_from_file_location(alias, full_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


# Load dependencies under the names that 05_consolidation_cycle.py expects
_load("research.01_spaced_repetition",   "research/01_spaced_repetition.py")
_load("research.02_semantic_forgetting",  "research/02_semantic_forgetting.py")
_load("research.06_contradiction_detection", "research/06_contradiction_detection.py")

cycle = _load("_consolidation_cycle", "research/05_consolidation_cycle.py")

report = cycle.run_consolidation_cycle(dry_run=DRY_RUN, run_dream_pass=RUN_DREAM)
print(json.dumps(report, indent=2))

# Dream pass summary (COS-303)
if RUN_DREAM and report.get("dream_pass"):
    dp = report["dream_pass"]
    bi = dp.get("bisociation", {})
    inc = dp.get("incubation", {})
    print(
        f"[Dream] bisociation: {bi.get('pairs_evaluated', 0)} pairs scanned, "
        f"{bi.get('insights_written', 0)} insights written; "
        f"incubation: {inc.get('queries_checked', 0)} deferred queries, "
        f"{inc.get('resolved', 0)} resolved",
        file=sys.stderr,
    )

# Bridge-P1 (COS-210): after consolidation, sync high-confidence memories
# to Hermes compact block. Runs in report-only mode during dry-run.
sync_script = os.path.join(AGENTMEMORY, "bin", "sync-memory-block.py")
if os.path.isfile(sync_script):
    python_bin = sys.executable
    sync_cmd = [python_bin, sync_script, "--top-k", "20", "--json"]
    if not DRY_RUN:
        sync_cmd.append("--write")
    try:
        r = subprocess.run(sync_cmd, capture_output=True, text=True, timeout=30)
        if r.stdout.strip():
            try:
                sync_report = json.loads(r.stdout)
                added = len(sync_report.get("added_memory", [])) + len(sync_report.get("added_user", []))
                candidates = len(sync_report.get("candidates_memory", [])) + len(sync_report.get("candidates_user", []))
                print(f"[Bridge-P1] sync-memory-block: {candidates} candidates, {added} added to compact block",
                      file=sys.stderr)
            except json.JSONDecodeError:
                pass
    except (subprocess.TimeoutExpired, OSError):
        pass

sys.exit(0 if report.get("completed_at") else 1)
PYEOF

# ── Trust decay pass (COS-273) ───────────────────────────────────────────────
# Run trust decay once per consolidation cycle (daily). Skipped in dry-run mode.
BRAINCTL="${AGENTMEMORY}/bin/brainctl"
if [ -x "$BRAINCTL" ] && [ "$DRY_RUN" = "False" ]; then
  TRUST_JSON="$("$BRAINCTL" trust decay 2>/dev/null)" || true
  if [ -n "$TRUST_JSON" ]; then
    DECAYED=$(echo "$TRUST_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('decayed',0))" 2>/dev/null || echo "?")
    echo "[Trust] decay pass: ${DECAYED} memories updated" >&2
  fi
fi

# ── Metacognition gap scan (COS-218) ─────────────────────────────────────────
BRAINCTL="${AGENTMEMORY}/bin/brainctl"
if [ -x "$BRAINCTL" ]; then
  GAP_JSON="$("$BRAINCTL" gaps scan 2>/dev/null)" || true
  if [ -n "$GAP_JSON" ]; then
    TOTAL_GAPS=$(echo "$GAP_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('total_gaps',0))" 2>/dev/null || echo "?")
    echo "[Metacognition] Gap scan complete: ${TOTAL_GAPS} new gaps logged" >&2
  fi
fi

# ── Global Workspace salience-score pass (COS-314) ───────────────────────────
# Re-score all active memories and refresh gw_broadcast flags after consolidation.
BRAINCTL="${AGENTMEMORY}/bin/brainctl"
if [ -x "$BRAINCTL" ] && [ "$DRY_RUN" = "False" ]; then
  GW_JSON="$("$BRAINCTL" gw score 2>/dev/null)" || true
  if [ -n "$GW_JSON" ]; then
    GW_SCORED=$(echo "$GW_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('scored',0))" 2>/dev/null || echo "?")
    GW_BROADCAST=$(echo "$GW_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('gw_broadcast_set',0))" 2>/dev/null || echo "?")
    echo "[GW] salience pass: ${GW_SCORED} scored, ${GW_BROADCAST} in broadcast spotlight" >&2
  fi
fi

# ── Health SLO snapshot after consolidation ──────────────────────────────────
BRAINCTL="${AGENTMEMORY}/bin/brainctl"
if [ -x "$BRAINCTL" ]; then
  HEALTH_JSON="$("$BRAINCTL" health --json 2>/dev/null)" || true
  if [ -n "$HEALTH_JSON" ]; then
    COMPOSITE=$(echo "$HEALTH_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('composite_score','?'))" 2>/dev/null || echo "?")
    OVERALL=$(echo "$HEALTH_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('overall','?'))" 2>/dev/null || echo "?")
    ALERTS_STR=$(echo "$HEALTH_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print('; '.join(d.get('alerts',[])) or 'none')" 2>/dev/null || echo "none")
    if [ "$DRY_RUN" = "False" ]; then
      "$BRAINCTL" -a hippocampus event add \
        "Memory health check: composite=${COMPOSITE} status=${OVERALL}" \
        -t result -p agentmemory \
        --metadata "$HEALTH_JSON" 2>/dev/null || true
      # Alert if composite < 0.4 (critical) — write a memory so it surfaces in recall
      CRITICAL=$(echo "$HEALTH_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print('yes' if d.get('composite_score',1)<0.4 else 'no')" 2>/dev/null || echo "no")
      if [ "$CRITICAL" = "yes" ]; then
        ALERT_MSG="ALERT: Memory store health critical (score=${COMPOSITE}). Signals: ${ALERTS_STR}"
        "$BRAINCTL" -a hippocampus memory add "$ALERT_MSG" \
          -c environment -s project:agentmemory --confidence 0.9 2>/dev/null || true
      fi
    fi
    echo "[Health] composite=${COMPOSITE} overall=${OVERALL} alerts=${ALERTS_STR}" >&2
  fi
fi
