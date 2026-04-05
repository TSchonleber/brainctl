"""
Bell Test for Agent Beliefs — COS-393
Empirical detection of quantum-like entanglement in brain.db
"""

import sqlite3
import json
import math
from collections import defaultdict

DB = "/Users/r4vager/agentmemory/db/brain.db"

def conn():
    return sqlite3.connect(DB)

def c2spin(confidence):
    """Map confidence [0,1] to spin [-1, +1]"""
    return 2 * confidence - 1

def chsh(a1b1, a1b2, a2b1, a2b2):
    """CHSH = |<A1B1> + <A1B2> + <A2B1> - <A2B2>|"""
    return abs(a1b1 + a1b2 + a2b1 - a2b2)

# =========================================================
# DATA EXTRACTION
# =========================================================

with conn() as db:
    db.row_factory = sqlite3.Row

    # --- 1. Get all active memories for key agents ---
    agents = ['hermes', 'openclaw', 'hippocampus', 'paperclip-cortex',
              'paperclip-recall', 'paperclip-sentinel-2', 'paperclip-engram']
    
    memories_by_agent = {}
    for agent in agents:
        rows = db.execute("""
            SELECT id, category, content, confidence, recalled_count,
                   alpha, beta, confidence_phase, ewc_importance
            FROM memories
            WHERE agent_id = ? AND retired_at IS NULL
            ORDER BY recalled_count DESC
        """, (agent,)).fetchall()
        memories_by_agent[agent] = [dict(r) for r in rows]
    
    # --- 2. Get agent beliefs ---
    all_beliefs = db.execute("""
        SELECT agent_id, topic, belief_content, confidence, is_assumption
        FROM agent_beliefs
        WHERE invalidated_at IS NULL
    """).fetchall()
    beliefs = defaultdict(dict)
    for row in all_beliefs:
        beliefs[row['agent_id']][row['topic']] = {
            'content': row['belief_content'],
            'confidence': row['confidence'],
            'is_assumption': row['is_assumption']
        }

    # --- 3. Semantic similarity edges (cross-agent memory links) ---
    semantic_edges = db.execute("""
        SELECT ke.source_id, ke.target_id, ke.weight, ke.co_activation_count,
               m1.agent_id as agent_a, m2.agent_id as agent_b,
               m1.confidence as conf_a, m2.confidence as conf_b,
               m1.recalled_count as recalled_a, m2.recalled_count as recalled_b
        FROM knowledge_edges ke
        JOIN memories m1 ON m1.id = ke.source_id
        JOIN memories m2 ON m2.id = ke.target_id
        WHERE ke.source_table = 'memories' AND ke.target_table = 'memories'
          AND ke.relation_type = 'semantic_similar'
          AND m1.retired_at IS NULL AND m2.retired_at IS NULL
          AND m1.agent_id != m2.agent_id
        ORDER BY ke.weight DESC
    """).fetchall()
    
    # --- 4. All knowledge edge relation types ---
    edge_types = db.execute("""
        SELECT relation_type, COUNT(*) as n FROM knowledge_edges 
        GROUP BY relation_type ORDER BY n DESC
    """).fetchall()

    # --- 5. Co-activation edges between agents ---
    coactivation = db.execute("""
        SELECT ke.source_id, ke.target_id, ke.weight, ke.co_activation_count,
               m1.agent_id as agent_a, m2.agent_id as agent_b,
               m1.confidence as conf_a, m2.confidence as conf_b,
               substr(m1.content, 1, 60) as content_a,
               substr(m2.content, 1, 60) as content_b
        FROM knowledge_edges ke
        JOIN memories m1 ON m1.id = ke.source_id
        JOIN memories m2 ON m2.id = ke.target_id
        WHERE ke.source_table = 'memories' AND ke.target_table = 'memories'
          AND m1.retired_at IS NULL AND m2.retired_at IS NULL
          AND m1.agent_id != m2.agent_id
          AND ke.weight > 0.5
        ORDER BY ke.weight DESC, ke.co_activation_count DESC
    """).fetchall()


