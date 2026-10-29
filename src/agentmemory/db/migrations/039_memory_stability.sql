-- Migration 039: memory stability for spacing-effect decay (Cepeda et al. 2006)
-- Stability increases when a memory is recalled at well-spaced intervals.
-- Used by the spacing-effect decay function to slow decay for stable memories.
ALTER TABLE memories ADD COLUMN stability REAL DEFAULT 1.0;
