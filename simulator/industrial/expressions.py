"""Safe expression language used by sensor models, composites, working conditions, behaviour
models and attribute mappings.

Expressions use a small Python-like syntax that is parsed with :mod:`ast` and converted into a
whitelisted, pickle-safe tuple tree. Nothing is ever passed to ``eval``/``exec``; attribute access
is only allowed as a *dotted variable name* (``sensor.temp``), calls only to whitelisted pure
functions, and literal sizes / nesting depth are bounded.

Supported::

    numbers, true/false (True/False), "strings"
    + - * / // % **   unary - + not
    < <= > >= == !=  (chains allowed)    and  or   (&& and || are accepted too)
    a if cond else b
    min max abs clamp(x, lo, hi) avg sum sqrt exp log log10 round floor ceil pow
    sin cos tanh sigmoid if_(cond, a, b) coalesce(a, b, ...) isnum(x)

Variables are dotted names resolved against a flat environment ``{"sensor.temp": 21.5, ...}``.
A missing variable raises :class:`MissingValue` so callers can apply their missing-value policy.
"""

from __future__ import annotations

import ast
import math
from typing import Any, Callable, Mapping

from ..errors import ValidationError

MAX_SOURCE_LEN = 2000
MAX_NODES = 400
MAX_DEPTH = 40
MAX_POW_EXPONENT = 64


class MissingValue(Exception):
    """A referenced variable has no value (yet)."""

    def __init__(self, name: str) -> None:
        super().__init__(f"missing value for '{name}'")
        self.name = name


class EvaluationError(Exception):
    """Runtime error while evaluating (division by zero, domain errors, type errors)."""


def _flatten(xs) -> list:
    out = []
    for x in xs:
        if isinstance(x, (list, tuple)):
            out.extend(x)
        else:
            out.append(x)
    return out


def _avg(*xs):
    vals = _flatten(xs)
    if not vals:
        raise EvaluationError("avg() of no values")
    return sum(vals) / len(vals)


def _coalesce(*xs):
    for x in xs:
        if x is not None:
            return x
    return None


def _pow(a, b):
    if abs(b) > MAX_POW_EXPONENT:
        raise EvaluationError(f"exponent {b} exceeds the limit of {MAX_POW_EXPONENT}")
    return a ** b


FUNCTIONS: dict[str, Callable[..., Any]] = {
    "min": lambda *xs: min(_flatten(xs)),
    "max": lambda *xs: max(_flatten(xs)),
    "abs": abs,
    "clamp": lambda x, lo, hi: max(lo, min(hi, x)),
    "avg": _avg,
    "sum": lambda *xs: sum(_flatten(xs)),
    "sqrt": math.sqrt,
    "exp": math.exp,
    "log": math.log,
    "log10": math.log10,
    "round": lambda x, nd=0: round(x, int(nd)),
    "floor": math.floor,
    "ceil": math.ceil,
    "pow": _pow,
    "sin": math.sin,
    "cos": math.cos,
    "tanh": math.tanh,
    "sigmoid": lambda x: 1.0 / (1.0 + math.exp(-x)),
    "if_": lambda c, a, b: a if c else b,
    "coalesce": _coalesce,
    "isnum": lambda x: isinstance(x, (int, float)) and not isinstance(x, bool),
}
# Functions that receive missing variables as None instead of failing immediately.
_LENIENT = {"coalesce", "isnum"}

_BINOPS = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.FloorDiv: "//", ast.Mod: "%",
           ast.Pow: "**"}
_CMPOPS = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Eq: "==", ast.NotEq: "!="}
_NAME_CONSTANTS = {"true": True, "false": False, "True": True, "False": False, "null": None, "None": None,
                   "pi": math.pi}


def _dotted(node: ast.AST) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


