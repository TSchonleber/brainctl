"""Code-aware ingestion into the brainctl knowledge graph.

Ships in 2.4.5 behind the optional ``[code]`` extra. Turns a source tree
into entities (files, functions, classes) and ``knowledge_edges``
(``contains``, ``imports``) without any LLM call. Pure CPU via
tree-sitter — no GPU, no network, no vector cost at ingest time. Vector
embeddings are added lazily later by ``brainctl vec reindex`` if the
user has the ``[vec]`` extra installed.

Design constraints:

  * **Lazy imports.** tree-sitter and the grammar packages are imported
    inside ``_load_parsers()`` so this module is safe to import when the
    ``[code]`` extra is missing. The CLI wrapper is responsible for
    surfacing an install hint when ``AVAILABLE`` is ``False``.

  * **SHA256 file cache.** Re-ingests of unchanged files are pure
    metadata reads — the tree-sitter parse is skipped entirely.
    Migration 051 owns the cache table.

  * **UPSERT by (name, scope).** Entity names encode path + qualname so
    the same function moving a few lines within a file updates an
    existing row rather than creating a duplicate.

  * **Small, reused vocabulary.** Entities use existing ``entity_type``
    values (``document`` for files, ``concept`` for functions / classes)
    and encode the fine-grained kind in ``properties.kind``. No new
    entity_type column, no migration beyond the cache.

  * **Provenance via ``knowledge_edges.weight``.** 1.0 = directly found
    in source (``contains``, resolvable ``imports``), 0.7 = inferred
    (unresolved external imports). No new column.

  * **Transactional per file.** Each file commits atomically so a
    parse-error mid-run never leaves half-extracted state.

  * **Hardcoded + .gitignore excludes.** If the target is inside a git
    repo we use ``git ls-files``; otherwise we walk with a small
    hardcoded exclude list. Zero extra deps.

  * **Inspired by** ``safishamsi/graphify``'s ``detect.py`` / ``extract.py``
    / ``cache.py`` pattern. Not a code port — the shape is the
    ``{nodes, edges}`` extractor protocol and the SHA256 skip-when-unchanged
    idea. brainctl's entity graph and migration discipline are reused as-is.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# File-size ceiling (bytes). Larger files are skipped — tree-sitter can
# parse them but the entity-graph signal-to-noise falls off a cliff and
# the ingest becomes the slow path. 1 MB covers every realistic source
# file; generated files above this are the ones we want to skip anyway.
MAX_FILE_BYTES = 1 * 1024 * 1024

# Fallback exclude list used when the target isn't inside a git repo.
# Matches the set graphify uses plus a few extras specific to Python,
# Rust, and frontend monorepos.
HARDCODED_EXCLUDES = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", "bower_components",
    ".venv", "venv", "env", ".env",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".tox", ".nox",
    "dist", "build", "out", ".next", ".nuxt",
    "target",                       # rust
    ".gradle",                      # jvm
    ".cache", ".parcel-cache",
    "coverage", ".nyc_output",
    ".terraform",
})

# Extension → language tag. Kept narrow on purpose — v1 ships grammars
# for python / typescript / go only. Expanding this mapping without also
# adding a grammar to pyproject.toml would silently drop files into the
# "unknown" bucket.
EXT_TO_LANG: Dict[str, str] = {
    ".py":  "python",
    ".pyi": "python",
    ".ts":  "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".go":  "go",
}

# Confidence encoding on knowledge_edges.weight. The categorical
# provenance labels from graphify's extractor schema map onto brainctl's
# existing continuous weight column — no schema change needed.
WEIGHT_EXTRACTED = 1.0   # found directly in source (import statement, containment)
WEIGHT_INFERRED  = 0.7   # reasonable deduction (external / unresolved import)
WEIGHT_AMBIGUOUS = 0.4   # flagged for review (not emitted by v1)


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass
class Node:
    """One extracted entity (pre-DB). Mirrors graphify's extractor shape
    so the per-language functions stay straightforward to read."""
    kind: str               # 'file' | 'function' | 'class'
    name: str               # canonical entity name incl. prefix (file:..., fn:..., class:...)
    label: str              # human-readable short name
    source_file: str        # project-relative POSIX path
    source_line: Optional[int] = None
    language: Optional[str] = None
    signature: Optional[str] = None
    parent: Optional[str] = None  # enclosing class/function name if nested


@dataclass
class Edge:
    """One extracted relation (pre-DB)."""
    source_name: str
    target_name: str
    relation: str           # 'contains' | 'imports'
    weight: float = WEIGHT_EXTRACTED


@dataclass
class Extraction:
    nodes: List[Node] = field(default_factory=list)
    edges: List[Edge] = field(default_factory=list)

    def add_node_if_new(self, node: "Node") -> bool:
        """Append ``node`` iff no existing node shares its name.

        Returns True when a node was added, False when deduplicated.
        Maintains an internal O(1) lookup set so repeated calls stay
        O(1) instead of the O(n²) per-iteration set-comprehension
        pattern we replaced in 2.5.0 (audit I26). Callers that append
        directly to ``.nodes`` bypass dedup — use this helper for any
        path where duplicates matter (e.g. import targets).
        """
        names = getattr(self, "_node_names", None)
        if names is None:
            names = {nd.name for nd in self.nodes}
            self._node_names = names
        if node.name in names:
            return False
        self.nodes.append(node)
        names.add(node.name)
        return True


@dataclass
class IngestStats:
    files_scanned:   int = 0
    files_processed: int = 0
    files_cached:    int = 0
    files_skipped:   int = 0
    entities_written: int = 0
    entities_updated: int = 0
    edges_written:    int = 0
    errors: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Availability probe
# ---------------------------------------------------------------------------

def _probe_availability() -> Tuple[bool, Optional[str]]:
    """Return (available, hint). Never raises."""
    try:
        import tree_sitter  # noqa: F401
    except ImportError:
        return False, "pip install 'brainctl[code]' to enable code ingestion"

    # Grammar packages are imported lazily below, but probe at least one
    # here so we give a helpful install hint before anyone tries to parse.
    missing: List[str] = []
    for mod in ("tree_sitter_python", "tree_sitter_typescript", "tree_sitter_go"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        return False, f"missing grammar packages: {', '.join(missing)} — run `pip install 'brainctl[code]'`"
    return True, None


AVAILABLE, _UNAVAILABLE_HINT = _probe_availability()


def availability_hint() -> Optional[str]:
    """Public accessor — the CLI uses this for its error message."""
    return _UNAVAILABLE_HINT


# ---------------------------------------------------------------------------
# Parser cache (lazy singletons)
# ---------------------------------------------------------------------------

_PARSERS: Dict[str, Any] = {}


def _get_parser(lang: str):
    """Return a cached tree-sitter Parser for the given language tag.

    Raises ImportError if the grammar isn't installed — callers should
    check AVAILABLE first. Parsers are cached for the life of the
    process to avoid the (small but non-zero) per-file parser init
    cost when walking thousands of files.
    """
    if lang in _PARSERS:
        return _PARSERS[lang]

    from tree_sitter import Language, Parser

    if lang == "python":
        import tree_sitter_python as _m
        ts_lang = Language(_m.language())
    elif lang == "typescript":
        import tree_sitter_typescript as _m
        # The typescript package ships two languages; tsx is a superset
        # that also parses plain .ts, so we use it uniformly.
        ts_lang = Language(_m.language_tsx())
    elif lang == "go":
        import tree_sitter_go as _m
        ts_lang = Language(_m.language())
    else:
        raise ValueError(f"unsupported language: {lang}")

    parser = Parser(ts_lang)
    _PARSERS[lang] = parser
    return parser


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def _is_binary(data: bytes) -> bool:
    """Cheap binary sniff — treat any NUL in the first 8 KB as binary."""
    return b"\x00" in data[:8192]


def _walk_with_excludes(root: Path) -> Iterable[Path]:
    """Depth-first walk of `root`, skipping HARDCODED_EXCLUDES dirs and
    symlinks. Yields files only. Used when the target isn't a git repo."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # in-place prune so os.walk doesn't descend into excludes
        dirnames[:] = [d for d in dirnames if d not in HARDCODED_EXCLUDES]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.is_symlink():
                continue
            yield p


