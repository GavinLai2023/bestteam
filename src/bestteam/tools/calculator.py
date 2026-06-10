from __future__ import annotations

import ast
import operator
from typing import Any

from ..exceptions import ConfigurationError

_SAFE_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

_SAFE_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Constant,
    ast.Add, ast.Sub, ast.Mult, ast.Div,
    ast.FloorDiv, ast.Mod, ast.Pow,
    ast.UAdd, ast.USub,
)


def _eval_node(node: Any) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in _SAFE_OPERATORS:
            raise ConfigurationError(f"Unsupported operator: {op_type.__name__}")
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        if op_type is ast.Pow and abs(right) > 1000:
            raise ConfigurationError("Exponent too large (max 1000)")
        return _SAFE_OPERATORS[op_type](left, right)
    if isinstance(node, ast.UnaryOp):
        op_type = type(node.op)
        if op_type not in _SAFE_OPERATORS:
            raise ConfigurationError(f"Unsupported unary operator: {op_type.__name__}")
        return _SAFE_OPERATORS[op_type](_eval_node(node.operand))
    raise ConfigurationError(f"Unsupported expression element: {type(node).__name__}")


def calculator(expression: str) -> str:
    """Evaluate a safe arithmetic expression and return the result.

    Supports +, -, *, /, //, %, ** and parentheses. No variables or
    function calls are allowed — this tool exists solely to prevent LLM
    arithmetic hallucinations.

    Args:
        expression: A numeric arithmetic expression, e.g. "(3 + 4) * 2 ** 8".

    Returns:
        The computed result as a string.
    """
    expression = expression.strip()
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ConfigurationError(f"Invalid expression syntax: {exc}") from exc

    for node in ast.walk(tree):
        if not isinstance(node, _SAFE_NODES):
            raise ConfigurationError(
                f"Expression contains disallowed element '{type(node).__name__}'. "
                "Only numeric arithmetic is supported."
            )

    try:
        result = _eval_node(tree.body)
        if isinstance(result, float) and result == int(result):
            return str(int(result))
        return str(result)
    except (ValueError, OverflowError) as exc:
        raise ConfigurationError(f"Result is too large to compute: {exc}") from exc
