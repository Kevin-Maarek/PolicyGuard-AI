from threading import Lock
from typing import TypedDict

import httpx
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from apps.policyguard import providers
from apps.policyguard.clarification import introduce
from apps.policyguard.config import settings
from apps.policyguard.ingestion import Extraction, TERMS, extract, validate_citations
from apps.policyguard.sas import SASClient
from apps.policyguard.synchronization import revision
from shared.logic import Expression, atoms, compare, generate_cases, normalize_rule, render
from shared.storage import now
from shared.proposals import proposal_rule, sandbox_payload, validate_proposal_exceptions
from apps.policyguard.rule_application import NO_OUTCOME_CHANGE


class AuditState(TypedDict, total=False):
    audit_id: str
    document_id: str
    document_version_id: str
    doc: dict
    requirements: list[dict]
    candidates: list[dict]
    matches: list[dict]
    snapshot: dict
    mappings: dict
    comparisons: list[dict]
    cases: list[dict]
    executions: list[dict]
    findings: list[dict]
    errors: list[str]
    attempts: int
    review_requirements: bool
    review_status: str
    retry_freshness: bool
    rule_selections: dict
    analysis_generation: int


NODES = [
    "document_intake",
    "requirement_extraction",
    "requirement_validation",
    "candidate_retrieval",
    "asset_investigation",
    "rule_planning",
    "business_term_mapping",
    "logical_analysis",
    "test_generation",
    "decision_execution",
    "evidence_verification",
    "impact_classification",
    "report_generation",
    "human_review",
]