class _Compiler:
    def __init__(self) -> None:
        self.nodes = 0
        self.variables: set[str] = set()

    def compile(self, node: ast.AST, depth: int = 0):
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise ValidationError(f"expression too complex (more than {MAX_NODES} nodes)")
        if depth > MAX_DEPTH:
            raise ValidationError("expression nested too deeply")
        d = depth + 1
        if isinstance(node, ast.Constant):
            v = node.value
            if v is None or isinstance(v, (bool, int, float)):
                return ("const", v)
            if isinstance(v, str):
                if len(v) > 200:
                    raise ValidationError("string literal too long")
                return ("const", v)
            raise ValidationError(f"unsupported literal {v!r}")
        if isinstance(node, ast.Name):
            if node.id in _NAME_CONSTANTS:
                return ("const", _NAME_CONSTANTS[node.id])
            if node.id.startswith("__"):
                raise ValidationError(f"name '{node.id}' is not allowed")
            self.variables.add(node.id)
            return ("var", node.id)
        if isinstance(node, ast.Attribute):
            name = _dotted(node)
            if name is None:
                raise ValidationError("attribute access is only allowed on dotted variable names")
            if any(part.startswith("__") for part in name.split(".")):
                raise ValidationError(f"name '{name}' is not allowed")
            self.variables.add(name)
            return ("var", name)
        if isinstance(node, ast.BinOp):
            op = _BINOPS.get(type(node.op))
            if op is None:
                raise ValidationError(f"operator {type(node.op).__name__} is not allowed")
            return ("bin", op, self.compile(node.left, d), self.compile(node.right, d))
        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.USub):
                return ("neg", self.compile(node.operand, d))
            if isinstance(node.op, ast.UAdd):
                return self.compile(node.operand, d)
            if isinstance(node.op, ast.Not):
                return ("not", self.compile(node.operand, d))
            raise ValidationError(f"operator {type(node.op).__name__} is not allowed")
        if isinstance(node, ast.BoolOp):
            kind = "and" if isinstance(node.op, ast.And) else "or"
            return (kind, tuple(self.compile(v, d) for v in node.values))
        if isinstance(node, ast.Compare):
            ops = []
            for o in node.ops:
                sym = _CMPOPS.get(type(o))
                if sym is None:
                    raise ValidationError(f"comparison {type(o).__name__} is not allowed")
                ops.append(sym)
            return ("cmp", self.compile(node.left, d), tuple(ops),
                    tuple(self.compile(c, d) for c in node.comparators))
        if isinstance(node, ast.IfExp):
            return ("if", self.compile(node.test, d), self.compile(node.body, d), self.compile(node.orelse, d))
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
                fname = _dotted(node.func) or "?"
                raise ValidationError(f"function '{fname}' is not allowed (allowed: {', '.join(sorted(FUNCTIONS))})")
            if node.keywords:
                raise ValidationError("keyword arguments are not supported")
            if len(node.args) > 32:
                raise ValidationError("too many function arguments")
            return ("call", node.func.id, tuple(self.compile(a, d) for a in node.args))
        if isinstance(node, (ast.List, ast.Tuple)):
            if len(node.elts) > 64:
                raise ValidationError("list literal too long")
            return ("list", tuple(self.compile(e, d) for e in node.elts))
        raise ValidationError(f"syntax element {type(node).__name__} is not allowed in expressions")


class Expression:
    """A compiled, pickle-safe expression."""

    __slots__ = ("source", "tree", "variables")

    def __init__(self, source: str) -> None:
        if not isinstance(source, str) or not source.strip():
            raise ValidationError("expression must be a non-empty string")
        if len(source) > MAX_SOURCE_LEN:
            raise ValidationError(f"expression longer than {MAX_SOURCE_LEN} characters")
        text = source.strip()
        normalized = text.replace("&&", " and ").replace("||", " or ")
        try:
            parsed = ast.parse(normalized, mode="eval")
        except SyntaxError as exc:
            raise ValidationError(f"invalid expression {text!r}: {exc.msg}") from None
        comp = _Compiler()
        self.tree = comp.compile(parsed.body)
        self.source = text
        self.variables = frozenset(comp.variables)

    def __getstate__(self):
        return (self.source, self.tree, tuple(sorted(self.variables)))

    def __setstate__(self, state):
        self.source, self.tree, variables = state
        self.variables = frozenset(variables)

    def evaluate(self, env: Mapping[str, Any]) -> Any:
        try:
            return _eval(self.tree, env)
        except (MissingValue, EvaluationError):
            raise
        except (ZeroDivisionError, OverflowError, ValueError, TypeError) as exc:
            raise EvaluationError(f"{self.source}: {exc}") from None

    def __repr__(self) -> str:
        return f"Expression({self.source!r})"


