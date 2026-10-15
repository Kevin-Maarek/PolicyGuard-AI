"""Supported rule proposal serialization; no publishing or database access."""

import copy

from shared.logic import Expression, INPUT_TYPES, atoms, compare, normalize_rule, parse, render


def validate_proposal_exceptions(requirement):
    """Validation only: never fill in or silently rewrite extracted conditions."""
    condition = Expression.model_validate(requirement["conditions"])
    for text in requirement.get("exceptions", []):
        try:
            exception = parse(text)
        except ValueError as exc:
            # Explicit seeded business dictionary entry; other prose needs clarification.
            if text.strip().casefold() == "verified same account holder":
                exception = parse("same_owner_verified = 1")
            else:
                raise ValueError(f"Document exception requires a supported logical interpretation: {text}") from exc
        excluding = Expression(operator="AND", children=[condition, Expression(operator="NOT", children=[exception])])
        if compare(condition, excluding).get("equivalent") is not True:
            raise ValueError(f"Proposed condition does not exclude the document exception: {text}")


def proposal_rule(name, node, action, signature):
    node = Expression.model_validate(node)
    terms = {t["name"]: t for t in signature}
    for leaf in atoms(node):
        if leaf.field not in terms or terms[leaf.field]["dataType"] != INPUT_TYPES[leaf.field]:
            raise ValueError(f"Target rule set has no compatible input: {leaf.field}")
    if terms.get("decision_action", {}).get("dataType") != "string":
        raise ValueError("Target rule set must have a string decision_action output")
    rule = {
        "name": name, "conditional": "if", "ruleFiredTrackingEnabled": True,
        "description": "PolicyGuard reviewed policy proposal",
        "conditions": [{"type": "complex", "expression": render(node).replace("==", "=")}],
        "actions": [{"type": "assignment", "term": terms["decision_action"], "expression": f"'{action}'"}],
    }
    normalize_rule(rule)
    return rule


def sandbox_payload(comparison, revision_id, cases):
    selection = comparison.get("human_selection") or {}
    create = selection.get("operation") == "create"
    return {
        "operation": "create" if create else "update",
        "rule_id": selection["proposal_rule_id"] if create else comparison["selected"]["rule"]["id"],
        "rule_set_id": comparison["selected"]["rule_set"]["id"],
        "rule_name": selection.get("rule_name") or "PolicyGuard proposal",
        "expression": comparison["new"],
        "action": comparison["requirement"]["expected_action"],
        "decision_revision_id": revision_id, "cases": cases,
    }


def writable_rule(rule):
    normalize_rule(rule)
    return {k: copy.deepcopy(v) for k, v in rule.items() if k in (
        "name", "description", "conditional", "ruleFiredTrackingEnabled", "conditions", "actions",
    )}
