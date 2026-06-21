"""Neuro Core — episode-level reflection helpers.

Three pure functions used by the ``memory_reflect`` tool:

* :func:`collect_episode_memories` — fetch docs that share an
  ``episode_id`` and sort them by timestamp.
* :func:`reflect_memories` — call the agent's LLM with the reflection
  system prompt and the collected memory texts.
* :func:`write_reflection` — persist the LLM's output as a new
  ``memory_type=concept`` memory that links back to the episode.

All three are defensive — the tool layer expects them never to raise.
The graceful-degradation contract is: empty input → empty output;
LLM failure → empty string; FAISS failure → empty doc-id string.
"""

from __future__ import annotations

import logging
import types
from datetime import datetime, timezone
from typing import Any, Iterable, List, Optional, Sequence

# We import Memory lazily inside the functions to avoid a hard import
# dependency on the framework at module load time. ``_Document`` is a
# type alias for anything that quacks like a LangChain Document.
_Document = Any

# Cap on the default limit so a runaway tool call cannot drag in the
# entire FAISS index. The tool layer is allowed to override this with
# the ``limit`` arg; this constant is the hard ceiling.
_HARD_MAX_MEMORIES = 100

# Reflection system prompt — also exposed as a plain text file in
# ``prompts/neuro.reflection.sys.md`` so it is editable by humans.
DEFAULT_REFLECTION_PROMPT = (
    "You are a memory reflection assistant. Given a set of related "
    "memory entries from an agent's knowledge store, your task is to "
    "synthesize a concise, high-value insight or summary.\n\n"
    "Guidelines:\n\n"
    "- Identify the most important patterns, decisions, or lessons "
    "across the provided memories.\n"
    "- Write a single cohesive paragraph (3-5 sentences) that "
    "captures the essence of what was learned or experienced.\n"
    "- Do not repeat individual facts verbatim - synthesize and "
    "elevate.\n"
    "- Assign high importance to durable insights (principles, "
    "strategies, recurring patterns).\n"
    "- Assign lower importance to transient details (specific values, "
    "one-off events).\n"
    "- Output only the reflection paragraph. No preamble, no labels, "
    "no metadata."
)


def _doc_id(doc: _Document) -> str:
    """Safely pull a stable id from a Document-like object."""
    md = getattr(doc, "metadata", None)
    if isinstance(md, dict):
        return str(md.get("id") or "")
    return ""


def _doc_timestamp(doc: _Document) -> str:
    """Safely pull an ISO timestamp from a Document-like object."""
    md = getattr(doc, "metadata", None)
    if isinstance(md, dict):
        ts = md.get("timestamp")
        if ts:
            return str(ts)
    return ""


def _parse_ts(ts: str) -> Optional[datetime]:
    """Tolerantly parse an ISO timestamp; return ``None`` on failure."""
    if not ts:
        return None
    try:
        # ``fromisoformat`` handles ``Z`` only on Python 3.11+; we
        # normalise by hand for older interpreters.
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _matches_episode(doc: _Document, episode_id: str) -> bool:
    md = getattr(doc, "metadata", None)
    if not isinstance(md, dict):
        return False
    ep = md.get("episode_id")
    if ep is None or episode_id is None:
        return False
    return str(ep) == str(episode_id)