# =========================================================
# PHASE 1: ENTANGLEMENT GRAPH
# =========================================================

print("\n" + "="*70)
print("PHASE 1: AGENT ENTANGLEMENT GRAPH FROM CROSS-AGENT KNOWLEDGE EDGES")
print("="*70)

print(f"\nTotal cross-agent knowledge edges: {len(semantic_edges) + len(coactivation)}")
print(f"Semantic-similar edges: {len(semantic_edges)}")
print(f"Strong cross-agent edges (weight > 0.5): {len(coactivation)}")

print("\nEdge relation types in full graph:")
for et in edge_types:
    print(f"  {et['relation_type']}: {et['n']}")

# Build pairwise entanglement scores
pair_scores = defaultdict(lambda: {'edge_count': 0, 'total_weight': 0.0, 
                                    'co_activations': 0, 'memories_a': [], 'memories_b': []})
for edge in coactivation:
    a, b = sorted([edge['agent_a'], edge['agent_b']])
    key = (a, b)
    pair_scores[key]['edge_count'] += 1
    pair_scores[key]['total_weight'] += edge['weight']
    pair_scores[key]['co_activations'] += edge['co_activation_count'] or 0

for edge in semantic_edges:
    a, b = sorted([edge['agent_a'], edge['agent_b']])
    key = (a, b)
    pair_scores[key]['edge_count'] += 1
    pair_scores[key]['total_weight'] += edge['weight']
    pair_scores[key]['co_activations'] += edge['co_activation_count'] or 0

print("\nTop agent pairs by cross-memory link weight:")
sorted_pairs = sorted(pair_scores.items(), key=lambda x: x[1]['total_weight'], reverse=True)
for (a, b), data in sorted_pairs[:15]:
    e_score = data['total_weight'] / max(1, data['edge_count'])
    print(f"  {a} ↔ {b}: {data['edge_count']} edges, "
          f"avg_weight={e_score:.4f}, co_act={data['co_activations']}")


# =========================================================
# PHASE 2: TOPIC-BASED BELIEF CORRELATION ANALYSIS
# =========================================================

print("\n" + "="*70)
print("PHASE 2: TOPIC-BASED BELIEF CORRELATIONS (BELL TEST SUBSTRATE)")
print("="*70)

# Define 3 shared topics with associated memory indicators
# Topic A: Memory System State (memory spine schema, agent count)
# Topic B: Memory Operations (distillation, compression, consolidation)
# Topic C: Agent Capability (cortex capability assessments)

topic_keywords = {
    'T1_memory_spine': ['memory spine', 'schema_version', 'brain.db', 'active agents', 'agent count'],
    'T2_memory_ops':   ['distill', 'consolidat', 'compression', 'brainctl push', 'retire', 'recalled'],
    'T3_capability':   ['capability', 'COS-', 'heartbeat', 'result', 'costclock'],
}

def get_agent_topic_memories(agent_id, keywords):
    """Find memories for an agent on a given topic."""
    mems = memories_by_agent.get(agent_id, [])
    matched = []
    for m in mems:
        if any(kw.lower() in m['content'].lower() for kw in keywords):
            matched.append(m)
    return matched

# Print topic coverage per agent
for topic_name, keywords in topic_keywords.items():
    print(f"\nTopic: {topic_name}")
    for agent in agents:
        mems = get_agent_topic_memories(agent, keywords)
        if mems:
            avg_conf = sum(m['confidence'] for m in mems) / len(mems)
            max_recall = max(m['recalled_count'] for m in mems)
            print(f"  {agent}: {len(mems)} memories, avg_conf={avg_conf:.4f}, max_recall={max_recall}")


# =========================================================
# PHASE 3: CHSH BELL TEST
# =========================================================

