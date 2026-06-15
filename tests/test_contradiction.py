"""Tests for ``helpers/lifecycle.py::run_contradiction_detection``.

The function is invoked by the ``_30_contradiction_detection.py``
job_loop extension. It reads fact-type memories, finds pairs whose
cosine similarity exceeds the configured threshold and whose content
shows lexical opposition, and marks the older of each pair as
``validation_status == "disputed"`` (returned in the result dict; the
caller is responsible for persisting the side-effect).

The test suite mirrors the seven test points requested in Workstream B:

    1. No flagging when memories agree
    2. Flagging when memories contradict
    3. Threshold boundary (above vs below)
    4. Already-flagged memories not re-flagged (idempotent re-runs)
    5. Single memory — no contradiction possible
    6. Empty subdir — no error
    7. Return value shape and counts

Patterns are inherited from ``test_lifecycle_jobs.py``:

* In-memory stand-ins instead of the real ``ScoreStore``/``GraphStore``
  classes (no file I/O — the contradiction helper writes nothing to
  disk; the side-effects are reported via the return value).
* Direct import of the lifecycle module — no plugin bootstrap.
* ``pytest.approx`` for any float comparisons.

Note on the lexical heuristic
-----------------------------
The opposition check uses ``re.search(rf"\\b{re.escape(t)}\\b", text)``,
so a token like ``enable`` only matches the **standalone** word
``enable`` (with word boundaries on both sides). Forms like
``enabled`` or ``enabling`` do **not** match — they contain ``enable``
but lack a trailing word boundary. Test data must therefore use the
exact root form with a non-word character (space, end of string,
punctuation) immediately before and after the polarity token.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pytest

from usr.plugins.neuro_core.helpers import lifecycle


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


def _fact(
    memory_id: str,
    content: str,
    *,
    timestamp: str = "2026-01-01T00:00:00+00:00",
    extra: Optional[dict] = None,
) -> Tuple[str, str, dict]:
    """Build a ``(id, content, metadata)`` triple in the shape
    ``run_contradiction_detection`` expects.

    ``timestamp`` is an ISO-8601 string — the lifecycle helper compares
    them with ``<=`` so lexicographic ordering is what matters. Earlier
    timestamps sort "older" and win the dispute.
    """
    meta: Dict[str, Any] = {"timestamp": timestamp}
    if extra:
        meta.update(extra)
    return (memory_id, content, meta)


class _FakeMemory:
    """In-memory stand-in for ``plugins._memory.helpers.memory.Memory``.

    Implements only the ``search_similarity_threshold`` hook used by
    the contradiction helper. The hook accepts a ``threshold`` argument
    and returns only those fake docs whose registered similarity is
    ``>= threshold``. A doc is also skipped when its ``id`` matches
    the query string (the real implementation excludes the query doc).

    The ``register(query, hits)`` method indexes hits under ``query`` —
    which must be the **content string** the production function passes
    as the first positional argument to ``search_similarity_threshold``
    (the function calls it with the candidate's content, not its id).
    """

    def __init__(self) -> None:
        self.calls: List[Tuple[str, float]] = []
        self._index: Dict[str, List[Tuple[str, str, dict, float]]] = {}

    def register(
        self,
        query: str,
        hits: List[Tuple[str, str, float, dict]],
    ) -> None:
        """Register pre-computed hits for ``query`` (the content string).

        ``hits`` is a list of ``(hit_id, hit_content, similarity, metadata)``
        tuples. They are returned (filtered by threshold) when
        ``search_similarity_threshold`` is called with
        ``query=query`` and the registered hit's similarity ``>=``
        the call's ``threshold`` argument.
        """
        self._index[query] = [
            (hid, hcontent, hmeta, sim) for (hid, hcontent, sim, hmeta) in hits
        ]

    def search_similarity_threshold(
        self,
        query: str,
        *,
        limit: int = 10,
        threshold: float = 0.0,
    ) -> List[dict]:
        self.calls.append((query, threshold))
        out: List[dict] = []
        entries = self._index.get(query, [])
        for (hid, hcontent, hmeta, sim) in entries:
            if sim >= threshold and hid != query:
                out.append({
                    "id": hid,
                    "metadata": {"id": hid, **hmeta},
                    "page_content": hcontent,
                    "content": hcontent,
                })
                if len(out) >= limit:
                    return out
        return out


# ---------------------------------------------------------------------------
# 1. No flagging when memories agree
# ---------------------------------------------------------------------------


class TestNoFlaggingWhenAgreeing:
    """Two semantically consistent facts must not be flagged."""

    def test_two_agreeing_facts_are_not_flagged(self) -> None:
        """Two facts that do not lexically oppose each other → disputed=0."""
        facts: List[Tuple[str, str, dict]] = [
            _fact("a", "the server is enabled and running",
                  timestamp="2026-01-01T00:00:00+00:00"),
            _fact("b", "the server is enabled and running",
                  timestamp="2026-01-02T00:00:00+00:00"),
        ]
        result = lifecycle.run_contradiction_detection(
            "default",
            {"contradiction_similarity_threshold": 0.85},
            memory=None,
            facts=facts,
        )
        assert result["disputed"] == 0
        assert result["checked"] == 2

    def test_two_agreeing_facts_with_negation_in_both(self) -> None:
        """Both sides negated → same polarity → no contradiction."""
        facts = [
            _fact("a", "the feature is not enabled",
                  timestamp="2026-01-01T00:00:00+00:00"),
            _fact("b", "the feature is not enabled",
                  timestamp="2026-01-02T00:00:00+00:00"),
        ]
        result = lifecycle.run_contradiction_detection(
            "default",
            {"contradiction_similarity_threshold": 0.85},
            memory=None,
            facts=facts,
        )
        assert result["disputed"] == 0
        assert result["checked"] == 2

    def test_three_agreeing_facts_no_flags(self) -> None:
        """Three consistent facts → 3 checked, 0 disputed."""
        facts = [
            _fact("a", "agent zero uses faiss for vector search"),
            _fact("b", "faiss is the vector store used by agent zero"),
            _fact("c", "the memory plugin stores vectors in faiss"),
        ]
        result = lifecycle.run_contradiction_detection(
            "default", {}, memory=None, facts=facts
        )
        assert result["checked"] == 3
        assert result["disputed"] == 0


# ---------------------------------------------------------------------------
# 2. Flagging when memories contradict
# ---------------------------------------------------------------------------


class TestFlaggingWhenContradicting:
    """Two facts that lexically oppose each other → one flag (older wins)."""

    def test_contradicting_pair_flags_older(self) -> None:
        """``enable`` vs ``disable`` → older memory marked disputed."""
        facts = [
            _fact("a", "please enable the plugin",
                  timestamp="2026-01-01T00:00:00+00:00"),  # older
            _fact("b", "please disable the plugin",
                  timestamp="2026-01-02T00:00:00+00:00"),  # newer
        ]
        result = lifecycle.run_contradiction_detection(
            "default",
            {"contradiction_similarity_threshold": 0.85},
            memory=None,
            facts=facts,
        )
        # ``a`` (older) is the one that loses the dispute.
        assert result["disputed"] == 1
        assert result["checked"] == 2

    def test_contradicting_pair_with_true_false(self) -> None:
        """``true`` vs ``false`` polarity pair triggers the heuristic."""
        facts = [
            _fact("a", "the statement is true",
                  timestamp="2026-01-01T00:00:00+00:00"),
            _fact("b", "the statement is false",
                  timestamp="2026-01-02T00:00:00+00:00"),
        ]
        result = lifecycle.run_contradiction_detection(
            "default",
            {"contradiction_similarity_threshold": 0.85},
            memory=None,
            facts=facts,
        )
        assert result["disputed"] == 1
        assert result["checked"] == 2

    def test_contradicting_pair_with_negation(self) -> None:
        """One side negated, the other not → opposite polarity."""
        facts = [
            _fact("a", "the build succeeded",
                  timestamp="2026-01-01T00:00:00+00:00"),
            _fact("b", "the build did not succeed",
                  timestamp="2026-01-02T00:00:00+00:00"),
        ]
        result = lifecycle.run_contradiction_detection(
            "default",
            {"contradiction_similarity_threshold": 0.85},
            memory=None,
            facts=facts,
        )
        assert result["disputed"] == 1
        assert result["checked"] == 2

    def test_mixed_set_flags_only_contradicting_pairs(self) -> None:
        """In a set of 4 facts, only the contradicting pair is flagged."""
        facts = [
            _fact("a", "the feature should enable x",  # older
                  timestamp="2026-01-01T00:00:00+00:00"),
            _fact("b", "the feature should disable x",  # newer, opposes a
                  timestamp="2026-01-02T00:00:00+00:00"),
            _fact("c", "agent zero uses faiss",  # neutral, no opposition
                  timestamp="2026-01-03T00:00:00+00:00"),
            _fact("d", "the memory plugin is online",  # neutral
                  timestamp="2026-01-04T00:00:00+00:00"),
        ]
        result = lifecycle.run_contradiction_detection(
            "default",
            {"contradiction_similarity_threshold": 0.85},
            memory=None,
            facts=facts,
        )
        # Only a/b oppose; c and d do not oppose a or b (no negation,
        # no opposite-polarity tokens with respect to the contradictor).
        assert result["disputed"] == 1
        assert result["checked"] == 4


# ---------------------------------------------------------------------------
# 3. Threshold boundary
# ---------------------------------------------------------------------------


class TestThresholdBoundary:
    """The similarity threshold is enforced via the ``memory`` hook."""

    def test_pair_above_threshold_is_flagged(self) -> None:
        """A pair at sim=0.86 (above 0.85) is flagged."""
        mem = _FakeMemory()
        # Register hits for the content of ``a`` (the function passes
        # the candidate's content as the query string).
        a_content = "please enable the plugin"
        mem.register(
            a_content,
            [
                ("b", "please disable the plugin", 0.86,
                 {"timestamp": "2026-01-02T00:00:00+00:00"}),
            ],
        )
        facts = [
            _fact("a", a_content,
                  timestamp="2026-01-01T00:00:00+00:00"),
            _fact("b", "please disable the plugin",
                  timestamp="2026-01-02T00:00:00+00:00"),
        ]
        result = lifecycle.run_contradiction_detection(
            "default",
            {"contradiction_similarity_threshold": 0.85},
            memory=mem,
            facts=facts,
        )
        assert result["disputed"] == 1
        assert result["checked"] == 2
        # The threshold was forwarded to the memory hook.
        assert any(
            abs(th - 0.85) < 1e-9 for (_q, th) in mem.calls
        )

    def test_pair_below_threshold_is_not_flagged(self) -> None:
        """A pair at sim=0.84 (below 0.85) is NOT flagged."""
        mem = _FakeMemory()
        a_content = "please enable the plugin"
        mem.register(
            a_content,
            [
                ("b", "please disable the plugin", 0.84,
                 {"timestamp": "2026-01-02T00:00:00+00:00"}),
            ],
        )
        facts = [
            _fact("a", a_content,
                  timestamp="2026-01-01T00:00:00+00:00"),
            _fact("b", "please disable the plugin",
                  timestamp="2026-01-02T00:00:00+00:00"),
        ]
        result = lifecycle.run_contradiction_detection(
            "default",
            {"contradiction_similarity_threshold": 0.85},
            memory=mem,
            facts=facts,
        )
        # Below the threshold → no candidates pass the filter → 0 disputes.
        assert result["disputed"] == 0
        assert result["checked"] == 2

    def test_threshold_at_exact_boundary_is_inclusive(self) -> None:
        """A pair at sim == threshold is included (>=)."""
        mem = _FakeMemory()
        a_content = "please enable the plugin"
        mem.register(
            a_content,
            [
                ("b", "please disable the plugin", 0.85,
                 {"timestamp": "2026-01-02T00:00:00+00:00"}),
            ],
        )
        facts = [
            _fact("a", a_content,
                  timestamp="2026-01-01T00:00:00+00:00"),
            _fact("b", "please disable the plugin",
                  timestamp="2026-01-02T00:00:00+00:00"),
        ]
        result = lifecycle.run_contradiction_detection(
            "default",
            {"contradiction_similarity_threshold": 0.85},
            memory=mem,
            facts=facts,
        )
        assert result["disputed"] == 1

    def test_below_threshold_pair_with_opposing_content_not_flagged(self) -> None:
        """Even lexically-opposing content is NOT flagged when sim < threshold."""
        mem = _FakeMemory()
        a_content = "please enable the plugin"
        # The hook returns no hits because the only candidate is below
        # the threshold; the lexical heuristic never runs.
        mem.register(
            a_content,
            [
                ("b", "please disable the plugin", 0.50,
                 {"timestamp": "2026-01-02T00:00:00+00:00"}),
            ],
        )
        facts = [
            _fact("a", a_content,
                  timestamp="2026-01-01T00:00:00+00:00"),
            _fact("b", "please disable the plugin",
                  timestamp="2026-01-02T00:00:00+00:00"),
        ]
        result = lifecycle.run_contradiction_detection(
            "default",
            {"contradiction_similarity_threshold": 0.85},
            memory=mem,
            facts=facts,
        )
        assert result["disputed"] == 0


# ---------------------------------------------------------------------------
# 4. Already-flagged memories not re-flagged
# ---------------------------------------------------------------------------


class TestAlreadyFlaggedNotReprocessed:
    """Memories already in the disputed set are skipped on subsequent runs."""

    def test_second_run_on_same_data_does_not_double_count(self) -> None:
        """Running the helper twice on the same facts yields the same count."""
        facts = [
            _fact("a", "please enable the plugin",
                  timestamp="2026-01-01T00:00:00+00:00"),
            _fact("b", "please disable the plugin",
                  timestamp="2026-01-02T00:00:00+00:00"),
        ]
        first = lifecycle.run_contradiction_detection(
            "default",
            {"contradiction_similarity_threshold": 0.85},
            memory=None,
            facts=facts,
        )
        second = lifecycle.run_contradiction_detection(
            "default",
            {"contradiction_similarity_threshold": 0.85},
            memory=None,
            facts=facts,
        )
        assert first["disputed"] == 1
        # Re-run produces the same count — the helper does not maintain
        # state between calls, so each run re-derives the same result.
        # The point of this test is that the result is *deterministic*
        # and *idempotent*, not that state is persisted.
        assert second["disputed"] == 1
        assert second["checked"] == 2

    def test_already_disputed_id_not_added_again_in_one_run(self) -> None:
        """A fact that becomes disputed early in the run is not re-added."""
        # Three facts: A oldest, B newer (opposes A), C newest (also
        # opposes A). The inner-loop check ``if aid in disputed_ids:
        # break`` ensures that once A is added to disputed_ids, the
        # A-C pair is short-circuited.
        facts = [
            _fact("a", "please enable the plugin",
                  timestamp="2026-01-01T00:00:00+00:00"),  # oldest
            _fact("b", "please disable the plugin",
                  timestamp="2026-01-02T00:00:00+00:00"),
            _fact("c", "please disable the plugin",
                  timestamp="2026-01-03T00:00:00+00:00"),  # newest
        ]
        result = lifecycle.run_contradiction_detection(
            "default",
            {"contradiction_similarity_threshold": 0.85},
            memory=None,
            facts=facts,
        )
        # Only A is added to disputed_ids (from the A-B pair). The
        # A-C pair is skipped because A is already in disputed_ids.
        assert result["disputed"] == 1
        assert result["checked"] == 3

    def test_newer_disputed_id_skipped_as_candidate(self) -> None:
        """A fact whose id is already in disputed_ids is skipped as a candidate."""
        # The O(n^2) fallback iterates (i, j) for j > i, so we cannot
        # re-encounter a pair with a different ordering within one run.
        # However, the inner check ``if bid in disputed_ids: continue``
        # would short-circuit if the helper ever sees the same id twice.
        # Construct a 4-fact scenario where the same id is the
        # *candidate* in two different outer iterations and assert
        # the second occurrence is skipped.
        facts = [
            _fact("a", "please enable the plugin",
                  timestamp="2026-01-01T00:00:00+00:00"),
            _fact("b", "please disable the plugin",
                  timestamp="2026-01-02T00:00:00+00:00"),
            _fact("c", "please enable the plugin",
                  timestamp="2026-01-03T00:00:00+00:00"),
            _fact("d", "please disable the plugin",
                  timestamp="2026-01-04T00:00:00+00:00"),
        ]
        result = lifecycle.run_contradiction_detection(
            "default",
            {"contradiction_similarity_threshold": 0.85},
            memory=None,
            facts=facts,
        )
        # Pairs: a-b (oppose, a older → a disputed), a-c (no oppose,
        # both have "enable"), a-d (oppose, a older → a already
        # disputed, break), b-c (oppose, b older → b disputed), b-d
        # (b already disputed, break), c-d (oppose, c older → c
        # disputed). Result: 3 unique disputed ids (a, b, c).
        assert result["disputed"] == 3
        assert result["checked"] == 4


# ---------------------------------------------------------------------------
# 5. Single memory — no contradiction possible
# ---------------------------------------------------------------------------


class TestSingleMemoryNoContradiction:
    """A subdir with exactly one fact can never produce a contradiction."""

    def test_single_fact_returns_zero_disputed(self) -> None:
        facts = [
            _fact("a", "please enable the plugin",
                  timestamp="2026-01-01T00:00:00+00:00"),
        ]
        result = lifecycle.run_contradiction_detection(
            "default",
            {"contradiction_similarity_threshold": 0.85},
            memory=None,
            facts=facts,
        )
        assert result["checked"] == 1
        assert result["disputed"] == 0

    def test_single_fact_with_self_negation_returns_zero(self) -> None:
        """A self-negating fact is not flagged against itself."""
        facts = [
            _fact("a", "the feature is not enabled",
                  timestamp="2026-01-01T00:00:00+00:00"),
        ]
        result = lifecycle.run_contradiction_detection(
            "default", {}, memory=None, facts=facts
        )
        assert result["disputed"] == 0
        assert result["checked"] == 1


# ---------------------------------------------------------------------------
# 6. Empty subdir — no error
# ---------------------------------------------------------------------------


class TestEmptySubdirNoError:
    """Contradiction detection on an empty subdir must complete cleanly."""

    def test_empty_facts_returns_zero_zero(self) -> None:
        result = lifecycle.run_contradiction_detection(
            "default",
            {"contradiction_similarity_threshold": 0.85},
            memory=None,
            facts=[],
        )
        assert result == {"checked": 0, "disputed": 0}

    def test_none_facts_and_none_memory_returns_zero_zero(self) -> None:
        """``facts=None`` and ``memory=None`` → graceful no-op."""
        result = lifecycle.run_contradiction_detection(
            "default",
            {"contradiction_similarity_threshold": 0.85},
            memory=None,
            facts=None,
        )
        assert result == {"checked": 0, "disputed": 0}

    def test_empty_facts_does_not_call_memory_hook(self) -> None:
        """An empty subdir must not even invoke the memory hook."""
        mem = _FakeMemory()
        lifecycle.run_contradiction_detection(
            "default",
            {"contradiction_similarity_threshold": 0.85},
            memory=mem,
            facts=[],
        )
        assert mem.calls == []

    def test_default_config_used_when_empty(self) -> None:
        """An empty config dict falls back to the documented defaults."""
        result = lifecycle.run_contradiction_detection(
            "default", {}, memory=None, facts=[]
        )
        # Default threshold is 0.85, default batch is 100. The empty
        # path is independent of those values, but we still want to
        # confirm the helper does not raise on a fully-empty config.
        assert result["checked"] == 0
        assert result["disputed"] == 0


# ---------------------------------------------------------------------------
# 7. Return value / reporting
# ---------------------------------------------------------------------------


class TestReturnValue:
    """The function returns ``{"checked": int, "disputed": int}``."""

    def test_return_value_has_checked_and_disputed_keys(self) -> None:
        facts = [
            _fact("a", "please enable the plugin",
                  timestamp="2026-01-01T00:00:00+00:00"),
            _fact("b", "please disable the plugin",
                  timestamp="2026-01-02T00:00:00+00:00"),
        ]
        result = lifecycle.run_contradiction_detection(
            "default",
            {"contradiction_similarity_threshold": 0.85},
            memory=None,
            facts=facts,
        )
        assert set(result.keys()) >= {"checked", "disputed"}
        assert isinstance(result["checked"], int)
        assert isinstance(result["disputed"], int)

    def test_return_value_counts_match_input(self) -> None:
        """``checked`` equals the number of facts; ``disputed`` the count of
        unique older-of-pair ids that lost a dispute.
        """
        facts = [
            _fact("a", "please enable the plugin",
                  timestamp="2026-01-01T00:00:00+00:00"),
            _fact("b", "please disable the plugin",
                  timestamp="2026-01-02T00:00:00+00:00"),
            _fact("c", "faiss is the vector store"),
            _fact("d", "agent zero uses faiss"),
        ]
        result = lifecycle.run_contradiction_detection(
            "default",
            {"contradiction_similarity_threshold": 0.85},
            memory=None,
            facts=facts,
        )
        # All 4 facts were checked.
        assert result["checked"] == 4
        # Only the a/b pair opposes → 1 dispute.
        assert result["disputed"] == 1

    def test_return_value_zero_zero_on_empty(self) -> None:
        """Empty input → return value is the canonical zero-zero dict."""
        result = lifecycle.run_contradiction_detection(
            "default", {}, memory=None, facts=[]
        )
        # Canonical shape — not just the values, the exact dict.
        assert result == {"checked": 0, "disputed": 0}

    def test_return_value_respects_batch_size_cap(self) -> None:
        """``contradiction_batch_size`` caps the number of facts processed."""
        facts = [
            _fact(f"m{i}", f"fact number {i} is online")
            for i in range(10)
        ]
        result = lifecycle.run_contradiction_detection(
            "default",
            {
                "contradiction_similarity_threshold": 0.85,
                "contradiction_batch_size": 3,
            },
            memory=None,
            facts=facts,
        )
        # Only the first 3 facts are processed.
        assert result["checked"] == 3

    def test_return_value_default_config_path(self) -> None:
        """No explicit config → defaults from ``DEFAULT_CONFIG`` are used."""
        facts = [
            _fact("a", "please enable the plugin",
                  timestamp="2026-01-01T00:00:00+00:00"),
            _fact("b", "please disable the plugin",
                  timestamp="2026-01-02T00:00:00+00:00"),
        ]
        # Force the memory hook with a high sim so the default 0.85
        # threshold lets the candidate through.
        mem = _FakeMemory()
        a_content = "please enable the plugin"
        mem.register(
            a_content,
            [
                ("b", "please disable the plugin", 0.90,
                 {"timestamp": "2026-01-02T00:00:00+00:00"}),
            ],
        )
        result = lifecycle.run_contradiction_detection(
            "default", config=None, memory=mem, facts=facts
        )
        # The default threshold of 0.85 is used, the candidate at 0.90
        # passes, and the older fact (a) is disputed.
        assert result["disputed"] == 1
        assert result["checked"] == 2