def _compare(op: str, a, b) -> bool:
    if op == "<":
        return a < b
    if op == "<=":
        return a <= b
    if op == ">":
        return a > b
    if op == ">=":
        return a >= b
    if op == "==":
        return a == b
    return a != b


def _eval(node, env):
    tag = node[0]
    if tag == "const":
        return node[1]
    if tag == "var":
        v = env.get(node[1]) if hasattr(env, "get") else env[node[1]]
        if v is None:
            raise MissingValue(node[1])
        return v
    if tag == "bin":
        a, b = _eval(node[2], env), _eval(node[3], env)
        op = node[1]
        if op == "+":
            if isinstance(a, str) != isinstance(b, str):
                raise EvaluationError("cannot add text and numbers")
            return a + b
        if isinstance(a, str) or isinstance(b, str):
            raise EvaluationError(f"operator {op} is not defined for text")
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op == "/":
            return a / b
        if op == "//":
            return a // b
        if op == "%":
            return a % b
        return _pow(a, b)
    if tag == "neg":
        return -_eval(node[1], env)
    if tag == "not":
        return not _eval(node[1], env)
    if tag == "and":
        result = True
        for v in node[1]:
            result = _eval(v, env)
            if not result:
                return result
        return result
    if tag == "or":
        result = False
        for v in node[1]:
            result = _eval(v, env)
            if result:
                return result
        return result
    if tag == "cmp":
        left = _eval(node[1], env)
        for op, comp in zip(node[2], node[3]):
            right = _eval(comp, env)
            if not _compare(op, left, right):
                return False
            left = right
        return True
    if tag == "if":
        return _eval(node[2], env) if _eval(node[1], env) else _eval(node[3], env)
    if tag == "call":
        name = node[1]
        if name in _LENIENT:
            args = []
            for a in node[2]:
                try:
                    args.append(_eval(a, env))
                except MissingValue:
                    args.append(None)
        else:
            args = [_eval(a, env) for a in node[2]]
        return FUNCTIONS[name](*args)
    if tag == "list":
        return [_eval(e, env) for e in node[1]]
    raise EvaluationError(f"unknown node {tag}")  # pragma: no cover


_CACHE: dict[str, Expression] = {}


def compile_expression(source: str) -> Expression:
    """Compile with a small process-wide cache (compiled trees are immutable)."""
    expr = _CACHE.get(source) if isinstance(source, str) else None
    if expr is None:
        expr = Expression(source)
        if len(_CACHE) > 4096:
            _CACHE.clear()
        _CACHE[source] = expr
    return expr


def validate_expression(source: Any, field: str, allowed: Callable[[str], bool] | None = None) -> str:
    """Compile ``source`` and check every referenced variable with ``allowed``. Returns the source."""
    if not isinstance(source, str):
        raise ValidationError(f"{field} must be an expression string")
    try:
        expr = compile_expression(source)
    except ValidationError as exc:
        raise ValidationError(f"{field}: {exc}") from None
    if allowed is not None:
        bad = sorted(v for v in expr.variables if not allowed(v))
        if bad:
            raise ValidationError(f"{field}: unknown variable(s) {', '.join(bad)}")
    return expr.source


def evaluate(source: str, env: Mapping[str, Any]) -> Any:
    return compile_expression(source).evaluate(env)


def as_number(value: Any, what: str) -> float:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if not isinstance(value, (int, float)) or value != value or value in (math.inf, -math.inf):
        raise EvaluationError(f"{what} did not evaluate to a finite number (got {value!r})")
    return float(value)
