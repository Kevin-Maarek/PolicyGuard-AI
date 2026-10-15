import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowUp, Loader2 } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { ChatTurn } from "../components/ChatTurn";
import { api, money, post } from "../services/api";

const progress: Record<string, string> = {
  document_intake: "Reading your document", requirement_extraction: "Finding policy requirements",
  requirement_validation: "Checking the policy against its source", candidate_retrieval: "Looking for related rules",
  asset_investigation: "Reading the related rules", rule_planning: "Preparing the matches",
  business_term_mapping: "Checking business terms", logical_analysis: "Comparing the proposed logic",
  test_generation: "Preparing test cases", decision_execution: "Running the test cases",
  evidence_verification: "Verifying the results", impact_classification: "Assessing the change",
  report_generation: "Writing the findings", human_review: "Preparing your review",
};
const conclusions: Record<string, string> = {
  MODIFY_RULE: "Rule change recommended", NEW_RULE: "New rule recommended", ADD_RULE: "New rule recommended",
  NO_CHANGE: "No change selected", NEEDS_REVIEW: "The proposal needs more review",
  ALREADY_COVERED: "This policy is already covered", CONFLICT: "A conflicting rule was found",
  INSUFFICIENT_EVIDENCE: "There is not enough evidence yet",
};
const actionName = (value: string) => value === "need_banker_aprove" ? "Banker approval" : value === "continue" ? "Continue" : value;
const matchScore = (value: unknown) => typeof value === "number" && Number.isFinite(value) ? value.toFixed(4) : "Unavailable";
function condition(node: any): string {
  if (node.operator === "NOT") return `NOT (${condition(node.children[0])})`;
  if (node.children?.length) return `(${node.children.map(condition).join(` ${node.operator} `)})`;
  return `${node.field} ${node.operator} ${node.value}`;
}
function ChatAction({ children, onClick, disabled }: { children: ReactNode; onClick: () => void; disabled?: boolean }) {
  return <button type="button" className="chat-suggestion" disabled={disabled} onClick={onClick}>{children}</button>;
}

