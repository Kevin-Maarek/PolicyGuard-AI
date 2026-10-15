"""Explicit simulator administration; not a SAS Viya deployment contract."""

import copy
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.dialects.postgresql import insert

from apps.sas_mock.seed import DECISION_ID, MODULE_ID, metadata, stable
from shared.logic import normalize_rule, parse
from shared.proposals import proposal_rule
from shared.storage import now


class RuleChange(BaseModel):
    finding_id: str
    operation: Literal["update", "create"]
    rule_set_id: str
    expected_rule_set_revision: str
    rule_id: str | None = None
    rule_name: str = Field(min_length=1, max_length=100)
    expression: str = Field(min_length=1, max_length=4000)
    action: Literal["continue", "need_banker_aprove"]


class RuleChanges(BaseModel):
    request_id: str = Field(min_length=1, max_length=80)
    expected_decision_revision_id: str
    changes: list[RuleChange] = Field(min_length=1, max_length=100)


def apply_changes(store, body: RuleChanges):
    """Validate and publish the whole reviewed batch in one database transaction."""
    with store.session() as session:
        def read(name, id, lock=False):
            table = store.table(name)
            query = table.select().where(table.c.id == id)
            row = session.execute(query.with_for_update() if lock else query).mappings().first()
            if not row:
                raise HTTPException(404, f"{name} object not found")
            return copy.deepcopy(row["data"])

        def write(name, id, data, parent=None, revision=None):
            table = store.table(name)
            values = dict(id=id, data=data, parent_id=parent, revision=revision)
            session.execute(insert(table).values(**values).on_conflict_do_update(
                index_elements=[table.c.id], set_=values))

        decision = read("decisions", DECISION_ID, lock=True)
        operations = store.table("rule_change_operations")
        prior = session.execute(operations.select().where(operations.c.id == body.request_id)).mappings().first()
        request = body.model_dump()
        if prior:
            if prior["data"]["request"] != request:
                raise HTTPException(409, "This application request was already used with different changes")
            return prior["data"]["result"] | {"idempotent": True}
        module = read("published_modules", MODULE_ID)
        if module["decisionRevisionId"] != body.expected_decision_revision_id:
            raise HTTPException(409, "The active decision changed since investigation. Start a new investigation.")
        attached = {step["ruleSet"]["id"] for step in decision["flow"]["steps"] if "ruleSet" in step}
        sets, changed_rules, results, seen = {}, {}, [], set()
        for change in body.changes:
            if change.finding_id in seen:
                raise HTTPException(400, "Duplicate finding in application request")
            seen.add(change.finding_id)
            if change.rule_set_id not in attached:
                raise HTTPException(409, "The target rule set is not attached to the active decision")
            rs = sets.setdefault(change.rule_set_id, read("rule_sets", change.rule_set_id, lock=True))
            if f"{rs['majorRevision']}.{rs['minorRevision']}" != change.expected_rule_set_revision:
                raise HTTPException(409, "The rule set changed since investigation. Start a new investigation.")
            try:
                native = proposal_rule(change.rule_name, parse(change.expression).model_dump(), change.action, rs["signature"])
            except (ValueError, KeyError) as exc:
                raise HTTPException(400, str(exc)) from exc
            if change.operation == "create":
                if any(r["name"] == change.rule_name for r in rs["rules"]):
                    raise HTTPException(409, "Rule name already exists in the target rule set")
                rule_id = stable(body.request_id + "/" + change.finding_id)
                rule = metadata(rule_id, change.rule_name, f"/businessRules/ruleSets/{rs['id']}/rules/{rule_id}") | native
                rule["version"] = 4
                rs["rules"].append(rule)
            else:
                rule_id = change.rule_id
                rule = next((r for r in rs["rules"] if r["id"] == rule_id), None)
                if not rule:
                    raise HTTPException(404, "Rule not found in the target rule set")
                if rule_id in changed_rules:
                    raise HTTPException(409, "Multiple proposals update the same rule; investigate a combined change")
                rule["conditions"] = native["conditions"]
                for action in rule["actions"]:
                    if action.get("term", {}).get("name") == "decision_action":
                        action["expression"] = f"'{change.action}'"
                normalize_rule(rule)
                rule["modifiedTimeStamp"] = now()
            changed_rules[rule_id] = (rule, rs["id"])
            results.append({"finding_id": change.finding_id, "operation": change.operation,
                            "rule_id": rule_id, "rule_name": rule["name"], "rule_set_id": rs["id"],
                            "expression": change.expression, "action": change.action})

        revisions = {}
        for rs in sets.values():
            rs["minorRevision"] += 1
            rs["modifiedTimeStamp"] = now()
            revision = f"{rs['majorRevision']}.{rs['minorRevision']}"
            revisions[rs["id"]] = revision
            revision_id = stable(rs["id"] + "/" + revision)
            write("rule_sets", rs["id"], rs, revision=revision)
            write("rule_set_revisions", revision_id, rs, rs["id"], revision)
            write("rule_ordering", rs["id"], {"order": [r["id"] for r in rs["rules"]]}, rs["id"], revision)
            for step in decision["flow"]["steps"]:
                if step.get("ruleSet", {}).get("id") == rs["id"]:
                    step["ruleSet"].update(versionId=revision_id, versionName=revision)
        for rule, parent in changed_rules.values():
            write("rules", rule["id"], rule, parent, revisions[parent])
        decision["minorRevision"] += 1
        decision["modifiedTimeStamp"] = now()
        d_revision = f"{decision['majorRevision']}.{decision['minorRevision']}"
        d_revision_id = stable(DECISION_ID + "/" + d_revision)
        write("decisions", DECISION_ID, decision, revision=d_revision)
        write("decision_revisions", d_revision_id, decision, DECISION_ID, d_revision)
        for rs_id, revision in revisions.items():
            write("decision_rule_sets", stable(DECISION_ID + rs_id),
                  {"ruleSetId": rs_id, "revisionId": stable(rs_id + "/" + revision)}, DECISION_ID, d_revision)
        write("published_modules", MODULE_ID,
              {"decisionId": DECISION_ID, "decisionRevisionId": d_revision_id}, DECISION_ID, d_revision)
        for result in results:
            result["rule_set_revision"] = revisions[result["rule_set_id"]]
        result = {"changes": results, "decision_revision": d_revision,
                  "decision_revision_id": d_revision_id, "active_decision_changed": True, "simulator_only": True}
        write("rule_change_operations", body.request_id, {"request": request, "result": result}, DECISION_ID)
    return result