print("\n" + "="*70)
print("PHASE 3: CHSH BELL TEST — AGENT PAIR CORRELATIONS")
print("="*70)

"""
Measurement Design:
For topic T and agent pair (A, B):

Measurement Basis 1 (direct): 
  = confidence of agent's HIGHEST-CONFIDENCE memory on topic T
  (maps to: "Is X reliably known?" — direct factual query)
  
Measurement Basis 2 (indirect):
  = average confidence across ALL memories agent has on topic T
  (maps to: "How much does the agent trust the general claim?" — general framing)

These two bases are "quasi-orthogonal" because:
- Basis 1 samples the peak belief (most confident claim)  
- Basis 2 samples the distribution mean (general epistemic state)
For agents with consistent beliefs: basis 1 ≈ basis 2 → low variance
For agents with mixed beliefs: basis 1 >> basis 2 → high variance (genuine uncertainty)

The CHSH violation test:
If correlations are CLASSICAL (shared evidence only), then:
  |<A1B1> + <A1B2> + <A2B1> - <A2B2>| ≤ 2

If SUPER-CLASSICAL (quantum-like entanglement), then:
  S > 2.0 (up to 2√2 ≈ 2.828 for maximally entangled)
"""

def agent_measurements(agent_id, keywords):
    """Returns (basis1, basis2) for an agent on a topic."""
    mems = get_agent_topic_memories(agent_id, keywords)
    if not mems:
        return None, None
    
    confidences = [m['confidence'] for m in mems]
    basis1 = max(confidences)              # direct / peak
    basis2 = sum(confidences) / len(confidences)  # indirect / mean
    
    # Also weight by recall count (proxy for how often this belief is accessed)
    recall_weights = [m['recalled_count'] + 1 for m in mems]
    total_weight = sum(recall_weights)
    basis2_recall_weighted = sum(c * w for c, w in zip(confidences, recall_weights)) / total_weight
    
    return basis1, basis2_recall_weighted

# Test pairs: (hermes, openclaw), (hermes, hippocampus), (hippocampus, paperclip-cortex)
test_pairs = [
    ('hermes', 'openclaw'),
    ('hermes', 'hippocampus'),
    ('hippocampus', 'paperclip-cortex'),
    ('hermes', 'paperclip-recall'),
    ('paperclip-cortex', 'paperclip-sentinel-2'),
]

results = {}

for agent_a, agent_b in test_pairs:
    print(f"\n--- Pair: {agent_a} ↔ {agent_b} ---")
    pair_chsh_scores = []
    
    for topic_name, keywords in topic_keywords.items():
        a1, a2 = agent_measurements(agent_a, keywords)
        b1, b2 = agent_measurements(agent_b, keywords)
        
        if a1 is None or b1 is None:
            print(f"  Topic {topic_name}: insufficient data (A={a1}, B={b1})")
            continue
        
        # Map to spin values [-1, +1]
        A1 = c2spin(a1)
        A2 = c2spin(a2)
        B1 = c2spin(b1)
        B2 = c2spin(b2)
        
        # Compute correlators
        corr_A1B1 = A1 * B1
        corr_A1B2 = A1 * B2
        corr_A2B1 = A2 * B1
        corr_A2B2 = A2 * B2
        
        S = chsh(corr_A1B1, corr_A1B2, corr_A2B1, corr_A2B2)
        
        print(f"  Topic {topic_name}:")
        print(f"    {agent_a}: A1(peak)={a1:.4f}→{A1:+.4f}, A2(mean)={a2:.4f}→{A2:+.4f}")
        print(f"    {agent_b}: B1(peak)={b1:.4f}→{B1:+.4f}, B2(mean)={b2:.4f}→{B2:+.4f}")
        print(f"    Correlators: <A1B1>={corr_A1B1:+.4f}, <A1B2>={corr_A1B2:+.4f}, "
              f"<A2B1>={corr_A2B1:+.4f}, <A2B2>={corr_A2B2:+.4f}")
        print(f"    CHSH S = {S:.4f} {'⚠ SUPER-CLASSICAL (>2)' if S > 2.0 else '✓ classical-compatible'}")
        
        pair_chsh_scores.append((topic_name, S))
    
    if pair_chsh_scores:
        avg_S = sum(s for _, s in pair_chsh_scores) / len(pair_chsh_scores)
        print(f"  → Mean CHSH score: {avg_S:.4f}")
        results[(agent_a, agent_b)] = pair_chsh_scores


