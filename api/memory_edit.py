"""Neuro Core ``memory_edit`` API handler (WI-P53-KI030-INSPECTOR-EDIT, KI-030).

Exposes safe-mode editing endpoints for one memory's Contents and scores
(Importance/Confidence/Stability) under
``/api/plugins/neuro_core/memory_edit``:

* ``GET  /memory_edit?memory_subdir=<sub>&id=<memory_id>`` — return the
  authoritative editable state: current document content plus the
  sidecar-backed score record (``null`` when no sidecar entry exists —
  legacy record; the UI then falls back to displayed metadata values).
* ``POST /memory_edit`` — apply edits in one request:
  ``{memory_subdir, id, content?, scores?}`` where ``scores`` is an
  object with any of ``importance`` / ``confidence`` / ``stability``.
  At least one of ``content`` or ``scores`` is required.

Persistence paths (KI-030, Phase-2-portable per D-NC1-122 — no new store,
no metadata-shape change):

* Content — the STANDARD metadata path (``Memory.get_by_subdir`` →
  ``db.aget_by_ids`` → mutate ``page_content`` → ``Memory.update_documents``),
  the same STANDARD path the ``memory_names`` handler uses for metadata.
  The framework Memory ID (``metadata['id']``) is never written or altered.
* Scores — ``ScoreStore.set()`` into the EXISTING ``scores.json`` sidecar
  ONLY (KI-009/WI-P12 single-write discipline: the sidecar is the score
  authority; FAISS metadata score keys are a potentially stale mirror and
  are NEVER written by this handler).

Validation (edit-time rejection, NOT clamping): ``ScoreStore``/``MemoryScores``
silently clamps out-of-range values via ``_clamp01`` (helpers/scores.py
``__post_init__``), so this handler validates every score as a real number
(bool excluded) in ``[0.0, 1.0]`` BEFORE calling ``ScoreStore.set``.

All endpoints require an authenticated session (``memory_names`` /
``RelationshipsApi`` precedent). ``memory_subdir`` is a query string /
parsed-input param — NOT a path segment (framework routing splits on
``path.split("/", 2)``).
"""

from __future__ import annotations

from helpers.api import ApiHandler, Request
from plugins._memory.helpers.memory import Memory

from usr.plugins.neuro_core.helpers.scores import ScoreStore


# The three editable score fields, in display order.
_SCORE_FIELDS = ("importance", "confidence", "stability")


