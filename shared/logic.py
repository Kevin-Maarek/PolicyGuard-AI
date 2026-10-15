"""Safe, deliberately limited SAS expression adapter and typed Boolean AST."""

import math
import re
from decimal import Decimal
from typing import Literal

import z3
from pydantic import BaseModel, ConfigDict, Field, model_validator

INPUT_TYPES = {
    "amount": "decimal",
    "new_beneficiary": "integer",
    "same_owner_verified": "integer",
    "trusted_device": "integer",
    "account_age_days": "integer",
    "failed_logins_24h": "integer",
}
BASE_INPUT = dict(
    amount=25000,
    new_beneficiary=1,
    same_owner_verified=0,
    trusted_device=1,
    account_age_days=250,
    failed_logins_24h=0,
)
PRIORITY = {"continue": 0, "need_banker_aprove": 1}


class Expression(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operator: Literal["AND", "OR", "NOT", ">", ">=", "<", "<=", "==", "!="]
    field: str | None = None
    value: float | int | None = None
    children: list["Expression"] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_shape(self):
        if self.operator in ("AND", "OR", "NOT"):
            if not self.children or self.field is not None or self.value is not None:
                raise ValueError("Boolean nodes require only children")
            if self.operator == "NOT" and len(self.children) != 1:
                raise ValueError("NOT requires one child")
        elif self.children or self.field not in INPUT_TYPES or self.value is None:
            raise ValueError("Comparison requires a known input field and numeric value")
        elif not math.isfinite(self.value):
            raise ValueError("Comparison values must be finite")
        return self


TOKEN = re.compile(
    r"\s*(>=|<=|==|!=|<>|[><=()]|\bAND\b|\bOR\b|\bNOT\b|[a-zA-Z_][a-zA-Z_0-9]*|-?\d+(?:\.\d+)?)", re.I
)


def parse(expression: str) -> Expression:
    """Recursive descent; no calls, strings, attributes, arithmetic or arbitrary code."""
    tokens, pos = [], 0
    expression = expression.strip()
    if len(expression) > 4000:
        raise ValueError("Expression too long")
    while pos < len(expression):
        match = TOKEN.match(expression, pos)
        if not match:
            raise ValueError(f"Unsupported SAS expression near {expression[pos : pos + 20]}")
        tokens.append(match.group(1))
        pos = match.end()
    cursor = 0

    def take():
        nonlocal cursor
        if cursor >= len(tokens):
            raise ValueError("Incomplete expression")
        token = tokens[cursor]
        cursor += 1
        return token

    def atom(depth=0):
        if depth > 30:
            raise ValueError("Expression nesting too deep")
        token = take()
        if token.upper() == "NOT":
            return Expression(operator="NOT", children=[atom(depth + 1)])
        if token == "(":
            node = boolean(0, depth + 1)
            if take() != ")":
                raise ValueError("Unbalanced expression")
            return node
        op, value = take(), take()
        op = {"=": "==", "<>": "!="}.get(op, op)
        return Expression(field=token, operator=op, value=float(value))

    def boolean(level, depth=0):
        if level == 2:
            return atom(depth)
        op = ("OR", "AND")[level]
        nodes = [boolean(level + 1, depth)]
        while cursor < len(tokens) and tokens[cursor].upper() == op:
            take()
            nodes.append(boolean(level + 1, depth))
        return nodes[0] if len(nodes) == 1 else Expression(operator=op, children=nodes)

    result = boolean(0)
    if cursor != len(tokens):
        raise ValueError("Trailing unsupported expression")
    return result


def render(node: Expression) -> str:
    if node.operator == "NOT":
        return f"NOT ({render(node.children[0])})"
    if node.children:
        return "(" + f" {node.operator} ".join(render(c) for c in node.children) + ")"
    return f"{node.field} {node.operator} {node.value:g}"


def validate_inputs(values: dict) -> dict:
    if set(values) != set(INPUT_TYPES):
        raise ValueError(f"Expected inputs {list(INPUT_TYPES)}; missing or extra inputs")
    result = {}
    for name, kind in INPUT_TYPES.items():
        value = values[name]
        if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} requires a finite nonnegative JSON number")
        if kind == "integer" and (
            int(value) != value
            or name.endswith(("beneficiary", "verified", "device"))
            and value not in (0, 1)
        ):
            raise ValueError(f"{name} requires an integer in its supported domain")
        result[name] = Decimal(str(value)) if kind == "decimal" else int(value)
    return result