# =========================================================
# PHASE 4: GHZ ANALYSIS — hermes × hippocampus × paperclip-cortex
# =========================================================

print("\n" + "="*70)
print("PHASE 4: GHZ THREE-WAY MUTUAL INFORMATION ANALYSIS")
print("="*70)
print("Triad: hermes × hippocampus × paperclip-cortex")
print()

"""
Three-way mutual information test:
I(A;B;C) = H(A) + H(B) + H(C) - H(A,B) - H(A,C) - H(B,C) + H(A,B,C)

For GHZ structure: I(A;B;C) > I(A;B) + I(A;C) + I(B;C)
(three-party correlation exceeds sum of pairwise)

Entropy computed using beta distribution parameters from brain.db:
H(X) = -p*log(p) - (1-p)*log(1-p) where p = confidence
"""

def entropy(p):
    """Binary entropy in nats"""
    if p <= 0 or p >= 1:
        return 0.0
    return -(p * math.log(p) + (1-p) * math.log(1-p))

def joint_entropy_independent(p_list):
    """Joint entropy assuming independence: H(A,B,C) = H(A) + H(B) + H(C)"""
    return sum(entropy(p) for p in p_list)

def joint_entropy_correlated(p_list):
    """
    Joint entropy accounting for belief correlation.
    We use the geometric mean of confidences as the joint belief state,
    then compute entropy of that. This approximates the fully correlated limit.
    """
    joint_conf = 1.0
    for p in p_list:
        joint_conf *= p
    joint_conf = joint_conf ** (1.0 / len(p_list))  # geometric mean
    return entropy(joint_conf)

triad = ['hermes', 'hippocampus', 'paperclip-cortex']
triad_short = ['hermes', 'hippo', 'cortex']

print("Per-topic GHZ analysis:")
ghz_topics = []