class MemoryEditApi(ApiHandler):
    """REST surface for safe-mode editing of memory Contents and scores."""

    @classmethod
    def requires_auth(cls) -> bool:
        return True

    @classmethod
    def get_methods(cls) -> list[str]:
        return ["GET", "POST"]

    async def process(self, input: dict, request: Request) -> dict:
        try:
            method = (request.method or "GET").upper()
            if method == "GET":
                return await self._get(input, request)
            if method == "POST":
                return await self._post(input, request)
            return {"success": False, "error": f"Unknown route: {method}"}
        except Exception as e:  # pragma: no cover - defensive top-level
            return {"success": False, "error": str(e)}

    # ---- helpers ----------------------------------------------------------

    @staticmethod
    def _param(input: dict, request: Request, name: str) -> str:
        """Parsed input keeps precedence over the query string (KI-018-AL
        consistency precedent from the relationships/memory_names handlers)."""
        return str(
            input.get(name)
            or request.args.get(name)
            or ""
        ).strip()

    @staticmethod
    def _validate_score_value(field: str, raw) -> float:
        """Validate one score value; returns it as a float.

        Raises ValueError with the handler's canonical error string on
        type/range violations. Booleans are rejected explicitly (bool is
        an int subclass in Python but is not a valid score).
        """
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(
                f"`{field}` must be a number between 0.0 and 1.0"
            )
        value = float(raw)
        if value < 0.0 or value > 1.0:
            raise ValueError(
                f"`{field}` must be a number between 0.0 and 1.0"
            )
        return value

    @classmethod
    def _validate_scores(cls, raw) -> dict:
        """Validate a ``scores`` edit payload; returns the cleaned dict.

        Raises ValueError on shape/type/range violations. Only the three
        known fields are accepted; unknown keys are rejected so typos
        fail loudly instead of silently doing nothing.
        """
        if not isinstance(raw, dict):
            raise ValueError(
                "`scores` must be an object with optional keys "
                "`importance`, `confidence`, `stability`"
            )
        unknown = [k for k in raw if k not in _SCORE_FIELDS]
        if unknown:
            raise ValueError(
                "`scores` must be an object with optional keys "
                "`importance`, `confidence`, `stability`"
            )
        if not raw:
            raise ValueError(
                "`scores` must be an object with optional keys "
                "`importance`, `confidence`, `stability`"
            )
        return {
            field: cls._validate_score_value(field, raw[field])
            for field in _SCORE_FIELDS
            if field in raw
        }

    # ---- GET --------------------------------------------------------------

    async def _get(self, input: dict, request: Request) -> dict:
        try:
            subdir = self._param(input, request, "memory_subdir")
            if not subdir:
                return {"success": False, "error": "`memory_subdir` is required"}
            memory_id = self._param(input, request, "id")
            if not memory_id:
                return {"success": False, "error": "`id` is required"}
        except Exception as e:  # pragma: no cover - defensive
            return {"success": False, "error": str(e)}

        try:
            memory = await Memory.get_by_subdir(subdir)
            docs = await memory.db.aget_by_ids([memory_id])
            if not docs:
                return {
                    "success": False,
                    "error": f"memory id not found: {memory_id}",
                }
            doc = docs[0]
            record = ScoreStore(subdir).get_optional(memory_id)
            scores = (
                {
                    "importance": float(record.importance),
                    "confidence": float(record.confidence),
                    "stability": float(record.stability),
                }
                if record is not None
                else None
            )
            return {
                "success": True,
                "memory_subdir": subdir,
                "memory_id": memory_id,
                "content": getattr(doc, "page_content", "") or "",
                "scores": scores,
                "scores_source": "sidecar" if record is not None else "none",
            }
        except Exception as e:  # pragma: no cover - defensive
            return {"success": False, "error": str(e)}

    # ---- POST -------------------------------------------------------------

    async def _post(self, input: dict, request: Request) -> dict:
        try:
            subdir = self._param(input, request, "memory_subdir")
            if not subdir:
                return {"success": False, "error": "`memory_subdir` is required"}
            memory_id = self._param(input, request, "id")
            if not memory_id:
                return {"success": False, "error": "`id` is required"}
        except Exception as e:  # pragma: no cover - defensive
            return {"success": False, "error": str(e)}

        content = input.get("content") if "content" in input else None
        raw_scores = input.get("scores") if "scores" in input else None

        if content is None and raw_scores is None:
            return {
                "success": False,
                "error": "nothing to update: provide `content` and/or `scores`",
            }

        if content is not None:
            if not isinstance(content, str):
                return {"success": False, "error": "`content` must be a string"}
            if not content.strip():
                return {"success": False, "error": "`content` must not be empty"}

        if raw_scores is not None:
            try:
                scores = self._validate_scores(raw_scores)
            except ValueError as e:
                return {"success": False, "error": str(e)}
        else:
            scores = None

        try:
            return await self._apply_edit(
                subdir, memory_id, content, scores
            )
        except Exception as e:  # pragma: no cover - defensive
            return {"success": False, "error": str(e)}

    @staticmethod
    async def _apply_edit(
        subdir: str,
        memory_id: str,
        content: str | None,
        scores: dict | None,
    ) -> dict:
        """Apply the validated edit through the EXISTING persistence paths.

        Content: STANDARD metadata path (Memory.get_by_subdir →
        db.aget_by_ids → mutate page_content → Memory.update_documents).
        Scores: ScoreStore.set() into the scores.json sidecar ONLY —
        no FAISS metadata score write ever occurs here (KI-009/WI-P12).
        """
        memory = await Memory.get_by_subdir(subdir)
        docs = await memory.db.aget_by_ids([memory_id])
        if not docs:
            return {
                "success": False,
                "error": f"memory id not found: {memory_id}",
            }

        content_changed = False
        if content is not None:
            doc = docs[0]
            # The framework Memory ID (metadata['id']) is untouched; only
            # page_content is mutated before update_documents.
            doc.page_content = content
            await memory.update_documents([doc])
            content_changed = True

        scores_changed = False
        updated_scores = None
        if scores is not None:
            record = ScoreStore(subdir).set(memory_id, **scores)
            updated_scores = {
                "importance": float(record.importance),
                "confidence": float(record.confidence),
                "stability": float(record.stability),
            }
            scores_changed = True

        return {
            "success": True,
            "memory_subdir": subdir,
            "memory_id": memory_id,
            "content_changed": content_changed,
            "scores_changed": scores_changed,
            "scores": updated_scores,
        }
