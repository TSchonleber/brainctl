-- Migration 042: Q-value utility scoring (Zhang et al. 2026 / MemRL)
ALTER TABLE memories ADD COLUMN q_value REAL DEFAULT 0.5;
