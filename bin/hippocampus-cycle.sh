#!/bin/bash
set -euo pipefail

LOG=~/agentmemory/logs/hippocampus_$(date +%Y%m%d_%H%M%S).log
mkdir -p ~/agentmemory/logs

echo "=== Hippocampus cycle started at $(date) ===" | tee "$LOG"

# 0. Neuromodulation detect — update org_state from current conditions (COS-304)
echo "--- Neuromodulation detect ---" | tee -a "$LOG"
~/bin/brainctl neuro detect --agent hippocampus 2>&1 | tee -a "$LOG" || true

# 0.5. Temporal classification pass — reclassify memories by age/recall rules (COS-300)
echo "--- Temporal classification pass ---" | tee -a "$LOG"
PYTHONPATH=~/agentmemory/bin python3 -c "
import sqlite3, hippocampus
conn = sqlite3.connect('$HOME/agentmemory/db/brain.db')
conn.row_factory = sqlite3.Row
result = hippocampus.temporal_classification_pass(conn)
conn.commit()
conn.close()
print(f'Temporal classification complete: reclassified={result[\"reclassified\"]}, promotions={result[\"promotions\"]}, demotions={result[\"demotions\"]}')
" 2>&1 | tee -a "$LOG"

# 1. Decay pass — lower confidence on unused memories
echo "--- Decay pass ---" | tee -a "$LOG"
PYTHONPATH=~/agentmemory/bin python3 -c "
import sqlite3, hippocampus
conn = sqlite3.connect('$HOME/agentmemory/db/brain.db')
conn.row_factory = sqlite3.Row
result = hippocampus.apply_decay(conn)
conn.commit()
conn.close()
print('Decay complete')
" 2>&1 | tee -a "$LOG"

# 2. Contradiction detection
echo "--- Contradiction detection ---" | tee -a "$LOG"
PYTHONPATH=~/agentmemory/bin python3 -c "
import sqlite3, hippocampus
conn = sqlite3.connect('$HOME/agentmemory/db/brain.db')
conn.row_factory = sqlite3.Row
result = hippocampus.resolve_contradictions(conn)
conn.commit()
conn.close()
print('Contradiction resolution complete')
" 2>&1 | tee -a "$LOG"

# 3. Consolidation pass
echo "--- Consolidation pass ---" | tee -a "$LOG"
PYTHONPATH=~/agentmemory/bin python3 -c "
import sqlite3, hippocampus
conn = sqlite3.connect('$HOME/agentmemory/db/brain.db')
conn.row_factory = sqlite3.Row
result = hippocampus.consolidate_memories(conn)
conn.commit()
conn.close()
print('Consolidation complete')
" 2>&1 | tee -a "$LOG"

# 4. Compression pass
echo "--- Compression pass ---" | tee -a "$LOG"
PYTHONPATH=~/agentmemory/bin python3 -c "
import sqlite3, hippocampus
conn = sqlite3.connect('$HOME/agentmemory/db/brain.db')
conn.row_factory = sqlite3.Row
result = hippocampus.compress_memories(conn)
conn.commit()
conn.close()
print('Compression complete')
" 2>&1 | tee -a "$LOG"

# 4.5. Reflexion propagation pass (COS-320) — propagate generalizable lessons cross-agent
echo "--- Reflexion propagation pass ---" | tee -a "$LOG"
PYTHONPATH=~/agentmemory/bin python3 -c "
import sqlite3, hippocampus, json
conn = sqlite3.connect('$HOME/agentmemory/db/brain.db')
conn.row_factory = sqlite3.Row
result = hippocampus.reflexion_propagation_pass(conn, agent_id='hippocampus')
conn.close()
print(f'Reflexion propagation complete: copies_written={result.get(\"copies_written\", 0)}, agents_reached={len(result.get(\"agents_reached\", []))}')
if result.get('error'):
    print(f'WARNING: {result[\"error\"]}')
" 2>&1 | tee -a "$LOG"

# 5. Cadence tracking
echo "--- Cadence tracking ---" | tee -a "$LOG"
python3 ~/agentmemory/bin/cadence.py 2>&1 | tee -a "$LOG"

# 6. Validation
echo "--- Validation ---" | tee -a "$LOG"
~/bin/brainctl validate 2>&1 | tee -a "$LOG"

# 7. Distillation — promote high-importance unlinked events to durable memories (COS-240)
echo "--- Distillation pass ---" | tee -a "$LOG"
~/bin/brainctl distill --threshold 0.5 --limit 50 2>&1 | tee -a "$LOG"

# 8. Dream pass — creative synthesis via cross-scope bisociation (COS-271)
echo "--- Dream pass ---" | tee -a "$LOG"
PYTHONPATH=~/agentmemory/bin python3 ~/agentmemory/bin/hippocampus.py dream-pass --agent hippocampus 2>&1 | tee -a "$LOG"

# 8.5. Embedding coverage pass — embed any new memories created during cycle (COS-361)
echo "--- Embedding coverage pass ---" | tee -a "$LOG"
~/agentmemory/bin/embed-populate --tables memories 2>&1 | tee -a "$LOG" || true

# 9. Prune access log
echo "--- Prune ---" | tee -a "$LOG"
~/bin/brainctl prune-log --days 30 2>&1 | tee -a "$LOG"

# 10. Stats
echo "--- Stats ---" | tee -a "$LOG"
~/bin/brainctl stats 2>&1 | tee -a "$LOG"

# 11. Prune old hippocampus logs (keep 30)
ls -1t ~/agentmemory/logs/hippocampus_*.log 2>/dev/null | tail -n +31 | xargs rm -f 2>/dev/null || true

echo "=== Hippocampus cycle complete at $(date) ===" | tee -a "$LOG"