def _git_tracked_files(root: Path) -> Optional[List[Path]]:
    """Return `git ls-files` output as absolute Paths, or None if this
    isn't a git repo / git isn't on PATH. Uses ``--cached --others
    --exclude-standard`` so untracked-but-not-ignored files are included
    (matches what a developer would consider "files in this repo")."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "--others",
             "--exclude-standard", "-z"],
            capture_output=True, check=True, timeout=30,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if not out.stdout:
        return []
    # -z gives NUL-separated paths (handles spaces / newlines in filenames)
    rels = [p for p in out.stdout.decode("utf-8", errors="replace").split("\x00") if p]
    return [(root / rel).resolve() for rel in rels]


def collect_files(root: Path, languages: Sequence[str]) -> List[Path]:
    """Yield source files under `root` filtered to the selected languages,
    sorted for determinism. Respects .gitignore when inside a git repo,
    falls back to the hardcoded exclude list otherwise."""
    langset = set(languages)
    selected_exts = {ext for ext, lang in EXT_TO_LANG.items() if lang in langset}

    tracked = _git_tracked_files(root)
    if tracked is not None:
        candidates = [p for p in tracked if p.is_file() and not p.is_symlink()]
    else:
        candidates = list(_walk_with_excludes(root))

    filtered = [p for p in candidates if p.suffix in selected_exts]
    filtered.sort()
    return filtered


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------

def _rel_posix(path: Path, root: Path) -> str:
    """Return `path` relative to `root` as a forward-slash string."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _file_name(relpath: str) -> str:
    return f"file:{relpath}"


