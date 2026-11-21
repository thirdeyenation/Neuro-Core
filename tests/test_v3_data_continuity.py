"""WI-003 — v1.1.0 data-continuity proof for the v3 NativeStore migration.

Builds a REAL v1.1.0 memory directory deterministically inside ``tmp_path``:

* ``index.pkl`` — a legacy MyFaiss-era docstore pickle (a
  ``(store, order)`` tuple whose classes carry the historical module
  identities ``langchain_community.docstore.in_memory.InMemoryDocstore``
  and ``langchain_core.documents.base.Document``). v3's BUILT-IN
  ``_LegacyReader`` (native_store.py) resolves exactly these names to
  its internal ``_LegacyRecord`` shim — writing a custom migration step
  is forbidden, so the pickle is shaped to the real reader's allowlist.
* ``index.faiss`` — the legacy binary marker (existence-checked by the
  v3 migration path, never parsed offline here).
* a pre-existing v1.1.0 SQLite sidecar (``neuro_core.db``) with the
  exact production schema (scores, validation status keyed by
  ``memory_id``).

The embedder is a LOCAL DETERMINISTIC FAKE — zero external API or
network calls. The migration itself is v3's own ``NativeStore.initialize()``
legacy branch; the test then reopens the store from disk and asserts:

(a) document-count parity with the pre-migration snapshot,
(b) metadata parity,
(c) document-ID stability,
(d) search/retrieval parity vs the pre-migration snapshot,
(e) the pre-existing sidecar rows join 1:1 with migrated document IDs.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import pickle
import sqlite3
import sys
import types
from pathlib import Path


# ---------------------------------------------------------------------------
# Legacy pickle shims — module identities spoofed to the _LegacyReader
# allowlist (native_store.py find_class). The unpickler replaces these
# classes with its internal _LegacyRecord, so only the (module, name)
# pairs and the ``{'__dict__': fields}`` state shape matter.
# ---------------------------------------------------------------------------


class InMemoryDocstore:
    """Legacy docstore shape: holds a ``_dict`` of id -> record."""

    def __init__(self, _dict=None):
        self._dict = _dict or {}

    def __reduce__(self):
        # Plain attribute state: after unpickling into _LegacyRecord,
        # ``store._dict`` must resolve directly (native_store.py:48).
        return (InMemoryDocstore, (), {"_dict": dict(self._dict)})


InMemoryDocstore.__module__ = "langchain_community.docstore.in_memory"


class Document:
    """Legacy document record shape (state nested under '__dict__')."""

    def __init__(self, page_content=None, metadata=None):
        self.page_content = page_content
        self.metadata = metadata

    def __reduce__(self):
        return (Document, (), {
            "__dict__": {
                "page_content": self.page_content,
                "metadata": self.metadata,
            }
        })


Document.__module__ = "langchain_core.documents.base"


class _FakeDeterministicEmbedder:
    """Local deterministic embedder — zero external API/network calls."""

    def embed_documents(self, texts):
        return [self._one(t) for t in texts]

    async def aembed_documents(self, texts):
        return self.embed_documents(texts)

    @staticmethod
    def _one(text: str):
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [b / 255.0 for b in digest[:4]]

    def embed_query(self, text: str):
        return self._one(text)


def _load_native_store_cls():
    """Load the REAL v3 NativeStore class from framework source.

    native_store.py has no intra-package imports (stdlib + faiss +
    numpy + langchain_core only), so path-loading is faithful.
    """
    spec = importlib.util.spec_from_file_location(
        "_neuro_core_test_continuity_native_store",
        "/a0/plugins/_memory/helpers/native_store.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.NativeStore


# ---------------------------------------------------------------------------
# v1.1.0 fixture data
# ---------------------------------------------------------------------------

_LEGACY_DOCS = [
    ("mem-001", "User lives in Berlin", {
        "id": "mem-001", "memory_type": "fact",
        "timestamp": "2026-01-05T10:00:00+00:00",
        "validation_status": "validated", "importance": 0.8,
    }),
    ("mem-002", "User prefers tea over coffee", {
        "id": "mem-002", "memory_type": "preference",
        "timestamp": "2026-01-06T11:00:00+00:00",
        "validation_status": "unvalidated", "importance": 0.5,
    }),
    ("mem-003", "Project deadline is 2026-12-01", {
        "id": "mem-003", "memory_type": "task",
        "timestamp": "2026-01-07T12:00:00+00:00",
        "validation_status": "disputed", "importance": 0.9,
    }),
]

# Pre-existing v1.1.0 sidecar rows (scores + validation keyed by memory_id).
_SIDECAR_ROWS = [
    ("mem-001", "User lives in Berlin", "capture", "neuro", "main",
     0.8, 0.95, "validated"),
    ("mem-002", "User prefers tea over coffee", "capture", "neuro", "main",
     0.5, 0.6, "unvalidated"),
    ("mem-003", "Project deadline is 2026-12-01", "capture", "neuro", "main",
     0.9, 0.7, "disputed"),
]


def _build_legacy_dir(folder: Path) -> None:
    """Create the deterministic v1.1.0 memory directory."""
    folder.mkdir(parents=True, exist_ok=True)

    store = InMemoryDocstore()
    store._dict = {
        key: Document(page_content=pc, metadata=md)
        for key, pc, md in _LEGACY_DOCS
    }
    order = {i: _LEGACY_DOCS[i][0] for i in range(len(_LEGACY_DOCS))}

    # Persist shim classes under their HISTORICAL module identities.
    # The real v1.1.0 index.pkl references
    # langchain_community.docstore.in_memory.InMemoryDocstore and
    # langchain_core.documents.base.Document. langchain_community is
    # not installed here, and pickle's save_global imports the module
    # before consulting any find_class hook, so we temporarily register
    # stub modules in sys.modules mapping those historical names to the
    # shim classes. The v3 _LegacyReader unpickler resolves these same
    # (module, name) pairs to its internal _LegacyRecord shims, so no
    # real import ever happens on the read side.
    legacy_targets = {
        "langchain_community.docstore.in_memory": {"InMemoryDocstore": InMemoryDocstore},
        "langchain_core.documents.base": {"Document": Document},
    }
    # pickle's save_global calls __import__ on the dotted module name,
    # which imports missing PARENT packages first. Stub any missing
    # parents (langchain_community is not installed here) and wire
    # parent->child attributes so the import chain resolves.
    parent_stubs = {}
    for mod_name in list(legacy_targets):
        parts = mod_name.split(".")
        for depth in range(1, len(parts)):
            parent_name = ".".join(parts[:depth])
            if parent_name not in sys.modules:
                parent_stubs[parent_name] = types.ModuleType(parent_name)
    saved_modules = {}
    try:
        for mod_name, stub in parent_stubs.items():
            saved_modules[mod_name] = sys.modules.get(mod_name)
            sys.modules[mod_name] = stub
        for mod_name, attrs in legacy_targets.items():
            saved_modules[mod_name] = sys.modules.get(mod_name)
            stub = types.ModuleType(mod_name)
            for attr_name, cls in attrs.items():
                setattr(stub, attr_name, cls)
            sys.modules[mod_name] = stub
            # Wire the child onto its (stubbed or real) parent package.
            parent_name, _, child_name = mod_name.rpartition(".")
            parent = sys.modules.get(parent_name)
            if parent is not None:
                setattr(parent, child_name, stub)

        buffer = io.BytesIO()
        pickle.dump((store, order), buffer, protocol=2)
    finally:
        for mod_name, saved in saved_modules.items():
            if saved is None:
                sys.modules.pop(mod_name, None)
            else:
                sys.modules[mod_name] = saved

    (folder / "index.pkl").write_bytes(buffer.getvalue())
    # Legacy binary marker — existence-checked by the v3 migration path.
    (folder / "index.faiss").write_bytes(b"LEGACY-FAISS-MARKER")

    # Pre-existing v1.1.0 SQLite sidecar with the production schema
    # (sqlite_store.py memories table).
    conn = sqlite3.connect(folder / "neuro_core.db")
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS memories ("
            "id TEXT PRIMARY KEY, text TEXT, source TEXT, project TEXT, "
            "agent TEXT, importance REAL, confidence REAL, validation TEXT)"
        )
        conn.executemany(
            "INSERT OR REPLACE INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            _SIDECAR_ROWS,
        )
        conn.commit()
    finally:
        conn.close()


def _evict_warm_cache(subdir: str) -> None:
    """Evict the test subdir key from Memory.index (cross-test hygiene)."""
    try:
        mem_mod = importlib.import_module("plugins._memory.helpers.memory")
        index = getattr(mem_mod.Memory, "index", None)
        if isinstance(index, dict):
            index.pop(subdir, None)
    except Exception:  # pragma: no cover - defensive hygiene only
        pass


# ---------------------------------------------------------------------------
# The continuity test
# ---------------------------------------------------------------------------


def test_v110_legacy_directory_migrates_to_nativestore_with_full_parity(
    tmp_path,
) -> None:
    """WI-003: a real v1.1.0 memory directory migrates through v3's
    BUILT-IN _LegacyReader path with zero data loss."""
    NativeStore = _load_native_store_cls()

    subdir = "wip3_continuity_v110"
    folder = tmp_path / "memory" / subdir
    _build_legacy_dir(folder)

    # ---- Pre-migration snapshot (from the fabricated legacy store) ------
    snapshot_ids = [key for key, _pc, _md in _LEGACY_DOCS]
    snapshot_meta = {key: dict(md) for key, _pc, md in _LEGACY_DOCS}
    snapshot_content = {key: pc for key, pc, _md in _LEGACY_DOCS}

    try:
        # ---- Migration via v3's BUILT-IN legacy branch -------------------
        store = NativeStore(
            folder=folder,
            model=_FakeDeterministicEmbedder(),
            identity={"provider": "test", "model": "deterministic-fake"},
            namespace="test/",
            cache=None,
        )
        # initialize() returns False when legacy docs were migrated in.
        assert store.initialize() is False

        # ---- REAL disk reload: brand-new store over the same folder ------
        reloaded = NativeStore(
            folder=folder,
            model=_FakeDeterministicEmbedder(),
            identity={"provider": "test", "model": "deterministic-fake"},
            namespace="test/",
            cache=None,
        )
        reloaded.initialize()  # idempotent: same identity, no re-migration
        docs = reloaded.get_all_docs()

        # (a) Document-count parity.
        assert len(docs) == len(snapshot_ids)

        # (c) Document-ID stability (exact set, and id field == metadata id).
        assert set(docs.keys()) == set(snapshot_ids)
        for key, doc in docs.items():
            assert doc.id == key
            assert doc.metadata.get("id") == key

        # (b) Metadata parity — every snapshot key/value survives.
        for key in snapshot_ids:
            migrated_md = docs[key].metadata
            for md_key, md_val in snapshot_meta[key].items():
                assert migrated_md.get(md_key) == md_val, (
                    f"metadata drift for {key}.{md_key}: "
                    f"{migrated_md.get(md_key)!r} != {md_val!r}"
                )
            assert docs[key].page_content == snapshot_content[key]

        # (d) Search/retrieval parity vs the pre-migration snapshot.
        # Expected nearest neighbor is computed by brute force over the
        # deterministic fake vectors of the SNAPSHOT contents.
        query_text = "User prefers tea over coffee"
        query_vec = _FakeDeterministicEmbedder._one(query_text)

        def _dot(a, b):
            return sum(x * y for x, y in zip(a, b))

        expected_best = max(
            snapshot_ids,
            key=lambda k: _dot(
                query_vec, _FakeDeterministicEmbedder._one(snapshot_content[k])
            ),
        )
        hits = reloaded.search(query_vec, limit=3)
        assert hits, "migrated store returned no search hits"
        assert hits[0][0].id == expected_best
        assert hits[0][0].page_content == snapshot_content[expected_best]

        # (e) Pre-existing v1.1.0 sidecar rows join 1:1 with migrated IDs.
        conn = sqlite3.connect(folder / "neuro_core.db")
        try:
            rows = conn.execute(
                "SELECT id, importance, confidence, validation FROM memories"
            ).fetchall()
        finally:
            conn.close()
        sidecar = {row[0]: row for row in rows}
        assert set(sidecar.keys()) == set(docs.keys()), (
            "sidecar rows must join 1:1 with migrated NativeStore doc IDs"
        )
        for row in _SIDECAR_ROWS:
            key, validation = row[0], row[7]
            assert sidecar[key][3] == validation
            assert docs[key].metadata.get("validation_status") == validation

        # The v3 store artifact exists alongside the untouched legacy files.
        assert (folder / "memory.sqlite3").is_file()
        assert (folder / "index.pkl").is_file()  # legacy source preserved
    finally:
        _evict_warm_cache(subdir)
