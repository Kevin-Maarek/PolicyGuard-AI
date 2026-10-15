"""Keep observed decision impact separate from explicit, tested rule authoring."""

from shared.logic import Expression, parse
from shared.proposals import validate_proposal_exceptions

NO_OUTCOME_CHANGE = "No final-decision difference in generated tests; isolated conditions differ. No global coverage proof."


def application_operation(finding):
    if finding.get("evidence_status") != "verified":
        return None
    classification = finding.get("classification")
    if classification in ("MODIFY_RULE", "NEW_RULE"):
        return "create" if classification == "NEW_RULE" else "update"
    # Older reports retain this classification and warning. Do not relabel their evidence.
    if classification != "NEEDS_REVIEW" or finding.get("coverage_reason") != NO_OUTCOME_CHANGE:
        return None
    selection = finding.get("human_selection") or {}
    operation = selection.get("operation")
    requirement = selection.get("approved_requirement") or {}
    tests = finding.get("tests") or []
    if operation not in ("update", "create") or selection.get("confirmed_interpretation") is not True:
        return None
    if (finding.get("freshness") != "verified" or not tests or requirement.get("ambiguities")
            or finding.get("logical_proof", {}).get("status") != "PROVEN"):
        return None
    if operation == "update" and not finding.get("rule_id"):
        return None
    if operation == "create" and not finding.get("proposal_rule_id"):
        return None
    try:
        proposed = Expression.model_validate(finding["canonical_proposed"])
        if proposed != parse(finding["proposed_expression"]) or proposed != Expression.model_validate(requirement["conditions"]):
            return None
        if requirement["expected_action"] != finding["policy_source"]["expected_action"]:
            return None
        validate_proposal_exceptions(requirement)
    except (ValueError, KeyError, TypeError):
        return None
    for test in tests:
        before = test.get("output", {}).get("decision_action")
        after = test.get("after", {}).get("decision_action")
        if (not test.get("execution_id") or test.get("diagnostics", {}).get("decision_revision_id") != finding.get("decision_revision_id")
                or before not in ("continue", "need_banker_aprove") or before != after
                or test.get("expected") != after or test.get("regression") or test.get("mismatch")):
            return None
    return operation


def with_application_options(data):
    return data | {"findings": [f | {"application_operation": application_operation(f)} for f in data.get("findings", [])]}