def _fn_name(relpath: str, qualname: str) -> str:
    return f"fn:{relpath}:{qualname}"


def _class_name(relpath: str, qualname: str) -> str:
    return f"class:{relpath}:{qualname}"


# ---------------------------------------------------------------------------
# Per-language extractors
# ---------------------------------------------------------------------------

def _node_text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _walk_named(tree_node):
    """Pre-order iterator over named tree-sitter nodes."""
    stack = [tree_node]
    while stack:
        n = stack.pop()
        yield n
        # reverse so we visit children in source order when popping
        stack.extend(reversed([c for c in n.named_children]))


def extract_python(path: Path, src: bytes, relpath: str) -> Extraction:
    parser = _get_parser("python")
    tree = parser.parse(src)
    ex = Extraction()
    file_nm = _file_name(relpath)
    ex.nodes.append(Node(kind="file", name=file_nm, label=Path(relpath).name,
                         source_file=relpath, language="python"))

    # Walk AST, collecting function_definition + class_definition + imports.
    # We track a simple "container stack" so nested functions / methods
    # get their qualname (e.g. `MyClass.my_method`).
    def walk(n, stack: List[str]):
        t = n.type
        if t == "function_definition":
            ident = n.child_by_field_name("name")
            if ident is not None:
                name = _node_text(ident, src)
                qual = ".".join(stack + [name])
                fn_nm = _fn_name(relpath, qual)
                parent_nm = (_class_name(relpath, ".".join(stack)) if stack
                             and any(s[:1].isupper() for s in stack[-1:])
                             else file_nm)
                ex.nodes.append(Node(
                    kind="function", name=fn_nm, label=qual,
                    source_file=relpath, source_line=ident.start_point[0] + 1,
                    language="python",
                    signature=_first_line(_node_text(n, src)),
                    parent=parent_nm,
                ))
                ex.edges.append(Edge(parent_nm, fn_nm, "contains", WEIGHT_EXTRACTED))
                # don't descend into the function body for further defs —
                # Python allows nested defs but they're noise in the graph
                return
        elif t == "class_definition":
            ident = n.child_by_field_name("name")
            if ident is not None:
                name = _node_text(ident, src)
                qual = ".".join(stack + [name])
                cls_nm = _class_name(relpath, qual)
                ex.nodes.append(Node(
                    kind="class", name=cls_nm, label=qual,
                    source_file=relpath, source_line=ident.start_point[0] + 1,
                    language="python", parent=file_nm,
                ))
                ex.edges.append(Edge(file_nm, cls_nm, "contains", WEIGHT_EXTRACTED))
                # descend into the class body so we capture methods
                body = n.child_by_field_name("body")
                if body is not None:
                    for c in body.named_children:
                        walk(c, stack + [name])
                return
        elif t in ("import_statement", "import_from_statement"):
            _emit_python_import(n, src, file_nm, ex)
            return
        for c in n.named_children:
            walk(c, stack)

    walk(tree.root_node, [])
    return ex


