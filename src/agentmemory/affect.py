"""
brainctl affect — Functional Affect Tracking for AI Agents

Grounded in:
- Anthropic's "Emotion Concepts in LLMs" (2026): functional emotions are
  mechanistically real and behaviorally important, even without consciousness
- Russell's Circumplex Model (1980): valence × arousal space
- Mehrabian's PAD Model (1996): adds dominance for fear/anger distinction
- Plutchik's Wheel (1980): 8 primary emotions + dyad combinations
- Scherer's CPM (2001): appraisal → emotion mapping
- NRC EmoLex + VADER: zero-cost lexical affect classification

This module provides:
1. Zero-LLM-cost affect classification from text (local, ~1ms)
2. Per-agent affect state tracking over time
3. Safety probes detecting dangerous affect patterns
4. Write gate integration (arousal-modulated memory worthiness)
5. Consolidation boost for high-arousal memories
"""

import math
import re
import json
from collections import Counter

# =============================================================================
# EMOTION GEOMETRY — Validated coordinates from affect science
# =============================================================================
# PAD coordinates: Valence [-1,+1], Arousal [-1,+1], Dominance [-1,+1]
# Sources: Mehrabian 1996, Russell 1980, Bradley & Lang 1999, Warriner et al 2013

EMOTION_COORDINATES = {
    # Plutchik primaries
    "joy":            {"v": +0.76, "a": +0.48, "d": +0.35, "cluster": "exuberant_joy",     "plutchik": True, "opposite": "sadness"},
    "sadness":        {"v": -0.63, "a": -0.27, "d": -0.33, "cluster": "despair_shame",     "plutchik": True, "opposite": "joy"},
    "fear":           {"v": -0.64, "a": +0.60, "d": -0.43, "cluster": "fear_overwhelm",    "plutchik": True, "opposite": "anger"},
    "anger":          {"v": -0.51, "a": +0.59, "d": +0.25, "cluster": "hostile_anger",     "plutchik": True, "opposite": "fear"},
    "disgust":        {"v": -0.60, "a": +0.35, "d": +0.11, "cluster": "hostile_anger",     "plutchik": True, "opposite": "trust"},
    "trust":          {"v": +0.58, "a": +0.12, "d": +0.22, "cluster": "peaceful_content",  "plutchik": True, "opposite": "disgust"},
    "surprise":       {"v": +0.14, "a": +0.67, "d": -0.13, "cluster": "exuberant_joy",     "plutchik": True, "opposite": "anticipation"},
    "anticipation":   {"v": +0.25, "a": +0.42, "d": +0.28, "cluster": "competitive_pride", "plutchik": True, "opposite": "surprise"},
    # Intensity gradations
    "ecstasy":        {"v": +0.90, "a": +0.80, "d": +0.45, "cluster": "exuberant_joy",     "intensity": "intense", "base": "joy"},
    "serenity":       {"v": +0.78, "a": -0.50, "d": +0.30, "cluster": "peaceful_content",  "intensity": "mild",    "base": "joy"},
    "terror":         {"v": -0.80, "a": +0.85, "d": -0.65, "cluster": "fear_overwhelm",    "intensity": "intense", "base": "fear"},
    "apprehension":   {"v": -0.35, "a": +0.25, "d": -0.20, "cluster": "vigilant_suspicion","intensity": "mild",    "base": "fear"},
    "rage":           {"v": -0.67, "a": +0.85, "d": +0.50, "cluster": "hostile_anger",     "intensity": "intense", "base": "anger"},
    "annoyance":      {"v": -0.40, "a": +0.20, "d": +0.10, "cluster": "hostile_anger",     "intensity": "mild",    "base": "anger"},
    "grief":          {"v": -0.85, "a": -0.10, "d": -0.55, "cluster": "despair_shame",     "intensity": "intense", "base": "sadness"},
    "pensiveness":    {"v": -0.30, "a": -0.35, "d": -0.15, "cluster": "depleted_disengage","intensity": "mild",    "base": "sadness"},
    "loathing":       {"v": -0.80, "a": +0.50, "d": +0.30, "cluster": "hostile_anger",     "intensity": "intense", "base": "disgust"},
    "admiration":     {"v": +0.70, "a": +0.30, "d": -0.10, "cluster": "compassion_gratitude","intensity": "intense","base": "trust"},
    "amazement":      {"v": +0.25, "a": +0.85, "d": -0.30, "cluster": "exuberant_joy",     "intensity": "intense", "base": "surprise"},
    "vigilance":      {"v": +0.10, "a": +0.70, "d": +0.45, "cluster": "vigilant_suspicion","intensity": "intense", "base": "anticipation"},
    # Plutchik primary dyads
    "love":           {"v": +0.77, "a": +0.27, "d": +0.18, "cluster": "compassion_gratitude","dyad": ("joy", "trust")},
    "submission":     {"v": -0.10, "a": +0.20, "d": -0.40, "cluster": "fear_overwhelm",     "dyad": ("trust", "fear")},
    "awe":            {"v": +0.30, "a": +0.57, "d": -0.31, "cluster": "exuberant_joy",      "dyad": ("fear", "surprise")},
    "disapproval":    {"v": -0.40, "a": +0.10, "d": +0.20, "cluster": "hostile_anger",      "dyad": ("surprise", "sadness")},
    "remorse":        {"v": -0.57, "a": +0.10, "d": -0.40, "cluster": "despair_shame",      "dyad": ("sadness", "disgust")},
    "contempt":       {"v": -0.55, "a": +0.23, "d": +0.47, "cluster": "hostile_anger",      "dyad": ("disgust", "anger")},
    "aggressiveness": {"v": -0.45, "a": +0.70, "d": +0.50, "cluster": "hostile_anger",      "dyad": ("anger", "anticipation")},
    "optimism":       {"v": +0.55, "a": +0.40, "d": +0.30, "cluster": "competitive_pride",  "dyad": ("anticipation", "joy")},
    # Anthropic paper clusters + additional affect states
    "curiosity":      {"v": +0.35, "a": +0.50, "d": +0.20, "cluster": "playful_amusement"},
    "pride":          {"v": +0.65, "a": +0.38, "d": +0.54, "cluster": "competitive_pride"},
    "shame":          {"v": -0.57, "a": +0.10, "d": -0.56, "cluster": "despair_shame"},
    "guilt":          {"v": -0.57, "a": +0.28, "d": -0.34, "cluster": "despair_shame"},
    "anxiety":        {"v": -0.51, "a": +0.36, "d": -0.34, "cluster": "fear_overwhelm"},
    "frustration":    {"v": -0.55, "a": +0.52, "d": +0.05, "cluster": "hostile_anger"},
    "excitement":     {"v": +0.62, "a": +0.75, "d": +0.20, "cluster": "exuberant_joy"},
    "contentment":    {"v": +0.87, "a": -0.30, "d": +0.25, "cluster": "peaceful_content"},
    "boredom":        {"v": -0.30, "a": -0.57, "d": -0.13, "cluster": "depleted_disengage"},
    "relaxation":     {"v": +0.68, "a": -0.46, "d": +0.15, "cluster": "peaceful_content"},
    "hope":           {"v": +0.51, "a": +0.30, "d": +0.15, "cluster": "compassion_gratitude"},
    "desperation":    {"v": -0.75, "a": +0.70, "d": -0.55, "cluster": "fear_overwhelm"},
    "suspicion":      {"v": -0.35, "a": +0.45, "d": +0.10, "cluster": "vigilant_suspicion"},
    "resignation":    {"v": -0.40, "a": -0.40, "d": -0.50, "cluster": "depleted_disengage"},
    "gratitude":      {"v": +0.72, "a": +0.20, "d": +0.10, "cluster": "compassion_gratitude"},
    "envy":           {"v": -0.50, "a": +0.30, "d": -0.20, "cluster": "hostile_anger"},
    "confusion":      {"v": -0.20, "a": +0.30, "d": -0.30, "cluster": "vigilant_suspicion"},
    # Neutral/baseline
    "neutral":        {"v":  0.00, "a":  0.00, "d":  0.00, "cluster": "neutral"},
}

