import { useMutation, useQuery } from "@tanstack/react-query";
import { ArrowUp, Loader2, Plus, SquarePen } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api, post } from "../services/api";
import { Audits } from "./Audits";
import { ChatTurn } from "../components/ChatTurn";

type UploadedDocument = {
  document_id: string;
  version_id: string;
  version: number;
  parsed: { title: string; filename: string; format: string };
};

export function Documents({ maxUploadMb }: { maxUploadMb?: number }) {
  const [linkedAudit] = useState(() => new URLSearchParams(window.location.search).get("audit"));
  const [saved] = useState(() => {
    try {
      return JSON.parse(sessionStorage.getItem("policyguard-investigation") || "null");
    } catch { return null; }
  });
  const [document, setDocument] = useState<UploadedDocument | null>(
    linkedAudit && linkedAudit !== saved?.auditId ? null : saved?.document || null
  );
  const [auditId, setAuditId] = useState<string | null>(linkedAudit || saved?.auditId || null);
  const [validationError, setValidationError] = useState<Error | null>(null);
  const [investigating, setInvestigating] = useState(false);
  useEffect(() => {
    sessionStorage.setItem("policyguard-investigation", JSON.stringify({ document, auditId }));
    const url = new URL(window.location.href);
    if (auditId) url.searchParams.set("audit", auditId);
    else url.searchParams.delete("audit");
    window.history.replaceState(null, "", url);
  }, [document, auditId]);
  const restoredDocument = useQuery({
    queryKey: ["audit-document", auditId],
    enabled: !!auditId && !document,
    queryFn: async () => {
      const audit = await api(`/audits/${auditId}`);
      const versions = await api<any[]>(`/documents/${audit.document_id}/versions`);
      const version = versions.find((v) => v.id === audit.document_version_id);
      if (!version) throw new Error("The investigation's document version could not be found.");
      return { document_id: audit.document_id, version_id: version.id, version: version.version, parsed: version.parsed } as UploadedDocument;
    },
  });
  useEffect(() => {
    if (restoredDocument.data) setDocument(restoredDocument.data);
  }, [restoredDocument.data]);
  const onStatusChange = useCallback((status: string) => {
    setInvestigating(status === "running");
  }, []);
  const start = useMutation({
    mutationFn: (uploaded: UploadedDocument) => {
      return post<{ id: string }>("/audits", {
        document_id: uploaded.document_id,
        version_id: uploaded.version_id,
        review_requirements: true,
      });
    },
    onSuccess: (data) => {
      setAuditId(data.id);
      setInvestigating(true);
    },
  });
  const upload = useMutation({
    mutationFn: async (file: File) => {
      const form = new FormData();
      form.append("file", file);
      return api<UploadedDocument>("/documents", { method: "POST", body: form });
    },
    onSuccess: (data) => {
      setDocument(data);
      start.mutate(data);
    },
  });
  const busy = upload.isPending || start.isPending || investigating || restoredDocument.isFetching;
  const resetDocument = () => {
    if (busy) return;
    setDocument(null);
    setAuditId(null);
    setValidationError(null);
    upload.reset();
    start.reset();
  };
  const chooseFile = (file?: File) => {
    if (!file || busy) return;
    setDocument(null);
    setAuditId(null);
    setValidationError(null);
    upload.reset();
    start.reset();
    if (!/\.(pdf|docx)$/i.test(file.name)) {
      setValidationError(new Error("Please upload a PDF or DOCX document."));
      return;
    }
    if (maxUploadMb && file.size > maxUploadMb * 1024 * 1024) {
      setValidationError(new Error(`The document must be ${maxUploadMb} MB or smaller.`));
      return;
    }
    upload.mutate(file);
  };
  return (
    <div className="document-analysis chat-only-workspace">
      <header className="analysis-page-header">
        <h1>PolicyGuard</h1>
        {(auditId || document) && <button className="new-chat-button" aria-label="New chat" title="New chat" onClick={resetDocument} disabled={busy}><SquarePen size={19} /></button>}
      </header>
      {!auditId && (
        <div className="chat-welcome" onDragOver={e => e.preventDefault()} onDrop={e => { e.preventDefault(); chooseFile(e.dataTransfer.files[0]); }}>
          {!document && !upload.isPending && <div className="chat-welcome-heading"><h2>What policy are we investigating?</h2><p>Attach a document, and we’ll work through it together.</p></div>}
          {(document || upload.isPending) && <div className="investigation-conversation" role="log" aria-label="Investigation conversation">
            {document && <ChatTurn role="user"><p>Investigate this policy.</p><p className="chat-attachment">{document.parsed.filename}</p></ChatTurn>}
            <ChatTurn>{busy ? <p className="chat-working" role="status"><Loader2 size={16} className="spin" />{upload.isPending ? "Reading your document…" : "Opening your investigation…"}</p> : <><p>The investigation could not start. Your document is ready to retry.</p><div className="chat-suggestions"><button className="chat-suggestion" onClick={() => document && start.mutate(document)}>Retry investigation</button><button className="chat-suggestion" onClick={resetDocument}>Change file</button></div></>}</ChatTurn>
          </div>}
          {(validationError || upload.error || start.error || restoredDocument.error) && <ChatTurn><p role="alert">{(validationError || upload.error || start.error || restoredDocument.error)?.message}</p></ChatTurn>}
          <form className="conversation-composer welcome-composer" aria-label="Chat composer" onSubmit={e => e.preventDefault()}>
            <textarea aria-label="Message PolicyGuard" rows={2} readOnly placeholder={busy ? "PolicyGuard is working…" : "Attach a policy document to begin…"} />
            <div className="conversation-composer-bottom"><label className="attach-policy" title="Attach a policy document"><Plus size={21} /><input aria-label="Upload policy document" type="file" accept=".pdf,.docx" disabled={busy} onChange={e => { chooseFile(e.target.files?.[0]); e.target.value = ""; }} /></label><small>PDF or DOCX{maxUploadMb ? ` · ${maxUploadMb} MB max` : ""}</small><button aria-label="Send message" disabled><ArrowUp size={19} /></button></div>
          </form>
        </div>
      )}
      {auditId && (
        <div className="document-investigation">
          {restoredDocument.error && <ChatTurn><p role="alert">{restoredDocument.error.message}</p></ChatTurn>}
          <Audits
            key={auditId}
            selected={auditId}
            onStatusChange={onStatusChange}
            document={document}
          />
        </div>
      )}
    </div>
  );
}
