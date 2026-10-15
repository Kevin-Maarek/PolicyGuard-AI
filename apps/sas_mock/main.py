import copy
import hmac
import os
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from apps.sas_mock.engine import execute
from apps.sas_mock.changes import RuleChanges, apply_changes
from apps.sas_mock.seed import DECISION_ID, MODULE_ID, SIGNATURE, link, stable, metadata
from shared.logic import Expression, normalize_rule
from shared.proposals import proposal_rule
from shared.storage import Store, now, uid

store = Store("sas")
app = FastAPI(
    title="SAS Viya Mock",
    version="0.1.0",
    description="Documented REST subset; synthetic evaluator and diagnostics.",
)
INTEGRAL = "application/vnd.sas.business.rule.set.integral+json"


@app.exception_handler(HTTPException)
async def http_error(request, exc):
    return JSONResponse(
        {
            "version": 2,
            "httpStatusCode": exc.status_code,
            "message": str(exc.detail),
            "details": [],
            "links": [],
        },
        status_code=exc.status_code,
        media_type="application/vnd.sas.error+json",
    )


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    return await http_error(request, HTTPException(400, str(exc)))


def required(table, id):
    row = store.get(table, id)
    if not row:
        raise HTTPException(404, "Object not found")
    return row


def admin(x_mock_admin_token: Annotated[str, Header()] = ""):
    if not hmac.compare_digest(x_mock_admin_token, os.getenv("MOCK_ADMIN_TOKEN", "local_simulator_only")):
        raise HTTPException(403, "Simulator administration token required")


def response(data, media="application/json", status=200):
    return JSONResponse(
        data,
        status_code=status,
        media_type=media,
        headers={"ETag": f'"{data.get("majorRevision", 1)}.{data.get("minorRevision", 0)}"'},
    )


def collection(items, path, start, limit):
    return {
        "name": path.split("/")[-1],
        "start": start,
        "limit": limit,
        "count": len(items),
        "items": items[start : start + limit],
        "links": [link(path)],
        "version": 2,
    }


@app.get("/health")
def health():
    with store.session() as s:
        s.execute(select(1))
    return {"status": "ok", "service": "sas-mock"}