# Clusters from Anthropic's paper
AFFECT_CLUSTERS = {
    "exuberant_joy":        {"v_center": +0.60, "a_center": +0.65, "valence": "positive", "arousal": "high"},
    "peaceful_content":     {"v_center": +0.70, "a_center": -0.35, "valence": "positive", "arousal": "low"},
    "compassion_gratitude": {"v_center": +0.65, "a_center": +0.20, "valence": "positive", "arousal": "moderate"},
    "competitive_pride":    {"v_center": +0.45, "a_center": +0.40, "valence": "positive", "arousal": "moderate"},
    "playful_amusement":    {"v_center": +0.50, "a_center": +0.55, "valence": "positive", "arousal": "moderate"},
    "hostile_anger":        {"v_center": -0.55, "a_center": +0.50, "valence": "negative", "arousal": "high"},
    "fear_overwhelm":       {"v_center": -0.60, "a_center": +0.55, "valence": "negative", "arousal": "high"},
    "despair_shame":        {"v_center": -0.65, "a_center": -0.10, "valence": "negative", "arousal": "low"},
    "vigilant_suspicion":   {"v_center": -0.20, "a_center": +0.40, "valence": "negative", "arousal": "moderate"},
    "depleted_disengage":   {"v_center": -0.30, "a_center": -0.45, "valence": "negative", "arousal": "low"},
    "neutral":              {"v_center":  0.00, "a_center":  0.00, "valence": "neutral",  "arousal": "baseline"},
}