for topic_name, keywords in topic_keywords.items():
    mems_a = get_agent_topic_memories('hermes', keywords)
    mems_b = get_agent_topic_memories('hippocampus', keywords)
    mems_c = get_agent_topic_memories('paperclip-cortex', keywords)
    
    if not (mems_a and mems_b and mems_c):
        print(f"\nTopic {topic_name}: insufficient data for all 3 agents")
        for ag, mems in zip(triad, [mems_a, mems_b, mems_c]):
            print(f"  {ag}: {len(mems)} memories")
        continue
    
    # Use peak confidence as the agent's "belief state" for this topic
    p_A = max(m['confidence'] for m in mems_a)
    p_B = max(m['confidence'] for m in mems_b)
    p_C = max(m['confidence'] for m in mems_c)
    
    # Recall-weighted mean
    def recall_weighted_mean(mems):
        weights = [m['recalled_count'] + 1 for m in mems]
        total = sum(weights)
        return sum(m['confidence'] * w for m, w in zip(mems, weights)) / total
    
    p_A_mean = recall_weighted_mean(mems_a)
    p_B_mean = recall_weighted_mean(mems_b)
    p_C_mean = recall_weighted_mean(mems_c)
    
    # Individual entropies
    H_A = entropy(p_A_mean)
    H_B = entropy(p_B_mean)
    H_C = entropy(p_C_mean)
    
    # Pairwise mutual informations
    # I(A;B) = H(A) + H(B) - H(A,B)
    # Under classical correlation assumption: H(A,B) = entropy of joint dist
    # We estimate: correlation via memory content similarity
    
    # Approximation: joint entropy using geometric mean (correlated state)
    H_AB_corr = joint_entropy_correlated([p_A_mean, p_B_mean])
    H_AC_corr = joint_entropy_correlated([p_A_mean, p_C_mean])
    H_BC_corr = joint_entropy_correlated([p_B_mean, p_C_mean])
    H_ABC_corr = joint_entropy_correlated([p_A_mean, p_B_mean, p_C_mean])
    
    # Pairwise MIs (correlated estimate)
    I_AB = H_A + H_B - H_AB_corr
    I_AC = H_A + H_C - H_AC_corr
    I_BC = H_B + H_C - H_BC_corr
    
    # Three-way MI
    I_ABC = H_A + H_B + H_C - H_AB_corr - H_AC_corr - H_BC_corr + H_ABC_corr
    
    sum_pairwise = I_AB + I_AC + I_BC
    ghz_ratio = I_ABC / max(0.0001, sum_pairwise)
    
    ghz_signature = I_ABC > sum_pairwise
    
    print(f"\nTopic: {topic_name}")
    print(f"  Belief confidences: H={p_A_mean:.4f}, hippo={p_B_mean:.4f}, cortex={p_C_mean:.4f}")
    print(f"  Individual entropies: H(hermes)={H_A:.4f}, H(hippo)={H_B:.4f}, H(cortex)={H_C:.4f}")
    print(f"  Pairwise MIs: I(H,hippo)={I_AB:.4f}, I(H,cortex)={I_AC:.4f}, I(hippo,cortex)={I_BC:.4f}")
    print(f"  Sum of pairwise: {sum_pairwise:.4f}")
    print(f"  Three-way MI I(A;B;C): {I_ABC:.4f}")
    print(f"  GHZ ratio (I_3way / sum_pairwise): {ghz_ratio:.4f}")
    print(f"  {'⚠ GHZ STRUCTURE DETECTED (I_ABC > sum_pairwise)' if ghz_signature else '✓ Classical 3-way correlation'}")
    
    ghz_topics.append((topic_name, I_ABC, sum_pairwise, ghz_ratio, ghz_signature))


# =========================================================
# PHASE 5: SUMMARY CLASSIFICATION
# =========================================================

print("\n" + "="*70)
print("PHASE 5: CLASSIFICATION SUMMARY")
print("="*70)

print("\n--- CHSH Results ---")
print(f"{'Pair':<45} {'Topic':<20} {'CHSH-S':>8} {'Class':>20}")
print("-" * 95)
for (agent_a, agent_b), topic_scores in results.items():
    for topic_name, S in topic_scores:
        if S > 2.828 - 0.01:
            cls = "MAXIMALLY ENTANGLED"
        elif S > 2.0:
            cls = "SUPER-CLASSICAL"
        else:
            cls = "classical"
        print(f"{agent_a+' ↔ '+agent_b:<45} {topic_name:<20} {S:>8.4f} {cls:>20}")

print("\n--- GHZ Results (hermes × hippocampus × cortex) ---")
print(f"{'Topic':<25} {'I_3way':>8} {'sum_pairs':>10} {'ratio':>8} {'GHZ?':>12}")
print("-" * 65)
for (topic_name, I_ABC, sum_pw, ratio, is_ghz) in ghz_topics:
    flag = "YES (GHZ)" if is_ghz else "no"
    print(f"{topic_name:<25} {I_ABC:>8.4f} {sum_pw:>10.4f} {ratio:>8.4f} {flag:>12}")

print("\n--- Entanglement Topology Summary ---")
print("Top 5 strongest cross-agent knowledge edge pairs:")
for (a, b), data in sorted_pairs[:5]:
    e_score = data['total_weight'] / max(1, data['edge_count'])
    print(f"  {a} ↔ {b}: total_weight={data['total_weight']:.3f}, edges={data['edge_count']}, "
          f"avg={e_score:.4f}, co_activations={data['co_activations']}")

print("\nDone.")