def _emit_python_import(n, src: bytes, file_nm: str, ex: Extraction) -> None:
    """Emit one or more 'imports' edges from the containing file to the
    imported module. Unresolved modules (anything we can't map to a
    local file in the same tree) get weight=WEIGHT_INFERRED."""
    mod_names: List[str] = []
    if n.type == "import_statement":
        # `import a.b.c` → one dotted_name child per imported module
        for c in n.named_children:
            if c.type in ("dotted_name", "aliased_import"):
                if c.type == "aliased_import":
                    real = c.child_by_field_name("name")
                    if real is not None:
                        mod_names.append(_node_text(real, src))
                else:
                    mod_names.append(_node_text(c, src))
    elif n.type == "import_from_statement":
        mod = n.child_by_field_name("module_name")
        if mod is not None:
            mod_names.append(_node_text(mod, src))

    for modname in mod_names:
        tgt = f"module:{modname}"
        ex.add_node_if_new(Node(kind="module", name=tgt, label=modname,
                                source_file="<external>", language="python"))
        ex.edges.append(Edge(file_nm, tgt, "imports", WEIGHT_INFERRED))


def extract_typescript(path: Path, src: bytes, relpath: str) -> Extraction:
    parser = _get_parser("typescript")
    tree = parser.parse(src)
    ex = Extraction()
    file_nm = _file_name(relpath)
    ex.nodes.append(Node(kind="file", name=file_nm, label=Path(relpath).name,
                         source_file=relpath, language="typescript"))

    def walk(n, stack: List[str]):
        t = n.type
        if t in ("function_declaration", "generator_function_declaration"):
            ident = n.child_by_field_name("name")
            if ident is not None:
                name = _node_text(ident, src)
                qual = ".".join(stack + [name])
                fn_nm = _fn_name(relpath, qual)
                parent_nm = file_nm
                ex.nodes.append(Node(
                    kind="function", name=fn_nm, label=qual,
                    source_file=relpath, source_line=ident.start_point[0] + 1,
                    language="typescript",
                    signature=_first_line(_node_text(n, src)),
                    parent=parent_nm,
                ))
                ex.edges.append(Edge(parent_nm, fn_nm, "contains", WEIGHT_EXTRACTED))
                return
        elif t in ("class_declaration", "abstract_class_declaration"):
            ident = n.child_by_field_name("name")
            if ident is not None:
                name = _node_text(ident, src)
                qual = ".".join(stack + [name])
                cls_nm = _class_name(relpath, qual)
                ex.nodes.append(Node(
                    kind="class", name=cls_nm, label=qual,
                    source_file=relpath, source_line=ident.start_point[0] + 1,
                    language="typescript", parent=file_nm,
                ))
                ex.edges.append(Edge(file_nm, cls_nm, "contains", WEIGHT_EXTRACTED))
                body = n.child_by_field_name("body")
                if body is not None:
                    for c in body.named_children:
                        if c.type in ("method_definition", "method_signature"):
                            mident = c.child_by_field_name("name")
                            if mident is not None:
                                mname = _node_text(mident, src)
                                mqual = f"{qual}.{mname}"
                                mnm = _fn_name(relpath, mqual)
                                ex.nodes.append(Node(
                                    kind="function", name=mnm, label=mqual,
                                    source_file=relpath,
                                    source_line=mident.start_point[0] + 1,
                                    language="typescript",
                                    signature=_first_line(_node_text(c, src)),
                                    parent=cls_nm,
                                ))
                                ex.edges.append(Edge(cls_nm, mnm, "contains", WEIGHT_EXTRACTED))
                return
        elif t == "import_statement":
            src_node = n.child_by_field_name("source")
            if src_node is not None:
                raw = _node_text(src_node, src).strip("\"'`")
                tgt = f"module:{raw}"
                ex.add_node_if_new(Node(kind="module", name=tgt, label=raw,
                                        source_file="<external>",
                                        language="typescript"))
                ex.edges.append(Edge(file_nm, tgt, "imports", WEIGHT_INFERRED))
            return
        for c in n.named_children:
            walk(c, stack)

    walk(tree.root_node, [])
    return ex


