-- Migration 073: connectome graph — Phase 1 schema
--
-- Operationalizes Avenue 5 from research/autonomous-research-avenues-2026-05-20.md:
-- "Connectome as a first-class graph." A first-class representation of
-- which subsystems talk to which, with edge types and weights. Enables
-- cycle detection, "what writes to this dial" queries, and impact
-- analysis when changing or disabling a subsystem.
--
-- Phase 1 ships the schema + seed catalog of known edges (walked from
-- the existing code base). Phase 2 adds query tools for graph
-- traversal and impact analysis. Phase 3 auto-updates the connectome
-- from runtime observations (which subsystem actually called which).
--
-- Edge types:
--   writes_to     — source mutates a column owned by target
--   reads_from    — source reads target's state but doesn't mutate
--   modulates     — source adjusts target's gain / threshold / weight
--   gates         — source decides whether target's output passes
--   depends_on    — source requires target's schema/tables to exist
--   broadcasts_to — source fires events target subscribes to
--
-- Rollback:
--   DROP TABLE IF EXISTS connectome_edges;
--   DROP TABLE IF EXISTS connectome_nodes;
--   DELETE FROM schema_version WHERE version = 73;
--
-- IDEMPOTENT.

CREATE TABLE IF NOT EXISTS connectome_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL CHECK(category IN (
        'subsystem', 'table', 'dial', 'event_bus', 'external'
    )),
    description TEXT,
    schema_version_introduced INTEGER,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_cn_category ON connectome_nodes(category);

CREATE TABLE IF NOT EXISTS connectome_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    edge_type TEXT NOT NULL CHECK(edge_type IN (
        'writes_to', 'reads_from', 'modulates', 'gates',
        'depends_on', 'broadcasts_to'
    )),
    weight REAL NOT NULL DEFAULT 1.0 CHECK(weight BETWEEN 0.0 AND 1.0),
    description TEXT,
    evidence_source TEXT,  -- e.g. 'code:bg_shadow.py:broadcast_td_error'
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    last_observed_at TEXT,
    FOREIGN KEY (source_id) REFERENCES connectome_nodes(id) ON DELETE CASCADE,
    FOREIGN KEY (target_id) REFERENCES connectome_nodes(id) ON DELETE CASCADE,
    UNIQUE (source_id, target_id, edge_type)
);
CREATE INDEX IF NOT EXISTS idx_ce_source ON connectome_edges(source_id);
CREATE INDEX IF NOT EXISTS idx_ce_target ON connectome_edges(target_id);
CREATE INDEX IF NOT EXISTS idx_ce_type ON connectome_edges(edge_type);

-- Seed the known subsystem nodes (walked from db/migrations + src/agentmemory).
INSERT OR IGNORE INTO connectome_nodes (name, category, description, schema_version_introduced) VALUES
    -- Brain subsystems
    ('thalamus', 'subsystem', 'typed routing layer + salience + gate', 50),
    ('basal_ganglia', 'subsystem', 'five-loop action selection + Go/NoGo learning', 54),
    ('cerebellum', 'subsystem', 'forward-model layer with predict/observe per partner', 56),
    ('amygdala', 'subsystem', 'rapid valence/threat tagging', 58),
    ('hippocampus_dg_ca3', 'subsystem', 'DG pattern separation + CA3 pattern completion', 59),
    ('hippocampus_ca1', 'subsystem', 'CA1 match/mismatch + Subiculum output bridge', 71),
    ('acc', 'subsystem', 'in-flight conflict / surprise / EVC monitor', 60),
    ('dmn', 'subsystem', 'default mode network — offline simulation', 61),
    ('drives', 'subsystem', 'hypothalamic-analog homeostatic drives', 62),
    ('insula', 'subsystem', 'self-state interoception', 63),
    ('pfc', 'subsystem', 'named PFC slots (dlPFC/vmPFC/OFC/frontopolar)', 64),
    ('entorhinal_grid', 'subsystem', '48 grid cells across 3 scales', 65),
    ('lc', 'subsystem', 'locus coeruleus — NE on surprise', 67),
    ('nb', 'subsystem', 'nucleus basalis — ACh on attention', 68),
    ('aras', 'subsystem', 'ascending reticular activating system — global arousal', 69),
    ('habenula', 'subsystem', 'lateral habenula — anti-reward / negative-PE', 70),
    ('workspace', 'subsystem', 'global neuronal workspace broadcasts', NULL),
    ('workspace_bandwidth', 'subsystem', 'top-K-per-epoch bandwidth limit on workspace', 72),
    -- Buses / shared dials
    ('bg_td_events', 'event_bus', 'TD-error broadcast bus (δ from outcome_annotate)', 54),
    ('bg_modulators', 'dial', 'global neuromod dials (tonic_da, lc_ne, serotonin, acetylcholine)', 54),
    ('workspace_broadcasts', 'table', 'global workspace broadcast event log', NULL),
    ('cerebellum_boundaries', 'table', 'cerebellum-fired boundary markers above threshold', 56);

