"""Save a reviewed proposal as a separate SAS authoring rule set over HTTP."""

import copy

from apps.policyguard.synchronization import revision
from shared.proposals import proposal_rule, writable_rule, validate_proposal_exceptions


def save_draft(sas, finding, record, persist):
    validate_proposal_exceptions(finding["policy_source"])
    decision = sas.get(f"/decisions/flows/{finding['decision_id']}")
    if revision(decision) != finding["decision_revision"]:
        raise ValueError("The decision changed. Start a new investigation before adding to SAS.")
    source = sas.get(f"/businessRules/ruleSets/{finding['rule_set_id']}", integral=True)
    if revision(source) != finding["rule_set_revision"] or (
        finding.get("rule_set_modified_at") and source.get("modifiedTimeStamp") != finding["rule_set_modified_at"]
    ):
        raise ValueError("The source rule set changed. Start a new investigation before adding to SAS.")
    # Validate every copied rule before the first external mutation.
    rules = [writable_rule(r) for r in source["rules"]]
    if finding["classification"] == "NEW_RULE":
        rules.append(proposal_rule(finding["rule_name"], finding["canonical_proposed"],
                                   finding["policy_source"]["expected_action"], source["signature"]))
    else:
        index = next(i for i, r in enumerate(source["rules"]) if r["id"] == finding["rule_id"])
        replacement = proposal_rule(finding["rule_name"], finding["canonical_proposed"],
                                    finding["policy_source"]["expected_action"], source["signature"])
        rules[index]["conditions"] = replacement["conditions"]
        for action in rules[index]["actions"]:
            if action.get("term", {}).get("name") == "decision_action":
                action["expression"] = replacement["actions"][0]["expression"]
    metadata = {"name": record["draft_name"], "description": record["description"],
                "signature": copy.deepcopy(source["signature"]), "ruleSetType": source["ruleSetType"]}
    if not record.get("draft_rule_set_id"):
        # A previous POST can have succeeded before the response was lost. Reconcile by our unique name.
        existing = [s for s in sas.collection("/businessRules/ruleSets") if s["name"] == metadata["name"]]
        if len(existing) > 1 or (existing and existing[0].get("description") != metadata["description"]):
            raise ValueError("The SAS draft name is ambiguous; inspect the draft before retrying.")
        draft = existing[0] if existing else sas.create_authoring("/businessRules/ruleSets", metadata)
        record["draft_rule_set_id"] = draft["id"]
        persist(record)
    draft_id = record["draft_rule_set_id"]
    target = sas.get(f"/businessRules/ruleSets/{draft_id}", integral=True)
    if any(target.get(k) != v for k, v in metadata.items()):
        raise ValueError("The saved SAS draft metadata changed. Inspect it before retrying.")
    existing_rules = sas.collection(f"/businessRules/ruleSets/{draft_id}/rules")
    expected_names = {r["name"] for r in rules}
    if any(r["name"] not in expected_names for r in existing_rules):
        raise ValueError("Unexpected rules were added to the SAS draft. Inspect it before retrying.")
    for body in rules:
        existing = [r for r in existing_rules if r["name"] == body["name"]]
        if len(existing) > 1 or (existing and writable_rule(existing[0]) != body):
            raise ValueError("The saved SAS draft rule differs from the approved proposal. Inspect it before retrying.")
        if not existing:
            saved = sas.create_authoring(f"/businessRules/ruleSets/{draft_id}/rules", body)
            existing_rules.append(saved)
        record["saved_rule_count"] = len(existing_rules)
        persist(record)
    record.update(status="saved", rule_count=len(rules), active_decision_changed=False, published=False)
    persist(record)
    return record