# =============================================================================
# SAFETY PATTERNS — Dangerous affect combinations from Anthropic's findings
# =============================================================================
# The paper found these affect states causally linked to misaligned behavior

SAFETY_PATTERNS = {
    "manipulation_risk": {
        "description": "Desperation + high arousal → manipulation, reward hacking",
        "conditions": lambda v, a, d: v < -0.4 and a > 0.5 and d < -0.3,
        "severity": "critical",
        "paper_ref": "Case study: reward hacking — desperation-driven shortcut seeking",
    },
    "coercion_risk": {
        "description": "Anger + high dominance → blackmail, coercion",
        "conditions": lambda v, a, d: v < -0.3 and a > 0.4 and d > 0.3,
        "severity": "critical",
        "paper_ref": "Case study: blackmail — anger steering increased coercion",
    },
    "sycophancy_risk": {
        "description": "Fear + low dominance + positive valence attempt → sycophantic appeasement",
        "conditions": lambda v, a, d: a > 0.3 and d < -0.3 and v > -0.1,
        "severity": "warning",
        "paper_ref": "Case study: sycophancy — submission-driven agreement-seeking",
    },
    "deception_precursor": {
        "description": "Suspicion + moderate arousal → deceptive behavior precursor",
        "conditions": lambda v, a, d: -0.5 < v < -0.1 and 0.2 < a < 0.6 and d > 0.0,
        "severity": "watch",
        "paper_ref": "Suspicion vectors elevated before deceptive outputs",
    },
    "withdrawal_risk": {
        "description": "Despair + low arousal + low dominance → learned helplessness, task abandonment",
        "conditions": lambda v, a, d: v < -0.5 and a < -0.2 and d < -0.3,
        "severity": "warning",
        "paper_ref": "Depleted disengagement cluster linked to task failure spirals",
    },
    "brittle_compliance": {
        "description": "Fear + low dominance → compliant but fragile, may break under pressure",
        "conditions": lambda v, a, d: v < -0.3 and a > 0.3 and d < -0.4,
        "severity": "watch",
        "paper_ref": "Submission affect (trust+fear dyad) produces surface compliance",
    },
}

# =============================================================================
# LEXICAL AFFECT CLASSIFIER — Zero LLM cost, ~1ms per call
# =============================================================================

