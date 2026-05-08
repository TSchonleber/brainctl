"""CLI handlers for ``brainctl cognition`` and ``brainctl interest``.

Two related concepts ship together because they're meaningless apart:

* ``cognition`` — set / inspect an agent's cognitive profile
  (``neurotypical`` default, ``autistic`` opt-in). Profiles retune the
  W(m) gate, AGM threshold, Bayesian recall priors, and retrieval
  rerank without forking the codepaths.

* ``interest`` — first-class special-interest tagging on entities
  (monotropism). Mark an entity as a special interest and it gains a
  retention multiplier, retire-resistance, and a ``--focus`` retrieval
  boost — but only when the agent's profile actually amplifies them
  (``monotropic_focus_boost > 1.0``).

The retuning constants live in ``agentmemory.cognitive_profile``; this
module is just thin CLI plumbing. See ``docs/AUTISTIC_BRAIN.md`` for the
design doc and the cognitive-science citations behind each profile.

Pattern mirrors ``commands/sign.py`` and ``commands/wallet.py``: a
``register_parser(sub)`` entry point + ``cmd_cognition(args)`` and
``cmd_interest(args)`` dispatchers.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from typing import Any


def _get_db() -> sqlite3.Connection:
    from agentmemory.paths import get_db_path
    conn = sqlite3.connect(str(get_db_path()), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _emit(payload: dict[str, Any], *, as_json: bool, exit_code: int = 0) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        if "ok" in payload and not payload["ok"] and payload.get("error"):
            print(f"FAIL: {payload['error']}", file=sys.stderr)
        elif payload.get("ok"):
            print("OK")
        for key in (
            "agent_id", "profile", "previous_profile",
            "entity_id", "entity_name", "special_interest", "interest_strength",
            "count",
        ):
            if key in payload and payload[key] is not None:
                print(f"  {key}: {payload[key]}")
    sys.exit(exit_code)


# ---------------------------------------------------------------------------
# cognition handlers
# ---------------------------------------------------------------------------

def cmd_cognition(args) -> None:
    sub = getattr(args, "cognition_cmd", None)
    if sub == "list":
        _cmd_cognition_list(args)
    elif sub == "show":
        _cmd_cognition_show(args)
    elif sub == "set":
        _cmd_cognition_set(args)
    elif sub == "status":
        _cmd_cognition_status(args)
    else:
        print("Usage: brainctl cognition {list|show|set|status} ...", file=sys.stderr)
        sys.exit(2)


def _cmd_cognition_list(args) -> None:
    from agentmemory.cognitive_profile import list_profiles
    profiles = list_profiles()
    if getattr(args, "json", False):
        print(json.dumps(profiles, indent=2, default=str))
        sys.exit(0)
    print("Available cognitive profiles:")
    for p in profiles:
        print(f"  {p['name']:14s}  {p['description']}")
    sys.exit(0)


def _cmd_cognition_show(args) -> None:
    from agentmemory.cognitive_profile import get_profile, VALID_PROFILES
    name = args.name
    if name not in VALID_PROFILES:
        _emit(
            {"ok": False, "error": f"Unknown profile {name!r}. "
                                   f"Valid: {sorted(VALID_PROFILES)}"},
            as_json=getattr(args, "json", False),
            exit_code=1,
        )
    p = get_profile(name)
    if getattr(args, "json", False):
        print(json.dumps(p, indent=2, default=str))
        sys.exit(0)
    print(f"Profile: {p['name']}")
    print(f"  description: {p['description']}")
    print(f"  W(m) weights: novelty={p['wm_novelty_weight']:.2f} "
          f"utility={p['wm_utility_weight']:.2f} "
          f"importance={p['wm_importance_weight']:.2f} "
          f"scope={p['wm_scope_weight']:.2f}")
    print(f"  W(m) skip threshold: {p['wm_skip_threshold']:.2f}")
    print(f"  AGM threshold: {p['agm_threshold']:.2f} "
          f"(preserve_both_on_tie={p['agm_preserve_both_on_tie']})")
    print(f"  Bayesian priors: alpha={p['bayesian_alpha_prior']:.2f} "
          f"beta={p['bayesian_beta_prior']:.2f}")
    print(f"  Recency half-life: {p['credibility_recency_half_life_days']:.0f} days")
    print(f"  Recall log divisor: {p['credibility_recall_log_divisor']:.1f}")
    print(f"  Preserve contradictions: {p['preserve_contradictions']}")
    print(f"  Monotropic focus boost: {p['monotropic_focus_boost']:.2f}×")
    print(f"  Interest retention mult: {p['interest_retention_multiplier']:.2f}×")
    print(f"  Sensory overload threshold: {p['sensory_overload_threshold']}")
    sys.exit(0)


def _cmd_cognition_set(args) -> None:
    from agentmemory.cognitive_profile import set_agent_profile, VALID_PROFILES
    name = args.profile
    agent = args.agent
    if name not in VALID_PROFILES:
        _emit(
            {"ok": False, "error": f"Unknown profile {name!r}. "
                                   f"Valid: {sorted(VALID_PROFILES)}"},
            as_json=getattr(args, "json", False),
            exit_code=1,
        )
    db = _get_db()
    try:
        prev = db.execute(
            "SELECT cognitive_profile FROM agents WHERE id = ?", (agent,)
        ).fetchone()
        if not prev:
            _emit(
                {"ok": False, "error": f"Agent {agent!r} not found"},
                as_json=getattr(args, "json", False),
                exit_code=1,
            )
        prev_name = prev["cognitive_profile"] if prev else None
        set_agent_profile(db, agent, name)
    finally:
        db.close()
    _emit(
        {"ok": True, "agent_id": agent, "profile": name, "previous_profile": prev_name},
        as_json=getattr(args, "json", False),
    )


def _cmd_cognition_status(args) -> None:
    from agentmemory.cognitive_profile import get_agent_profile_name
    agent = args.agent or os.environ.get("BRAINCTL_AGENT_ID")
    if not agent:
        _emit(
            {"ok": False, "error": "Pass --agent or set $BRAINCTL_AGENT_ID"},
            as_json=getattr(args, "json", False),
            exit_code=2,
        )
    db = _get_db()
    try:
        name = get_agent_profile_name(db, agent)
    finally:
        db.close()
    _emit(
        {"ok": True, "agent_id": agent, "profile": name},
        as_json=getattr(args, "json", False),
    )


# ---------------------------------------------------------------------------
# interest handlers (special-interest tagging on entities)
# ---------------------------------------------------------------------------

def cmd_interest(args) -> None:
    sub = getattr(args, "interest_cmd", None)
    if sub == "add":
        _cmd_interest_add(args)
    elif sub == "remove":
        _cmd_interest_remove(args)
    elif sub == "list":
        _cmd_interest_list(args)
    else:
        print("Usage: brainctl interest {add|remove|list} ...", file=sys.stderr)
        sys.exit(2)


def _resolve_entity(db: sqlite3.Connection, name_or_id: str) -> sqlite3.Row | None:
    # Try id first, then unique-by-name.
    if name_or_id.isdigit():
        row = db.execute(
            "SELECT id, name, scope, special_interest, interest_strength "
            "FROM entities WHERE id = ? AND retired_at IS NULL",
            (int(name_or_id),),
        ).fetchone()
        if row:
            return row
    row = db.execute(
        "SELECT id, name, scope, special_interest, interest_strength "
        "FROM entities "
        "WHERE name = ? AND retired_at IS NULL "
        "ORDER BY updated_at DESC LIMIT 1",
        (name_or_id,),
    ).fetchone()
    return row


def _cmd_interest_add(args) -> None:
    strength = float(args.strength)
    if not (0.0 <= strength <= 1.0):
        _emit(
            {"ok": False, "error": f"--strength must be in [0.0, 1.0], got {strength}"},
            as_json=getattr(args, "json", False),
            exit_code=2,
        )
    db = _get_db()
    try:
        row = _resolve_entity(db, args.entity)
        if not row:
            _emit(
                {"ok": False, "error": f"Entity {args.entity!r} not found"},
                as_json=getattr(args, "json", False),
                exit_code=1,
            )
        db.execute(
            "UPDATE entities SET special_interest = 1, interest_strength = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (strength, row["id"]),
        )
        db.commit()
    finally:
        db.close()
    _emit(
        {
            "ok": True,
            "entity_id": row["id"],
            "entity_name": row["name"],
            "special_interest": True,
            "interest_strength": strength,
        },
        as_json=getattr(args, "json", False),
    )


def _cmd_interest_remove(args) -> None:
    db = _get_db()
    try:
        row = _resolve_entity(db, args.entity)
        if not row:
            _emit(
                {"ok": False, "error": f"Entity {args.entity!r} not found"},
                as_json=getattr(args, "json", False),
                exit_code=1,
            )
        db.execute(
            "UPDATE entities SET special_interest = 0, interest_strength = 0.0, "
            "updated_at = datetime('now') WHERE id = ?",
            (row["id"],),
        )
        db.commit()
    finally:
        db.close()
    _emit(
        {
            "ok": True,
            "entity_id": row["id"],
            "entity_name": row["name"],
            "special_interest": False,
        },
        as_json=getattr(args, "json", False),
    )


def _cmd_interest_list(args) -> None:
    db = _get_db()
    try:
        params: list[Any] = []
        sql = (
            "SELECT id, name, entity_type, scope, interest_strength "
            "FROM entities "
            "WHERE special_interest = 1 AND retired_at IS NULL"
        )
        if args.scope:
            sql += " AND scope = ?"
            params.append(args.scope)
        sql += " ORDER BY interest_strength DESC, updated_at DESC"
        if args.limit:
            sql += f" LIMIT {int(args.limit)}"
        rows = db.execute(sql, params).fetchall()
    finally:
        db.close()
    out = [dict(r) for r in rows]
    if getattr(args, "json", False):
        print(json.dumps(out, indent=2, default=str))
        sys.exit(0)
    if not out:
        print("No special-interest entities tagged.")
        sys.exit(0)
    print(f"Special-interest entities ({len(out)}):")
    for r in out:
        print(
            f"  #{r['id']:5d} [{r['entity_type']:10s}] "
            f"strength={r['interest_strength']:.2f}  "
            f"{r['name']}  ({r['scope']})"
        )
    sys.exit(0)


# ---------------------------------------------------------------------------
# Parser registration
# ---------------------------------------------------------------------------

def register_parser(sub: Any) -> None:
    """Attach ``cognition`` and ``interest`` top-level subcommands."""
    cog = sub.add_parser(
        "cognition",
        help="Manage per-agent cognitive profiles (neurotypical, autistic)",
        description=(
            "Cognitive profiles retune brainctl's W(m) gate, AGM "
            "conflict-resolution threshold, Bayesian recall priors, and "
            "retrieval rerank without forking the codepaths. The "
            "neurotypical profile (default) preserves all pre-052 "
            "behavior. The autistic profile applies HIPPEA-grounded "
            "retuning — see docs/AUTISTIC_BRAIN.md."
        ),
    )
    cog_sub = cog.add_subparsers(dest="cognition_cmd")

    p_list = cog_sub.add_parser("list", help="List built-in cognitive profiles")
    p_list.add_argument("--json", action="store_true")

    p_show = cog_sub.add_parser("show", help="Show one profile's tunables")
    p_show.add_argument("name", help="Profile name (neurotypical|autistic)")
    p_show.add_argument("--json", action="store_true")

    p_set = cog_sub.add_parser(
        "set", help="Set an agent's cognitive profile",
    )
    p_set.add_argument("profile", help="Profile name (neurotypical|autistic)")
    p_set.add_argument("--agent", "-a", required=True, help="Agent id")
    p_set.add_argument("--json", action="store_true")

    p_status = cog_sub.add_parser(
        "status", help="Show an agent's current cognitive profile",
    )
    p_status.add_argument("--agent", "-a", default=None,
                          help="Agent id (defaults to $BRAINCTL_AGENT_ID)")
    p_status.add_argument("--json", action="store_true")

    # ---- interest ----
    intr = sub.add_parser(
        "interest",
        help="Tag entities as special interests (monotropism)",
        description=(
            "Special-interest tagging is a first-class concept for the "
            "autistic cognitive profile. Tagged entities receive a "
            "retention multiplier and a retrieval boost when --focus "
            "matches them, but only when the agent's profile actually "
            "amplifies them (monotropic_focus_boost > 1.0)."
        ),
    )
    intr_sub = intr.add_subparsers(dest="interest_cmd")

    p_add = intr_sub.add_parser("add", help="Mark an entity as a special interest")
    p_add.add_argument("entity", help="Entity id or unique name")
    p_add.add_argument("--strength", type=float, default=0.75,
                       help="Interest strength 0.0–1.0 (default: 0.75)")
    p_add.add_argument("--json", action="store_true")

    p_rm = intr_sub.add_parser("remove", help="Untag a special interest")
    p_rm.add_argument("entity", help="Entity id or unique name")
    p_rm.add_argument("--json", action="store_true")

    p_ls = intr_sub.add_parser("list", help="List tagged special interests")
    p_ls.add_argument("--scope", "-s", default=None, help="Filter by scope")
    p_ls.add_argument("--limit", "-l", type=int, default=50)
    p_ls.add_argument("--json", action="store_true")
