import { useEffect } from "react";
import { SessionProvider, useSession } from "./contexts/SessionContext";
import SessionShell from "./components/SessionShell";
import QuestionTracker from "./components/QuestionTracker";

const DEFAULT_PR_URL = "https://github.com/example-org/auth-service/pull/142";

function AppContent() {
  const { session, loading, error, initSession } = useSession();

  useEffect(() => {
    // Auto-load the fixture session on mount
    initSession(DEFAULT_PR_URL);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  if (loading) {
    return (
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", color: "var(--text-muted)" }}>
        Loading session…
      </div>
    );
  }

  if (error) {
    return (
      <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", height: "100%", gap: 12 }}>
        <div style={{ color: "var(--danger)" }}>Error: {error}</div>
        <button
          style={{ background: "var(--accent)", color: "#fff", border: "none", padding: "8px 16px", borderRadius: "var(--radius)", cursor: "pointer" }}
          onClick={() => initSession(DEFAULT_PR_URL)}
        >
          Retry
        </button>
      </div>
    );
  }

  if (!session) return null;

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <header style={{ background: "var(--surface)", borderBottom: "1px solid var(--border)", padding: "8px 16px", display: "flex", alignItems: "center", gap: 12, flexShrink: 0 }}>
        <span style={{ fontWeight: 700, fontSize: 14, color: "var(--text)" }}>pr-walkthrough</span>
        <span style={{ color: "var(--text-muted)", fontSize: 13 }}>
          {session.plan.pr.repo} #{session.plan.pr.number}
        </span>
      </header>
      <div style={{ flex: 1, overflow: "hidden", display: "flex" }}>
        <div style={{ flex: 1, overflow: "hidden" }}>
          <SessionShell />
        </div>
        <div style={{ width: 320, borderLeft: "1px solid var(--border)", overflowY: "auto", background: "var(--surface)", flexShrink: 0 }}>
          <QuestionTracker />
        </div>
      </div>
    </div>
  );
}

export default function App() {
  return (
    <SessionProvider>
      <AppContent />
    </SessionProvider>
  );
}