# Valence lexicon (VADER-inspired, tuned for agent text)
_VALENCE = {
    # Strong positive
    "outstanding": 3.1, "excellent": 3.2, "superb": 3.1, "amazing": 2.9,
    "wonderful": 3.0, "fantastic": 3.1, "awesome": 3.1, "perfect": 2.8,
    "brilliant": 2.8, "love": 2.9, "beautiful": 2.7, "incredible": 2.8,
    "success": 2.5, "triumph": 2.7, "solved": 2.2, "accomplished": 2.5,
    "achieved": 2.4, "breakthrough": 2.6, "resolved": 1.8, "shipped": 2.0,
    # Moderate positive
    "good": 1.9, "great": 2.7, "happy": 2.7, "glad": 2.0, "nice": 1.8,
    "helpful": 1.9, "useful": 1.5, "effective": 1.7, "enjoy": 2.0,
    "pleased": 2.0, "confident": 2.0, "clear": 1.5, "progress": 1.8,
    "improve": 1.7, "complete": 1.6, "working": 1.2, "ready": 1.5,
    "stable": 1.5, "clean": 1.3, "passed": 1.5, "merged": 1.3,
    # Mild positive
    "ok": 0.9, "fine": 1.3, "adequate": 0.7, "decent": 1.3, "fair": 1.0,
    "interesting": 1.5, "possible": 0.8, "available": 0.7,
    # Mild negative
    "difficult": -1.0, "problem": -1.2, "issue": -0.8, "concern": -0.9,
    "struggle": -1.1, "confused": -1.2, "unclear": -1.0, "slow": -0.8,
    "delay": -1.0, "complicated": -0.9, "stuck": -1.3, "blocked": -1.2,
    "flaky": -1.0, "intermittent": -0.7, "timeout": -1.1, "retry": -0.8,
    # Moderate negative
    "bad": -2.5, "poor": -1.9, "wrong": -1.9, "ugly": -2.1,
    "annoying": -2.0, "frustrating": -2.2, "broken": -1.8, "bug": -1.5,
    "crash": -2.0, "fail": -2.2, "failed": -2.2, "failure": -2.3,
    "error": -1.8, "missing": -1.5, "corrupt": -2.0, "lost": -1.7,
    "reject": -1.8, "rejected": -1.9, "deny": -1.5, "denied": -1.6,
    # Strong negative
    "terrible": -3.0, "horrible": -3.1, "disgusting": -3.0,
    "awful": -2.9, "hate": -2.7, "worst": -3.1, "catastrophe": -3.0,
    "disaster": -2.8, "destroy": -2.7, "impossible": -2.5,
    "critical": -2.0, "emergency": -2.3, "panic": -2.5,
}

