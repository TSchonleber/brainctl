-- Migration 038: synaptic tagging protection (Frey & Morris 1997)
-- Memories within the labile window of a high-importance event get
-- tagged for protection from consolidation downscaling.
ALTER TABLE memories ADD COLUMN tag_cycles_remaining INTEGER DEFAULT 0;
