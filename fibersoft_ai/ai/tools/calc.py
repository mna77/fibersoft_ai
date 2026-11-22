"""Exact arithmetic so the model never does mental math on business figures."""

import ast
import operator
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, getcontext

from fibersoft_ai.ai.guard import ToolError
from fibersoft_ai.ai.tools import tool

getcontext().prec = 34

_BIN = {
	ast.Add: operator.add,
	ast.Sub: operator.sub,
	ast.Mult: operator.mul,
	ast.Div: operator.truediv,
	ast.FloorDiv: operator.floordiv,
	ast.Mod: operator.mod,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _round(x, places=Decimal(2)):
	q = Decimal(1).scaleb(-int(places))
	return x.quantize(q, rounding=ROUND_HALF_UP)


_FUNCS = {
	"round": lambda x, n=Decimal(2): _round(x, n),
	"abs": abs,
	"min": lambda *a: min(a),
	"max": lambda *a: max(a),
	"sum": lambda *a: sum(a, Decimal(0)),
	"avg": lambda *a: sum(a, Decimal(0)) / Decimal(len(a)),
	"pct": lambda part, whole: part / whole * 100,
	"pct_change": lambda old, new: (new - old) / old * 100,
}


def evaluate(expression):
	if not isinstance(expression, str) or not expression.strip():
		raise ToolError("expression is required")
	if len(expression) > 2000:
		raise ToolError("expression too long")
	expr = expression.replace(",", "") if _looks_like_thousands(expression) else expression
	try:
		tree = ast.parse(expr, mode="eval")
	except SyntaxError:
		raise ToolError(f"Invalid expression: {expression}")
	return _eval(tree.body)


def _looks_like_thousands(s):
	# "1,250,000 * 2" -> thousands separators; function args like max(1, 2) keep commas
	return "(" not in s


def _eval(node):
	if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
		return Decimal(str(node.value))
	if isinstance(node, ast.BinOp):
		if isinstance(node.op, ast.Pow):
			base, exp = _eval(node.left), _eval(node.right)
			if exp != exp.to_integral_value() or abs(exp) > 100:
				raise ToolError("Only whole-number powers up to 100 are supported")
			return base ** int(exp)
		op = _BIN.get(type(node.op))
		if not op:
			raise ToolError("Unsupported operator")
		try:
			return op(_eval(node.left), _eval(node.right))
		except (ZeroDivisionError, InvalidOperation):
			raise ToolError("Division by zero")
	if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
		return _UNARY[type(node.op)](_eval(node.operand))
	if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
		args = [_eval(a) for a in node.args]
		try:
			return _FUNCS[node.func.id](*args)
		except (ZeroDivisionError, InvalidOperation):
			raise ToolError("Division by zero")
		except TypeError:
			raise ToolError(f"Wrong number of arguments for {node.func.id}")
	raise ToolError("Only numbers, + - * / // % **, parentheses and round/abs/min/max/sum/avg/pct/pct_change are allowed")


def _fmt(d):
	d = d.normalize() if d == d.to_integral_value() else d
	if d == d.to_integral_value():
		return str(d.quantize(Decimal(1)))
	return format(_round(d, 6).normalize(), "f")


@tool(
	"calculate",
	"""Exact decimal calculator. Use it for every non-trivial arithmetic on business figures (differences,
percentages, ratios, growth, per-unit values). Supports + - * / // % ** ( ) and functions
round(x, places), abs, min, max, sum, avg, pct(part, whole), pct_change(old, new).
You can evaluate several labelled expressions at once.""",
	{
		"expressions": {
			"type": "object",
			"description": 'Map of label -> expression, e.g. {"margin_pct": "pct(450000, 1800000)", "diff": "1800000 - 1350000"}',
		}
	},
	["expressions"],
)
def calculate(ctx, expressions):
	if isinstance(expressions, str):
		expressions = {"result": expressions}
	if not isinstance(expressions, dict) or not expressions:
		raise ToolError("expressions must be an object of label -> expression")
	if len(expressions) > 50:
		raise ToolError("At most 50 expressions at once")
	out = {}
	for label, expr in expressions.items():
		try:
			out[label] = _fmt(evaluate(str(expr)))
		except ToolError as e:
			out[label] = f"ERROR: {e}"
	return {"results": out}