-- Seed the known edges (walked from code as of 2026-05-20).
-- Subsystem → bus / dial edges first.
INSERT OR IGNORE INTO connectome_edges (source_id, target_id, edge_type, weight, description, evidence_source) VALUES
    -- BG closes the actor-critic loop through bg_td_events + bg_modulators
    ((SELECT id FROM connectome_nodes WHERE name='basal_ganglia'),
     (SELECT id FROM connectome_nodes WHERE name='bg_td_events'),
     'writes_to', 1.0, 'broadcast_td_error inserts TD events', 'code:bg_shadow.py:broadcast_td_error'),
    ((SELECT id FROM connectome_nodes WHERE name='basal_ganglia'),
     (SELECT id FROM connectome_nodes WHERE name='bg_modulators'),
     'writes_to', 1.0, 'bg_modulator_set + cascade', 'code:mcp_tools_basal_ganglia.py'),
    -- Cerebellum fires boundaries + feeds bg_td_events
    ((SELECT id FROM connectome_nodes WHERE name='cerebellum'),
     (SELECT id FROM connectome_nodes WHERE name='cerebellum_boundaries'),
     'writes_to', 1.0, 'high |delta_forward| → boundary marker', 'code:cerebellum_shadow.py'),
    ((SELECT id FROM connectome_nodes WHERE name='cerebellum'),
     (SELECT id FROM connectome_nodes WHERE name='bg_td_events'),
     'broadcasts_to', 0.8, 'cerebellum delta supplements BG TD signal', 'code:cerebellum_shadow.py'),
    ((SELECT id FROM connectome_nodes WHERE name='cerebellum_boundaries'),
     (SELECT id FROM connectome_nodes WHERE name='workspace_broadcasts'),
     'broadcasts_to', 1.0, 'high-PE events fire workspace broadcasts', 'migration:057_cerebellum_workspace_bridge.sql'),
    -- Thalamus reads modulators (cascade source)
    ((SELECT id FROM connectome_nodes WHERE name='thalamus'),
     (SELECT id FROM connectome_nodes WHERE name='bg_modulators'),
     'reads_from', 1.0, 'tonic_da → wake_focused vs wake_exploratory cascade', 'commit:32c466e'),
    ((SELECT id FROM connectome_nodes WHERE name='basal_ganglia'),
     (SELECT id FROM connectome_nodes WHERE name='thalamus'),
     'modulates', 1.0, 'BG modulator cascade to thalamus mode', 'commit:32c466e'),
    -- LC + NB + ARAS + Habenula (tonight's shipping) all write/read bg_modulators
    ((SELECT id FROM connectome_nodes WHERE name='lc'),
     (SELECT id FROM connectome_nodes WHERE name='bg_modulators'),
     'reads_from', 1.0, 'reads lc_ne dial in lc_status (Phase 1); will write in Phase 2', 'code:mcp_tools_locus_coeruleus.py'),
    ((SELECT id FROM connectome_nodes WHERE name='nb'),
     (SELECT id FROM connectome_nodes WHERE name='bg_modulators'),
     'depends_on', 1.0, 'migration 068 adds acetylcholine column to bg_modulators', 'migration:068_nucleus_basalis.sql'),
    ((SELECT id FROM connectome_nodes WHERE name='aras'),
     (SELECT id FROM connectome_nodes WHERE name='lc'),
     'modulates', 0.5, 'Phase 3: low arousal damps LC phasic firings (planned)', 'docs/proposals/aras.md'),
    ((SELECT id FROM connectome_nodes WHERE name='aras'),
     (SELECT id FROM connectome_nodes WHERE name='nb'),
     'modulates', 0.5, 'Phase 3: high arousal amplifies NB attention bursts (planned)', 'docs/proposals/aras.md'),
    ((SELECT id FROM connectome_nodes WHERE name='habenula'),
     (SELECT id FROM connectome_nodes WHERE name='bg_modulators'),
     'modulates', 0.5, 'Phase 3: suggested_da_damp subtracts from tonic_da (planned)', 'docs/proposals/habenula.md'),
    -- Hippocampal chain
    ((SELECT id FROM connectome_nodes WHERE name='hippocampus_dg_ca3'),
     (SELECT id FROM connectome_nodes WHERE name='hippocampus_ca1'),
     'broadcasts_to', 1.0, 'CA3 pattern completion feeds CA1 comparison (Phase 2 will auto-wire)', 'docs/proposals/hippocampus_ca1_subiculum.md'),
    ((SELECT id FROM connectome_nodes WHERE name='hippocampus_ca1'),
     (SELECT id FROM connectome_nodes WHERE name='workspace_broadcasts'),
     'broadcasts_to', 0.5, 'Phase 3: subiculum output fires workspace broadcasts (planned)', 'docs/proposals/hippocampus_ca1_subiculum.md'),
    -- Workspace bandwidth gates workspace_broadcasts
    ((SELECT id FROM connectome_nodes WHERE name='workspace_bandwidth'),
     (SELECT id FROM connectome_nodes WHERE name='workspace_broadcasts'),
     'gates', 1.0, 'Phase 2: every workspace broadcast checked against bandwidth limit (planned)', 'docs/proposals (workspace_bandwidth)'),
    -- ACC fires BG holds
    ((SELECT id FROM connectome_nodes WHERE name='acc'),
     (SELECT id FROM connectome_nodes WHERE name='basal_ganglia'),
     'gates', 0.8, 'high EVC fires BG holds', 'code:mcp_tools_acc.py'),
    -- DMN reads insula state
    ((SELECT id FROM connectome_nodes WHERE name='dmn'),
     (SELECT id FROM connectome_nodes WHERE name='insula'),
     'reads_from', 0.7, 'DMN simulation conditions on self-state vector', 'code:mcp_tools_dmn.py'),
    -- Insula subscribers
    ((SELECT id FROM connectome_nodes WHERE name='insula'),
     (SELECT id FROM connectome_nodes WHERE name='drives'),
     'broadcasts_to', 0.7, 'self-state changes notify drive monitors', 'code:mcp_tools_insula.py');

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (73, 'connectome Phase 1: 2 tables (nodes + edges) + seed catalog',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