def extract_go(path: Path, src: bytes, relpath: str) -> Extraction:
    parser = _get_parser("go")
    tree = parser.parse(src)
    ex = Extraction()
    file_nm = _file_name(relpath)
    ex.nodes.append(Node(kind="file", name=file_nm, label=Path(relpath).name,
                         source_file=relpath, language="go"))

    def walk(n):
        t = n.type
        if t == "function_declaration":
            ident = n.child_by_field_name("name")
            if ident is not None:
                name = _node_text(ident, src)
                fn_nm = _fn_name(relpath, name)
                ex.nodes.append(Node(
                    kind="function", name=fn_nm, label=name,
                    source_file=relpath, source_line=ident.start_point[0] + 1,
                    language="go",
                    signature=_first_line(_node_text(n, src)),
                    parent=file_nm,
                ))
                ex.edges.append(Edge(file_nm, fn_nm, "contains", WEIGHT_EXTRACTED))
                return
        elif t == "method_declaration":
            ident = n.child_by_field_name("name")
            recv = n.child_by_field_name("receiver")
            if ident is not None:
                name = _node_text(ident, src)
                recv_type = _go_receiver_type(recv, src) if recv is not None else "?"
                qual = f"{recv_type}.{name}"
                fn_nm = _fn_name(relpath, qual)
                parent_nm = _class_name(relpath, recv_type)
                ex.nodes.append(Node(
                    kind="function", name=fn_nm, label=qual,
                    source_file=relpath, source_line=ident.start_point[0] + 1,
                    language="go",
                    signature=_first_line(_node_text(n, src)),
                    parent=parent_nm,
                ))
                ex.edges.append(Edge(parent_nm, fn_nm, "contains", WEIGHT_EXTRACTED))
                return
        elif t == "type_declaration":
            for c in n.named_children:
                if c.type == "type_spec":
                    nm = c.child_by_field_name("name")
                    ty = c.child_by_field_name("type")
                    if nm is not None and ty is not None and ty.type in ("struct_type", "interface_type"):
                        tname = _node_text(nm, src)
                        cls_nm = _class_name(relpath, tname)
                        ex.nodes.append(Node(
                            kind="class", name=cls_nm, label=tname,
                            source_file=relpath, source_line=nm.start_point[0] + 1,
                            language="go", parent=file_nm,
                        ))
                        ex.edges.append(Edge(file_nm, cls_nm, "contains", WEIGHT_EXTRACTED))
        elif t == "import_declaration":
            for c in n.named_children:
                if c.type == "import_spec_list":
                    for spec in c.named_children:
                        _emit_go_import_spec(spec, src, file_nm, ex)
                elif c.type == "import_spec":
                    _emit_go_import_spec(c, src, file_nm, ex)
            return
        for c in n.named_children:
            walk(c)

    walk(tree.root_node)
    return ex


