"""Neuro Core ``memory_edit`` API handler (WI-P53-KI030-INSPECTOR-EDIT, KI-030).

Exposes safe-mode editing endpoints for one memory's Contents and scores
(Importance/Confidence/Stability) under
``/api/plugins/neuro_core/memory_edit``:

* ``GET  /memory_edit?memory_subdir=<sub>&id=<memory_id>`` — return the
  authoritative editable state: current document content plus the
  sidecar-backed score record (``null`` when no sidecar entry exists —
  legacy record; the UI then falls back to displayed metadata values).
* ``POST /memory_edit`` — apply edits in one request:
  ``{memory_subdir, id, content?, scores?, types?, validation_status?}``
  where ``scores`` is an object with any of ``importance`` /
  ``confidence`` / ``stability`` and ``validation_status`` is a single
  target-state string governed by ``USER_ALLOWED_TRANSITIONS`` (WI-P61).
  At least one component is required.

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

import copy
import sys
from pathlib import Path

from helpers.api import ApiHandler, Request
from plugins._memory.helpers.memory import Memory

# WI-P61-KI030 (C6 enabling import): the legacy top-level module cluster
# (activity_ledger -> neuro_core -> memory_lifecycle) resolves only with
# the plugin root on sys.path — the same mechanism the legacy tools use.
# APPEND (never insert-at-0) so framework packages such as ``helpers``
# always resolve from the framework root first.
_PLUGIN_ROOT = str(Path(__file__).resolve().parents[1])
if _PLUGIN_ROOT not in sys.path:
    sys.path.append(_PLUGIN_ROOT)

from activity_ledger import ActivityEvent, ActivityLedger  # noqa: E402
from neuro_core import Scope  # noqa: E402

from usr.plugins.neuro_core.helpers.scores import ScoreStore
from usr.plugins.neuro_core.helpers.metadata import normalize_memory_types


# The three editable score fields, in display order.
_SCORE_FIELDS = ("importance", "confidence", "stability")

# WI-P61-KI030 (C1): the validation_status vocabulary and the ARC-approved
# user transition matrix. The sweep remains the only automated writer; this
# matrix governs USER edits only and is enforced server-side at the single
# validation point in _apply_edit. ``deprecated`` is terminal (C2).
_VALIDATION_STATUSES = ("unvalidated", "validated", "disputed", "deprecated")
USER_ALLOWED_TRANSITIONS = {
    "unvalidated": ("disputed", "validated"),
    "validated": ("disputed", "unvalidated"),
    "disputed": ("unvalidated", "validated"),
    "deprecated": (),
}

# WI-P61-KI030 (C6): handler-level in-memory activity ledger. Each
# SUCCESSFUL user status transition appends exactly one event whose kind
# distinguishes the user edit from sweep persists (the sweep writes no
# ledger events; neuro_service uses "validation_changed" for service-level
# updates — this kind collides with neither). Rejected transitions write
# no entry (the loud API rejection is the record).
ACTIVITY_LEDGER = ActivityLedger()


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
            # WI-P59-KI029: normalized type set via the C1 single authority
            # (helpers/metadata.py). Read-derivation ONLY — no FAISS mutation
            # occurs here (KI-009 display-path discipline).
            meta = getattr(doc, "metadata", {}) or {}
            types_payload = normalize_memory_types(meta).to_payload()
            # WI-P61-KI030 (C2): the current validation_status as stored.
            # An absent key reads as "unvalidated" (the documented default,
            # matching the sweep's md.get default); an unknown stored value
            # is surfaced AS-IS with an explicit flag — never silently
            # mapped, defaulted, or invented.
            raw_status = meta.get("validation_status")
            if raw_status is None:
                current_status, status_unknown = "unvalidated", False
            else:
                current_status = str(raw_status)
                status_unknown = current_status not in _VALIDATION_STATUSES
            status_payload = {
                "current": current_status,
                "unknown": status_unknown,
                "allowed_targets": []
                if status_unknown
                else list(USER_ALLOWED_TRANSITIONS.get(current_status, ())),
            }
            return {
                "success": True,
                "memory_subdir": subdir,
                "memory_id": memory_id,
                "content": getattr(doc, "page_content", "") or "",
                "scores": scores,
                "scores_source": "sidecar" if record is not None else "none",
                "types": types_payload,
                "validation_status": status_payload,
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
        raw_types = input.get("types") if "types" in input else None
        raw_status = (
            input.get("validation_status")
            if "validation_status" in input
            else None
        )

        if (
            content is None
            and raw_scores is None
            and raw_types is None
            and raw_status is None
        ):
            return {
                "success": False,
                "error": "nothing to update: provide `content`, `scores`, `types`, and/or `validation_status`",
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

        if raw_types is not None:
            # WI-P59-KI029 (C4): FULL-SET-REPLACE of the authoritative type
            # set, validated entirely server-side at this ONE point via the
            # C1 authority (strict mode). Loud rejection with no partial
            # write; the Memory ID is never written or altered.
            try:
                types = normalize_memory_types(raw_types, strict=True)
            except ValueError as e:
                return {"success": False, "error": str(e)}
        else:
            types = None

        if raw_status is not None:
            # WI-P61-KI030 (C1/C2): shape + vocabulary validation here;
            # the transition matrix itself is enforced at the SINGLE
            # validation point in _apply_edit, once the stored current
            # state is known. Loud rejection, no partial write.
            if not isinstance(raw_status, str):
                return {
                    "success": False,
                    "error": "`validation_status` must be a string",
                }
            status_target = raw_status.strip()
            if status_target not in _VALIDATION_STATUSES:
                return {
                    "success": False,
                    "error": "`validation_status` must be one of: unvalidated, validated, disputed, deprecated",
                }
        else:
            status_target = None

        try:
            return await self._apply_edit(
                subdir, memory_id, content, scores, types, status_target
            )
        except Exception as e:  # pragma: no cover - defensive
            return {"success": False, "error": str(e)}

    @staticmethod
    async def _apply_edit(
        subdir: str,
        memory_id: str,
        content: str | None,
        scores: dict | None,
        types=None,
        status_target: str | None = None,
    ) -> dict:
        """Apply the validated edit through the EXISTING persistence paths.

        Content/types/validation_status: the STANDARD metadata path
        (Memory.get_by_subdir -> db.aget_by_ids -> staged deep-copy
        mutation -> Memory.update_documents). WI-P61-KI030 (C5): the FAISS
        metadata mutation is built on a DEEP COPY of the fetched document
        (WI-P60 rev3 mutation-before-write lesson) so a failed write
        leaves on-disk and in-memory state unchanged; the staged copy is
        what is passed to update_documents.
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

        doc = docs[0]

        # WI-P61-KI030 (C1/C2): the SINGLE transition-matrix validation
        # point. The stored current state decides everything: deprecated
        # is terminal, unknown stored values are rejected (never mapped or
        # invented), and only matrix edges are permitted. Runs BEFORE any
        # mutation; a rejection writes nothing anywhere.
        current_status = None
        status_changed = False
        if status_target is not None:
            meta_now = getattr(doc, "metadata", {}) or {}
            raw_now = meta_now.get("validation_status")
            current_status = "unvalidated" if raw_now is None else str(raw_now)
            allowed = USER_ALLOWED_TRANSITIONS.get(current_status, ())
            if status_target == current_status or status_target not in allowed:
                return {
                    "success": False,
                    "error": (
                        "`validation_status` transition not permitted: "
                        f"{current_status} -> {status_target}"
                    ),
                }

        # WI-P60 rev3 (C5): stage every FAISS metadata mutation on a deep
        # copy; the fetched document object is never mutated.
        staged = copy.deepcopy(doc)
        content_changed = False
        types_changed = False

        if content is not None:
            # The framework Memory ID (metadata['id']) is untouched; only
            # page_content is mutated before update_documents.
            staged.page_content = content
            content_changed = True

        if types is not None:
            # WI-P59-KI029 (C4/C5): full-set-replace type set via the SAME
            # standard metadata path. Memory ID untouched; NO sidecar
            # write (KI-009).
            meta = getattr(staged, "metadata", None)
            if meta is None:
                meta = {}
                staged.metadata = meta
            meta["memory_type"] = types.primary
            meta["memory_types"] = list(types.types)
            types_changed = True

        if status_target is not None:
            # WI-P61-KI030 (C5): validation_status is FAISS metadata
            # (WI-P60 A1), written through the SAME governed path as the
            # types payload — NOT the sidecar (the KI-009 discipline is
            # untouched; scores are never written to metadata here).
            meta = getattr(staged, "metadata", None)
            if meta is None:
                meta = {}
                staged.metadata = meta
            meta["validation_status"] = status_target
            status_changed = True

        if content_changed or types_changed or status_changed:
            await memory.update_documents([staged])

        # WI-P61-KI030 (C6): one activity-ledger entry per SUCCESSFUL user
        # status transition; rejected transitions never reach this point
        # and write no entry. Best-effort: a ledger failure is reported in
        # the response but never rolls back the committed user edit.
        ledger_recorded = False
        if status_changed:
            try:
                ACTIVITY_LEDGER.append(
                    ActivityEvent(
                        kind="validation_status_user_edit",
                        scope=Scope(project=subdir),
                        targets=(memory_id,),
                        outcome=status_target or "",
                        evidence={
                            "from": current_status or "",
                            "to": status_target or "",
                            "writer": "user_edit",
                        },
                    )
                )
                ledger_recorded = True
            except Exception:
                ledger_recorded = False

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
            "types_changed": types_changed,
            "validation_status_changed": status_changed,
            "validation_status": status_target,
            "ledger_recorded": ledger_recorded,
            "scores": updated_scores,
            "types": types.to_payload() if types is not None else None,
        }
