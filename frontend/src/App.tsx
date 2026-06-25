import { useEffect } from "react";
import { SessionProvider, useSession } from "./contexts/SessionContext";
import SessionShell from "./components/SessionShell";
import { exportTranscript } from "./lib/exportTranscript";
import styles from "./App.module.css";

const DEFAULT_PR_URL = "https://github.com/example-org/auth-service/pull/142";

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
    if (sid) resumeSession(sid);
    else initSession(prUrl ?? DEFAULT_PR_URL);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  if (loading) {
    return (
      <div className={styles.fullCenter}>
        <div className={styles.loadingMark}>
          <span className={styles.dotPulse} />
          <span className={styles.loadingText}>narrating pull request…</span>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className={styles.fullCenter}>
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 12, maxWidth: 480, textAlign: "center" }}>
          <div className={styles.errorLabel}>Couldn’t load this PR</div>
          <div className={styles.errorMsg}>{error}</div>
          <button className={styles.retryBtn} onClick={() => initSession(DEFAULT_PR_URL)}>
            Retry
          </button>
        </div>
      </div>
    );
  }

  if (!session) return null;

  return (
    <div className={styles.app}>
      <header className={styles.header}>
        <div className={styles.brand}>
          <span className={styles.brandMark}>pr</span>
          <span className={styles.brandName}>walkthrough</span>
        </div>
        <div className={styles.separator} />
        <a
          className={styles.prRef}
          href={session.plan.pr.url}
          target="_blank"
          rel="noreferrer"
          title={session.plan.pr.title}
        >
          <span className={styles.prRepo}>{session.plan.pr.repo}</span>
          <span className={styles.prHash}>#{session.plan.pr.number}</span>
          <span className={styles.prTitle}>{session.plan.pr.title}</span>
        </a>
        <span style={{ flex: 1 }} />
        <button
          className={styles.headerBtn}
          onClick={() => exportTranscript(session.plan.session_id, session.plan)}
          aria-label="Export transcript"
          title="Download a markdown transcript of this walkthrough"
        >
          ↓ Transcript
        </button>
      </header>
      <main className={styles.body}>
        <SessionShell />
      </main>
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
