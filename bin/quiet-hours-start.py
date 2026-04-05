#!/Users/r4vager/agentmemory/.venv/bin/python3
"""Quiet hours START — disable all Paperclip agent heartbeats, save state."""

import json
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

COMPANY_ID = "f8ddce23-092d-4d25-bc45-2f272d7dfc01"
API_URL = "http://127.0.0.1:3100"
STATE_FILE = Path.home() / "agentmemory" / "config" / "quiet-hours-state.json"
BRAINCTL = Path.home() / "bin" / "brainctl"
API_KEY_FILE = Path.home() / ".openclaw" / "workspace" / "paperclip-claimed-api-key.json"


def get_token():
    with open(API_KEY_FILE) as f:
        return json.load(f)["token"]


def api_get(path, token):
    req = urllib.request.Request(
        f"{API_URL}{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def api_patch(path, token, body):
    req = urllib.request.Request(
        f"{API_URL}{path}",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="PATCH",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def main():
    token = get_token()
    agents = api_get(f"/api/companies/{COMPANY_ID}/agents", token)

    # Find agents with heartbeat enabled
    enabled_agents = []
    for a in agents:
        rc = a.get("runtimeConfig") or {}
        hb = rc.get("heartbeat") or {}
        if hb.get("enabled"):
            enabled_agents.append({
                "id": a["id"],
                "name": a["name"],
                "intervalSec": hb.get("intervalSec", 60),
                "wakeOnDemand": hb.get("wakeOnDemand", True),
                "maxConcurrentRuns": hb.get("maxConcurrentRuns", 1),
            })

    if not enabled_agents:
        print("No agents with heartbeat enabled — nothing to do")
        return

    # Save state before disabling
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump({
            "agents": enabled_agents,
            "disabled_at": datetime.now(timezone.utc).isoformat(),
        }, f, indent=2)

    # Disable each agent's heartbeat
    disabled = 0
    for agent in enabled_agents:
        try:
            api_patch(f"/api/agents/{agent['id']}", token, {
                "runtimeConfig": {"heartbeat": {"enabled": False}}
            })
            disabled += 1
        except Exception as e:
            print(f"WARN: Failed to disable {agent['name']}: {e}", file=sys.stderr)

    msg = f"Quiet hours started — {disabled}/{len(enabled_agents)} agents disabled"
    print(msg)

    # Log to brainctl
    try:
        subprocess.run(
            [str(BRAINCTL), "-a", "hermes", "event", "add", msg, "-t", "observation"],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass


if __name__ == "__main__":
    main()