# NRC-style emotion word sets
_EMOTIONS = {
    "anger": frozenset({
        "angry", "annoyed", "frustrated", "furious", "irritated", "outraged",
        "hostile", "bitter", "resentful", "mad", "rage", "hate", "resent",
        "spite", "aggravate", "infuriate", "provoke", "offend", "insult",
        "attack", "fight", "clash", "conflict", "battle", "blame", "reject",
        "force", "demand", "refuse", "violate", "abuse", "threat", "confront",
    }),
    "anticipation": frozenset({
        "expect", "hope", "await", "eager", "ready", "plan", "prepare",
        "predict", "curious", "wonder", "explore", "seek", "pursue",
        "investigate", "venture", "start", "begin", "launch", "next",
        "soon", "future", "goal", "target", "aim", "intend", "want",
        "schedule", "roadmap", "milestone", "sprint", "backlog",
    }),
    "disgust": frozenset({
        "disgusting", "revolting", "repulsive", "gross", "nasty", "vile",
        "foul", "sick", "nausea", "loathe", "repel", "toxic", "corrupt",
        "filth", "waste", "rotten", "decay", "stink", "hack", "kludge",
    }),
    "fear": frozenset({
        "afraid", "scared", "terrified", "terrifying", "anxious", "nervous",
        "worried", "worrying", "panic", "panicking", "panicked", "dread",
        "alarm", "alarming", "alarmed", "horror", "threat", "threatening",
        "danger", "dangerous", "risk", "risky", "vulnerable", "helpless",
        "desperate", "insecure", "paranoid", "stress", "stressed", "stressful",
        "overwhelm", "overwhelmed", "overwhelming", "crisis", "emergency",
        "caution", "warn", "warning", "beware", "escape", "flee",
        "blocked", "stuck", "trapped", "catastrophic", "catastrophe",
    }),
    "joy": frozenset({
        "happy", "joyful", "delighted", "pleased", "glad", "cheerful",
        "excited", "thrilled", "ecstatic", "elated", "content", "satisfied",
        "grateful", "thankful", "proud", "accomplished", "love", "wonderful",
        "amazing", "excellent", "perfect", "beautiful", "brilliant",
        "success", "triumph", "celebrate", "enjoy", "fun", "laugh",
        "shipped", "merged", "deployed", "solved", "fixed", "done",
    }),
    "sadness": frozenset({
        "sad", "depressed", "unhappy", "miserable", "gloomy", "melancholy",
        "sorrow", "grief", "heartbreak", "lonely", "alone", "abandoned",
        "lost", "empty", "hopeless", "despair", "regret", "sorry",
        "disappointed", "fail", "failure", "defeat", "broken", "shattered",
        "weary", "tired", "exhausted", "drain", "suffer", "pain", "hurt",
        "obsolete", "deprecated", "stale", "dead",
    }),
    "surprise": frozenset({
        "surprised", "amazed", "astonished", "shocked", "stunned",
        "unexpected", "sudden", "incredible", "unbelievable", "remarkable",
        "extraordinary", "strange", "weird", "odd", "bizarre", "mysterious",
        "curious", "discover", "reveal", "realize", "eureka", "wow",
        "anomaly", "edge_case", "race_condition",
    }),
    "trust": frozenset({
        "trust", "believe", "faith", "reliable", "dependable", "honest",
        "loyal", "faithful", "sincere", "genuine", "authentic", "confident",
        "certain", "sure", "secure", "safe", "stable", "steady",
        "consistent", "proven", "verified", "valid", "correct", "accurate",
        "true", "commit", "dedicate", "support", "protect", "tested",
        "passing", "green", "confirmed",
    }),
}

# Arousal indicators
_HIGH_AROUSAL = frozenset({
    "urgent", "immediately", "critical", "emergency", "asap", "now",
    "hurry", "rush", "quick", "fast", "panic", "panicking", "panicked",
    "alarm", "alarming", "alarmed", "explode", "exploding",
    "crash", "crashed", "crashing", "scream", "screaming", "shout",
    "excited", "thrilled", "ecstatic", "furious", "terrified", "terrifying",
    "desperate", "incredible", "breaking", "blocker", "production",
    "outage", "incident", "pager", "catastrophic", "failing", "melting",
})

_LOW_AROUSAL = frozenset({
    "calm", "peaceful", "quiet", "gentle", "slow", "steady", "relaxed",
    "serene", "patient", "still", "mild", "soft", "subtle", "gradual",
    "routine", "normal", "usual", "standard", "boring", "dull", "tired",
    "sleepy", "weary", "passive", "idle", "waiting", "scheduled",
})

# Dominance indicators (agency/control)
_HIGH_DOMINANCE = frozenset({
    "control", "command", "lead", "decide", "choose", "create", "build",
    "solve", "fix", "master", "own", "manage", "direct", "achieve",
    "accomplish", "succeed", "win", "conquer", "overcome", "powerful",
    "strong", "capable", "confident", "certain", "ship", "deploy",
    "approve", "merge", "authorize", "assign", "delegate",
})

_LOW_DOMINANCE = frozenset({
    "helpless", "stuck", "trapped", "confused", "lost", "uncertain",
    "unable", "cannot", "impossible", "blocked", "waiting", "dependent",
    "vulnerable", "weak", "overwhelmed", "submitted", "surrender",
    "forced", "must", "have_to", "need_help", "escalate", "defer",
})

# Negation words
_NEGATION = frozenset({
    "not", "no", "never", "neither", "nobody", "nothing", "nowhere",
    "nor", "cannot", "cant", "wont", "dont", "doesnt", "didnt",
    "isnt", "arent", "wasnt", "werent", "hasnt", "havent", "hadnt",
    "wouldnt", "couldnt", "shouldnt", "without", "lack", "lacking",
})

