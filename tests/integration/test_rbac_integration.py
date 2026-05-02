"""Integration test for RBAC filter behavior.

Two layers of coverage:

1. **Filter-shape tests** (no Qdrant required) — verify that
   `build_rbac_filter()` produces the right Qdrant `Filter` object for each of
   the 5 roles defined in `src.config.rbac_config`.

2. **Qdrant end-to-end tests** (skip if Qdrant unreachable) — confirm Qdrant
   actually honors the filter against the live FinanceBench collection. All FB
   chunks are tagged `doc_type=10k, confidentiality=public`, so:
     - admin / analyst / finance / c_level → all 68k+ chunks visible
     - hr (expense_policy only) → ZERO chunks visible

This locks down the architectural promise that RBAC is enforced at the vector-DB
level (chunks unauthorized for a role are never retrieved, not just hidden after
retrieval). Per the Sprint 7.6 Day 1 RBAC audit (SESSION_HANDOFF.md §6), the
research agent's `retrieve_for_subq` sub-node must delegate through the existing
retrieval path so it inherits this filter — that contract is enforced in Day 2.
"""
from __future__ import annotations

import pytest

from src.config.rbac_config import get_permissions
from src.services.vector_store import build_rbac_filter

FB_COLLECTION = "financebench_corpus_pypdf_clean"


def _qdrant_alive() -> bool:
    try:
        from src.services.vector_store import get_qdrant_client

        client = get_qdrant_client()
        client.get_collections()
        return True
    except Exception:  # noqa: BLE001
        return False


qdrant_required = pytest.mark.skipif(
    not _qdrant_alive(),
    reason="Qdrant not reachable on localhost:6333 — skipping live RBAC tests",
)


# ────────────────────────────────────────────────────────────
# Layer 1: filter-shape tests (no Qdrant)
# ────────────────────────────────────────────────────────────

class TestRBACFilterShape:
    """Verify filter construction matches each role's documented permissions."""

    def test_admin_filter_is_none(self):
        """admin has wildcards on both axes → no filter at all."""
        perms = get_permissions("admin")
        filt = build_rbac_filter(perms["allowed_doc_types"], perms["allowed_confidentiality"])
        assert filt is None

    def test_analyst_constrains_doc_type_and_confidentiality(self):
        """analyst can only see 10k + public — both axes filtered."""
        perms = get_permissions("analyst")
        filt = build_rbac_filter(perms["allowed_doc_types"], perms["allowed_confidentiality"])
        assert filt is not None
        keys = {c.key for c in filt.must}
        assert keys == {"doc_type", "confidentiality"}

        doc_type_cond = next(c for c in filt.must if c.key == "doc_type")
        assert list(doc_type_cond.match.any) == ["10k"]

        conf_cond = next(c for c in filt.must if c.key == "confidentiality")
        assert list(conf_cond.match.any) == ["public"]

    def test_hr_filter_blocks_10k_documents(self):
        """hr can ONLY see expense_policy → 10k chunks must not match."""
        perms = get_permissions("hr")
        filt = build_rbac_filter(perms["allowed_doc_types"], perms["allowed_confidentiality"])
        assert filt is not None

        doc_type_cond = next(c for c in filt.must if c.key == "doc_type")
        allowed = list(doc_type_cond.match.any)
        assert "expense_policy" in allowed
        assert "10k" not in allowed
        assert "invoice" not in allowed

    def test_finance_allows_10k_invoice_expense_policy(self):
        perms = get_permissions("finance")
        filt = build_rbac_filter(perms["allowed_doc_types"], perms["allowed_confidentiality"])
        assert filt is not None
        doc_type_cond = next(c for c in filt.must if c.key == "doc_type")
        allowed = set(doc_type_cond.match.any)
        assert allowed == {"10k", "invoice", "expense_policy"}

    def test_c_level_allows_confidential_docs(self):
        """c_level is the only non-admin role that can see confidential."""
        perms = get_permissions("c_level")
        filt = build_rbac_filter(perms["allowed_doc_types"], perms["allowed_confidentiality"])
        assert filt is not None
        conf_cond = next(c for c in filt.must if c.key == "confidentiality")
        assert "confidential" in list(conf_cond.match.any)

    def test_unknown_role_falls_back_to_analyst(self):
        """Sprint 3 convention: unknown role gets the most-restrictive permissions."""
        perms_known = get_permissions("analyst")
        perms_unknown = get_permissions("not_a_real_role")
        assert perms_known == perms_unknown


