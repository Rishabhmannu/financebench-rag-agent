"""Unit tests for the HITL gate's dollar-amount extraction."""
from src.graph.nodes.hitl_gate import _extract_max_amount


def test_extracts_plain_amount():
    assert _extract_max_amount("Approve a $500,000 reimbursement") == 500_000.0


def test_extracts_suffix_amounts():
    assert _extract_max_amount("revenue of $383.3 billion") == 383_300_000_000.0
    assert _extract_max_amount("a $5 million payment") == 5_000_000.0


def test_picks_largest_of_several():
    assert _extract_max_amount("$1,000 fee on a $2,500,000 deal") == 2_500_000.0


def test_no_amount_returns_zero():
    assert _extract_max_amount("what is the revenue trend") == 0.0


def test_ignores_oversized_artifact():
    # A malformed generated draft can emit a nonsensical figure (stray
    # zero-groups); it must not be reported. The legitimate $197,653 wins.
    text = "Total $500,000,000,000,000,000 ... actual due $197,653.12"
    assert _extract_max_amount(text) == 197_653.12