def _go_receiver_type(recv_node, src: bytes) -> str:
    """Extract the receiver type name from a Go method_declaration
    receiver parameter list. Handles pointer receivers `(s *MyStruct)`
    and value receivers `(s MyStruct)`. Returns '?' on anything exotic."""
    for c in recv_node.named_children:
        if c.type == "parameter_declaration":
            ty = c.child_by_field_name("type")
            if ty is None:
                continue
            if ty.type == "pointer_type":
                inner = ty.named_children
                if inner:
                    return _node_text(inner[0], src)
            elif ty.type in ("type_identifier", "qualified_type"):
                return _node_text(ty, src)
    return "?"


def _emit_go_import_spec(spec, src: bytes, file_nm: str, ex: Extraction) -> None:
    if spec.type != "import_spec":
        return
    path_node = spec.child_by_field_name("path")
    if path_node is None:
        return
    raw = _node_text(path_node, src).strip("\"`")
    tgt = f"module:{raw}"
    ex.add_node_if_new(Node(kind="module", name=tgt, label=raw,
                            source_file="<external>", language="go"))
    ex.edges.append(Edge(file_nm, tgt, "imports", WEIGHT_INFERRED))


EXTRACTORS = {
    "python":     extract_python,
    "typescript": extract_typescript,
    "go":         extract_go,
}


def _first_line(s: str) -> str:
    # trim a function/method source slice to its first line — good enough
    # for a "signature" surface without storing the whole body
    line = s.splitlines()[0] if s else ""
    return line.strip()[:512]


# ---------------------------------------------------------------------------
# DB writer
# ---------------------------------------------------------------------------

def _upsert_entity(db, node: Node, scope: str, agent_id: str) -> Tuple[int, bool]:
    """Insert or update an entity by (name, scope). Returns (entity_id, created)."""
    row = db.execute(
        "SELECT id FROM entities WHERE name = ? AND scope = ? AND retired_at IS NULL",
        (node.name, scope),
    ).fetchone()

    entity_type = "document" if node.kind in ("file", "module") else "concept"
    props: Dict[str, Any] = {"kind": node.kind}
    if node.language:
        props["language"] = node.language
    if node.source_file:
        props["path"] = node.source_file
    if node.source_line is not None:
        props["line"] = node.source_line
    if node.signature:
        props["signature"] = node.signature
    if node.parent:
        props["parent"] = node.parent
    props_json = json.dumps(props, sort_keys=True)

    if row is None:
        cur = db.execute(
            "INSERT INTO entities (name, entity_type, properties, observations, "
            "agent_id, confidence, scope) VALUES (?, ?, ?, '[]', ?, 1.0, ?)",
            (node.name, entity_type, props_json, agent_id, scope),
        )
        return cur.lastrowid, True

    # UPDATE properties + bump updated_at (the trigger doesn't auto-refresh it)
    db.execute(
        "UPDATE entities SET properties = ?, updated_at = datetime('now') WHERE id = ?",
        (props_json, row["id"]),
    )
    return row["id"], False


