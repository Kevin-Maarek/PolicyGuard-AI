"""Limited simulator semantics: accumulate hits and execute an explicit priority flow step."""

from apps.sas_mock.seed import PRIORITY_ID
from shared.logic import PRIORITY, evaluate, normalize_rule, validate_inputs


def execute(decision, rule_sets, inputs, replacement=None):
    values = validate_inputs(inputs)
    fired, trace = [], []
    resolved = False
    for step in decision["flow"]["steps"]:
        if "ruleSet" in step:
            rs = next(r for r in rule_sets if r["id"] == step["ruleSet"]["id"])
            for rule in rs["rules"]:
                node, action = normalize_rule(rule)
                if replacement and replacement["rule_id"] == rule["id"]:
                    from shared.logic import Expression

                    node = Expression.model_validate(replacement["expression"])
                    action = replacement.get("action", action)
                    if action not in PRIORITY:
                        raise ValueError("Unsupported sandbox action")
                hit = evaluate(node, values)
                trace.append({"rule_id": rule["id"], "rule_name": rule["name"], "fired": hit})
                if hit:
                    fired.append((action, rule["name"].split()[0]))
        elif step.get("customObject", {}).get("uri") == f"/decisions/codeFiles/{PRIORITY_ID}":
            action, reason = max(
                fired, key=lambda pair: PRIORITY[pair[0]], default=("continue", "NO_CONTROL_TRIGGERED")
            )
            resolved = True
        else:
            raise ValueError("Unsupported decision step")
    if not resolved:
        raise ValueError("Decision lacks the explicit priority resolver")
    return {"decision_action": action, "reason_code": reason}, trace
