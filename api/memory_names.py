"""Neuro Core ``memory_names`` API handler (WI-P52-KI031).

Exposes user-assigned display-name endpoints under
``/api/plugins/neuro_core/memory_names``:

* ``GET  /memory_names?memory_subdir=<sub>&id=<memory_id>`` — return the
  Memory Name alias for one memory (``null`` when none is set).
* ``GET  /memory_names?memory_subdir=<sub>&cluster_names=1`` — return the
  full custom Cluster-name mapping for the subdir.
* ``POST /memory_names`` — set or clear exactly ONE name per request:
  ``{memory_subdir, id, name}`` for a Memory Name, or
  ``{memory_subdir, cluster_key, name}`` for a Cluster Name.
  An empty/whitespace ``name`` CLEARS the stored name.

Persistence paths (KI-031, additive-only):

* Memory Name — a plain ``memory_name`` key in the document's FAISS
  metadata, written through the STANDARD metadata path
  (``Memory.get_by_subdir`` → ``db.aget_by_ids`` → mutate ``metadata`` →
  ``Memory.update_documents``). The update_documents sanitizer extension
  strips only ``neuro_*`` keys, so ``memory_name`` persists. The key rides
  the Phase 2 absorb migration without further change (binding condition,
  D-NC1-122). The framework Memory ID is never written or altered.
* Cluster Name — the reserved ``_cluster_names`` mapping inside the
  EXISTING ``relationships.json`` sidecar (ADR-NC1-002 boundary 5;
  GraphStore remains the single writer). No new store, no schema migration.

All endpoints require an authenticated session (RelationshipsApi
precedent). ``memory_subdir`` is a query string / parsed-input param — NOT
a path segment (framework routing splits on ``path.split("/", 2)``).
"""

from __future__ import annotations

from helpers.api import ApiHandler, Request
from usr.plugins.neuro_core.helpers.graph_store import GraphStore
from plugins._memory.helpers.memory import Memory


# Maximum stored display-name length (both name kinds).
_MAX_NAME_LENGTH = 120


class MemoryNamesApi(ApiHandler):
    """REST surface for Memory Name aliases and custom Cluster names."""

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
        consistency precedent from the relationships handler)."""
        return str(
            input.get(name)
            or request.args.get(name)
            or ""
        ).strip()

    @staticmethod
    def _clean_name(raw) -> str | None:
        """Validate/normalize a display name; None means CLEAR.

        Raises ValueError on type/length violations so the caller can
        surface a 4xx-style validation message.
        """
        if raw is None:
            return None
        if not isinstance(raw, str):
            raise ValueError("`name` must be a string or null to clear")
        clean = raw.strip()
        if not clean:
            return None
        if len(clean) > _MAX_NAME_LENGTH:
            raise ValueError(
                f"`name` must be at most {_MAX_NAME_LENGTH} characters"
            )
        return clean

    @staticmethod
    def _require_subdir(input: dict, request: Request) -> str:
        subdir = MemoryNamesApi._param(input, request, "memory_subdir")
        if not subdir:
            raise ValueError("`memory_subdir` is required")
        return subdir

    # ---- GET --------------------------------------------------------------

    async def _get(self, input: dict, request: Request) -> dict:
        try:
            subdir = self._require_subdir(input, request)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        memory_id = self._param(input, request, "id")
        want_clusters = self._param(input, request, "cluster_names")

        try:
            if memory_id:
                name = await self._read_memory_name(subdir, memory_id)
                return {
                    "success": True,
                    "memory_subdir": subdir,
                    "memory_id": memory_id,
                    "memory_name": name,
                }
            if want_clusters in ("1", "true", "True", "yes"):
                store = GraphStore(subdir)
                return {
                    "success": True,
                    "memory_subdir": subdir,
                    "cluster_names": store.get_cluster_names(),
                }
            return {
                "success": False,
                "error": "`id` or `cluster_names=1` is required",
            }
        except Exception as e:  # pragma: no cover - defensive
            return {"success": False, "error": str(e)}

    @staticmethod
    async def _read_memory_name(subdir: str, memory_id: str) -> str | None:
        """Read-only lookup of the ``memory_name`` metadata key."""
        memory = await Memory.get_by_subdir(subdir)
        docs = await memory.db.aget_by_ids([memory_id])
        if not docs:
            return None
        value = (docs[0].metadata or {}).get("memory_name")
        return value if isinstance(value, str) and value.strip() else None

    # ---- POST -------------------------------------------------------------

    async def _post(self, input: dict, request: Request) -> dict:
        try:
            subdir = self._require_subdir(input, request)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        memory_id = self._param(input, request, "id")
        cluster_key = self._param(input, request, "cluster_key")

        if bool(memory_id) == bool(cluster_key):
            return {
                "success": False,
                "error": (
                    "exactly one of `id` (Memory Name) or `cluster_key` "
                    "(Cluster Name) is required per request"
                ),
            }

        try:
            name = self._clean_name(input.get("name") if "name" in input
                                    else request.args.get("name"))
        except ValueError as e:
            return {"success": False, "error": str(e)}

        try:
            if memory_id:
                return await self._write_memory_name(
                    subdir, memory_id, name
                )
            return self._write_cluster_name(subdir, cluster_key, name)
        except Exception as e:  # pragma: no cover - defensive
            return {"success": False, "error": str(e)}

    @staticmethod
    async def _write_memory_name(
        subdir: str, memory_id: str, name: str | None
    ) -> dict:
        """Set/clear ``memory_name`` through the STANDARD metadata path.

        STANDARD path (binding condition, D-NC1-122): fetch the live
        Document via ``Memory.get_by_subdir`` + ``db.aget_by_ids``, mutate
        only ``metadata['memory_name']``, persist via
        ``Memory.update_documents``. The document ID is untouched. The
        sanitizer strips only ``neuro_*`` keys, so this plain key persists.
        """
        memory = await Memory.get_by_subdir(subdir)
        docs = await memory.db.aget_by_ids([memory_id])
        if not docs:
            return {
                "success": False,
                "error": f"memory id not found: {memory_id}",
            }
        doc = docs[0]
        metadata = doc.metadata if isinstance(doc.metadata, dict) else {}
        if name is None:
            metadata.pop("memory_name", None)
        else:
            metadata["memory_name"] = name
        doc.metadata = metadata
        # Delete + re-add by metadata['id'] inside update_documents; the
        # framework Memory ID (metadata['id']) is never modified here.
        await memory.update_documents([doc])
        return {
            "success": True,
            "memory_subdir": subdir,
            "memory_id": memory_id,
            "memory_name": name,
            "changed": True,
        }

    @staticmethod
    def _write_cluster_name(
        subdir: str, cluster_key: str, name: str | None
    ) -> dict:
        """Set/clear one custom Cluster name via GraphStore (additive
        reserved key in the existing relationships.json sidecar)."""
        store = GraphStore(subdir)
        changed = store.set_cluster_name(cluster_key, name) == 1
        return {
            "success": True,
            "memory_subdir": subdir,
            "cluster_key": cluster_key,
            "cluster_name": name,
            "changed": changed,
        }