def _upsert_edge(db, source_id: int, target_id: int, relation: str,
                 weight: float, agent_id: str) -> bool:
    """INSERT OR IGNORE an entity→entity edge. Returns True if a new row
    was created.

    On an existing edge we only promote ``weight`` when the caller has
    stronger evidence (``MAX(weight, ?)``). We deliberately do **not**
    touch ``last_reinforced_at`` / ``co_activation_count``: those are
    synaptic reinforcement signals consumed by ``hippocampus.py``'s
    co_referenced decay / promotion math. Re-parsing a source file
    because its bytes changed is an idempotent state-sync, not an
    activation event — bumping reinforcement here would poison the
    cognitive layer over many re-ingests.
    """
    cur = db.execute(
        "INSERT OR IGNORE INTO knowledge_edges "
        "(source_table, source_id, target_table, target_id, relation_type, "
        " weight, agent_id) VALUES ('entities', ?, 'entities', ?, ?, ?, ?)",
        (source_id, target_id, relation, weight, agent_id),
    )
    if cur.rowcount > 0:
        return True
    db.execute(
        "UPDATE knowledge_edges SET weight = MAX(weight, ?) "
        "WHERE source_table='entities' AND source_id=? AND target_table='entities' "
        "AND target_id=? AND relation_type=?",
        (weight, source_id, target_id, relation),
    )
    return False


def _check_cache(db, relpath: str, scope: str, content_sha: str) -> bool:
    """True iff the cache has a fresh entry for (relpath, scope)."""
    row = db.execute(
        "SELECT content_sha FROM code_ingest_cache WHERE file_path = ? AND scope = ?",
        (relpath, scope),
    ).fetchone()
    return row is not None and row["content_sha"] == content_sha