class Workflow:
    def __init__(self, store, retriever, synchronizer, checkpointer):
        self.store, self.retriever, self.synchronizer = store, retriever, synchronizer
        self.active = set()
        self.clarifying = set()
        self.lock = Lock()
        graph = StateGraph(AuditState)
        for name in NODES:
            graph.add_node(name, self._wrap(name, getattr(self, name)))
        graph.add_edge(START, NODES[0])
        for a, b in zip(NODES, NODES[1:]):
            if a not in ("requirement_validation", "candidate_retrieval", "evidence_verification"):
                graph.add_edge(a, b)
        graph.add_conditional_edges(
            "requirement_validation",
            lambda state: "search" if state["requirements"] else "report",
            {"search": "candidate_retrieval", "report": "report_generation"},
        )
        graph.add_edge("candidate_retrieval", "asset_investigation")
        graph.add_conditional_edges(
            "evidence_verification",
            lambda state: "refresh" if state.get("retry_freshness") else "classify",
            {"refresh": "candidate_retrieval", "classify": "impact_classification"},
        )
        graph.add_edge(NODES[-1], END)
        self.graph = graph.compile(checkpointer=checkpointer)

    def update(self, id, **changes):
        row = self.store.get("audit_runs", id)
        data = row["data"]
        data.update(changes)
        self.store.put("audit_runs", id, data, row["parent_id"])

    def event(self, id, node, status, detail=None):
        data = self.store.get("audit_runs", id)["data"]
        events = data.get("events", [])
        events.append({"node": node, "status": status, "timestamp": now(), "detail": detail})
        self.update(id, events=events, current_node=node)

    def _wrap(self, name, fn):
        def run(state):
            self.event(state["audit_id"], name, "running")
            for attempt in range(2):
                try:
                    result = fn(state)
                    self.event(state["audit_id"], name, "completed")
                    return result or {}
                except httpx.HTTPError as exc:
                    self.event(state["audit_id"], name, "retry" if attempt == 0 else "failed", str(exc))
                    if attempt == 1:
                        raise

        return run

    def run(self, id, resume=None, continue_from_checkpoint=False):
        with self.lock:
            if id in self.active:
                return
            self.active.add(id)
        try:
            audit = self.store.get("audit_runs", id)["data"]
            if audit["status"] in ("completed", "rejected"):
                return
            self.update(id, status="running", error=None)
            config = {"configurable": {"thread_id": id}, "recursion_limit": 40}
            if continue_from_checkpoint:
                payload = None
            elif resume is not None:
                payload = Command(resume=resume)
            else:
                payload = {
                    "audit_id": id,
                    "document_id": audit["document_id"],
                    "document_version_id": audit["document_version_id"],
                    "review_requirements": audit.get("review_requirements", False),
                    "errors": [],
                    "attempts": 0,
                }
            result = self.graph.invoke(payload, config)
            interrupts = result.get("__interrupt__", ())
            if interrupts:
                self.update(id, status="awaiting_review", interrupt=interrupts[0].value)
            else:
                status = "rejected" if result.get("review_status") == "rejected" else "completed"
                self.update(id, status=status, completed_at=now(), interrupt=None)
        except Exception as exc:
            self.update(id, status="failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            with self.lock:
                self.active.discard(id)

    def document_intake(self, state):
        document = self.store.get("policy_document_versions", state["document_version_id"])
        if not document:
            raise ValueError("Pinned policy document version not found")
        return {"doc": document["data"]["parsed"]}

    def requirement_extraction(self, state):
        extraction = extract(state["doc"])
        requirements = [r.model_dump() for r in extraction.requirements]
        for req in requirements:
            req.update(
                source_document_id=state["document_id"], source_document_version=state["document_version_id"]
            )
            self.store.put(
                "extracted_requirements",
                state["audit_id"] + ":" + req["requirement_id"],
                req,
                state["audit_id"],
                state["document_version_id"],
            )
        self.update(state["audit_id"], requirements=requirements)
        return {"requirements": requirements}

    def requirement_validation(self, state):
        requirements = state["requirements"]
        validate_citations(state["doc"], requirements)
        needs_review = state.get("review_requirements") or any(r["ambiguities"] for r in requirements)
        if needs_review:
            # Persist once before the interrupt; graph resumption must not regenerate the greeting.
            audit = self.store.get("audit_runs", state["audit_id"])["data"]
            if not audit.get("clarification") and not audit.get("clarification_error"):
                try:
                    self.initialize_clarification(state["audit_id"], state["doc"], requirements)
                except (httpx.HTTPError, ValueError) as exc:
                    self.update(state["audit_id"], clarification_error=f"Could not prepare the interpretation: {exc}")
            answer = interrupt(
                {
                    "kind": "requirements",
                    "requirements": requirements,
                    "message": "Review extracted requirements. Correct ambiguous mappings before proof.",
                }
            )
            if not answer.get("approved"):
                return {"requirements": [], "errors": ["Requirements rejected by reviewer"]}
            if answer.get("requirements"):
                corrected = Extraction.model_validate({"requirements": answer["requirements"]})
                requirements = [
                    r.model_dump()
                    | {
                        "source_document_id": state["document_id"],
                        "source_document_version": state["document_version_id"],
                    }
                    for r in corrected.requirements
                ]
                validate_citations(state["doc"], requirements)
        for req in requirements:
            req["review_status"] = "approved" if needs_review else "validated"
            self.store.put(
                "extracted_requirements",
                state["audit_id"] + ":" + req["requirement_id"],
                req,
                state["audit_id"],
                state["document_version_id"],
            )
        self.update(state["audit_id"], requirements=requirements)
        return {"requirements": requirements}

    def initialize_clarification(self, id, doc, requirements):
        reply = introduce(doc, requirements)
        clarification = {
            "revision": 0, "status": "initial", "requirements": requirements,
            "original_requirements": requirements,
            "messages": [{"role": "assistant", "content": reply.message, "timestamp": now()}],
            "input_suggestion": reply.input_suggestion,
            "provider": settings.llm_provider,
            "model": settings.llm_model,
        }
        self.update(id, clarification=clarification, clarification_error=None)
        return clarification

    def candidate_retrieval(self, state):
        candidates = [
            self.retriever.search(r["original_text"] + " " + " ".join(r["related_terms"]), kind="rule")
            for r in state["requirements"]
        ]
        self.update(state["audit_id"], candidates=candidates)
        for i, candidate in enumerate(candidates):
            self.store.put("requirement_matches", f"{state['audit_id']}:{i}", candidate, state["audit_id"])
        return {"candidates": candidates}

    def rule_candidates(self, id):
        """Describe the best ranked rule per requirement from the pinned HTTP snapshot."""
        state = self.graph.get_state({"configurable": {"thread_id": id}}).values
        snapshot = state.get("snapshot", {})
        rules = {
            rule["id"]: (rule, rs)
            for rs in snapshot.get("rule_sets", []) for rule in rs["rules"]
        }
        groups = []
        for req, retrieved in zip(state.get("requirements", []), state.get("candidates", [])):
            candidates = []
            # Older checkpoints keep the component scores only in their ranked lists.
            bm25_scores = {hit["id"]: hit["score"] for hit in retrieved.get("bm25", [])}
            vector_scores = {hit["id"]: hit["score"] for hit in retrieved.get("vector", [])}
            for rank, item in enumerate(retrieved["fused"], 1):
                if item["id"] not in rules:
                    continue
                rule, rs = rules[item["id"]]
                candidate = {
                    **item, "rank": rank, "name": rule["name"], "rule_set_name": rs["name"], "rule_set_id": rs["id"],
                    "revision": revision(rs), "supported": True,
                    "bm25_score": item.get("bm25_score", bm25_scores.get(item["id"])),
                    "vector_score": item.get("vector_score", vector_scores.get(item["id"])),
                    "expression": " AND ".join(c.get("expression", "") for c in rule["conditions"]),
                }
                try:
                    node, action = normalize_rule(rule)
                    candidate.update(expression=render(node), action=action)
                except (ValueError, KeyError, StopIteration) as exc:
                    candidate.update(supported=False, limitation=str(exc))
                candidates.append(candidate)
                break
            groups.append({"requirement_id": req["requirement_id"], "candidates": candidates,
                           "rule_sets": [{"id": rs["id"], "name": rs["name"], "revision": revision(rs)}
                                         for rs in snapshot.get("rule_sets", [])]})
        return groups

    def prepare_rule_selection(self, id, requirements, selection):
        config = {"configurable": {"thread_id": id}}
        state = self.graph.get_state(config).values
        selections = {**state.get("rule_selections", {}), selection["requirement_id"]: selection}
        # Re-enter investigation, keeping extraction and retrieval while discarding prior proof and executions.
        self.graph.update_state(config, {
            "requirements": requirements, "rule_selections": selections,
            "comparisons": [], "cases": [], "executions": [], "findings": [],
            "review_status": "", "retry_freshness": False,
            "attempts": 0, "analysis_generation": state.get("analysis_generation", 0) + 1,
        }, as_node="candidate_retrieval")
        self.update(id, requirements=requirements, rule_selections=selections, findings=[], comparisons=[],
                    executions=[], status="running", interrupt=None, completed_at=None, error=None)

    def rule_planning(self, state):
        missing = [r["requirement_id"] for r in state["requirements"]
                   if r["requirement_id"] not in state.get("rule_selections", {})]
        if missing:
            # Choices are submitted through select-rule, which re-enters the graph with persisted selections.
            interrupt({"kind": "rule_planning", "requirement_ids": missing,
                       "message": "Choose update an existing rule, create a new rule, or no change."})
        return {}

    def asset_investigation(self, state):
        with SASClient() as sas:
            snapshot = sas.get("/internal/snapshot")
            d = sas.get(f"/decisions/flows/{snapshot['decision']['id']}")
            if revision(d) != revision(snapshot["decision"]):
                raise ValueError("Current decision changed during investigation")
            # Refresh only when needed, independently of document upload.
            index = self.store.get("sas_indexed_assets", d["id"])
            if not index or index["revision"] != revision(d):
                sync = self.synchronizer.run()
                if sync["status"] != "completed":
                    raise ValueError("Fresh SAS index could not be verified")
            for rs in snapshot["rule_sets"]:
                live = sas.get(f"/businessRules/ruleSets/{rs['id']}", integral=True)
                if revision(live) != revision(rs):
                    raise ValueError("Published module and current rule set differ")
        rules = [{"rule": rule, "rule_set": rs} for rs in snapshot["rule_sets"] for rule in rs["rules"]]
        matches = []
        for req in state["requirements"]:
            # A recorded human choice overrides discovery only; formal comparison and evidence still run.
            selection = state.get("rule_selections", {}).get(req["requirement_id"])
            if selection:
                operation = selection.get("operation", "update")
                if operation == "no_change":
                    matches.append({"requirement": req, "selected": None, "human_selection": selection,
                                    "assessment": "Reviewer chose no change; this is not proof of policy coverage."})
                    continue
                if operation == "create":
                    rs = next((s for s in snapshot["rule_sets"] if s["id"] == selection["rule_set_id"]), None)
                    if rs is None:
                        raise ValueError("The target rule set is no longer in the decision")
                    rule = proposal_rule(selection["rule_name"], req["conditions"], req["expected_action"], rs["signature"])
                    rule["id"] = selection["proposal_rule_id"]
                    matches.append({"requirement": req, "selected": {"rule": rule, "rule_set": rs},
                                    "human_selection": selection, "assessment": "Reviewer chose a new rule; existing rules are preserved."})
                    continue
                chosen = next((r for r in rules if r["rule"]["id"] == selection["rule_id"]), None)
                if chosen is None:
                    raise ValueError("The human-selected rule is no longer available in the current SAS revision")
                matches.append({"requirement": req, "selected": chosen,
                                "assessment": "Candidate explicitly selected by the human reviewer.",
                                "human_selection": selection})
                continue
            matches.append({"requirement": req, "selected": None,
                            "assessment": "Related rules retrieved; awaiting the reviewer's change direction."})
        return {"snapshot": snapshot, "matches": matches}

    def business_term_mapping(self, state):
        mapping = {
            "dictionary": TERMS,
            "validated_against_signature": [t["name"] for t in state["snapshot"]["decision"]["signature"]],
        }
        self.update(state["audit_id"], mappings=mapping)
        return {"mappings": mapping}

    def logical_analysis(self, state):
        comparisons = []
        for match in state["matches"]:
            req, selected = match["requirement"], match["selected"]
            operation = (match.get("human_selection") or {}).get("operation")
            if operation == "no_change":
                comparisons.append({**match, "classification": "NO_CHANGE"})
                continue
            if req["ambiguities"] or not selected:
                comparisons.append(
                    {**match, "classification": "NEEDS_REVIEW" if req["ambiguities"] else "NEW_RULE"}
                )
                continue
            try:
                validate_proposal_exceptions(req)
            except ValueError as exc:
                comparisons.append({**match, "classification": "NEEDS_REVIEW", "coverage_reason": str(exc)})
                continue
            old, action = normalize_rule(selected["rule"])
            if operation == "create":
                # Impossible in the supported nonnegative input domain: no prior rule to replace.
                old = Expression(field="amount", operator="<", value=0)
            new = Expression.model_validate(req["conditions"])
            proof = compare(old, new)
            old_fields = {a.field for a in atoms(old)}
            new_fields = {a.field for a in atoms(new)}
            classification = (
                "ALREADY_COVERED"
                if proof["equivalent"] and action == req["expected_action"]
                else "MODIFY_RULE"
            )
            if operation == "create":
                classification = "NEW_RULE"
            if action != req["expected_action"]:
                classification = "CONFLICT"
            if (operation != "create" and old_fields != new_fields) or proof["equivalent"] is None:
                classification = "NEEDS_REVIEW"
            comparisons.append(
                {
                    **match,
                    "classification": classification,
                    "proof": proof,
                    "old": old.model_dump(),
                    "new": new.model_dump(),
                    "action": action,
                }
            )
        self.update(state["audit_id"], comparisons=comparisons)
        return {"comparisons": comparisons}

    def test_generation(self, state):
        cases = []
        for i, comparison in enumerate(state["comparisons"]):
            if "old" not in comparison or comparison["classification"] == "NEEDS_REVIEW":
                continue
            for n, case in enumerate(
                generate_cases(
                    Expression.model_validate(comparison["old"]), Expression.model_validate(comparison["new"])
                )
            ):
                generation = state.get("analysis_generation", 0)
                prefix = f"{state['audit_id']}:selection-{generation}" if generation else state["audit_id"]
                case.update(id=f"{prefix}:{state.get('attempts', 0)}:{i}:{n}", comparison_index=i)
                self.store.put("generated_test_cases", case["id"], case, state["audit_id"])
                cases.append(case)
        return {"cases": cases}

    def decision_execution(self, state):
        results = []
        with SASClient() as sas:
            for case in state["cases"]:
                existing = self.store.get("test_executions", case["id"])
                execution = existing["data"] if existing else {**case, **sas.score(case["inputs"])}
                self.store.put("test_executions", case["id"], execution, state["audit_id"])
                results.append(execution)
        self.update(state["audit_id"], executions=results)
        return {"executions": results}

    def evidence_verification(self, state):
        snapshot = state["snapshot"]
        comparisons = state["comparisons"]
        with SASClient() as sas:
            current = sas.get("/internal/snapshot")
            fresh = current["module"]["decisionRevisionId"] == snapshot["module"]["decisionRevisionId"]
            for comparison in comparisons:
                if "old" not in comparison or comparison["classification"] == "NEEDS_REVIEW":
                    continue
                index = comparisons.index(comparison)
                tests = [e for e in state["executions"] if e["comparison_index"] == index]
                pinned = all(
                    e.get("diagnostics")
                    and e["diagnostics"]["decision_revision_id"] == snapshot["module"]["decisionRevisionId"]
                    for e in tests
                )
                simulation = sas.post(
                    "/internal/sandbox",
                    sandbox_payload(comparison, snapshot["module"]["decisionRevisionId"], tests),
                )
                evidence = []
                for actual, simulated in zip(tests, simulation["results"], strict=True):
                    expected = simulated["after"]["decision_action"]
                    evidence.append(
                        {
                            **actual,
                            "expected": expected,
                            "after": simulated["after"],
                            "mismatch": actual["output"]["decision_action"] != expected,
                            "regression": actual["output"]["decision_action"] == "need_banker_aprove"
                            and expected != "need_banker_aprove",
                        }
                    )
                comparison.update(
                    tests=evidence,
                    freshness="verified" if fresh and pinned and tests else "stale",
                    mismatches=sum(e["mismatch"] for e in evidence),
                    simulation=simulation,
                )
                if not fresh or not pinned:
                    comparison["classification"] = "INSUFFICIENT_EVIDENCE"
                    comparison["stale_reason"] = "SAS changed during analysis. Refresh and start a new audit."
        if not fresh:
            self.synchronizer.run()
        attempts = state.get("attempts", 0)
        return {
            "comparisons": comparisons,
            "retry_freshness": not fresh and attempts < 2,
            "attempts": attempts + 1 if not fresh else attempts,
        }

    def impact_classification(self, state):
        for c in state["comparisons"]:
            if c["classification"] in ("MODIFY_RULE", "NEW_RULE") and not c.get("mismatches"):
                c["classification"] = "NEEDS_REVIEW"
                c["coverage_reason"] = NO_OUTCOME_CHANGE
            if c["classification"] == "NEW_RULE" and any(t.get("regression") for t in c.get("tests", [])):
                c["classification"] = "NEEDS_REVIEW"
                c["coverage_reason"] = "The proposed addition regresses an existing approval outcome."
        return {"comparisons": state["comparisons"]}

    def report_generation(self, state):
        findings = []
        comparisons = state.get("comparisons", [])
        if not comparisons:
            comparisons = [
                {
                    "classification": "INSUFFICIENT_EVIDENCE",
                    "requirement": req,
                    "assessment": "No supported candidates or requirements available.",
                }
                for req in state.get("requirements", [])
            ]
        for i, c in enumerate(comparisons):
            selected = c.get("selected")
            create = (c.get("human_selection") or {}).get("operation") == "create"
            req = c["requirement"]
            d = state.get("snapshot", {}).get("decision", {})
            finding = {
                "id": f"{state['audit_id']}:{i}",
                "classification": c["classification"],
                "document_id": state["document_id"],
                "document_version_id": state["document_version_id"],
                "policy_source": req,
                "decision_id": d.get("id"),
                "decision_name": d.get("name"),
                "decision_revision": revision(d) if d else None,
                "decision_revision_id": state.get("snapshot", {}).get("module", {}).get("decisionRevisionId"),
                "rule_set_id": selected["rule_set"]["id"] if selected else None,
                "rule_set_name": selected["rule_set"]["name"] if selected else None,
                "rule_set_revision": revision(selected["rule_set"]) if selected else None,
                "rule_set_modified_at": selected["rule_set"].get("modifiedTimeStamp") if selected else None,
                "rule_id": selected["rule"]["id"] if selected and not create else None,
                "proposal_rule_id": selected["rule"]["id"] if selected and create else None,
                "rule_name": selected["rule"]["name"] if selected else None,
                "raw_sas_rule": selected["rule"] if selected and not create else None,
                "existing_expression": None if create else c.get("proof", {}).get("existing_expression"),
                "proposed_expression": c.get("proof", {}).get("proposed_expression"),
                "canonical_existing": c.get("old"),
                "canonical_proposed": c.get("new"),
                "logical_proof": c.get("proof"),
                "tests": c.get("tests", []),
                "freshness": c.get("freshness", "unverified"),
                "candidate_analysis": c.get("assessment"),
                "human_selection": c.get("human_selection"),
                "coverage_reason": c.get("coverage_reason"),
                "review_status": "pending",
                "evidence_status": "verified" if c.get("freshness") == "verified" else "unverified",
                "recommendation": None,
            }
            if finding["classification"] == "MODIFY_RULE" and finding["evidence_status"] == "verified":
                finding["recommendation"] = (
                    f"Modify {finding['rule_name']} in {finding['rule_set_name']}: {finding['existing_expression']} → {finding['proposed_expression']}. Preserve all unchanged predicates and review the cited exception. Human implementation required."
                )
            if finding["classification"] == "NEW_RULE" and finding["evidence_status"] == "verified":
                finding["recommendation"] = (
                    f"Create {finding['rule_name']} in {finding['rule_set_name']}: {finding['proposed_expression']}. "
                    "Preserve existing rules. Review the sandbox results before explicitly applying to the simulator or saving a SAS draft."
                )
            finding["explanation"] = providers.explain(
                {k: v for k, v in finding.items() if k not in ("raw_sas_rule", "tests")}
                | {
                    "tests": [
                        {"inputs": t["inputs"], "actual": t["output"], "expected": t["expected"]}
                        for t in finding["tests"]
                    ]
                },
            )
            self.store.put(
                "audit_findings", finding["id"], finding, state["audit_id"], state["document_version_id"]
            )
            findings.append(finding)
        self.update(
            state["audit_id"],
            findings=findings,
            report_generated_at=now(),
            message="No extractable requirements" if not state.get("requirements") else None,
        )
        return {"findings": findings}

    def human_review(self, state):
        answer = interrupt(
            {
                "kind": "findings",
                "message": "Review the evidence and approve or reject the report. Explicit approve-and-apply also applies verified changes to the local simulator.",
                "finding_ids": [f["id"] for f in state["findings"]],
            }
        )
        review = "acknowledged" if answer.get("approved") else "rejected"
        review_id = state["audit_id"] + ":final"
        self.store.put(
            "human_reviews",
            review_id,
            {
                "status": review,
                "comment": answer.get("comment", ""),
                "timestamp": now(),
                "applied_changes": False,
            },
            state["audit_id"],
        )
        findings = state["findings"]
        for finding in findings:
            finding["review_status"] = review
            self.store.put(
                "audit_findings", finding["id"], finding, state["audit_id"], state["document_version_id"]
            )
        self.update(state["audit_id"], findings=findings)
        return {"review_status": review, "findings": findings}