def evaluate(node: Expression, values: dict) -> bool:
    if node.operator == "AND":
        return all(evaluate(c, values) for c in node.children)
    if node.operator == "OR":
        return any(evaluate(c, values) for c in node.children)
    if node.operator == "NOT":
        return not evaluate(node.children[0], values)
    a, b = Decimal(str(values[node.field])), Decimal(str(node.value))
    return {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b, "==": a == b, "!=": a != b}[node.operator]


def normalize_rule(rule: dict) -> tuple[Expression, str]:
    if rule["conditional"] != "if" or any(c["type"] != "complex" for c in rule["conditions"]):
        raise ValueError("Only independent if rules with supported complex conditions are implemented")
    nodes = [parse(c["expression"]) for c in rule["conditions"]]
    node = nodes[0] if len(nodes) == 1 else Expression(operator="AND", children=nodes)
    action = next(a for a in rule["actions"] if a.get("term", {}).get("name") == "decision_action")
    value = action["expression"].strip("'")
    if action["type"] != "assignment" or value not in PRIORITY:
        raise ValueError("Unsupported action")
    return node, value


def atoms(node: Expression) -> list[Expression]:
    return [leaf for child in node.children for leaf in atoms(child)] if node.children else [node]


def compare(old: Expression, new: Expression) -> dict:
    """Z3 proves equivalence only within the explicit numeric and Boolean input domain."""
    variables = {k: z3.Real(k) if v == "decimal" else z3.Int(k) for k, v in INPUT_TYPES.items()}

    def convert(node):
        children = [convert(c) for c in node.children]
        if node.operator in ("AND", "OR", "NOT"):
            return {
                "AND": lambda: z3.And(*children),
                "OR": lambda: z3.Or(*children),
                "NOT": lambda: z3.Not(children[0]),
            }[node.operator]()
        a, b = variables[node.field], z3.RealVal(str(node.value))
        return {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b, "==": a == b, "!=": a != b}[node.operator]

    solver = z3.Solver()
    solver.set(timeout=3000)
    solver.add(*[v >= 0 for v in variables.values()])
    solver.add(
        *[
            z3.Or(variables[k] == 0, variables[k] == 1)
            for k in ("new_beneficiary", "same_owner_verified", "trusted_device")
        ]
    )
    solver.add(z3.Xor(convert(old), convert(new)))
    check = solver.check()
    if check == z3.unknown:
        return {"equivalent": None, "status": "UNSUPPORTED", "counterexample": None}
    witness = None
    if check == z3.sat:
        model = solver.model()
        witness = {}
        for name, var in variables.items():
            val = model.eval(var, model_completion=True)
            witness[name] = (
                float(val.numerator_as_long()) / val.denominator_as_long()
                if z3.is_rational_value(val)
                else val.as_long()
            )
    return {
        "equivalent": check == z3.unsat,
        "status": "PROVEN",
        "counterexample": witness,
        "existing_expression": render(old),
        "proposed_expression": render(new),
        "domain": "nonnegative decimal amount, nonnegative integer counts, binary flags",
    }


def generate_cases(old: Expression, new: Expression) -> list[dict]:
    amounts = {25000}
    thresholds = [a.value for a in atoms(old) + atoms(new) if a.field == "amount"]
    for value in thresholds:
        amounts.update([max(0, value - 1), value, value + 1, max(0, value - 0.01), value + 0.01])
    if thresholds:
        amounts.add(sum(thresholds) / len(thresholds))
    cases = [
        {
            "label": "Amount " + f"{amount:.2f}".rstrip("0").rstrip("."),
            "inputs": {**BASE_INPUT, "amount": amount},
        }
        for amount in sorted(amounts)
    ]
    cases += [
        {"label": "Verified same-owner exception", "inputs": {**BASE_INPUT, "same_owner_verified": 1}},
        {"label": "Existing beneficiary", "inputs": {**BASE_INPUT, "new_beneficiary": 0}},
        {"label": "Normal transfer", "inputs": {**BASE_INPUT, "amount": 1000}},
        {"label": "Banker-approval priority", "inputs": {**BASE_INPUT, "amount": 110000}},
        {"label": "New-account priority", "inputs": {**BASE_INPUT, "account_age_days": 3}},
        {"label": "Untrusted device", "inputs": {**BASE_INPUT, "trusted_device": 0}},
        {"label": "Failed logins", "inputs": {**BASE_INPUT, "failed_logins_24h": 5}},
    ]
    witness = compare(old, new).get("counterexample")
    if witness:
        cases.append({"label": "Solver counterexample", "inputs": witness})
    return cases