async def collect_episode_memories(
    memory_subdir: str,
    episode_id: str,
    memory: Any,
    limit: int = 20,
) -> List[_Document]:
    """Return docs that share ``episode_id``, sorted by timestamp asc.

    Parameters
    ----------
    memory_subdir:
        Memory subdir passed through to ``Memory``. Currently unused at
        the helper level (the subdir is captured by the ``memory``
        instance) but accepted to keep the signature explicit and to
        support a future migration to per-subdir FAISS instances.
    episode_id:
        The ``episode_id`` to filter on.
    memory:
        A live ``Memory`` instance. The helper tolerates ``None``
        and any object that exposes ``.db.get_by_ids()`` and
        ``.db.index_to_docstore_id``.
    limit:
        Maximum number of docs to return. Values > ``_HARD_MAX_MEMORIES``
        are silently clamped down.

    Returns
    -------
    list
        Sorted documents, possibly empty. Never raises.

    Notes
    -----
    Episode collection requires exhaustive enumeration of all docs in
    the subdir, not semantic ranking. We use the same pattern as
    ``memory_score`` and ``memory_relate``:

    1. Enumerate all doc IDs from ``memory.db.index_to_docstore_id``
       (the FAISS-to-docstore ID mapping). If the mapping is empty
       (new or unloaded index), return ``[]`` immediately.
    2. Fetch all documents via ``memory.db.get_by_ids(all_ids)`` —
       this is a synchronous pure-dict-lookup on
       ``memory.db.docstore._dict`` and returns ``List[Document]``.
       No ``await`` — ``MyFaiss.get_by_ids`` is sync (its async wrapper
       ``aget_by_ids`` simply delegates to it).
    3. Filter client-side by ``metadata.episode_id == episode_id``.
    4. Sort by timestamp ascending, apply ``[:limit]`` cap.

    The earlier ``search_similarity_threshold`` approach (D21 v1–v2)
    was abandoned: ``episode_id`` strings like ``"execute-test-001"``
    have no guaranteed semantic similarity to episode memories, so
    the semantic search returned 0 results regardless of threshold.
    Exhaustive enumeration matches the pattern used by ``memory_score``
    and ``memory_relate`` for ID-based retrieval.
    """
    # ``memory_subdir`` is captured by the ``memory`` instance itself
    # (``memory.memory_subdir``); ``get_by_ids`` operates on the db
    # instance which is already subdir-scoped. We keep the parameter in
    # the signature for API stability with the existing ``memory_reflect``
    # tool caller.

    if not episode_id:
        return []

    # Clamp the user-facing limit to a sane range.
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = 20
    n = max(1, min(n, _HARD_MAX_MEMORIES))

    docs: List[_Document] = []
    try:
        # Enumerate all doc IDs from the FAISS index. ``index_to_docstore_id``
        # is a dict mapping FAISS index positions to docstore IDs. If it
        # is empty (new or unloaded index), there is nothing to enumerate.
        index_to_docstore_id = getattr(memory.db, "index_to_docstore_id", None)
        if not index_to_docstore_id:
            return []
        all_ids = list(index_to_docstore_id.values())
        if not all_ids:
            return []

        # Fetch all documents synchronously. ``get_by_ids`` is a pure
        # dict lookup on ``memory.db.docstore._dict`` — no IO, no await.
        # Returns ``List[Document]`` (not a dict).
        docs_raw = memory.db.get_by_ids(all_ids)

        # Filter client-side by ``metadata.episode_id``.
        docs = [d for d in docs_raw if _matches_episode(d, episode_id)]
    except Exception as _exc:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "collect_episode_memories: search failed for "
            "episode_id=%r subdir=%r: %s: %s",
            episode_id, memory_subdir,
            type(_exc).__name__, _exc,
        )
        return []

    # Sort by timestamp asc, falling back to id for stability.
    def _sort_key(d: _Document):
        ts = _parse_ts(_doc_timestamp(d))
        if ts is None:
            # ``datetime.min`` (naive) is acceptable because we use it
            # only as a sort key and the comparison is purely ordinal.
            return (datetime.min.replace(tzinfo=timezone.utc), _doc_id(d))
        return (ts, _doc_id(d))

    docs.sort(key=_sort_key)
    return docs[:n]

    # Sort by timestamp asc, falling back to id for stability.
    def _sort_key(d: _Document):
        ts = _parse_ts(_doc_timestamp(d))
        if ts is None:
            # ``datetime.min`` (naive) is acceptable because we use it
            # only as a sort key and the comparison is purely ordinal.
            return (datetime.min.replace(tzinfo=timezone.utc), _doc_id(d))
        return (ts, _doc_id(d))

    docs.sort(key=_sort_key)
    return docs[:n]


async def reflect_memories(
    docs: Sequence[_Document],
    agent: Any,
    system_prompt: Optional[str] = None,
) -> str:
    """Call the agent's LLM to synthesise an episode-level reflection.

    Parameters
    ----------
    docs:
        Episode memories to reflect on. May be empty — in that case the
        function returns an empty string immediately without touching
        the LLM.
    agent:
        Agent instance. The helper tolerates ``None`` and falls back
        to a no-op response. The expected LLM-call entry point is
        ``agent.call_llm(...)`` with kwargs ``system`` and ``user``.
    system_prompt:
        Override for the system prompt. When ``None`` the helper uses
        :data:`DEFAULT_REFLECTION_PROMPT`.

    Returns
    -------
    str
        The LLM's response, stripped of leading/trailing whitespace.
        An empty string is returned for any failure or empty input.
    """
    if not docs:
        return ""

    prompt = (system_prompt or DEFAULT_REFLECTION_PROMPT).strip()

    # Build the user message from the doc contents.
    chunks: List[str] = []
    for i, d in enumerate(docs, start=1):
        body = getattr(d, "page_content", "") or ""
        ts = _doc_timestamp(d)
        head = f"[{i}]"
        if ts:
            head += f" ({ts})"
        chunks.append(f"{head}\n{body.strip()}\n")
    user_msg = "Memories:\n\n" + "\n".join(chunks)

    # Look up the LLM-call entry point. We support a small set of
    # common names so this helper works with both the runtime and
    # the test double.
    if agent is None:
        return ""
    call = getattr(agent, "call_utility_model", None)
    if call is None or not callable(call):
        return ""

    try:
        result = call(
            system=prompt,
            message=user_msg,
            background=False,
        )
    except Exception:
        return ""

    # The agent may return either a string, a coroutine, or an object
    # with a ``content`` / ``response`` / ``text`` attribute. Normalise.
    if hasattr(result, "__await__"):
        try:
            result = await result
        except Exception:
            return ""

    if isinstance(result, str):
        return result.strip()
    if isinstance(result, dict):
        for key in ("content", "response", "text", "message"):
            if key in result and isinstance(result[key], str):
                return result[key].strip()
        return ""
    for attr in ("content", "response", "text", "message"):
        v = getattr(result, attr, None)
        if isinstance(v, str):
            return v.strip()
    return ""


