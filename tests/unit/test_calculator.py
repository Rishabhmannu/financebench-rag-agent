"""Unit tests for src/tools/calculator.py.

Three axes of coverage:
  1. Arithmetic correctness on legitimate finance expressions
  2. Whitelist boundary — explicit injection / sandbox-escape attempts
  3. Error handling — empty input, syntax errors, division by zero, non-finite results
"""
from __future__ import annotations

import math

import pytest

from src.tools.calculator import (
    CalculatorDisallowedError,
    CalculatorError,
    CalculatorParseError,
    CalculatorRuntimeError,
    MAX_EXPRESSION_LEN,
    compute,
)


# ─── Arithmetic correctness ──────────────────────────────────────────────


class TestBasicOperators:
    def test_addition(self):
        assert compute("2 + 3") == 5.0

    def test_subtraction(self):
        assert compute("10 - 4") == 6.0

    def test_multiplication(self):
        assert compute("3 * 4") == 12.0

    def test_division_returns_float(self):
        assert compute("12 / 4") == 3.0
        assert isinstance(compute("12 / 4"), float)

    def test_floor_division(self):
        assert compute("17 // 5") == 3.0

    def test_unary_minus(self):
        assert compute("-5 + 3") == -2.0

    def test_unary_plus(self):
        assert compute("+5") == 5.0

    def test_parentheses(self):
        assert compute("(2 + 3) * 4") == 20.0

    def test_operator_precedence(self):
        # multiplication binds tighter than addition
        assert compute("2 + 3 * 4") == 14.0
        assert compute("(2 + 3) * 4") == 20.0

    def test_negative_in_paren(self):
        assert compute("-(5 + 3)") == -8.0

    def test_double_negative(self):
        assert compute("--5") == 5.0


class TestNumericLiterals:
    def test_int_literal(self):
        assert compute("42") == 42.0

    def test_float_literal(self):
        assert compute("3.14") == 3.14

    def test_scientific_notation(self):
        assert compute("1.5e3") == 1500.0
        assert compute("1e-2") == 0.01

    def test_thousand_separator(self):
        assert compute("1,234.56") == 1234.56

    def test_thousand_separator_in_op(self):
        assert compute("1,000 + 2,500") == 3500.0

    def test_thousand_separator_million(self):
        assert compute("1,234,567") == 1234567.0

    def test_decimal_no_leading_zero(self):
        assert compute(".5 + .5") == 1.0


class TestFinanceExpressions:
    """Real numerical patterns the synthesizer is expected to emit. Numbers
    are illustrative — the point is that the expression parses & evaluates."""

    def test_op_cash_flow_ratio(self):
        # cash_from_operations / current_liabilities
        # Adobe FY2017: gold ≈ 0.83
        result = compute("(7438) / (8970)")
        assert abs(result - 0.829) < 0.001

    def test_fixed_asset_turnover(self):
        # revenue / net_fixed_assets
        result = compute("8086 / 245")
        assert abs(result - 33.0) < 0.5

    def test_growth_pct(self):
        # (current_year - prior_year) / prior_year
        result = compute("(86392 - 79474) / 79474")
        assert abs(result - 0.0871) < 0.001

    def test_dividend_payout_ratio(self):
        # cash_dividends / net_income
        result = compute("7616 / 9542")
        assert abs(result - 0.798) < 0.001

    def test_cogs_pct_margin(self):
        # cost_of_goods_sold / revenue
        result = compute("38528 / 86392")
        assert abs(result - 0.446) < 0.001

    def test_ratio_with_thousand_separators(self):
        # numbers as they often appear in 10-K text
        result = compute("(7,438) / (8,970)")
        assert abs(result - 0.829) < 0.001


# ─── Whitelist boundary — security ───────────────────────────────────────