def _update_cache(db, relpath: str, scope: str, content_sha: str,
                  language: str, entity_count: int, edge_count: int) -> None:
    db.execute(
        "INSERT INTO code_ingest_cache "
        "(file_path, scope, content_sha, language, entity_count, edge_count, last_ingested_at) "
        "VALUES (?, ?, ?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(file_path, scope) DO UPDATE SET "
        "  content_sha = excluded.content_sha, "
        "  language = excluded.language, "
        "  entity_count = excluded.entity_count, "
        "  edge_count = excluded.edge_count, "
        "  last_ingested_at = excluded.last_ingested_at",
        (relpath, scope, content_sha, language, entity_count, edge_count),
    )


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def ingest(
    root: Path,
    *,
    scope: str = "global",
    agent_id: str = "code-ingest",
    languages: Optional[Sequence[str]] = None,
    use_cache: bool = True,
    max_files: int = 10000,
    db=None,
    on_file=None,
) -> IngestStats:
    """Ingest a source tree. See the module docstring for the overall
    contract. Returns an ``IngestStats`` with counts + any per-file
    error strings.

    Parameters
    ----------
    root : Path
        Directory to scan.
    scope : str
        Entity scope, e.g. ``"project:brainctl"``. Default ``"global"``.
    agent_id : str
        Agent id stamped on writes. Must already exist in the ``agents``
        table — the CLI auto-registers it the same way every other
        write path does.
    languages : sequence of str, optional
        Subset of ``EXT_TO_LANG`` values. Default: all v1 languages.
    use_cache : bool
        Skip unchanged (by SHA256) files. Default True.
    max_files : int
        Hard cap on files processed per run. Default 10_000.
    db : sqlite3.Connection, optional
        Override the default ``get_db()`` connection (used by tests).
    on_file : callable, optional
        Per-file progress callback ``(relpath, status)`` where status
        is one of ``"processed"``, ``"cached"``, ``"skipped"``,
        ``"error"``.
    """
    if not AVAILABLE:
        raise RuntimeError(
            f"brainctl[code] extra not installed: {_UNAVAILABLE_HINT}"
        )

    root = root.resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"not a directory: {root}")

    if languages is None:
        languages = sorted(set(EXT_TO_LANG.values()))
    else:
        bad = [l for l in languages if l not in set(EXT_TO_LANG.values())]
        if bad:
            raise ValueError(f"unsupported languages: {bad}")

    if db is None:
        from agentmemory._impl import get_db, _ensure_agent
        db = get_db()
        _ensure_agent(db, agent_id)

    # One access_log row per file rather than per-entity — keeps the
    # audit trail honest without flooding it with 1000+ rows per ingest.
    try:
        from agentmemory._impl import log_access as _log_access
    except Exception:
        _log_access = None

    stats = IngestStats()
    files = collect_files(root, languages)
    stats.files_scanned = len(files)
    if len(files) > max_files:
        stats.errors.append(
            f"file cap hit: {len(files)} found, processing first {max_files}. "
            f"Pass --max-files to raise."
        )
        files = files[:max_files]

    for path in files:
        relpath = _rel_posix(path, root)
        try:
            stat = path.stat()
        except OSError as e:
            stats.files_skipped += 1
            stats.errors.append(f"{relpath}: stat failed: {e}")
            if on_file: on_file(relpath, "error")
            continue

        if stat.st_size > MAX_FILE_BYTES:
            stats.files_skipped += 1
            if on_file: on_file(relpath, "skipped")
            continue

        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            stats.files_skipped += 1
            stats.errors.append(f"{relpath}: read failed: {e}")
            if on_file: on_file(relpath, "error")
            continue

        if _is_binary(data):
            stats.files_skipped += 1
            if on_file: on_file(relpath, "skipped")
            continue

        sha = hashlib.sha256(data).hexdigest()
        lang = EXT_TO_LANG[path.suffix]

        if use_cache and _check_cache(db, relpath, scope, sha):
            stats.files_cached += 1
            if on_file: on_file(relpath, "cached")
            continue

        try:
            extractor = EXTRACTORS[lang]
            ex = extractor(path, data, relpath)
        except Exception as e:
            stats.files_skipped += 1
            stats.errors.append(f"{relpath}: parse failed: {e}")
            if on_file: on_file(relpath, "error")
            continue

        # Write this file's extraction atomically. savepoint scoped so a
        # per-file failure doesn't poison the outer transaction, and we
        # keep the FK + FTS triggers (migration 048) happy.
        try:
            db.execute("SAVEPOINT ingest_file")
            id_by_name: Dict[str, int] = {}
            for node in ex.nodes:
                eid, created = _upsert_entity(db, node, scope, agent_id)
                id_by_name[node.name] = eid
                if created:
                    stats.entities_written += 1
                else:
                    stats.entities_updated += 1
            for edge in ex.edges:
                sid = id_by_name.get(edge.source_name)
                tid = id_by_name.get(edge.target_name)
                if sid is None or tid is None:
                    # dangling edge — never create an entity implicitly,
                    # that's what modules are for. Skip and record.
                    continue
                if _upsert_edge(db, sid, tid, edge.relation, edge.weight, agent_id):
                    stats.edges_written += 1

            _update_cache(db, relpath, scope, sha, lang,
                          entity_count=len(ex.nodes), edge_count=len(ex.edges))
            if _log_access is not None:
                # Attribute this whole file's writes to the
                # code_ingest_cache row — one audit-log line per file.
                try:
                    _log_access(db, agent_id, "write", "code_ingest_cache")
                except Exception:
                    pass
            db.execute("RELEASE SAVEPOINT ingest_file")
            db.commit()
            stats.files_processed += 1
            if on_file: on_file(relpath, "processed")
        except Exception as e:
            try:
                db.execute("ROLLBACK TO SAVEPOINT ingest_file")
                db.execute("RELEASE SAVEPOINT ingest_file")
            except Exception:
                pass
            stats.files_skipped += 1
            stats.errors.append(f"{relpath}: write failed: {e}")
            if on_file: on_file(relpath, "error")

    return stats


__all__ = [
    "AVAILABLE", "availability_hint",
    "EXT_TO_LANG", "HARDCODED_EXCLUDES", "MAX_FILE_BYTES",
    "WEIGHT_EXTRACTED", "WEIGHT_INFERRED", "WEIGHT_AMBIGUOUS",
    "Node", "Edge", "Extraction", "IngestStats",
    "collect_files", "extract_python", "extract_typescript", "extract_go",
    "ingest",
]
