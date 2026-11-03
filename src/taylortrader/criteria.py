"""Tiny, safe rule language for "only ask the AI when...": `rsi < 35 and close > ma_200`.

Only names of bar/indicator columns, numbers, arithmetic, comparisons, and/or/not are
allowed. It's parsed with Python's ast and walked by hand: nothing is ever eval'd.
Names may also be prefixed `prev_` for the bar before (e.g. `prev_rsi < 30 and rsi >= 30`).
"""

from __future__ import annotations

import ast
import math
import operator
from collections.abc import Mapping

COLUMNS = ("open", "high", "low", "close", "volume", "ma_20", "ma_200", "rsi",
           "macd", "macd_signal", "macd_diff", "chaikin")
NAMES = frozenset(COLUMNS) | frozenset(f"prev_{c}" for c in COLUMNS)

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
_CMP = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
        ast.Eq: operator.eq, ast.NotEq: operator.ne}


class CriteriaError(ValueError):
    pass


def parse(expr: str) -> ast.Expression | None:
    """Parse and check an expression. Empty means "no rule" (returns None)."""
    expr = (expr or "").strip()
    if not expr:
        return None
    if len(expr) > 300:
        raise CriteriaError("rule is too long (300 characters max)")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise CriteriaError(f"can't read the rule {expr!r}: {e.msg}") from None
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if node.id not in NAMES:
                raise CriteriaError(f"unknown name {node.id!r}; use one of: {', '.join(COLUMNS)} (or prev_<name>)")
        elif isinstance(node, ast.Constant):
            if not isinstance(node.value, int | float) or isinstance(node.value, bool):
                raise CriteriaError("only numbers are allowed as values")
        elif not isinstance(node, (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub,
                                   ast.BinOp, ast.Compare, ast.Load, *_BIN, *_CMP)):
            what = {"Call": "function calls", "Attribute": "dots (a.b)", "Subscript": "brackets",
                    "Lambda": "lambdas", "IfExp": "if/else"}.get(type(node).__name__, type(node).__name__)
            raise CriteriaError(f"{what} aren't allowed in a rule; use names, numbers, + - * /, "
                                "comparisons, and/or/not")
    return tree


def evaluate(tree: ast.Expression | None, now: Mapping[str, float], prev: Mapping[str, float]) -> bool:
    """True if the rule holds (or there is no rule). A rule touching a missing value is False."""
    if tree is None:
        return True

    def val(node):
        if isinstance(node, ast.Expression):
            return val(node.body)
        if isinstance(node, ast.Constant):
            return float(node.value)
        if isinstance(node, ast.Name):
            src, key = (prev, node.id[5:]) if node.id.startswith("prev_") else (now, node.id)
            v = src.get(key)
            if v is None or (isinstance(v, float) and math.isnan(v)):
                raise _Missing
            return float(v)
        if isinstance(node, ast.UnaryOp):
            return (not val(node.operand)) if isinstance(node.op, ast.Not) else -val(node.operand)
        if isinstance(node, ast.BinOp):
            right = val(node.right)
            if isinstance(node.op, ast.Div) and right == 0:
                raise _Missing
            return _BIN[type(node.op)](val(node.left), right)
        if isinstance(node, ast.BoolOp):
            vals = (bool(val(v)) for v in node.values)
            return all(vals) if isinstance(node.op, ast.And) else any(vals)
        if isinstance(node, ast.Compare):
            left = val(node.left)
            for op, comp in zip(node.ops, node.comparators, strict=True):
                right = val(comp)
                if not _CMP[type(op)](left, right):
                    return False
                left = right
            return True
        raise CriteriaError(f"{type(node).__name__} isn't allowed")

    try:
        return bool(val(tree))
    except _Missing:
        return False


class _Missing(Exception):
    pass