# ────────────────────────────────────────────────────────────
# Layer 2: Qdrant end-to-end (skip if Qdrant down)
# ────────────────────────────────────────────────────────────

@qdrant_required
class TestRBACQdrantEnforcement:
    """Confirm Qdrant respects the filter against the live FB collection.

    The FinanceBench corpus mixes three doc_types — `10k`, `10q`, `8k` — all
    tagged `confidentiality=public`. Per `rbac_config.py`, only admin and
    c_level have wildcard or broad-enough doc_type permissions to see all
    three; analyst/finance can only see `10k`; hr can see nothing.

    Test asserts the count delta matches the rbac_config contract. If a
    future config change broadens analyst's permissions to include 10q/8k,
    these assertions will need an update — that's the point.
    """

    @pytest.fixture(scope="class")
    def client(self):
        from src.services.vector_store import get_qdrant_client

        return get_qdrant_client()

    @pytest.fixture(scope="class")
    def collection_size(self, client) -> int:
        info = client.get_collection(FB_COLLECTION)
        return info.points_count or 0

    @pytest.fixture(scope="class")
    def n_10k_chunks(self, client) -> int:
        """How many chunks are doc_type=10k. Used as the analyst/finance ceiling."""
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        filt = Filter(must=[FieldCondition(key="doc_type", match=MatchValue(value="10k"))])
        return client.count(collection_name=FB_COLLECTION, count_filter=filt).count

    def _count_for_role(self, client, role: str) -> int:
        perms = get_permissions(role)
        filt = build_rbac_filter(perms["allowed_doc_types"], perms["allowed_confidentiality"])
        return client.count(collection_name=FB_COLLECTION, count_filter=filt).count

    def test_admin_sees_all_chunks(self, client, collection_size):
        """admin has wildcards on both axes — sees every chunk regardless of doc_type."""
        n = self._count_for_role(client, "admin")
        assert n == collection_size, f"admin should see all {collection_size} chunks, got {n}"

    def test_analyst_sees_only_10k_chunks(self, client, n_10k_chunks):
        """analyst is restricted to doc_type=10k — sees 10k chunks but not 10q/8k."""
        n = self._count_for_role(client, "analyst")
        assert n == n_10k_chunks, (
            f"analyst should see exactly the 10k subset ({n_10k_chunks}), got {n}. "
            f"Config drift in rbac_config or filter logic regression."
        )

    def test_finance_sees_only_10k_chunks_in_fb_corpus(self, client, n_10k_chunks):
        """finance allows 10k+invoice+expense_policy. FB has only 10k of those three.

        Note: a future config might broaden finance's doc_types — if so update here.
        """
        n = self._count_for_role(client, "finance")
        assert n == n_10k_chunks

    def test_c_level_matches_analyst_on_fb_corpus(self, client):
        """c_level allows {10k, invoice, expense_policy, board_report} —
        does NOT include 10q or 8k.

        Surfaces a real config gap: c_level can't see quarterly (10q) or
        material-event (8k) filings on FB. Sprint 7.6 Day 2+ may want to
        revisit `rbac_config.c_level.allowed_doc_types` to include 10q/8k.
        """
        n_c_level = self._count_for_role(client, "c_level")
        n_analyst = self._count_for_role(client, "analyst")
        assert n_c_level == n_analyst, (
            f"c_level should match analyst on the FB corpus (both see 10k only "
            f"because neither allows 10q or 8k). Got c_level={n_c_level}, "
            f"analyst={n_analyst}."
        )

    def test_hr_sees_zero_fb_chunks(self, client):
        """The critical RBAC enforcement test.

        hr's permissions are {doc_types: [expense_policy], confidentiality:
        [public, internal]}. FinanceBench has no expense_policy chunks, so hr
        must see EXACTLY zero. If this test ever returns >0, RBAC is broken at
        the vector-DB level.
        """
        n = self._count_for_role(client, "hr")
        assert n == 0, (
            f"hr role leaked {n} chunks. RBAC IS BROKEN at the vector-DB level. "
            f"Check whether build_rbac_filter() is producing the right Filter shape "
            f"and that Qdrant is honoring `must` conditions."
        )
