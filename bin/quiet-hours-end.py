#!/Users/r4vager/agentmemory/.venv/bin/python3
"""Quiet hours END — restore Paperclip agent heartbeats from saved state."""

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

API_URL = "http://127.0.0.1:3100"
STATE_FILE = Path.home() / "agentmemory" / "config" / "quiet-hours-state.json"
BRAINCTL = Path.home() / "bin" / "brainctl"
API_KEY_FILE = Path.home() / ".openclaw" / "workspace" / "paperclip-claimed-api-key.json"


def get_token():
    with open(API_KEY_FILE) as f:
        return json.load(f)["token"]


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
    if not STATE_FILE.exists():
        print("No quiet-hours state file found — nothing to restore")
        return

    with open(STATE_FILE) as f:
        state = json.load(f)

    agents = state.get("agents", [])
    if not agents:
        print("State file has no agents — nothing to restore")
        return

    token = get_token()

    # Restore each agent's heartbeat config
    restored = 0
    for agent in agents:
        try:
            api_patch(f"/api/agents/{agent['id']}", token, {
                "runtimeConfig": {
                    "heartbeat": {
                        "enabled": True,
                        "intervalSec": agent["intervalSec"],
                        "wakeOnDemand": agent.get("wakeOnDemand", True),
                        "maxConcurrentRuns": agent.get("maxConcurrentRuns", 1),
                    }
                }
            })
            restored += 1
        except Exception as e:
            print(f"WARN: Failed to restore {agent['name']}: {e}", file=sys.stderr)

    msg = f"Quiet hours ended — {restored}/{len(agents)} agents restored"
    print(msg)

    # Clean up state file
    STATE_FILE.unlink(missing_ok=True)

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
