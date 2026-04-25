# Procedural Memory Migration Notes

This note documents the safety boundary for
`db/migrations/052_procedural_memory_layer.sql`.

## What Changes

Migration 052 adds procedural memory as a first-class layer:

- widens `memories.memory_type` from `episodic|semantic` to
  `episodic|semantic|procedural`;
- adds canonical procedure tables:
  `procedures`, `procedure_steps`, `procedure_sources`, `procedure_runs`, and
  `procedure_candidates`;
- adds `procedures_fts` plus triggers so procedural records are searchable
  with plain SQLite FTS5;
- keeps a one-to-one bridge row in `memories` through
  `procedures.memory_id` so older generic memory search surfaces still have a
  human-readable synopsis.

## Transaction Safety

The migration runs inside SQLite transaction semantics used by the existing
migration runner. The `memories` table is rebuilt to widen the CHECK
constraint because SQLite cannot alter CHECK constraints in place. The rebuild
copies existing rows forward and preserves existing memory IDs before swapping
the replacement table into place.

The procedural companion tables are additive. They do not delete or compress
episodic evidence, semantic facts, events, decisions, entities, or graph edges.

## Backwards Compatibility

Newer brainctl versions can read older databases and apply migration 052.

Older brainctl versions are expected to keep reading migrated databases for
ordinary episodic and semantic rows because the existing `memories` columns are
preserved. Older versions will not understand canonical procedure tables or
`memory_type='procedural'` rows. Operators that need strict older-version
compatibility should not write procedural rows before rolling all clients
forward.

## Failure and Rollback

If migration application fails before commit, SQLite rolls the transaction back
and the original schema remains in place.

If an operator needs to roll back after a successful migration, use the normal
local-first backup path:

1. stop writers using the target `brain.db`;
2. restore the pre-migration `brain.db` backup if one was taken;
3. otherwise run a forward-only corrective migration rather than editing
   migration 052 in place.

Migration files remain append-only. Do not modify 052 after release; add a new
numbered migration for corrections.

## Versioning Notes

This schema should ship with a version bump because it introduces a new
user-visible memory type and new public procedure APIs. The compatibility
matrix should state that procedural-memory writes require a version at or above
the release containing migration 052, while older clients may still read
non-procedural rows from the migrated database.

## Fresh Install Parity

`db/init_schema.sql` and `src/agentmemory/db/init_schema.sql` must include the
same procedural schema as migration 052 so fresh installs and upgraded
databases converge. Keep `tests/test_schema_parity.py` and
`tests/test_migrate.py` passing when changing either schema path.
