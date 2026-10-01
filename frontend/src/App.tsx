import { useEffect, useState } from "react";
import { useAuth } from "./auth";
import IngestReview from "./components/IngestReview";
import Kanban from "./components/Kanban";
import LeadDrawer from "./components/LeadDrawer";
import LeadsTable from "./components/LeadsTable";
import Login from "./components/Login";
import QueryBar from "./components/QueryBar";
import type { Lead } from "./types";

const TABS = [
  { id: "pipeline", label: "Pipeline" },
  { id: "leads", label: "Leads" },
  { id: "ingest", label: "Ingest" },
] as const;
type Tab = (typeof TABS)[number]["id"];

function tabFromHash(): Tab {
  const hash = window.location.hash.replace("#", "");
  return TABS.find((t) => t.id === hash)?.id ?? "pipeline";
}

export default function App() {
  const { user, ready, logout } = useAuth();
  const [tab, setTab] = useState<Tab>(tabFromHash);
  const [openLead, setOpenLead] = useState<Lead | null>(null);

  useEffect(() => {
    const onHash = () => setTab(tabFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  if (!ready) return <div className="splash muted">Loading…</div>;
  if (!user) return <Login />;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand"><span className="brand-mark" aria-hidden /> Clearpipe</div>
        <nav aria-label="Main">
          {TABS.map((t) => (
            <a key={t.id} href={`#${t.id}`} className={tab === t.id ? "active" : ""} aria-current={tab === t.id ? "page" : undefined}>
              {t.label}
            </a>
          ))}
        </nav>
        <div className="user">
          <span className="muted">{user.full_name} · {user.role}</span>
          <button className="ghost" onClick={logout}>Sign out</button>
        </div>
      </header>

      <main>
        <QueryBar />
        {tab === "pipeline" && <Kanban onOpenLead={setOpenLead} />}
        {tab === "leads" && <LeadsTable onOpenLead={setOpenLead} />}
        {tab === "ingest" && <IngestReview />}
      </main>

      {openLead && <LeadDrawer lead={openLead} onClose={() => setOpenLead(null)} />}
    </div>
  );
}
