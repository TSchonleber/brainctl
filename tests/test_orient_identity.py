"""orient(): identity, peers, and legacy-id aliases for agents sharing a brain."""
from __future__ import annotations

from agentmemory.brain import Brain


def _brain(tmp_path, agent_id, monkeypatch, aliases=""):
    monkeypatch.setenv("BRAINCTL_AGENT_ALIASES", aliases)
    return Brain(db_path=str(tmp_path / "brain.db"), agent_id=agent_id)


class TestIdentity:
    def test_identity_returned_without_query(self, tmp_path, monkeypatch):
        b = _brain(tmp_path, "grok", monkeypatch)
        b.remember("I am Grok, the stable agent_id grok on this Mac", category="identity",
                   scope="agent:grok")
        b.remember("The user is Terrence and authors brainctl", category="user")
        ctx = b.orient()
        assert [m["content"] for m in ctx["identity"]["self"]] == [
            "I am Grok, the stable agent_id grok on this Mac"]
        assert ctx["identity"]["user"][0]["content"] == "The user is Terrence and authors brainctl"

    def test_other_agents_identity_not_returned_as_self(self, tmp_path, monkeypatch):
        _brain(tmp_path, "claude", monkeypatch).remember(
            "I am Claude, agent_id claude", category="identity", scope="agent:claude")
        ctx = _brain(tmp_path, "grok", monkeypatch).orient()
        assert ctx["identity"]["self"] == []


class TestAliases:
    def test_handoff_found_under_alias(self, tmp_path, monkeypatch):
        _brain(tmp_path, "claude-code-site", monkeypatch).handoff(
            "ship the site", "half done", "nav", "finish nav")
        ctx = _brain(tmp_path, "claude", monkeypatch, "cc:*,claude-code*").orient()
        assert ctx["handoff"]["goal"] == "ship the site"
        assert ctx["handoff"]["agent_id"] == "claude-code-site"

    def test_newest_handoff_wins_across_aliases(self, tmp_path, monkeypatch):
        _brain(tmp_path, "claude", monkeypatch).handoff("old goal", "s", "l", "n")
        _brain(tmp_path, "cc:repo", monkeypatch).handoff("new goal", "s", "l", "n")
        b = _brain(tmp_path, "claude", monkeypatch, "cc:*")
        b._get_conn().execute(
            "UPDATE handoff_packets SET created_at = '2026-01-01T00:00:00Z' WHERE goal = 'old goal'")
        b._get_conn().commit()
        assert b.orient()["handoff"]["goal"] == "new goal"

    def test_no_aliases_keeps_exact_match(self, tmp_path, monkeypatch):
        _brain(tmp_path, "claude-code-site", monkeypatch).handoff("theirs", "s", "l", "n")
        assert _brain(tmp_path, "claude", monkeypatch).orient()["handoff"] is None


class TestPeers:
    def test_peers_fold_published_aliases(self, tmp_path, monkeypatch):
        # Claude publishes its aliases by orienting once with them set.
        _brain(tmp_path, "claude", monkeypatch, "claude-code*").orient()
        _brain(tmp_path, "claude-code-a", monkeypatch).handoff("task a", "s", "l", "n")
        _brain(tmp_path, "claude-code-b", monkeypatch).handoff("task b", "s", "l", "n")
        _brain(tmp_path, "hermes", monkeypatch).handoff("hermes task", "s", "l", "n")

        peers = _brain(tmp_path, "grok", monkeypatch).orient()["peers"]
        ids = [p["agent_id"] for p in peers]
        assert sorted(ids) == ["claude", "hermes"]
        claude = next(p for p in peers if p["agent_id"] == "claude")
        assert claude["via"].startswith("claude-code-")

    def test_own_handoffs_not_listed_as_peers(self, tmp_path, monkeypatch):
        _brain(tmp_path, "cc:repo", monkeypatch).handoff("mine", "s", "l", "n")
        assert _brain(tmp_path, "claude", monkeypatch, "cc:*").orient()["peers"] == []
