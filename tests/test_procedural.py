"""Tests for the procedural memory service and Brain API integration."""

from __future__ import annotations

import sqlite3


class TestBrainProcedures:
    def test_remember_procedure_creates_bridge_and_structured_row(self, brain):
        result = brain.remember_procedure(
            goal="Deploy to staging safely",
            title="Staging deploy",
            description="Run tests, apply migrations, deploy, and verify health checks.",
            steps=[
                "Run tests",
                "Apply migrations",
                "Deploy release",
                "Verify health checks",
            ],
            tools_json=["pytest", "brainctl", "deployctl"],
        )

        conn = sqlite3.connect(str(brain.db_path))
        proc = conn.execute(
            "SELECT id, memory_id, title, goal FROM procedures WHERE id = ?",
            (result["id"],),
        ).fetchone()
        memory = conn.execute(
            "SELECT memory_type, content FROM memories WHERE id = ?",
            (result["memory_id"],),
        ).fetchone()
        step_count = conn.execute(
            "SELECT count(*) FROM procedure_steps WHERE procedure_id = ?",
            (result["id"],),
        ).fetchone()[0]
        conn.close()

        assert proc is not None
        assert memory is not None
        assert memory[0] == "procedural"
        assert "Deploy to staging safely" in memory[1]
        assert step_count == 4

    def test_remember_with_procedural_type_extracts_structure(self, brain):
        mid = brain.remember(
            "How to roll back a release: first pause deploys, then redeploy the previous version, finally verify health checks.",
            category="convention",
            memory_type="procedural",
        )

        conn = sqlite3.connect(str(brain.db_path))
        proc = conn.execute(
            "SELECT id, goal, procedure_kind FROM procedures WHERE memory_id = ?",
            (mid,),
        ).fetchone()
        steps = conn.execute(
            "SELECT action FROM procedure_steps WHERE procedure_id = ? ORDER BY step_order",
            (proc[0],),
        ).fetchall()
        conn.close()

        assert proc is not None
        assert proc[2] in {"workflow", "rollback"}
        assert len(steps) >= 1

    def test_search_prefers_active_procedure_over_stale_legacy(self, brain):
        brain.remember_procedure(
            goal="Deploy to staging safely",
            title="Staging deploy",
            description="Current runbook for staging deploys.",
            steps=["Run tests", "Apply migrations", "Deploy", "Verify health checks"],
            status="active",
            execution_count=8,
            success_count=7,
        )
        brain.remember_procedure(
            goal="Deploy to staging safely",
            title="Legacy staging deploy",
            description="Old runbook kept for audit history.",
            steps=["Deploy directly", "Run tests later"],
            status="stale",
            execution_count=2,
            success_count=1,
            failure_count=1,
        )

        result = brain.search_procedures("How do I deploy to staging?", limit=5)
        assert result["procedures"]
        assert result["procedures"][0]["status"] == "active"
        assert result["procedures"][0]["title"] == "Staging deploy"

    def test_feedback_updates_execution_and_validation(self, brain):
        proc = brain.remember_procedure(
            goal="Apply migrations",
            title="Migration runbook",
            description="Run brainctl migrate before restarting services.",
            steps=["Inspect pending migrations", "Run brainctl migrate", "Restart the service"],
        )

        feedback = brain.procedure_feedback(
            proc["id"],
            success=True,
            usefulness_score=0.9,
            outcome_summary="Migrations applied cleanly",
            validated=True,
        )
        fetched = brain.get_procedure(proc["id"])

        assert feedback["id"] == proc["id"]
        assert fetched["execution_count"] == 1
        assert fetched["success_count"] == 1
        assert fetched["last_validated_at"] is not None

    def test_backfill_promotes_procedural_free_text(self, brain):
        brain.remember(
            "Deployment checklist: 1. Run pytest. 2. Apply migrations. 3. Deploy to staging. 4. Verify health checks.",
            category="convention",
        )

        result = brain.backfill_procedures(limit=20)
        procedures = brain.list_procedures(limit=20)

        assert result["ok"] is True
        assert result["created_procedures"] >= 1
        assert any("Deployment checklist" in (proc.get("description") or "") for proc in procedures)

    def test_orient_surfaces_procedures(self, brain):
        brain.remember_procedure(
            goal="Deploy to staging safely",
            title="Staging deploy",
            description="Run tests, apply migrations, deploy, verify.",
            steps=["Run tests", "Apply migrations", "Deploy", "Verify"],
        )

        snapshot = brain.orient(query="deploy to staging")

        assert "procedures" in snapshot
        assert snapshot["procedures"]
