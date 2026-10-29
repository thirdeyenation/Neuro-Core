"""WI-P80-KI057-COMPAT-CONTRACT — embed-guard tests (ARC condition C3).

Pins the guarded cross-framework capability detection in
``_compute_embeddings`` (extensions/python/job_loop/_30_contradiction_detection.py):

    1. A wrapper with a callable batch-shaped ``embed`` (current framework
       shape, models.py:884) has ``embed`` used.
    2. A v2.8-shaped wrapper (commit 5ff106a2 — only ``embed_documents`` /
       ``embed_query``, no batch ``embed``) falls back to the batch-shaped
       ``embed_documents`` LangChain compatibility adapter (identical batch
       semantics per the framework embedding contract).
    3. A wrapper exposing neither callable method cleanly skips: the EMPTY
       mapping is returned (gated detections skipped for the pass), never
       None, never raised.
    4. ``embed`` is preferred when both methods exist (fixed order, C3).
    5. The WI-P63 condition-4 outer fail-safe is retained as the backstop:
       a raising fallback still yields the empty mapping.

No version-string gating, no caching, and no change to the verified model
resolution mechanism is exercised or pinned here — the resolution seam is
faked at its module boundary (the suite runtime has no litellm, so the real
``models`` module cannot import); the guard logic under test is the real
production code.

All data is synthetic in-memory fixtures; no plugin data files, no FAISS
index, and no LLM are touched.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


_PLUGIN = Path(__file__).resolve().parent.parent
_JOB = _PLUGIN / "extensions" / "python" / "job_loop" / "_30_contradiction_detection.py"


def _load_job_module():
    spec = importlib.util.spec_from_file_location(
        "_neuro_core_test_wip80_embed_guard", str(_JOB)
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _doc(memory_id: str, content: str) -> dict:
    md = {"id": memory_id, "timestamp": "2026-01-01T00:00:00+00:00"}
    return {"id": memory_id, "metadata": md, "page_content": content}


class _FakeModelConfig:
    """Minimal stand-in for the embedding model-config object."""

    provider = "fake-provider"
    name = "fake-model"

    def build_kwargs(self):
        return {}


class _CurrentWrapper:
    """Current-framework shape: batch-shaped embed() present (models.py:884)."""

    def __init__(self):
        self.embed_calls = []
        self.doc_calls = []

    def embed(self, inputs):
        self.embed_calls.append(list(inputs))
        return [[0.1, 0.2] for _ in inputs]

    def embed_documents(self, texts):
        self.doc_calls.append(list(texts))
        return [[0.9, 0.9] for _ in texts]


class _V28Wrapper:
    """v2.8-shaped wrapper (commit 5ff106a2): only embed_documents/embed_query
    — no batch embed() (models.py:831/863/870 at 5ff106a2)."""

    def __init__(self):
        self.doc_calls = []

    def embed_documents(self, texts):
        self.doc_calls.append(list(texts))
        return [[0.3, 0.4] for _ in texts]

    def embed_query(self, text):
        return [0.3, 0.4]


class _BareWrapper:
    """Wrapper exposing neither callable batch method — clean-skip tier."""

    embed = None  # non-callable attribute must NOT count as capability
    embed_documents = None


def _install_fake_resolution(monkeypatch, model):
    """Fake the model-resolution seam at its module boundary.

    ``_compute_embeddings`` imports ``get_embedding_model_config_object``
    and ``models`` locally inside its try block; injecting fake modules
    into ``sys.modules`` makes those local imports resolve to the fakes.
    """
    fake_mc_module = ModuleType("plugins._model_config.helpers.model_config")
    fake_mc_module.get_embedding_model_config_object = lambda: _FakeModelConfig()

    def _fake_get_embedding_model(provider, name, model_config=None, **kwargs):
        return model

    fake_models_module = ModuleType("models")
    fake_models_module.get_embedding_model = _fake_get_embedding_model

    # Parent packages must exist for the dotted local import to resolve.
    fake_pkg = ModuleType("plugins")
    fake_mc_pkg = ModuleType("plugins._model_config")
    fake_helpers_pkg = ModuleType("plugins._model_config.helpers")
    monkeypatch.setitem(sys.modules, "plugins", fake_pkg)
    monkeypatch.setitem(sys.modules, "plugins._model_config", fake_mc_pkg)
    monkeypatch.setitem(sys.modules, "plugins._model_config.helpers", fake_helpers_pkg)
    monkeypatch.setitem(
        sys.modules, "plugins._model_config.helpers.model_config", fake_mc_module
    )
    monkeypatch.setitem(sys.modules, "models", fake_models_module)


@pytest.fixture()
def job_module():
    return _load_job_module()


def test_current_wrapper_embed_used(job_module, monkeypatch):
    """C3 tier 1: a callable batch embed() is used (current framework)."""
    wrapper = _CurrentWrapper()
    _install_fake_resolution(monkeypatch, wrapper)
    mapping = job_module.ContradictionDetectionJob._compute_embeddings(
        [_doc("a", "alpha"), _doc("b", "beta")]
    )
    assert wrapper.embed_calls == [["alpha", "beta"]]
    assert wrapper.doc_calls == []  # fallback NOT used when embed() exists
    assert mapping == {"a": [0.1, 0.2], "b": [0.1, 0.2]}


def test_v28_wrapper_embed_documents_fallback(job_module, monkeypatch):
    """C3 tier 2: a v2.8-shaped wrapper (no batch embed) uses the
    embed_documents fallback with identical batch semantics."""
    wrapper = _V28Wrapper()
    _install_fake_resolution(monkeypatch, wrapper)
    mapping = job_module.ContradictionDetectionJob._compute_embeddings(
        [_doc("a", "alpha"), _doc("b", "beta")]
    )
    assert wrapper.doc_calls == [["alpha", "beta"]]
    assert mapping == {"a": [0.3, 0.4], "b": [0.3, 0.4]}


def test_bare_wrapper_clean_skip_empty_mapping(job_module, monkeypatch):
    """C3 tier 3: a wrapper exposing neither callable method cleanly skips —
    the EMPTY mapping is returned, never None, never raised."""
    wrapper = _BareWrapper()
    _install_fake_resolution(monkeypatch, wrapper)
    mapping = job_module.ContradictionDetectionJob._compute_embeddings(
        [_doc("a", "alpha")]
    )
    assert mapping == {}


def test_embed_preferred_over_embed_documents(job_module, monkeypatch):
    """C3 fixed order: when both methods exist, embed() is used and the
    fallback is not invoked."""
    wrapper = _CurrentWrapper()
    _install_fake_resolution(monkeypatch, wrapper)
    job_module.ContradictionDetectionJob._compute_embeddings([_doc("a", "alpha")])
    assert len(wrapper.embed_calls) == 1
    assert wrapper.doc_calls == []


def test_failsafe_backstop_on_raising_fallback(job_module, monkeypatch):
    """WI-P63 condition 4 backstop retained: a raising embed_documents
    fallback still degrades to the empty mapping via the outer fail-safe —
    never None, never raised, scheduler stays healthy."""

    class _RaisingWrapper:
        def embed_documents(self, texts):
            raise RuntimeError("boom")

    wrapper = _RaisingWrapper()
    _install_fake_resolution(monkeypatch, wrapper)
    mapping = job_module.ContradictionDetectionJob._compute_embeddings(
        [_doc("a", "alpha")]
    )
    assert mapping == {}


def test_empty_docs_short_circuit(job_module, monkeypatch):
    """No docs → empty mapping before any model resolution (existing
    behavior preserved)."""
    mapping = job_module.ContradictionDetectionJob._compute_embeddings([])
    assert mapping == {}
