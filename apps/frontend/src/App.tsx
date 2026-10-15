import { useQuery } from "@tanstack/react-query";
import {
Database,
FileText,
ShieldCheck
} from "lucide-react";
import { useState } from "react";
import { api } from "./services/api";

import { Documents } from "./pages/Documents";
import { Repository } from "./pages/Repository";
const navigation = [
  { name: "SAS Repository", icon: Database },
  { name: "Document Analysis", icon: FileText },
] as const;
type Page = (typeof navigation)[number]["name"];

export function App() {
  const [page, setPage] = useState<Page>(() =>
    new URLSearchParams(window.location.search).has("audit") ? "Document Analysis" : "SAS Repository"
  );
  const query = useQuery({
    queryKey: ["settings"],
    queryFn: () => api("/settings"),
  });
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a
          className="brand"
          href="#"
          onClick={(e) => {
            e.preventDefault();
            setPage("SAS Repository");
          }}
        >
          <span className="brand-symbol">
            <ShieldCheck size={24} />
          </span>
          <span>
            PolicyGuard<span className="brand-ai">AI</span>
            <small>DECISION INTELLIGENCE</small>
          </span>
        </a>
        <nav aria-label="Main navigation">
          {navigation.map((item) => (
            <button
              key={item.name}
              className={page === item.name ? "active" : ""}
              aria-current={page === item.name ? "page" : undefined}
              title={item.name}
              onClick={() => setPage(item.name)}
            >
              <item.icon size={18} />
              {item.name}
            </button>
          ))}
        </nav>
      </aside>
      <main className={page === "Document Analysis" ? "chat-page" : undefined}>
        <div className="content">
          {page === "SAS Repository" && <Repository />}
          <div hidden={page !== "Document Analysis"}>
            <Documents maxUploadMb={query.data?.max_upload_mb} />
          </div>
        </div>
      </main>
    </div>
  );
}
