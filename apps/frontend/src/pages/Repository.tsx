import { useMutation,useQuery } from "@tanstack/react-query";
import {
Layers3,
Loader2,
Play
} from "lucide-react";
import { useEffect,useState } from "react";
import { Button } from "../components/ui/button";
import { api,post,time } from "../services/api";

import { Badge,Empty,ErrorBox,Json,PageTitle } from "../components/shared";
const baseInputs = {
  amount: 25000,
  new_beneficiary: 1,
  same_owner_verified: 0,
  trusted_device: 1,
  account_age_days: 250,
  failed_logins_24h: 0,
};

export function Repository() {
  const query = useQuery({
    queryKey: ["repository"],
    queryFn: () => api<any[]>("/repository"),
    refetchInterval: 5000,
  });
  const [selectedId, select] = useState<string | null>(null);
  const [tab, setTab] = useState("Business logic");
  const [input, setInput] = useState(baseInputs);
  const [expression, setExpression] = useState("");
  const [editResult, setEditResult] = useState<any>();
  const [editError, setEditError] = useState<unknown>();
  const execute = useMutation({
    mutationFn: () => post("/execute", { inputs: input }),
  });
  const assets = query.data || [];
  const selected =
    assets.find((a) => a.id === selectedId) ||
    assets.find((a) => a.kind === "rule");
  useEffect(() => {
    setExpression(
      selected?.raw.conditions?.map((c: any) => c.expression).join(" AND ") ||
        "",
    );
  }, [selected?.id, selected?.content_hash]);
  const edit = async () => {
    try {
      setEditError(null);
      const r = await fetch(`/mock/internal/rules/${selected.id}`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ expression }),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.message);
      setEditResult(data);
      await post("/synchronization");
    } catch (e) {
      setEditError(e);
    }
  };
  return (
    <>
      <PageTitle
        eyebrow=""
        title="SAS decision knowledge"
        text=""
      />
      <ErrorBox error={query.error} />
      <div className="repository-grid">
        <section className="card tree">
          <div className="card-heading">
            <h3>Decision hierarchy</h3>
            <Badge>
              {assets.filter((a) => a.kind === "rule").length} rules
            </Badge>
          </div>
          {assets
            .filter((a) => a.kind === "decision")
            .map((d) => {
              const ruleSetIds = new Set(
                (d.raw.flow?.steps || [])
                  .filter((step: any) => step.ruleSet)
                  .map((step: any) => step.ruleSet.id),
              );
              const rules = assets
                .filter((a) => a.kind === "rule" && ruleSetIds.has(a.parent_id))
                .sort((a, b) => a.raw.name.localeCompare(b.raw.name));
              return (
                <div key={d.id}>
                  <div className="tree-decision">
                    <Layers3 size={17} />
                    <span>{d.raw.name.replaceAll("_", " ")}</span>
                  </div>
                  {rules.map((rule) => (
                    <button
                      key={rule.id}
                      className={`tree-rule ${selected?.id === rule.id ? "selected" : ""}`}
                      onClick={() => select(rule.id)}
                    >
                      <span className="tree-dot" />
                      {rule.raw.name}
                    </button>
                  ))}
                </div>
              );
            })}
        </section>
        <section className="card rule-detail">
          {selected ? (
            <>
              <div className="card-heading">
                <div className="rule-title">
                  <Badge tone="blue">{selected.kind.replace("_", " ")}</Badge>
                  <h3>{selected.raw.name.replaceAll("_", " ")}</h3>
                </div>
                <Badge>Revision {selected.revision}</Badge>
              </div>
              <div className="tabs">
                {[
                  "Business logic",
                  "Original SAS JSON",
                  "Canonical AST",
                  "Simulator edit",
                ].map((name) => (
                  <button
                    key={name}
                    className={tab === name ? "active" : ""}
                    onClick={() => setTab(name)}
                  >
                    {name}
                  </button>
                ))}
              </div>
              <div className="rule-body">
                {tab === "Business logic" && (
                  <>
                    <div className="eyebrow">BUSINESS SEARCH TEXT</div>
                    <p>{selected.search_text}</p>
                    {selected.raw.conditions && (
                      <div className="logic-box">
                        {selected.raw.conditions.map((c: any, i: number) => (
                          <code key={i}>{c.expression}</code>
                        ))}
                      </div>
                    )}
                    <dl>
                      <div>
                        <dt>Object ID</dt>
                        <dd className="mono">{selected.id}</dd>
                      </div>
                      <div>
                        <dt>Last synchronized</dt>
                        <dd>{time(selected.sync_time)}</dd>
                      </div>
                      <div>
                        <dt>Embedding model</dt>
                        <dd>qwen3-embedding</dd>
                      </div>
                    </dl>
                  </>
                )}
                {tab === "Original SAS JSON" && <Json value={selected.raw} />}{" "}
                {tab === "Canonical AST" && (
                  <Json
                    value={
                      selected.canonical || {
                        note: "Select an individual rule to inspect its normalized AST.",
                      }
                    }
                  />
                )}
                {tab === "Simulator edit" && (
                  <>
                    <p>
                      This explicit edit creates new rule-set and decision
                      revisions in the synthetic simulator. It is separate from
                      recommendations.
                    </p>
                    {selected.kind === "rule" ? (
                      <>
                        <label>
                          Supported SAS condition
                          <textarea
                            value={expression}
                            onChange={(e) => setExpression(e.target.value)}
                          />
                        </label>
                        <Button onClick={edit}>
                          Save simulator revision
                        </Button>
                        <ErrorBox error={editError} />
                        {editResult && (
                          <div className="success">
                            Created rule-set revision{" "}
                            {editResult.rule_set_revision}. Incremental
                            synchronization scheduled.
                          </div>
                        )}
                      </>
                    ) : (
                      <p>Select a rule first.</p>
                    )}
                  </>
                )}
              </div>
            </>
          ) : (
            <Empty
              title="Waiting for the SAS index"
              text="Run synchronization to load the decision repository."
            />
          )}
        </section>
      </div>
      <section className="card transfer-card">
        <div className="card-heading">
          <div>
            <h3>Execute a banking transfer</h3>
            <p>
              Real HTTP scoring against the current published mock revision.
            </p>
          </div>
          <Badge tone="blue">SAS MAS</Badge>
        </div>
        <div className="transfer-inputs">
          {Object.entries(input).map(([name, value]) => (
            <label key={name}>
              {name.replaceAll("_", " ")}
              <input
                type="number"
                min="0"
                step={name === "amount" ? ".01" : "1"}
                value={value}
                onChange={(e) =>
                  setInput({ ...input, [name]: Number(e.target.value) })
                }
              />
            </label>
          ))}
        </div>
        <div className="transfer-bottom">
          <Button onClick={() => execute.mutate()} disabled={execute.isPending}>
            {execute.isPending ? (
              <Loader2 size={16} className="spin" />
            ) : (
              <Play size={16} />
            )}
            Execute transfer
          </Button>
          {execute.data && (
            <div className="execution-output">
              <Badge
                tone={
                  execute.data.output.decision_action === "continue"
                    ? "green"
                    : "amber"
                }
              >
                {execute.data.output.decision_action}
              </Badge>
              <span>{execute.data.output.reason_code}</span>
              <small>
                Revision {execute.data.diagnostics?.decision_revision}
              </small>
            </div>
          )}
        </div>
        <ErrorBox error={execute.error} />
      </section>
    </>
  );
}
