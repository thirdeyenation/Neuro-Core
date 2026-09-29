"""Agent Zero tool for explainable Neuro Core retrieval.

Re-homed per ADR-NC1-003 §3 (WI-P43-PHASE1-RECALL-SHAPING, D-NC1-106):
the retrieval backend is the plugin's explainable hybrid pipeline
``search_context_graph`` (helpers/retrieval.py) over the HOST memory
universe, replacing the retired SQLite-domain backend. The tool is
INDEPENDENT of the ``recall_shaping_enabled`` gate: that gate governs
only the end-hook re-ranking of ordinary host recall (memory_load);
this tool always serves the explainable pipeline it is asked for.

Honest behavior (KI-008 contract): when ScoreStore/GraphStore are
unavailable or a sidecar read fails, nodes degrade to metadata fallback
values with explicit ``neuro_degraded`` markers — no fabricated healthy
baselines. Every delivered factor is the actual value used by the
pipeline's ranking (hop-0 seeds: ``_semantic_for`` over metadata keys;
graph neighbors: the pipeline's ``sem = 0.5`` baseline; ``score`` is the
pipeline's node score verbatim). Validation status is delivered as
ANNOTATION data only — it never gates or blocks retrievability. No
performance, concurrency, or security property is claimed or implied.
"""

from __future__ import annotations

from helpers.tool import Tool, Response


def _plugin_config() -> dict:
    """Resolved plugin config as a plain dict (framework settings chain)."""
    try:
        from helpers.plugins import get_plugin_config

        cfg = get_plugin_config("neuro_core")
        if isinstance(cfg, dict):
            return cfg
    except Exception:
        pass
    return {}


class NeuroRetrieve(Tool):
    async def execute(
        self,
        query="",
        limit=None,
        threshold=None,
        memory_subdir="",
        **kwargs,
    ):
        if not query or not str(query).strip():
            return Response(
                message="Error: `query` is required",
                break_loop=False,
            )

        from plugins._memory.helpers.memory import Memory
        from usr.plugins.neuro_core.helpers.retrieval import search_context_graph

        cfg = _plugin_config()
        subdir = str(memory_subdir).strip() or "default"

        # Invocation defaults (ADR-NC1-003 §3): explicit agent arguments
        # win; otherwise the pipeline config keys apply, falling back to
        # the documented tool defaults (limit=10, threshold=0.6).
        config = dict(cfg)
        if limit is not None:
            config["semantic_limit"] = int(limit)
        elif "semantic_limit" not in config:
            config["semantic_limit"] = 10
        if threshold is not None:
            config["semantic_threshold"] = float(threshold)
        elif "semantic_threshold" not in config:
            config["semantic_threshold"] = 0.6

        # Stores are best-effort: a missing/unreadable sidecar degrades
        # the pipeline (metadata fallback + neuro_degraded) instead of
        # failing the tool (KI-008 honesty contract).
        graph_store = None
        score_store = None
        try:
            from usr.plugins.neuro_core.helpers.graph_store import GraphStore

            graph_store = GraphStore(subdir)
        except Exception:
            graph_store = None
        try:
            from usr.plugins.neuro_core.helpers.scores import ScoreStore

            score_store = ScoreStore(subdir)
        except Exception:
            score_store = None

        try:
            memory = await Memory.get_by_subdir(subdir, preload_knowledge=False)
        except Exception as exc:
            return Response(
                message=(
                    f"Error: host memory for subdir '{subdir}' is "
                    f"unavailable: {exc}"
                ),
                break_loop=False,
            )

        graph = await search_context_graph(
            memory=memory,
            query=str(query),
            graph_store=graph_store,
            score_store=score_store,
            config=config,
        )
        return self._serialize(graph, score_store)

    def _serialize(self, graph, score_store) -> dict:
        """Serialize the ContextGraph with per-node factor records.

        Factors are reconstructed with the EXACT helpers and baselines the
        pipeline used for its ranking (helpers/retrieval.py): hop-0 seeds
        take ``_semantic_for`` over metadata keys; graph neighbors take the
        pipeline's ``sem = 0.5`` baseline; ``score`` is the pipeline's node
        score verbatim. Confidence and validation status are sidecar-
        authoritative reads delivered as annotation data.
        """
        from usr.plugins.neuro_core.helpers.retrieval import (
            _importance_for,
            _recency_score,
            _semantic_for,
        )

        nodes_out = []
        for n in graph.nodes:
            meta = getattr(n, "metadata", None) or {}
            degraded = bool(meta.get("neuro_degraded"))

            # Similarity: identical source semantics as the pipeline.
            if n.hop == 0:
                sem = _semantic_for(n, default=1.0)
            else:
                sem = 0.5  # pipeline's graph-neighbor baseline

            imp, imp_degraded = _importance_for(n.doc_id, n, score_store)
            degraded = degraded or imp_degraded

            conf, conf_degraded = self._confidence_for(
                n.doc_id, meta, score_store
            )
            degraded = degraded or conf_degraded

            rec = _recency_score(
                meta.get("last_accessed_at") or meta.get("timestamp")
            )
            status = meta.get("validation_status")
            if not isinstance(status, str) or not status:
                status = "unvalidated"

            nodes_out.append({
                "doc_id": n.doc_id,
                "content": n.content,
                "hop": n.hop,
                "factors": {
                    "similarity": round(float(sem), 6),
                    "importance": round(float(imp), 6),
                    "confidence": round(float(conf), 6),
                    "recency": round(float(rec), 6),
                    "validation_status": status,
                    # The pipeline's actual ranking value, verbatim.
                    "score": round(float(n.score), 6),
                },
                "neuro_degraded": degraded,
            })

        edges_out = [
            {
                "from_id": e.from_id,
                "to_id": e.to_id,
                "type": e.type,
                "confidence": round(float(e.confidence), 6),
            }
            for e in graph.edges
        ]

        return {
            "query": graph.query,
            "seed_ids": list(graph.seed_ids),
            "nodes": nodes_out,
            "edges": edges_out,
        }

    @staticmethod
    def _confidence_for(doc_id: str, meta: dict, score_store) -> tuple:
        """Sidecar-authoritative confidence; metadata fallback + degraded.

        Mirrors helpers/recall_shaping.py:_confidence_for semantics:
        sidecar record present -> rec.confidence (healthy); read failure
        or absent (legacy) record -> metadata confidence, else 0.7 default
        (helpers/metadata.py:258-260), with an explicit degraded marker.
        """
        if score_store is not None:
            rec = None
            degraded = False
            try:
                if hasattr(score_store, "get_optional"):
                    rec = score_store.get_optional(doc_id)
                else:
                    rec = score_store.get(doc_id)
            except Exception:
                rec = None
                degraded = True
            else:
                degraded = rec is None
            if rec is not None:
                try:
                    return max(0.0, min(1.0, float(rec.confidence))), False
                except (TypeError, ValueError):
                    pass
        else:
            degraded = True
        conf = meta.get("confidence")
        if isinstance(conf, (int, float)):
            return max(0.0, min(1.0, float(conf))), degraded
        return 0.7, degraded
