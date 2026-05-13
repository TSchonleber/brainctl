"""`brainctl import <provider> <source>` — onboarding from other memory
providers (mem0, generic JSON, more coming). By default, imported
memories land under a quarantine scope ``imported:<provider>`` so the
agent's primary scope stays clean until the user explicitly promotes
specific records via ``brainctl memory promote``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict


def _emit(payload: Dict[str, Any], *, as_json: bool, exit_code: int = 0) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(_format_human(payload))
    sys.exit(exit_code)


def _format_human(payload: Dict[str, Any]) -> str:
    if not payload.get("ok"):
        msg = f"import failed: {payload.get('error', 'unknown error')}"
        detail = payload.get("detail")
        if detail:
            msg += f"\n  detail: {detail}"
        return msg
    lines = [
        f"imported {payload['inserted']} memories from "
        f"{payload['provider']!r} ({payload['source']})",
        f"  scope:     {payload['scope']}",
        f"  skipped:   {payload['skipped']}",
        f"  warnings:  {len(payload.get('warnings') or [])}",
    ]
    if payload.get("warnings"):
        lines.append("  first 5 warnings:")
        for w in payload["warnings"][:5]:
            lines.append(f"    - {w}")
    if payload.get("dry_run"):
        lines.append("  (DRY RUN — nothing was written to brain.db)")
    return "\n".join(lines)


def cmd_import(args: Any) -> None:
    as_json = bool(getattr(args, "json", False))
    provider = args.provider
    source = Path(args.source).expanduser()

    from agentmemory import importers
    try:
        importer = importers.get_importer(provider)
    except importers.ImporterError as e:
        _emit({"ok": False, "error": "unknown_provider", "detail": str(e)},
              as_json=as_json, exit_code=2)
        return

    try:
        result = importer.parse(source)
    except importers.ImporterError as e:
        _emit({"ok": False, "error": "parse_failed", "detail": str(e),
               "provider": provider, "source": str(source)},
              as_json=as_json, exit_code=1)
        return
    except Exception as e:  # noqa: BLE001 — surface unexpected importer crash
        _emit({"ok": False, "error": "importer_crash", "detail": repr(e),
               "provider": provider, "source": str(source)},
              as_json=as_json, exit_code=1)
        return

    quarantine = not getattr(args, "no_quarantine", False)
    explicit_scope = getattr(args, "scope", None)
    if explicit_scope:
        scope = explicit_scope
    elif quarantine:
        scope = f"imported:{provider}"
    else:
        scope = "global"

    category_override = getattr(args, "category", None)
    dry_run = bool(getattr(args, "dry_run", False))

    inserted = 0
    skipped = 0
    insertion_warnings = list(result.warnings)

    if not dry_run:
        # Use the canonical cmd_memory_add path so FTS5 indexing,
        # vec_memories embeddings, and scope/tag persistence stay
        # consistent with `brainctl memory add`. We pass force=True
        # because imports are user-trusted bulk writes and we don't
        # want the W(m) worthiness gate rejecting near-duplicates
        # (which is the common shape of provider exports).
        from types import SimpleNamespace
        from agentmemory._impl import cmd_memory_add
        for rec in result.records:
            ns = SimpleNamespace(
                content=rec.content,
                category=category_override or rec.normalized_category(),
                tags=",".join(rec.tags) if rec.tags else "",
                type="episodic",
                confidence=rec.confidence,
                agent=rec.agent_id,
                scope=scope,
                force=True,
                dry_run_worthiness=False,
                reflexion=False,
                json=False,
            )
            try:
                cmd_memory_add(ns)
                inserted += 1
            except SystemExit as e:
                # cmd_memory_add may call sys.exit() on success or
                # failure depending on the path. exit_code 0 ⇒ ok.
                if getattr(e, "code", 0) in (None, 0):
                    inserted += 1
                else:
                    skipped += 1
                    insertion_warnings.append(
                        f"cmd_memory_add exit {e.code} on {rec.content[:60]}"
                    )
            except Exception as e:  # noqa: BLE001
                skipped += 1
                insertion_warnings.append(f"insert failed: {e}")
    else:
        inserted = len(result.records)

    payload = {
        "ok": True,
        "provider": provider,
        "source": str(source),
        "scope": scope,
        "parsed": len(result.records),
        "inserted": inserted,
        "skipped": result.skipped + skipped,
        "warnings": insertion_warnings,
        "dry_run": dry_run,
    }
    _emit(payload, as_json=as_json)


def register_parser(sub: Any) -> None:
    from agentmemory import importers

    providers = importers.list_importers()
    p = sub.add_parser(
        "import",
        help="Import memories from another provider (mem0, json, ...)",
        description=(
            "Onboard from another memory provider. Imported memories land in a "
            "quarantine scope (imported:<provider>) by default; promote into "
            "your primary scope explicitly when you've reviewed them."
        ),
    )
    p.add_argument("provider", choices=providers,
                   help="Source provider")
    p.add_argument("source", help="Path to the provider's export file")
    p.add_argument("--scope", default=None,
                   help="Override the destination scope "
                        "(default: imported:<provider>)")
    p.add_argument("--category", default=None,
                   help="Override the category for every imported memory "
                        "(default: provider's per-record mapping)")
    p.add_argument("--no-quarantine", dest="no_quarantine",
                   action="store_true",
                   help="Skip the quarantine scope — imports land in global. "
                        "Use only if you trust the source.")
    p.add_argument("--dry-run", dest="dry_run", action="store_true",
                   help="Parse + print summary without touching brain.db.")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_import)


__all__ = ["register_parser", "cmd_import"]
