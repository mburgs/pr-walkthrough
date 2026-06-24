import { useEffect } from "react";
import { SessionProvider, useSession } from "./contexts/SessionContext";
import SessionShell from "./components/SessionShell";
import QuestionTracker from "./components/QuestionTracker";
import { exportTranscript } from "./lib/exportTranscript";

const DEFAULT_PR_URL = "https://github.com/example-org/auth-service/pull/142";

// "#session=<sid>" resumes; "?pr=<url>" overrides the default for a fresh session.
function readBootHints(): { sid?: string; prUrl?: string } {
  const hash = window.location.hash.replace(/^#/, "");
  const sid = new URLSearchParams(hash).get("session") || undefined;
  const prUrl = new URLSearchParams(window.location.search).get("pr") || undefined;
  return { sid, prUrl };
}

function AppContent() {
  const { session, loading, error, initSession, resumeSession } = useSession();

  useEffect(() => {
    const { sid, prUrl } = readBootHints();
    if (sid) {
      resumeSession(sid);
    } else {
      initSession(prUrl ?? DEFAULT_PR_URL);
    }
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
        <span style={{ flex: 1 }} />
        <button
          aria-label="Export transcript"
          onClick={() => exportTranscript(session.plan.session_id, session.plan)}
          style={{ background: "transparent", color: "var(--text-muted)", border: "1px solid var(--border)", padding: "4px 10px", borderRadius: "var(--radius)", cursor: "pointer", fontSize: 12 }}
        >
          Export transcript
        </button>
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