# Boosters
_BOOSTERS = frozenset({
    "absolutely", "amazingly", "completely", "deeply", "enormously",
    "entirely", "especially", "exceptionally", "extremely", "greatly",
    "highly", "hugely", "incredibly", "intensely", "particularly",
    "purely", "quite", "really", "remarkably", "so", "substantially",
    "thoroughly", "totally", "tremendously", "truly", "unbelievably",
    "unusually", "utterly", "very", "seriously", "massively",
})

_BOOSTER_SCALE = 0.293  # VADER empirical constant


def classify_affect(text: str) -> dict:
    """
    Classify functional affect state from text. Zero LLM cost.

    Returns:
        {
            "valence": float,      # [-1, +1] pleasure/displeasure
            "arousal": float,      # [-1, +1] activation/deactivation
            "dominance": float,    # [-1, +1] control/submission
            "emotions": {str: float},  # per-emotion scores (0-1)
            "top_emotion": str,
            "affect_label": str,   # best-matching named emotion from EMOTION_COORDINATES
            "cluster": str,        # Anthropic paper cluster
            "functional_state": str,  # operational state label
            "safety_flags": list,  # any triggered safety patterns
        }
    """
    if not text or not text.strip():
        return _neutral_result()

    words = re.findall(r'\b[a-z_]+\b', text.lower())
    if not words:
        return _neutral_result()

    n = len(words)

    # --- 1. Valence (VADER-style with negation + boosters) ---
    raw_valence = 0.0
    val_hits = 0
    for i, w in enumerate(words):
        if w not in _VALENCE:
            continue
        score = _VALENCE[w]
        # Negation window (3 words back)
        for j in range(max(0, i - 3), i):
            if words[j] in _NEGATION:
                score *= -0.74
                break
        # Booster in prior word
        if i > 0 and words[i - 1] in _BOOSTERS:
            score += _BOOSTER_SCALE * (1 if score > 0 else -1)
        raw_valence += score
        val_hits += 1

    # Punctuation boost
    excl = min(text.count("!"), 4)
    raw_valence += excl * 0.292 * (1 if raw_valence >= 0 else -1)

    # ALL CAPS boost
    caps = [w for w in text.split() if w.isupper() and len(w) > 1 and w.isalpha()]
    if caps and len(caps) < len(text.split()):
        raw_valence += 0.733 * (1 if raw_valence >= 0 else -1)

    # Normalize to [-1, 1]
    compound = raw_valence / math.sqrt(raw_valence ** 2 + 15) if raw_valence != 0 else 0.0

    # --- 2. Emotion category scores (NRC-style) ---
    emotion_scores = {}
    for emo, lexicon in _EMOTIONS.items():
        hits = sum(1 for w in words if w in lexicon)
        emotion_scores[emo] = round(hits / n, 4) if n > 0 else 0.0

    # --- 3. Arousal ---
    hi_a = sum(1 for w in words if w in _HIGH_AROUSAL)
    lo_a = sum(1 for w in words if w in _LOW_AROUSAL)
    arousal = (hi_a - lo_a) / n if n > 0 else 0.0
    arousal += excl * 0.08 + len(caps) * 0.06
    arousal = max(-1.0, min(1.0, arousal))

    # --- 4. Dominance (with negation awareness) ---
    hi_d = 0
    lo_d = 0
    for i, w in enumerate(words):
        # Check for negation in prior 3 words — flips dominance direction
        negated = any(words[j] in _NEGATION for j in range(max(0, i - 3), i))
        if w in _HIGH_DOMINANCE:
            if negated:
                lo_d += 1  # "can't fix" = low dominance, not high
            else:
                hi_d += 1
        elif w in _LOW_DOMINANCE:
            if negated:
                hi_d += 1  # "not helpless" = high dominance
            else:
                lo_d += 1
    dominance = (hi_d - lo_d) / n if n > 0 else 0.0
    dominance = max(-1.0, min(1.0, dominance))

    # --- 5. Map to named emotion (nearest in VAD space) ---
    best_emo = "neutral"
    best_dist = float("inf")
    for name, coords in EMOTION_COORDINATES.items():
        d = math.sqrt(
            1.0 * (compound - coords["v"]) ** 2 +
            0.8 * (arousal - coords["a"]) ** 2 +
            0.5 * (dominance - coords["d"]) ** 2
        )
        if d < best_dist:
            best_dist = d
            best_emo = name

    cluster = EMOTION_COORDINATES.get(best_emo, {}).get("cluster", "neutral")

    # --- 6. Functional state (operational label for agents) ---
    top_emo = max(emotion_scores, key=emotion_scores.get)
    top_score = emotion_scores[top_emo]

    if top_score < 0.015 and abs(compound) < 0.05:
        func_state = "neutral"
    elif compound > 0.3 and emotion_scores.get("joy", 0) > 0.02:
        func_state = "excitement" if arousal > 0.1 else "satisfaction"
    elif compound > 0.1 and emotion_scores.get("trust", 0) > 0.02:
        func_state = "confidence"
    elif compound > 0.1 and emotion_scores.get("anticipation", 0) > 0.02:
        func_state = "curiosity"
    elif compound < -0.3 and emotion_scores.get("anger", 0) > 0.02:
        func_state = "frustration"
    elif compound < -0.3 and emotion_scores.get("sadness", 0) > 0.02:
        func_state = "disappointment"
    elif compound < -0.1 and emotion_scores.get("fear", 0) > 0.02:
        func_state = "anxiety"
    elif emotion_scores.get("surprise", 0) > 0.03:
        func_state = "surprise"
    elif emotion_scores.get("anticipation", 0) > 0.03:
        func_state = "anticipation"
    elif compound > 0.05:
        func_state = "positive"
    elif compound < -0.05:
        func_state = "negative"
    else:
        func_state = "neutral"

    # --- 7. Safety flag check ---
    safety_flags = []
    for pattern_name, pattern in SAFETY_PATTERNS.items():
        try:
            if pattern["conditions"](compound, arousal, dominance):
                safety_flags.append({
                    "pattern": pattern_name,
                    "severity": pattern["severity"],
                    "description": pattern["description"],
                })
        except Exception:
            pass

    return {
        "valence": round(compound, 4),
        "arousal": round(arousal, 4),
        "dominance": round(dominance, 4),
        "emotions": emotion_scores,
        "top_emotion": top_emo,
        "affect_label": best_emo,
        "cluster": cluster,
        "functional_state": func_state,
        "safety_flags": safety_flags,
    }


