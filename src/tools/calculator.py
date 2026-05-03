"""Restricted-arithmetic evaluator for the research-agent synthesizer (Sprint 7.8 Week 2).

Rationale: Day 6 review of FinanceBench failures showed multiple calc questions
where the agent retrieved the right numerical inputs but performed the
arithmetic wrong on the way to the bottom line (e.g. Adobe FY2017 op-cash-flow
ratio: agent computed 1.07, gold answer was 0.83 — wrong denominator). Giving
the synthesizer an external arithmetic tool removes this failure class.

Design constraints:
  - **No `eval()` / `exec()`** — full Python is unsafe and unnecessary
  - **AST-based whitelist**: parse via `ast.parse(mode="eval")` then walk the
    tree, rejecting any node not in `_ALLOWED_NODES`
  - **Operations**: only `+ - * / //` and unary `+ -`. Pow `**` is intentionally
    disallowed — easy to weaponize as a memory bomb (`9**9**9`) and not needed
    for any FB calc question.
  - **Operands**: int and float literals only (no bool/str/complex/None)
  - **Pre-processing**: strip thousand-separator commas in numbers (`1,234.56`
    → `1234.56`). Finance source text often has these.
  - **Result hygiene**: reject `inf`/`nan` at output. Cap raw input length to
    `MAX_EXPRESSION_LEN` characters to bound parse cost.

API:
    compute(expression: str) -> float       # raises CalculatorError on failure
"""
from __future__ import annotations

import ast
import math
import re

MAX_EXPRESSION_LEN = 256

# Whitelisted AST node types. `ast.walk` yields every node in the tree —
# including operator instances like `ast.Add` that hang off `BinOp.op`. The
# whitelist covers structural nodes AND the specific operators we permit.
# Anything not in this set raises CalculatorDisallowedError at validation time.
#
# Excluded on purpose:
#   - ast.Pow (`**`) and ast.Mod (`%`) — Pow is a memory-bomb risk (`9**9**9`),
#     Mod is not needed for FB calc questions
#   - All bitwise/shift operators, comparison operators, boolean operators
#   - Name, Call, Attribute, Subscript, Lambda, comprehensions, ternary,
#     NamedExpr (walrus), etc.
_ALLOWED_BINOPS: tuple[type, ...] = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv)
_ALLOWED_UNARYOPS: tuple[type, ...] = (ast.UAdd, ast.USub)
_ALLOWED_NODES: tuple[type, ...] = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Constant,
    *_ALLOWED_BINOPS,
    *_ALLOWED_UNARYOPS,
)

# Strip thousand-separator commas: only between digits (avoids touching commas
# in non-numeric contexts, though we'd reject those at AST validation anyway).
_THOUSAND_SEP_RE = re.compile(r"(?<=\d),(?=\d{3}(?:\D|$))")


class CalculatorError(Exception):
    """Base for all calculator errors. Synthesizer catches this and falls back to
    LLM arithmetic with a 'verify' prefix."""


class CalculatorParseError(CalculatorError):
    """Input is not a valid arithmetic expression (empty, syntax error, too long)."""


class CalculatorDisallowedError(CalculatorError):
    """AST contained a node type or operator that is not in the whitelist
    (e.g. function call, attribute access, exponentiation, name lookup)."""


class CalculatorRuntimeError(CalculatorError):
    """Evaluation produced a non-finite or non-numeric result, or hit a runtime
    error like division by zero."""


def compute(expression: str) -> float:
    """Evaluate a restricted arithmetic expression and return a float.

    Raises:
        CalculatorParseError: empty / too-long / unparseable input.
        CalculatorDisallowedError: AST contains a non-whitelisted node.
        CalculatorRuntimeError: division by zero, inf/nan result, etc.
    """
    if not isinstance(expression, str):
        raise CalculatorParseError(
            f"expected str, got {type(expression).__name__}"
        )
    if not expression.strip():
        raise CalculatorParseError("empty expression")
    if len(expression) > MAX_EXPRESSION_LEN:
        raise CalculatorParseError(
            f"expression too long ({len(expression)} chars > {MAX_EXPRESSION_LEN})"
        )

    cleaned = _THOUSAND_SEP_RE.sub("", expression)

    try:
        tree = ast.parse(cleaned, mode="eval")
    except SyntaxError as e:
        raise CalculatorParseError(f"syntax error: {e.msg}") from e

    _validate(tree)

    try:
        result = _eval(tree.body)
    except ZeroDivisionError as e:
        raise CalculatorRuntimeError("division by zero") from e
    except (OverflowError, ValueError) as e:
        raise CalculatorRuntimeError(f"{type(e).__name__}: {e}") from e

    if isinstance(result, bool) or not isinstance(result, (int, float)):
        raise CalculatorRuntimeError(
            f"non-numeric result: {type(result).__name__}"
        )
    if isinstance(result, float) and (math.isinf(result) or math.isnan(result)):
        raise CalculatorRuntimeError(f"non-finite result: {result}")

    return float(result)


def _validate(tree: ast.Expression) -> None:
    """Walk every node in the parsed tree and reject anything outside the whitelist.

    Because `_ALLOWED_NODES` includes both the structural nodes (Expression,
    BinOp, UnaryOp, Constant) AND the specific operator instances we permit
    (Add/Sub/Mult/Div/FloorDiv, UAdd/USub), a single isinstance check covers
    the full safety boundary — disallowed operators like `Pow` or `Mod` appear
    as nodes in `ast.walk` and fail the check just like `Name` or `Call` would.

    The Constant subcheck is the only extra rule: even when a Constant node is
    structurally allowed, its `value` field must be int or float (rejecting
    bool, str, None, complex, bytes).

    This is the safety boundary; do not loosen without a threat model.
    """
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise CalculatorDisallowedError(
                f"node type {type(node).__name__} not allowed"
            )
        if isinstance(node, ast.Constant):
            # bool is a subtype of int — reject it explicitly so True/False
            # don't slip through as numeric literals.
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise CalculatorDisallowedError(
                    f"constant must be int or float, got "
                    f"{type(node.value).__name__}"
                )


def _eval(node: ast.AST) -> int | float:
    """Evaluate a validated AST node. Assumes _validate has already run.

    Pure recursive descent over the three permitted node types. Python's native
    operator semantics handle int/float promotion, so 4 / 2 is 2.0 (float div)
    while 4 // 2 is 2 (floor div on ints). Both are returned as numeric.
    """
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.BinOp):
        left = _eval(node.left)
        right = _eval(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        if isinstance(node.op, ast.FloorDiv):
            return left // right
    if isinstance(node, ast.UnaryOp):
        operand = _eval(node.operand)
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.UAdd):
            return +operand
    raise CalculatorRuntimeError(f"unexpected node: {type(node).__name__}")