@app.get("/decisions/flows")
def decisions(start: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    items = [
        {
            k: r["data"][k]
            for k in (
                "id",
                "name",
                "description",
                "majorRevision",
                "minorRevision",
                "modifiedTimeStamp",
                "links",
            )
        }
        for r in store.all("decisions")
    ]
    return response(
        collection(items, "/decisions/flows", start, limit), "application/vnd.sas.collection+json"
    )


@app.get("/decisions/flows/{id}")
def decision(id: str):
    return response(required("decisions", id)["data"], "application/vnd.sas.decision+json")


@app.get("/decisions/flows/{id}/revisions")
def decision_revisions(id: str, start: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    required("decisions", id)
    return collection(
        [revision_summary(r) for r in store.all("decision_revisions", id)],
        f"/decisions/flows/{id}/revisions",
        start,
        limit,
    )


@app.get("/decisions/flows/{id}/revisions/{revision_id}")
def decision_revision(id: str, revision_id: str):
    row = required("decision_revisions", revision_id)
    if row["parent_id"] != id:
        raise HTTPException(404, "Revision does not belong to decision")
    return response(row["data"], "application/vnd.sas.decision+json")


def revision_summary(row):
    return {
        "id": row["id"],
        "majorRevision": row["data"]["majorRevision"],
        "minorRevision": row["data"]["minorRevision"],
        "modifiedTimeStamp": row["data"]["modifiedTimeStamp"],
        "name": row["revision"],
        "links": [],
    }


@app.get("/businessRules/ruleSets")
def rule_sets(start: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    return response(
        collection(
            [{k: v for k, v in r["data"].items() if k != "rules"} for r in store.all("rule_sets")],
            "/businessRules/ruleSets",
            start,
            limit,
        ),
        "application/vnd.sas.collection+json",
    )


@app.get("/businessRules/ruleSets/{id}")
def rule_set(id: str, accept: Annotated[str, Header()] = "application/json"):
    data = copy.deepcopy(required("rule_sets", id)["data"])
    if INTEGRAL in accept:
        return response(data, INTEGRAL)
    data.pop("rules")
    data["version"] = 10
    return response(data, "application/vnd.sas.business.rule.set+json")


@app.get("/businessRules/ruleSets/{id}/revisions")
def ruleset_revisions(id: str, start: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    required("rule_sets", id)
    return collection(
        [revision_summary(r) for r in store.all("rule_set_revisions", id)],
        f"/businessRules/ruleSets/{id}/revisions",
        start,
        limit,
    )


@app.get("/businessRules/ruleSets/{id}/revisions/{revision_id}")
def ruleset_revision(id: str, revision_id: str, accept: Annotated[str, Header()] = "application/json"):
    row = required("rule_set_revisions", revision_id)
    if row["parent_id"] != id:
        raise HTTPException(404, "Revision does not belong to rule set")
    data = copy.deepcopy(row["data"])
    if INTEGRAL in accept:
        return response(data, INTEGRAL)
    data.pop("rules")
    data["version"] = 10
    return response(data, "application/vnd.sas.business.rule.set+json")


@app.get("/businessRules/ruleSets/{id}/rules")
def rules(id: str, start: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    rs = required("rule_sets", id)["data"]
    return collection(rs["rules"], f"/businessRules/ruleSets/{id}/rules", start, limit)


@app.get("/businessRules/ruleSets/{id}/order")
def order(id: str):
    required("rule_sets", id)
    return required("rule_ordering", id)["data"]["order"]


@app.get("/businessRules/ruleSets/{id}/rules/{rule_id}")
def rule(id: str, rule_id: str):
    row = required("rules", rule_id)
    if row["parent_id"] != id:
        raise HTTPException(404, "Rule does not belong to rule set")
    return response(row["data"], "application/vnd.sas.business.rule+json")


@app.get("/businessRules/ruleSets/{id}/revisions/{revision_id}/rules")
def revision_rules(
    id: str, revision_id: str, start: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)
):
    row = required("rule_set_revisions", revision_id)
    if row["parent_id"] != id:
        raise HTTPException(404, "Revision does not belong to rule set")
    return collection(
        row["data"]["rules"], f"/businessRules/ruleSets/{id}/revisions/{revision_id}/rules", start, limit
    )


def snapshot():
    module = required("published_modules", MODULE_ID)["data"]
    d = required("decision_revisions", module["decisionRevisionId"])["data"]
    sets = [
        required("rule_set_revisions", step["ruleSet"]["versionId"])["data"]
        for step in d["flow"]["steps"]
        if "ruleSet" in step
    ]
    return module, d, sets


def check_module(module_id, step_id):
    if module_id != MODULE_ID or step_id != "execute":
        raise HTTPException(404, "Module or step not found")


@app.get("/microanalyticScore/modules/{module_id}/steps/{step_id}")
def step_signature(module_id: str, step_id: str):
    check_module(module_id, step_id)
    return response(
        {
            "id": "execute",
            "moduleId": MODULE_ID,
            "version": 1,
            "inputs": [
                {"name": t["name"], "type": t["dataType"]} for t in SIGNATURE if t["direction"] == "input"
            ],
            "outputs": [
                {"name": t["name"], "type": t["dataType"], "size": 32}
                for t in SIGNATURE
                if t["direction"] == "output"
            ],
            "links": [],
        },
        "application/vnd.sas.microanalytic.module.step+json",
    )


class StepInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inputs: list[dict]
    version: int = 1


@app.post("/microanalyticScore/modules/{module_id}/steps/{step_id}")
def score(module_id: str, step_id: str, body: StepInput, request: Request, waitTime: int | None = None):
    check_module(module_id, step_id)
    if waitTime is not None:
        raise HTTPException(400, "This mock implements synchronous scoring only; omit waitTime")
    if request.headers.get("content-type", "").split(";")[0] not in (
        "application/json",
        "application/vnd.sas.microanalytic.module.step.input+json",
    ):
        raise HTTPException(415, "Unsupported scoring media type")
    if len({v.get("name") for v in body.inputs}) != len(body.inputs):
        raise HTTPException(400, "Duplicate inputs")
    module, d, sets = snapshot()
    try:
        inputs = {v["name"]: v["value"] for v in body.inputs}
        output, trace = execute(d, sets, inputs)
    except (ValueError, KeyError) as exc:
        raise HTTPException(400, str(exc)) from exc
    execution_id = uid()
    store.put(
        "execution_records",
        execution_id,
        {
            "inputs": inputs,
            "outputs": output,
            "trace": trace,
            "decision_id": d["id"],
            "decision_revision_id": module["decisionRevisionId"],
            "decision_revision": f"{d['majorRevision']}.{d['minorRevision']}",
            "rule_set_revisions": {s["id"]: f"{s['majorRevision']}.{s['minorRevision']}" for s in sets},
            "timestamp": now(),
        },
        d["id"],
        f"{d['majorRevision']}.{d['minorRevision']}",
    )
    result = {
        "moduleId": MODULE_ID,
        "stepId": "execute",
        "executionState": "completed",
        "outputs": [{"name": k, "value": v} for k, v in output.items()],
        "links": [],
        "version": 2,
    }
    r = response(result, "application/vnd.sas.microanalytic.module.step.output+json", 201)
    r.headers["X-Mock-Execution-Id"] = execution_id
    return r


@app.get("/internal/executions/{id}")
def execution_trace(id: str):
    return required("execution_records", id)["data"]


@app.get("/internal/snapshot")
def current_snapshot():
    module, d, sets = snapshot()
    return {"module": module, "decision": d, "rule_sets": sets, "simulated": True}


class Sandbox(BaseModel):
    rule_id: str
    operation: Literal["update", "create"] = "update"
    rule_set_id: str | None = None
    rule_name: str = "PolicyGuard proposal"
    expression: Expression
    action: Literal["continue", "need_banker_aprove"] | None = None
    cases: list[dict] = Field(max_length=100)
    decision_revision_id: str


@app.post("/internal/sandbox")
def sandbox(body: Sandbox):
    d = required("decision_revisions", body.decision_revision_id)["data"]
    sets = [
        required("rule_set_revisions", step["ruleSet"]["versionId"])["data"]
        for step in d["flow"]["steps"]
        if "ruleSet" in step
    ]
    if body.operation == "update" and not any(r["id"] == body.rule_id for s in sets for r in s["rules"]):
        raise HTTPException(404, "Rule not in pinned decision revision")
    proposed_sets = copy.deepcopy(sets)
    if body.operation == "create":
        target = next((s for s in proposed_sets if s["id"] == body.rule_set_id), None)
        if target is None:
            raise HTTPException(404, "Target rule set not in pinned decision revision")
        if not body.action:
            raise HTTPException(400, "A new sandbox rule requires an action")
        try:
            rule = proposal_rule(body.rule_name, body.expression.model_dump(), body.action, target["signature"])
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if any(r["id"] == body.rule_id or r["name"] == body.rule_name for s in sets for r in s["rules"]):
            raise HTTPException(400, "Sandbox new rule must have a distinct ID and name")
        rule["id"] = body.rule_id
        target["rules"].append(rule)
    replacement = {"rule_id": body.rule_id, "expression": body.expression.model_dump()}
    if body.action is not None:
        replacement["action"] = body.action
    try:
        results = []
        for case in body.cases:
            before, _ = execute(d, sets, case["inputs"])
            after, _ = execute(d, proposed_sets, case["inputs"], replacement if body.operation == "update" else None)
            results.append(
                {
                    **case,
                    "before": before,
                    "after": after,
                    "changed": before["decision_action"] != after["decision_action"],
                }
            )
        return {"decision_revision_id": body.decision_revision_id, "isolated": True, "results": results}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


class AuthoringRuleSet(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    signature: list[dict]
    ruleSetType: Literal["assignment"] = "assignment"


class AuthoringRule(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    conditional: Literal["if"]
    ruleFiredTrackingEnabled: bool
    conditions: list[dict]
    actions: list[dict]


def authoring_write(session, table_name, id, data, parent=None):
    from sqlalchemy.dialects.postgresql import insert
    table = store.table(table_name)
    values = dict(id=id, data=data, parent_id=parent, revision="1.0")
    session.execute(insert(table).values(**values).on_conflict_do_update(index_elements=[table.c.id], set_=values))


@app.post("/businessRules/ruleSets", dependencies=[Depends(admin)])
def create_authoring_rule_set(body: AuthoringRuleSet):
    """Documented authoring subset; creates no published revision or decision reference."""
    id = uid()
    data = metadata(id, body.name, f"/businessRules/ruleSets/{id}") | body.model_dump()
    data.update(rules=[], majorRevision=1, minorRevision=0, version=2, locked=False)
    with store.session() as session:
        # Serialize mock authoring writes and ensure unique draft names for retry reconciliation.
        session.execute(select(store.table("decisions")).where(store.table("decisions").c.id == DECISION_ID).with_for_update())
        rows = session.execute(store.table("rule_sets").select()).mappings().all()
        if any(r["data"]["name"] == body.name for r in rows):
            raise HTTPException(409, "Rule set name already exists")
        authoring_write(session, "rule_sets", id, data)
        authoring_write(session, "rule_ordering", id, {"order": []}, id)
    result = copy.deepcopy(data)
    result.pop("rules")
    result["version"] = 10
    return response(result, "application/vnd.sas.business.rule.set+json", 201)


@app.post("/businessRules/ruleSets/{id}/rules", dependencies=[Depends(admin)])
def create_authoring_rule(id: str, body: AuthoringRule):
    with store.session() as session:
        table = store.table("rule_sets")
        row = session.execute(table.select().where(table.c.id == id).with_for_update()).mappings().first()
        if not row:
            raise HTTPException(404, "Rule set not found")
        rs = copy.deepcopy(row["data"])
        if rs.get("locked", True):
            raise HTTPException(409, "Published mock rule sets are locked; create a separate authoring draft")
        if any(r["name"] == body.name for r in rs["rules"]):
            raise HTTPException(409, "Rule name already exists in this rule set")
        rule_id = uid()
        rule = metadata(rule_id, body.name, f"/businessRules/ruleSets/{id}/rules/{rule_id}") | body.model_dump()
        rule["version"] = 4
        try:
            node, action = normalize_rule(rule)
            proposal_rule(body.name, node.model_dump(), action, rs["signature"])
        except (ValueError, KeyError, StopIteration) as exc:
            raise HTTPException(400, "Unsupported rule or incompatible signature") from exc
        rs["rules"].append(rule)
        rs["modifiedTimeStamp"] = now()
        authoring_write(session, "rule_sets", id, rs)
        authoring_write(session, "rules", rule_id, rule, id)
        authoring_write(session, "rule_ordering", id, {"order": [r["id"] for r in rs["rules"]]}, id)
    return response(rule, "application/vnd.sas.business.rule+json", 201)


class RuleEdit(BaseModel):
    expression: str = Field(max_length=4000)
    action: Literal["continue", "need_banker_aprove"] | None = None
    expected_rule_set_revision: str | None = None


@app.post("/internal/rule-changes", dependencies=[Depends(admin)])
def apply_rule_changes(body: RuleChanges):
    return apply_changes(store, body)


@app.patch("/internal/rules/{rule_id}", dependencies=[Depends(admin)])
def edit_rule(rule_id: str, body: RuleEdit):
    """Explicit user edit, atomically creates immutable rule-set and decision revisions and republishes."""
    from sqlalchemy.dialects.postgresql import insert

    with store.session() as s:
        s.execute(
            select(store.table("decisions"))
            .where(store.table("decisions").c.id == DECISION_ID)
            .with_for_update()
        )
        rt = store.table("rules")
        row = s.execute(rt.select().where(rt.c.id == rule_id)).mappings().first()
        if not row:
            raise HTTPException(404, "Rule not found")
        rs_table = store.table("rule_sets")
        rs = copy.deepcopy(
            s.execute(rs_table.select().where(rs_table.c.id == row["parent_id"])).mappings().one()["data"]
        )
        if body.expected_rule_set_revision is not None and body.expected_rule_set_revision != (
            f"{rs['majorRevision']}.{rs['minorRevision']}"
        ):
            raise HTTPException(409, "The rule set changed since investigation. Start a new investigation before updating.")
        updated = next(r for r in rs["rules"] if r["id"] == rule_id)
        updated["conditions"] = [{"type": "complex", "expression": body.expression}]
        if body.action is not None:
            for action in updated["actions"]:
                if action.get("term", {}).get("name") == "decision_action":
                    action["expression"] = f"'{body.action}'"
        updated["modifiedTimeStamp"] = now()
        try:
            normalize_rule(updated)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        rs["minorRevision"] += 1
        rs["modifiedTimeStamp"] = now()
        revision = f"{rs['majorRevision']}.{rs['minorRevision']}"
        rs_revision_id = stable(rs["id"] + "/" + revision)
        d_table = store.table("decisions")
        d = copy.deepcopy(
            s.execute(d_table.select().where(d_table.c.id == DECISION_ID)).mappings().one()["data"]
        )
        for step in d["flow"]["steps"]:
            if step.get("ruleSet", {}).get("id") == rs["id"]:
                step["ruleSet"].update(versionId=rs_revision_id, versionName=revision)
        d["minorRevision"] += 1
        d["modifiedTimeStamp"] = now()
        d_revision = f"{d['majorRevision']}.{d['minorRevision']}"
        d_revision_id = stable(d["id"] + "/" + d_revision)

        def write(table_name, id, data, parent, rev):
            t = store.table(table_name)
            values = dict(id=id, data=data, parent_id=parent, revision=rev)
            s.execute(insert(t).values(**values).on_conflict_do_update(index_elements=[t.c.id], set_=values))

        write("rules", rule_id, updated, rs["id"], revision)
        write("rule_sets", rs["id"], rs, None, revision)
        write("rule_set_revisions", rs_revision_id, rs, rs["id"], revision)
        write("decisions", DECISION_ID, d, None, d_revision)
        write("decision_revisions", d_revision_id, d, DECISION_ID, d_revision)
        write(
            "decision_rule_sets",
            stable(DECISION_ID + rs["id"]),
            {"ruleSetId": rs["id"], "revisionId": rs_revision_id},
            DECISION_ID,
            d_revision,
        )
        write(
            "published_modules",
            MODULE_ID,
            {"decisionId": DECISION_ID, "decisionRevisionId": d_revision_id},
            DECISION_ID,
            d_revision,
        )
    return {"rule": updated, "rule_set_revision": revision, "decision_revision": d_revision}
