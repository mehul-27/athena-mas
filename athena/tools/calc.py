"""calculate: a safe arithmetic evaluator over a Python AST.

There is no ``eval`` or ``exec`` anywhere in this module. An expression either
has a shape this evaluator understands — numbers, _BIN_OPS, parentheses, and a
short whitelist of math functions — or it is rejected with ``ok=False``.
"""

from __future__ import annotations

import ast
import math
import operator
import time

from athena.tools.schemas import CalcResult

MAX_EXPRESSION_CHARS = 200
MAX_RESULT = 1e12
MAX_EXPONENT = 64

_FUNCTIONS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "log": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "exp": math.exp,
    "pow": math.pow,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "floor": math.floor,
    "ceil": math.ceil,
    "radians": math.radians,
    "degrees": math.degrees,
}
_CONSTANTS = {"pi": math.pi, "e": math.e, "tau": math.tau}
_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


class _Rejected(ValueError):
    """An expression that is well-formed Python but not allowed arithmetic."""


def _eval(node: ast.AST, depth: int = 0):
    if depth > 20:
        raise _Rejected("expression is nested too deeply")

    if isinstance(node, ast.Expression):
        return _eval(node.body, depth + 1)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise _Rejected(f"only numbers are allowed, got {type(node.value).__name__}")
        return node.value

    if isinstance(node, ast.Name):
        if node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        raise _Rejected(f"name {node.id!r} is not allowed")

    if isinstance(node, ast.UnaryOp):
        operation = _UNARY_OPS.get(type(node.op))
        if operation is None:
            raise _Rejected(f"operator {type(node.op).__name__} is not allowed")
        return operation(_eval(node.operand, depth + 1))

    if isinstance(node, ast.BinOp):
        operation = _BIN_OPS.get(type(node.op))
        if operation is None:
            raise _Rejected(f"operator {type(node.op).__name__} is not allowed")
        left = _eval(node.left, depth + 1)
        right = _eval(node.right, depth + 1)
        if isinstance(node.op, ast.Pow) and abs(right) > MAX_EXPONENT:
            raise _Rejected(f"exponent must be within +/-{MAX_EXPONENT}")
        return operation(left, right)

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise _Rejected("only calls to whitelisted functions are allowed")
        function = _FUNCTIONS.get(node.func.id)
        if function is None:
            raise _Rejected(f"function {node.func.id!r} is not allowed")
        if node.keywords:
            raise _Rejected("keyword arguments are not allowed")
        return function(*[_eval(arg, depth + 1) for arg in node.args])

    raise _Rejected(f"{type(node).__name__} is not allowed in an expression")


def calculate(expression: str) -> CalcResult:
    """Evaluate arithmetic. Never raises: failures come back as ``ok=False``."""
    started = time.perf_counter()

    def result(**kwargs) -> CalcResult:
        return CalcResult(
            expression=expression if isinstance(expression, str) else repr(expression),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
            **kwargs,
        )

    if not isinstance(expression, str) or not expression.strip():
        return result(ok=False, error="expression must be a non-empty string")
    if len(expression) > MAX_EXPRESSION_CHARS:
        return result(
            ok=False,
            error=f"expression is longer than {MAX_EXPRESSION_CHARS} characters",
        )

    try:
        value = _eval(ast.parse(expression, mode="eval"))
    except _Rejected as exc:
        return result(ok=False, error=str(exc))
    except SyntaxError as exc:
        return result(ok=False, error=f"invalid syntax: {exc.msg}")
    except ZeroDivisionError:
        return result(ok=False, error="division by zero")
    except (ArithmeticError, ValueError, TypeError) as exc:
        return result(ok=False, error=f"{type(exc).__name__}: {exc}")

    if isinstance(value, complex):
        return result(ok=False, error="result is not a real number")
    if not math.isfinite(value):
        return result(ok=False, error="result is not a finite number")
    if abs(value) > MAX_RESULT:
        return result(ok=False, error=f"result magnitude exceeds {MAX_RESULT:g}")
    return result(value=float(value))