async def write_reflection(
    memory_subdir: str,
    content: str,
    episode_id: str,
    memory: Any,
) -> str:
    """Persist the reflection as a new concept memory.

    The new doc is written with:

    * ``memory_type = "concept"``
    * ``stability = 0.9``
    * ``importance = 0.8``
    * ``episode_id = <episode_id>``
    * ``source = "neuro_reflect"``

    Returns the new doc's id, or an empty string on failure.
    """
    if not content or not content.strip():
        return ""
    if memory is None:
        return ""

    metadata = {
        "memory_type": "concept",
        "stability": 0.9,
        "importance": 0.8,
        "episode_id": str(episode_id or ""),
        "source": "neuro_reflect",
        "area": getattr(
            getattr(memory, "Area", None), "MAIN", "main"
        ) if hasattr(memory, "Area") else "main",
    }

    try:
        # The ``Memory`` API is async — see ``memory_save.py`` for the
        # canonical call site. We try both ``insert_text`` and
        # ``insert_documents`` to support both shims.
        if hasattr(memory, "insert_text") and callable(memory.insert_text):
            new_id = await memory.insert_text(content, metadata)
        else:
            # Build a minimal Document. We prefer the langchain
            # ``Document`` class when it is available, but fall back to
            # a duck-typed ``SimpleNamespace`` so this helper works in
            # test environments that do not import langchain_core. Both
            # shapes expose ``page_content`` and ``metadata`` which is
            # all the FAISS insert path needs.
            doc = _build_document(content, metadata)
            if hasattr(memory, "insert_documents") and callable(memory.insert_documents):
                ids = await memory.insert_documents([doc])
                new_id = ids[0] if ids else ""
            else:
                return ""
    except Exception:
        return ""

    # D50 — write the scores.json sidecar entry for the new reflection
    # memory. Without this, scores.json diverges from FAISS metadata and
    # every Neuro Core subsystem that reads importance/stability/confidence
    # from the sidecar (Dashboard UI, decay job, contradiction job,
    # ContextGraph weighting, recall ranking) is blind to the reflection.
    if new_id:
        try:
            from usr.plugins.neuro_core.helpers.scores import ScoreStore
            _ss = ScoreStore(memory_subdir)
            # Core score fields via the public set() API.
            _ss.set(
                memory_id=str(new_id),
                importance=0.8,
                stability=0.9,
                confidence=0.9,
            )
            # Operational metadata fields the sidecar records.
            # set() already persisted the numeric fields; we append
            # ``source`` and ``episode_id`` and save once more.
            _ss._ensure_loaded()
            with _ss._locked():
                _rec = _ss._data.setdefault(
                    str(new_id),
                    {
                        "importance": 0.5,
                        "confidence": 0.7,
                        "stability": 0.5,
                        "access_count": 0,
                        "last_accessed_at": None,
                    },
                )
                _rec["source"] = "neuro_reflect"
                _rec["episode_id"] = str(episode_id or "")
                _ss.save()
        except Exception as _exc:  # pragma: no cover — defensive
            try:
                _log = logging.getLogger(__name__)
                _log.warning(
                    "write_reflection: sidecar write failed for %r: %s",
                    new_id,
                    _exc,
                )
            except Exception:
                pass

    return str(new_id or "")


def _build_document(content: str, metadata: dict) -> Any:
    """Return a Document-like object suitable for FAISS insertion.

    Prefers ``langchain_core.documents.Document`` when the package is
    importable; otherwise falls back to a plain object that exposes
    ``page_content`` and ``metadata``. This keeps the helper usable in
    test environments that have not imported the full langchain stack.
    """
    try:
        from langchain_core.documents import Document  # type: ignore
        return Document(page_content=content, metadata=metadata)
    except Exception:
        # ``SimpleNamespace`` quacks like a Document for our purposes:
        # the FAISS insert path only reads ``.page_content`` and
        # ``.metadata``.
        return types.SimpleNamespace(page_content=content, metadata=metadata)


# ---------------------------------------------------------------------- sync

def collect_episode_memories_sync(
    memory_subdir: str,
    episode_id: str,
    memory: Any,
    limit: int = 20,
) -> List[_Document]:
    """Synchronous wrapper around :func:`collect_episode_memories`."""
    import asyncio
    return asyncio.run(
        collect_episode_memories(memory_subdir, episode_id, memory, limit)
    )


def reflect_memories_sync(
    docs: Sequence[_Document],
    agent: Any,
    system_prompt: Optional[str] = None,
) -> str:
    """Synchronous wrapper around :func:`reflect_memories`."""
    import asyncio
    return asyncio.run(
        reflect_memories(docs, agent, system_prompt)
    )


def write_reflection_sync(
    memory_subdir: str,
    content: str,
    episode_id: str,
    memory: Any,
) -> str:
    """Synchronous wrapper around :func:`write_reflection`."""
    import asyncio
    return asyncio.run(
        write_reflection(memory_subdir, content, episode_id, memory)
    )