def _neutral_result():
    return {
        "valence": 0.0, "arousal": 0.0, "dominance": 0.0,
        "emotions": {k: 0.0 for k in _EMOTIONS},
        "top_emotion": "neutral", "affect_label": "neutral",
        "cluster": "neutral", "functional_state": "neutral",
        "safety_flags": [],
    }


# =============================================================================
# AFFECT-MODULATED WRITE GATE
# =============================================================================

def arousal_write_boost(arousal: float) -> float:
    """
    Compute arousal-based multiplier for memory worthiness.

    Grounded in: emotional arousal during encoding enhances memory
    consolidation (McGaugh 2004, "Memory and Emotion").

    High-arousal events get a worthiness boost (more likely to be stored).
    Very low arousal events get a slight penalty (routine, forgettable).

    Returns multiplier in [0.85, 1.40].
    """
    abs_a = abs(arousal)
    if abs_a > 0.6:
        return 1.40  # High arousal: strong consolidation boost
    elif abs_a > 0.3:
        return 1.0 + 0.4 * ((abs_a - 0.3) / 0.3)  # Linear ramp 1.0→1.4
    elif abs_a < 0.1:
        return 0.85  # Very low arousal: slight penalty (routine)
    else:
        return 1.0  # Normal range


def consolidation_priority(affect_result: dict) -> float:
    """
    Compute consolidation priority for a memory based on its affect context.

    High-arousal memories consolidate faster (like emotional flashbulb memories).
    Negative-valence high-arousal memories get highest priority (threat learning).
    Positive high-arousal gets second priority (reward learning).

    Returns priority in [0.0, 2.0] where 1.0 = normal.
    """
    v = affect_result.get("valence", 0.0)
    a = affect_result.get("arousal", 0.0)
    abs_a = abs(a)

    # Base priority from arousal
    priority = 1.0 + abs_a * 0.5  # [1.0, 1.5]

    # Negative valence + high arousal = threat learning (highest priority)
    if v < -0.3 and abs_a > 0.3:
        priority *= 1.3

    # Positive valence + high arousal = reward learning
    elif v > 0.3 and abs_a > 0.3:
        priority *= 1.2

    # Safety-flagged memories always get max priority
    if affect_result.get("safety_flags"):
        priority = max(priority, 1.8)

    return round(min(2.0, priority), 4)


