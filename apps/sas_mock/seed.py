"""Repeatable synthetic SAS repository seed. Never overwrites an existing revision."""

from uuid import NAMESPACE_URL, uuid5

from shared.logic import INPUT_TYPES
from shared.storage import Store, now


def stable(name):
    return str(uuid5(NAMESPACE_URL, "policyguard-demo/" + name))


DECISION_ID = stable("Retail_Transfer_Fraud_Controls")
AMOUNT_ID = stable("Transfer_Amount_Controls")
SECURITY_ID = stable("Account_Security_Controls")
PRIORITY_ID = stable("Resolve_Transfer_Control_Priority")
MODULE_ID = "retail_transfer_fraud_controls"
SIGNATURE = [{"name": k, "dataType": v, "direction": "input"} for k, v in INPUT_TYPES.items()] + [
    {"name": "decision_action", "dataType": "string", "direction": "output", "length": 32},
    {"name": "reason_code", "dataType": "string", "direction": "output", "length": 32},
]
RULE_SPECS = [
    (
        "R001",
        "High Value New Beneficiary",
        "amount > 100000 AND new_beneficiary = 1",
        "need_banker_aprove",
        AMOUNT_ID,
    ),
    (
        "R002",
        "New Account Large Transfer",
        "account_age_days < 7 AND amount > 15000",
        "need_banker_aprove",
        AMOUNT_ID,
    ),
    (
        "R003",
        "New Beneficiary Verification",
        "amount > 30000 AND new_beneficiary = 1 AND same_owner_verified = 0",
        "need_banker_aprove",
        AMOUNT_ID,
    ),
    (
        "R004",
        "Untrusted Device",
        "trusted_device = 0 AND amount > 10000",
        "need_banker_aprove",
        SECURITY_ID,
    ),
    (
        "R005",
        "Failed Authentication Attempts",
        "failed_logins_24h >= 5 AND amount > 5000",
        "need_banker_aprove",
        SECURITY_ID,
    ),
]


def link(uri, media="application/json"):
    return {"method": "GET", "rel": "self", "href": uri, "uri": uri, "type": media}


def metadata(id, name, uri):
    return dict(
        id=id,
        name=name,
        creationTimeStamp=now(),
        modifiedTimeStamp=now(),
        createdBy="synthetic-demo",
        modifiedBy="synthetic-demo",
        links=[link(uri)],
    )


def build_seed():
    sets = []
    for id, name in [(AMOUNT_ID, "Transfer_Amount_Controls"), (SECURITY_ID, "Account_Security_Controls")]:
        rules = []
        for code, title, expression, action, parent in RULE_SPECS:
            if parent != id:
                continue
            rule = metadata(
                stable(code), code + " " + title, f"/businessRules/ruleSets/{id}/rules/{stable(code)}"
            )
            rule.update(
                description=f"Synthetic domestic retail money transfer fraud control: {title}.",
                conditional="if",
                ruleFiredTrackingEnabled=True,
                version=4,
                conditions=[{"type": "complex", "expression": expression}],
                actions=[
                    {"type": "assignment", "term": SIGNATURE[-2], "expression": f"'{action}'"},
                    {"type": "assignment", "term": SIGNATURE[-1], "expression": f"'{code}'"},
                ],
            )
            rules.append(rule)
        rs = metadata(id, name, f"/businessRules/ruleSets/{id}")
        rs.update(
            description="Synthetic domestic retail money transfer controls. Not actual banking policy.",
            majorRevision=1,
            minorRevision=0,
            signature=SIGNATURE,
            ruleSetType="assignment",
            rules=rules,
            version=2,
            locked=True,
        )
        sets.append(rs)
    decision = metadata(DECISION_ID, "Retail_Transfer_Fraud_Controls", f"/decisions/flows/{DECISION_ID}")
    mappings = [
        {"stepTermName": t["name"], "direction": t["direction"], "targetDecisionTermName": t["name"]}
        for t in SIGNATURE
    ]
    decision.update(
        description="Synthetic domestic retail transfers with explicit priority resolution.",
        majorRevision=1,
        minorRevision=0,
        signature=SIGNATURE,
        version=1,
        nodeCount=3,
        properties={"demoPriority": "need_banker_aprove > continue"},
        flow={
            "steps": [
                {
                    "type": "application/vnd.sas.decision.step.ruleset",
                    "ruleSet": {
                        "id": rs["id"],
                        "name": rs["name"],
                        "versionId": stable(rs["id"] + "/1.0"),
                        "versionName": "1.0",
                    },
                    "mappings": mappings,
                }
                for rs in sets
            ]
            + [
                {
                    "type": "application/vnd.sas.decision.step.custom.object",
                    "customObject": {
                        "name": "Resolve_Transfer_Control_Priority",
                        "uri": f"/decisions/codeFiles/{PRIORITY_ID}",
                        "type": "decisionDs2CodeFile",
                    },
                    "mappings": mappings,
                }
            ]
        },
    )
    return decision, sets


def seed(store):
    if store.get("decisions", DECISION_ID):
        return
    decision, sets = build_seed()
    store.put("decisions", DECISION_ID, decision, revision="1.0")
    store.put("decision_revisions", stable(DECISION_ID + "/1.0"), decision, DECISION_ID, "1.0")
    for rs in sets:
        revision_id = stable(rs["id"] + "/1.0")
        store.put("rule_sets", rs["id"], rs, revision="1.0")
        store.put("rule_set_revisions", revision_id, rs, rs["id"], "1.0")
        store.put(
            "decision_rule_sets",
            stable(DECISION_ID + rs["id"]),
            {"ruleSetId": rs["id"], "revisionId": revision_id},
            DECISION_ID,
            "1.0",
        )
        store.put("rule_ordering", rs["id"], {"order": [r["id"] for r in rs["rules"]]}, rs["id"])
        for rule in rs["rules"]:
            store.put("rules", rule["id"], rule, rs["id"], "1.0")
    store.put(
        "published_modules",
        MODULE_ID,
        {"decisionId": DECISION_ID, "decisionRevisionId": stable(DECISION_ID + "/1.0")},
        DECISION_ID,
        "1.0",
    )


if __name__ == "__main__":
    store = Store("sas")
    store.initialize()
    seed(store)