class TestSandboxBoundary:
    """Each of these is a real attack pattern that an LLM might emit (by
    accident or via prompt injection). The calculator must reject all of
    them at AST validation, before any code executes."""

    def test_reject_function_call(self):
        with pytest.raises(CalculatorDisallowedError):
            compute("__import__('os').system('id')")

    def test_reject_attribute_access(self):
        with pytest.raises(CalculatorDisallowedError):
            compute("().__class__.__bases__")

    def test_reject_name_lookup(self):
        with pytest.raises(CalculatorDisallowedError):
            compute("os")

    def test_reject_subscript(self):
        with pytest.raises(CalculatorDisallowedError):
            compute("[1, 2, 3][0]")

    def test_reject_lambda(self):
        with pytest.raises(CalculatorDisallowedError):
            compute("(lambda x: x)(5)")

    def test_reject_list_comprehension(self):
        with pytest.raises(CalculatorDisallowedError):
            compute("[x for x in range(10)]")

    def test_reject_string_literal(self):
        with pytest.raises(CalculatorDisallowedError):
            compute("'1+1'")

    def test_reject_bool_literal(self):
        # bool is technically a subtype of int — reject explicitly
        with pytest.raises(CalculatorDisallowedError):
            compute("True + 1")

    def test_reject_none(self):
        with pytest.raises(CalculatorDisallowedError):
            compute("None")

    def test_reject_complex(self):
        with pytest.raises(CalculatorDisallowedError):
            compute("1j + 2")

    def test_reject_pow_memory_bomb(self):
        # ** is intentionally disallowed even for legit small exponents —
        # the synthesizer can express compound expressions via repeated mul.
        with pytest.raises(CalculatorDisallowedError):
            compute("2 ** 10")

    def test_reject_modulo(self):
        with pytest.raises(CalculatorDisallowedError):
            compute("10 % 3")

    def test_reject_bitwise_and(self):
        with pytest.raises(CalculatorDisallowedError):
            compute("5 & 3")

    def test_reject_bitwise_or(self):
        with pytest.raises(CalculatorDisallowedError):
            compute("5 | 3")

    def test_reject_bitwise_xor(self):
        with pytest.raises(CalculatorDisallowedError):
            compute("5 ^ 3")

    def test_reject_left_shift(self):
        with pytest.raises(CalculatorDisallowedError):
            compute("1 << 4")

    def test_reject_walrus(self):
        # Walrus assignment inside a parenthesized expression
        with pytest.raises(CalculatorError):
            compute("(x := 5)")

    def test_reject_ternary(self):
        with pytest.raises(CalculatorDisallowedError):
            compute("1 if True else 2")

    def test_reject_compare(self):
        with pytest.raises(CalculatorDisallowedError):
            compute("1 < 2")


# ─── Error handling ──────────────────────────────────────────────────────


class TestErrorHandling:
    def test_division_by_zero(self):
        with pytest.raises(CalculatorRuntimeError, match="division by zero"):
            compute("1 / 0")

    def test_floor_division_by_zero(self):
        with pytest.raises(CalculatorRuntimeError, match="division by zero"):
            compute("1 // 0")

    def test_empty_string(self):
        with pytest.raises(CalculatorParseError, match="empty"):
            compute("")

    def test_whitespace_only(self):
        with pytest.raises(CalculatorParseError, match="empty"):
            compute("   \t\n  ")

    def test_too_long(self):
        # Build an expression just over the limit
        expr = "1" + "+1" * (MAX_EXPRESSION_LEN // 2)
        with pytest.raises(CalculatorParseError, match="too long"):
            compute(expr)

    def test_syntax_error_dangling_op(self):
        with pytest.raises(CalculatorParseError, match="syntax"):
            compute("1 +")

    def test_syntax_error_unbalanced_paren(self):
        with pytest.raises(CalculatorParseError, match="syntax"):
            compute("(1 + 2")

    def test_empty_paren_rejected(self):
        # `()` parses as an empty Tuple in Python — it's not a syntax error,
        # but Tuple is not in the whitelist, so we reject it as disallowed.
        with pytest.raises(CalculatorDisallowedError, match="Tuple"):
            compute("()")

    def test_overflow_to_inf_is_rejected(self):
        # 1e300 * 1e300 overflows to +inf; calculator must reject
        with pytest.raises(CalculatorRuntimeError, match="non-finite"):
            compute("1e300 * 1e300")

    def test_non_string_input_int(self):
        with pytest.raises(CalculatorParseError, match="expected str"):
            compute(42)  # type: ignore[arg-type]

    def test_non_string_input_none(self):
        with pytest.raises(CalculatorParseError, match="expected str"):
            compute(None)  # type: ignore[arg-type]


# ─── Misc ────────────────────────────────────────────────────────────────


class TestReturnContract:
    def test_always_returns_float(self):
        # Even pure-int operations must return float
        assert isinstance(compute("2 + 2"), float)
        assert isinstance(compute("100 - 50"), float)
        assert isinstance(compute("17 // 5"), float)

    def test_negative_zero_handled(self):
        # -0.0 == 0.0 in Python; just confirm we don't crash
        result = compute("0 - 0")
        assert result == 0.0
        assert not math.isnan(result)