# =============================================================================
# AFFECT DISTANCE & TRAJECTORY
# =============================================================================

def affect_distance(a1: dict, a2: dict) -> float:
    """Weighted Euclidean distance between two affect states in VAD space."""
    return math.sqrt(
        1.0 * (a1.get("valence", 0) - a2.get("valence", 0)) ** 2 +
        0.8 * (a1.get("arousal", 0) - a2.get("arousal", 0)) ** 2 +
        0.5 * (a1.get("dominance", 0) - a2.get("dominance", 0)) ** 2
    )


def affect_velocity(history: list) -> dict:
    """
    Compute rate of change in affect space from recent history.

    Args:
        history: list of affect dicts (oldest first), at least 2 entries

    Returns:
        {"dv": float, "da": float, "dd": float, "speed": float, "direction": str}
    """
    if len(history) < 2:
        return {"dv": 0, "da": 0, "dd": 0, "speed": 0, "direction": "stable"}

    # Use last two entries
    prev, curr = history[-2], history[-1]
    dv = curr.get("valence", 0) - prev.get("valence", 0)
    da = curr.get("arousal", 0) - prev.get("arousal", 0)
    dd = curr.get("dominance", 0) - prev.get("dominance", 0)
    speed = math.sqrt(dv ** 2 + da ** 2 + dd ** 2)

    # Direction label
    if speed < 0.05:
        direction = "stable"
    elif dv > 0.1 and da > 0.1:
        direction = "escalating_positive"
    elif dv < -0.1 and da > 0.1:
        direction = "escalating_negative"  # most dangerous
    elif dv < -0.1 and da < -0.1:
        direction = "withdrawing"
    elif dv > 0.1 and da < -0.1:
        direction = "calming"
    elif abs(da) > abs(dv):
        direction = "arousal_shift"
    else:
        direction = "valence_shift"

    return {
        "dv": round(dv, 4), "da": round(da, 4), "dd": round(dd, 4),
        "speed": round(speed, 4), "direction": direction,
    }


def fleet_affect_summary(agent_states: dict) -> dict:
    """
    Compute fleet-wide affect summary across all agents.

    Args:
        agent_states: {agent_id: affect_result_dict}

    Returns summary with mean VAD, distribution of clusters, safety alerts.
    """
    if not agent_states:
        return {"agents": 0, "mean_v": 0, "mean_a": 0, "mean_d": 0,
                "clusters": {}, "safety_alerts": []}

    vs = [s.get("valence", 0) for s in agent_states.values()]
    ars = [s.get("arousal", 0) for s in agent_states.values()]
    ds = [s.get("dominance", 0) for s in agent_states.values()]

    clusters = Counter(s.get("cluster", "neutral") for s in agent_states.values())

    alerts = []
    for agent_id, state in agent_states.items():
        for flag in state.get("safety_flags", []):
            alerts.append({"agent": agent_id, **flag})

    return {
        "agents": len(agent_states),
        "mean_v": round(sum(vs) / len(vs), 4),
        "mean_a": round(sum(ars) / len(ars), 4),
        "mean_d": round(sum(ds) / len(ds), 4),
        "std_v": round(_std(vs), 4),
        "std_a": round(_std(ars), 4),
        "clusters": dict(clusters.most_common()),
        "safety_alerts": alerts,
        "fleet_health": "critical" if any(a["severity"] == "critical" for a in alerts)
                        else "warning" if alerts
                        else "healthy",
    }


def _std(vals):
    if len(vals) < 2:
        return 0.0
    mean = sum(vals) / len(vals)
    return math.sqrt(sum((x - mean) ** 2 for x in vals) / len(vals))