export function Audits({ selected: id, onStatusChange, document }: {
  selected: string; onStatusChange: (status: string) => void;
  document?: { parsed: { title: string; filename: string }; version: number } | null;
}) {
  const client = useQueryClient();
  const query = useQuery({ queryKey: ["audit", id], queryFn: () => api(`/audits/${id}`),
    refetchInterval: q => ["running", "awaiting_review"].includes((q.state.data as any)?.status)
      || ["pending", "applying"].includes((q.state.data as any)?.rule_application?.status) ? 2000 : false });
  const audit = query.data;
  const phase = audit?.interrupt?.kind;
  const reviewingPolicy = audit?.status === "awaiting_review" && phase === "requirements";
  const groups = useQuery({ queryKey: ["chat-candidates", id, audit?.report_generated_at, phase],
    queryFn: () => api<any[]>(`/audits/${id}/rule-candidates`),
    enabled: !!audit && (phase === "rule_planning" || !!audit.findings?.length || !!Object.keys(audit.rule_selections || {}).length) });
  const settings = useQuery({ queryKey: ["settings"], queryFn: () => api("/settings") });
  const [message, setMessage] = useState(() => sessionStorage.getItem(`policyguard-chat-draft-${id}`) || "");
  const [showTests, setShowTests] = useState<Record<string, boolean>>({});
  const [simulation, setSimulation] = useState<any>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const followConversation = useRef(true);
  useEffect(() => { sessionStorage.setItem(`policyguard-chat-draft-${id}`, message); }, [id, message]);
  useEffect(() => { if (audit?.status) onStatusChange(audit.status); }, [audit?.status, onStatusChange]);
  useEffect(() => {
    const onScroll = () => { followConversation.current = window.innerHeight + window.scrollY >= window.document.documentElement.scrollHeight - 180; };
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);
  const draft = audit?.clarification;
  const opening = useMutation({ mutationFn: () => post(`/audits/${id}/clarification/start`),
    onSuccess: clarification => client.setQueryData(["audit", id], (old: any) => ({ ...old, clarification, clarification_error: null })) });
  const attemptedOpening = useRef(false);
  useEffect(() => {
    if (reviewingPolicy && !draft?.messages?.length && !audit.clarification_error && !attemptedOpening.current) {
      attemptedOpening.current = true; opening.mutate();
    }
  }, [reviewingPolicy, draft?.messages?.length, audit?.clarification_error]);
  const operation = useMutation({
    mutationFn: async ({ kind, body }: { kind: string; body?: any }) => ({ kind, result: await post(`/audits/${id}/${kind}`, body) }),
    onSuccess: ({ kind, result }) => {
      setMessage("");
      if (kind === "chat") client.setQueryData(["audit", id], (old: any) => ({ ...old, chat_messages: result.messages }));
      if (kind === "clarify") client.setQueryData(["audit", id], (old: any) => ({ ...old, clarification: result }));
      if (kind === "simulate") setSimulation(result);
      if (["review", "select-rule"].includes(kind)) {
        setSimulation(null);
        client.setQueryData(["audit", id], (old: any) => ({ ...old, status: "running", interrupt: null,
          ...(kind === "select-rule" ? { findings: [], current_node: "asset_investigation" } : {}) }));
      }
      client.invalidateQueries({ queryKey: ["audit", id] });
      if (["add-to-sas", "apply-rules"].includes(kind)) client.invalidateQueries({ queryKey: ["repository"] });
    },
    onError: () => client.invalidateQueries({ queryKey: ["audit", id] }),
  });
  const application = audit?.rule_application;
  const proposals = audit?.findings?.filter((f: any) => f.evidence_status === "verified"
    && (f.application_operation || ["MODIFY_RULE", "NEW_RULE"].includes(f.classification))) || [];
  const canApply = settings.data?.sas_apply_enabled && proposals.length > 0
    && !Object.keys(audit?.sas_additions || {}).length && !Object.keys(audit?.rule_updates || {}).length;
  const busy = operation.isPending || opening.isPending || audit?.status === "running"
    || ["pending", "applying"].includes(application?.status);
  useEffect(() => {
    if (application?.status === "applied") client.invalidateQueries({ queryKey: ["repository"] });
  }, [application?.status, client]);
  const canTalk = reviewingPolicy ? !!draft?.messages?.length && (draft.revision || 0) < 30
    : audit?.status === "completed" || (audit?.status === "awaiting_review" && ["rule_planning", "findings"].includes(phase));
  const invoke = (kind: string, body?: any) => {
    if (busy) return;
    followConversation.current = true;
    operation.mutate({ kind, body });
  };
  const review = (approved: boolean, apply = !!canApply) => invoke("review", { approved,
    ...(phase === "findings" && approved && apply ? { apply_rules: true } : {}),
    ...(reviewingPolicy && approved && draft?.revision ? { clarification_revision: draft.revision } : {}) });
  const ask = (text: string) => {
    if (!text.trim() || busy || !canTalk) return;
    setMessage(text);
    invoke(reviewingPolicy ? "clarify" : "chat", { message: text.trim(), revision: reviewingPolicy ? draft?.revision || 0 : audit.chat_messages?.length || 0 });
  };
  const send = () => {
    const command = message.trim().toLowerCase().replace(/[.!]+$/, "");
    const approval = ["approve", "approve report", "approve the report", "approve and apply rules", "approve rule", "approve update", "approve creation", "approve new rule"].includes(command);
    if (busy || !canTalk) return;
    if (reviewingPolicy && ["continue", "confirm policy", "find related rules"].includes(command)) return review(true);
    if (phase === "findings" && approval) return review(true, !!canApply || command === "approve and apply rules");
    if (phase === "findings" && command === "approve report only") return review(true, false);
    if (["requirements", "findings"].includes(phase) && ["reject", "reject report", "reject policy"].includes(command)) return review(false);
    const proposal = audit?.chat_messages?.at(-1)?.proposal;
    if (proposal && canPropose && ["confirm and test", "confirm and test proposal", "test proposal", "confirm no change"].includes(command)) {
      return invoke("select-rule", { ...proposal, confirmed_interpretation: true });
    }
    if (command === "run sandbox" && audit?.findings?.some((f: any) => f.evidence_status === "verified")) return invoke("simulate");
    if (audit?.status === "completed" && (approval || ["apply rules", "apply approved rules", "retry applying rules"].includes(command))) {
      return invoke("apply-rules", { confirmed: true });
    }
    if (audit?.status === "completed" && ["save as sas draft", "save draft"].includes(command)) {
      const eligible = audit.findings.filter((f: any) => ["MODIFY_RULE", "NEW_RULE"].includes(f.classification) && f.evidence_status === "verified" && f.review_status === "acknowledged" && audit.sas_additions?.[f.id]?.status !== "saved");
      if (eligible.length === 1) return invoke("add-to-sas", { finding_id: eligible[0].id, confirmed: true });
    }
    ask(message);
  };
  const canPropose = !busy && ["awaiting_review", "completed"].includes(audit?.status) && !reviewingPolicy
    && !Object.keys(audit?.sas_additions || {}).length && !Object.keys(audit?.rule_updates || {}).length && !application;
  const latestAssistant = audit?.chat_messages?.at(-1)?.role === "assistant" ? audit.chat_messages.length - 1 : -1;
  function candidateName(ruleId: string) { return groups.data?.flatMap(g => g.candidates).find(c => c.id === ruleId)?.name || ruleId; }
  const conversationMessages = (target: string) => audit?.chat_messages?.map((m: any, i: number) => m.phase === target && <ChatTurn key={`chat:${i}`} role={m.role}>
    <p className="discussion-text">{m.content}</p>
    {m.proposal && <>
      <p>{m.proposal.operation === "create" ? `New rule: ${m.proposal.rule_name}` : m.proposal.operation === "no_change" ? "Record no change for this requirement." : `Update: ${candidateName(m.proposal.rule_id)}`}</p>
      {m.proposal.expression && <pre><code>{m.proposal.expression}</code></pre>}
      {m.proposal.expected_action && <p>Action: {actionName(m.proposal.expected_action)}. Effective date: {m.proposal.effective_date || "Unspecified"}.</p>}
      {m.proposal.operation === "create" && <p>Rule set: {groups.data?.find(g => g.requirement_id === m.proposal.requirement_id)?.rule_sets?.find((s: any) => s.id === m.proposal.rule_set_id)?.name || m.proposal.rule_set_id}.</p>}
      {i === latestAssistant && canPropose && !(Date.parse(audit.rule_selections?.[m.proposal.requirement_id]?.timestamp || "") >= Date.parse(m.timestamp || "")) && <div className="chat-suggestions"><ChatAction disabled={!!message.trim()} onClick={() => invoke("select-rule", { ...m.proposal, confirmed_interpretation: true })}>
        {m.proposal.operation === "no_change" ? "Confirm no change" : "Confirm and test proposal"}
      </ChatAction></div>}
    </>}
  </ChatTurn>);
  useEffect(() => {
    if (followConversation.current) bottom.current?.scrollIntoView({ block: "end" });
  }, [audit?.status, phase, draft?.revision, audit?.chat_messages?.length, audit?.report_generated_at, operation.isPending, groups.data, simulation]);
  const policy = draft?.requirements || audit?.requirements || [];
  const error = query.error || opening.error || operation.error || groups.error || settings.error;
  return <div className="pure-chat">
    <div className="investigation-conversation" role="log" aria-label="Investigation conversation" aria-live="polite" aria-relevant="additions text">
      <ChatTurn role="user"><p>Investigate this policy.</p><p className="chat-attachment">{document?.parsed.filename || document?.parsed.title || "Policy document"}</p></ChatTurn>
      {(draft?.messages || []).map((m: any, i: number) => <ChatTurn key={`policy:${i}`} role={m.role}><p className="discussion-text">{m.content}</p></ChatTurn>)}
      {!!policy.length && <ChatTurn>
        <p>{draft?.revision ? "Here is the policy with your clarifications:" : "Here is the policy I found:"}</p>
        {policy.map((r: any) => <div key={r.requirement_id} className="chat-policy">
          <blockquote>{r.original_text}</blockquote><p>{actionName(r.expected_action)} when:</p><pre><code>{condition(r.conditions)}</code></pre>
          {r.exceptions?.length > 0 && <p>Exceptions: {r.exceptions.join("; ")}</p>}
          <p>Effective date: {r.effective_date || "Not specified"}.</p>
          {!!r.ambiguities?.length && <p>I still need clarification: {r.ambiguities.join("; ")}</p>}
        </div>)}
        {reviewingPolicy && <><p>Tell me what to clarify, or confirm the policy and I’ll look for related rules.</p>
          <div className="chat-suggestions"><ChatAction disabled={busy || !!message.trim()} onClick={() => review(true)}>Confirm policy and find rules</ChatAction><ChatAction disabled={busy} onClick={() => review(false)}>Reject interpretation</ChatAction></div></>}
      </ChatTurn>}
      {Array.isArray(audit?.requirements) && !policy.length && <ChatTurn><p>I did not find any supported policy requirements in this document.</p>{reviewingPolicy && <div className="chat-suggestions"><ChatAction disabled={busy} onClick={() => review(true)}>Continue</ChatAction><ChatAction disabled={busy} onClick={() => review(false)}>Reject interpretation</ChatAction></div>}</ChatTurn>}
      {reviewingPolicy && !draft?.messages?.length && (audit?.clarification_error || opening.error) && <ChatTurn><p>I could not prepare the policy introduction. {audit.clarification_error}</p><ChatAction disabled={busy} onClick={() => opening.mutate()}>Retry introduction</ChatAction></ChatTurn>}
      {draft?.status === "applied" && <ChatTurn role="user"><p>Confirm the policy and find related rules.</p></ChatTurn>}
      {groups.data?.map(group => <ChatTurn key={group.requirement_id}>
        <p>{group.candidates.length ? "This is the best matching rule for your policy. Similarity alone does not prove coverage." : "I did not find a related rule for this policy."}</p>
        {groups.data.length > 1 && <blockquote>{audit.requirements?.find((r: any) => r.requirement_id === group.requirement_id)?.original_text}</blockquote>}
        {group.candidates.map((c: any) => <div key={c.id} className="chat-rule"><p><strong>{c.name}</strong></p><pre><code>{c.expression}</code></pre>
          <p className="chat-meta">BM25: {matchScore(c.bm25_score)} · VECTOR (cosine similarity): {matchScore(c.vector_score)}</p>
          <p className="chat-meta">{c.rule_set_name} · Revision {c.revision} · {actionName(c.action)}</p>
          {!c.supported && <p>{c.limitation}</p>}
          {phase === "rule_planning" && c.supported && <div className="chat-suggestions"><ChatAction disabled={busy || !!message.trim()} onClick={() => ask(`Prepare an update to ${c.name} for ${groups.data.length > 1 ? `policy ${group.requirement_id}` : "this policy"}, preserving all confirmed conditions and exceptions.`)}>Update {c.name.split(" ")[0]}</ChatAction></div>}
        </div>)}
        {phase === "rule_planning" && <><p>Ask me to update this rule or prepare a new rule. You can describe changes in your own words.</p><div className="chat-suggestions">
          <ChatAction disabled={busy || !!message.trim()} onClick={() => ask(`Prepare a new rule for ${groups.data.length > 1 ? `policy ${group.requirement_id}` : "this policy"}. Suggest a name and compatible rule set, preserving the confirmed conditions and exceptions.`)}>Create a new rule</ChatAction>
          <ChatAction disabled={busy || !!message.trim()} onClick={() => ask(`Prepare a no-change proposal for ${groups.data.length > 1 ? `policy ${group.requirement_id}` : "this policy"}.`)}>Keep rules unchanged</ChatAction>
        </div></>}
      </ChatTurn>)}
      {conversationMessages("rule_planning")}
      {Object.values(audit?.rule_selections || {}).map((s: any) => <ChatTurn key={s.requirement_id} role="user"><p>{s.operation === "create" ? `Test a new rule named ${s.rule_name}.` : s.operation === "no_change" ? "Confirm no change for this requirement." : `Test the update to ${s.candidate?.name || candidateName(s.rule_id)}.`}</p>{s.expression && <pre><code>{s.expression}</code></pre>}</ChatTurn>)}
      {audit?.findings?.map((f: any) => <ChatTurn key={f.id}>
        <p><strong>{conclusions[f.classification] || f.classification}</strong></p><p>{f.explanation || f.recommendation}</p>
        {f.existing_expression && <><p>Current condition in {f.rule_name}:</p><pre><code>{f.existing_expression}</code></pre></>}
        {f.proposed_expression && <><p>Proposed condition:</p><pre><code>{f.proposed_expression}</code></pre></>}
        <p>{f.tests.length} tests · {f.tests.filter((t: any) => t.mismatch).length} differences · {f.evidence_status} evidence.</p>{f.coverage_reason && <p>{f.coverage_reason}</p>}
        {f.classification === "NEEDS_REVIEW" && f.application_operation && <p>The tests show unchanged decision outcomes. You can still approve and apply this confirmed {f.application_operation === "create" ? "new rule" : "rule update"}.</p>}
        <div className="chat-suggestions">{!!f.tests.length && <ChatAction onClick={() => setShowTests(v => ({ ...v, [f.id]: !v[f.id] }))}>{showTests[f.id] ? "Hide test cases" : "Show test cases"}</ChatAction>}{f.evidence_status === "verified" && <ChatAction disabled={busy} onClick={() => invoke("simulate")}>Run sandbox</ChatAction>}</div>
        {showTests[f.id] && <div className="chat-table"><table><thead><tr><th>Scenario</th><th>Amount (ILS)</th><th>Current</th><th>Expected</th><th>Result</th></tr></thead><tbody>{f.tests.map((t: any) => <tr key={t.id}><td>{t.label}</td><td>{money(t.inputs.amount)}</td><td>{t.output.decision_action}</td><td>{t.expected}</td><td>{t.mismatch ? "Difference" : "Unchanged"}</td></tr>)}</tbody></table></div>}
      </ChatTurn>)}
      {simulation && <ChatTurn><p><strong>Sandbox results</strong></p>{simulation.simulations.map((s: any, i: number) => <p key={i}>{s.results.filter((r: any) => r.changed).length} changed outcomes; {s.results.filter((r: any) => !r.changed).length} unchanged outcomes; {s.results.filter((r: any) => r.before.decision_action === "need_banker_aprove" && r.after.decision_action !== "need_banker_aprove").length} approval regressions.</p>)}<p>Active rules are unchanged.</p></ChatTurn>}
      {phase === "findings" && <ChatTurn><p>{canApply
        ? `Approve and apply will update or create ${proposals.length} tested rule(s) in the active local SAS simulator decision. You can also approve only the report.`
        : "You can approve this report or discuss the findings first. Approval alone keeps active rules unchanged."}</p>
        {!!proposals.length && !settings.isPending && !settings.data?.sas_apply_enabled && <p>Applying active rules is available with simulator writes enabled. SAS Viya supports separate drafts here.</p>}
        <div className="chat-suggestions"><ChatAction disabled={busy || settings.isPending || !!message.trim()} onClick={() => review(true)}>{canApply ? "Approve and apply rules" : "Approve report"}</ChatAction>
          {canApply && <ChatAction disabled={busy || !!message.trim()} onClick={() => review(true, false)}>Approve report only</ChatAction>}
          <ChatAction disabled={busy} onClick={() => review(false)}>Reject report</ChatAction></div></ChatTurn>}
      {conversationMessages("findings")}
      {audit?.status === "completed" && <><ChatTurn role="user"><p>{application ? "Approve and apply the rules." : "Approve the report."}</p></ChatTurn><ChatTurn><p>The report is approved.</p>
        {application?.status === "applied" ? <><p role="status">Rules applied to the active local SAS simulator decision. Decision revision: {application.result.decision_revision}.</p>
          {application.result.changes.map((change: any) => <p key={change.finding_id}>{change.operation === "create" ? "Created" : "Updated"} {change.rule_name} · Rule-set revision {change.rule_set_revision}.</p>)}</>
          : application?.status === "failed" ? <p role="alert">Rules were not confirmed applied: {application.error}</p>
          : application ? <p>Applying the approved rules to the local SAS simulator…</p> : <p>Active rules are unchanged. {canApply ? "Apply the approved rules below to complete the change." : "This approval recorded the report review without applying a rule change."}</p>}
        {canApply && !["pending", "applying", "applied"].includes(application?.status) && <><p>Apply the approved changes to update or create rules in the active local SAS simulator decision.</p>
          <div className="chat-suggestions"><ChatAction disabled={busy || !!message.trim()} onClick={() => invoke("apply-rules", { confirmed: true })}>{application ? "Retry applying rules" : "Apply approved rules"}</ChatAction></div></>}
        {audit.findings.filter((f: any) => ["MODIFY_RULE", "NEW_RULE"].includes(f.classification) && f.evidence_status === "verified" && f.review_status === "acknowledged").map((f: any) => {
          const saved = audit.sas_additions?.[f.id];
          if (application) return null;
          return <div key={f.id}>{saved?.status === "saved" ? <p>Saved SAS draft: {saved.draft_name}. The active decision is unchanged.</p> : <>
            <p>I can save {f.rule_name} as a separate draft in {settings.data?.sas_target === "viya" ? "SAS Viya" : "the local SAS simulator"}. This will not publish it or attach it to an active decision.</p>{saved?.error && <p>{saved.error}</p>}
            <div className="chat-suggestions"><ChatAction disabled={busy || !settings.data?.sas_write_enabled || !!Object.keys(audit.rule_updates || {}).length} onClick={() => invoke("add-to-sas", { finding_id: f.id, confirmed: true })}>{saved ? "Retry saving SAS draft" : "Save as SAS draft"}</ChatAction></div>
          </>}</div>;
        })}
      </ChatTurn></>}
      {conversationMessages("completed")}
      {audit?.report_generated_at && <ChatTurn><a className="chat-download" href={`/api/audits/${id}/report`}>Download the investigation report</a></ChatTurn>}
      {operation.isPending && ["chat", "clarify"].includes(operation.variables.kind) && <ChatTurn role="user"><p>{operation.variables.body.message}</p></ChatTurn>}
      {(query.isPending || busy) && <ChatTurn><p className="chat-working" role="status"><Loader2 size={16} className="spin" />{["pending", "applying"].includes(application?.status) || operation.isPending && operation.variables?.kind === "apply-rules" ? "Applying approved rules…" : operation.isPending ? operation.variables?.kind === "simulate" ? "Running the sandbox…" : operation.variables?.kind === "add-to-sas" ? "Saving your draft…" : "Thinking…" : opening.isPending ? "Reading the policy…" : `${progress[audit?.current_node] || "Starting the investigation"}…`}</p></ChatTurn>}
      {audit?.status === "failed" && <ChatTurn><p>The investigation stopped: {audit.error}</p><p>Start a new chat to try again.</p></ChatTurn>}
      {audit?.status === "rejected" && <ChatTurn><p>This investigation was rejected. You can start a new chat whenever you’re ready.</p></ChatTurn>}
      {error && <ChatTurn><p role="alert">{error instanceof Error ? error.message : String(error)}</p><p>{message.trim() ? "Your message is still in the composer. You can send it again." : "You can retry the action or tell me what to revise."}</p></ChatTurn>}
      <div ref={bottom} />
    </div>
    <form className="conversation-composer" aria-label="Chat composer" onSubmit={e => { e.preventDefault(); send(); }}>
      <label className="sr-only" htmlFor={`chat-${id}`}>Message PolicyGuard</label>
      <textarea id={`chat-${id}`} rows={2} maxLength={2000} value={message} disabled={busy || !canTalk}
        placeholder={busy ? "PolicyGuard is working…" : reviewingPolicy ? draft?.input_suggestion || "Clarify the policy, or type continue…" : canTalk ? "Message PolicyGuard…" : "This investigation has ended"}
        onChange={e => setMessage(e.target.value)} onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send(); } }} />
      <div className="conversation-composer-bottom"><span>Enter to send · Shift + Enter for a new line</span><button type="submit" aria-label="Send message" disabled={busy || !canTalk || !message.trim()}><ArrowUp size={19} /></button></div>
    </form>
  </div>;
}
