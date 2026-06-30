import { useEffect, useRef, useState } from "react";
import { SessionProvider, useSession } from "./contexts/SessionContext";
import SessionShell from "./components/SessionShell";
import { exportTranscript } from "./lib/exportTranscript";
import type { LoadingPhase } from "./contexts/SessionContext";
import styles from "./App.module.css";

function readBootHints(): { sid?: string } {
  const hash = window.location.hash.replace(/^#/, "");
  const sid = new URLSearchParams(hash).get("session") || undefined;
  return { sid };
}

function AppContent() {
  const { session, loading, loadingPhase, error, initSession, resumeSession } = useSession();
  const [regenConfirm, setRegenConfirm] = useState(false);
  const [booted, setBooted] = useState(false);
  const regenTimerRef = useRef<number | null>(null);

  // Clear regen-confirm + its timer whenever the session changes (regenerate
  // succeeded, user navigated, etc.). Without this, a pending "Confirm?"
  // state flashes back into the new session's header before the timer fires.
  useEffect(() => {
    setRegenConfirm(false);
    if (regenTimerRef.current != null) {
      window.clearTimeout(regenTimerRef.current);
      regenTimerRef.current = null;
    }
  }, [session?.plan.session_id]);

  useEffect(() => () => {
    if (regenTimerRef.current != null) window.clearTimeout(regenTimerRef.current);
  }, []);

  useEffect(() => {
    const { sid } = readBootHints();
    if (sid) resumeSession(sid).finally(() => setBooted(true));
    else setBooted(true); // no hint → show the no-session message
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // No session, no hint, no error: the CLI is the entry point now, so
  // this state only appears if someone opened the dev URL directly.
  // Tell them how to launch instead of rendering nothing.
  if (booted && !session && !loading && !error) {
    return (
      <div className={styles.fullCenter}>
        <div className={styles.emptyCard}>
          <div className={styles.homeBrand}>
            <span className={styles.brandMark}>pr</span>
            <span className={styles.homeBrandName}>walkthrough</span>
          </div>
          <div className={styles.homeTagline}>
            Launch from your terminal:
          </div>
          <pre className={styles.emptyCmd}>pr-walkthrough owner/repo/pull/N</pre>
          <div className={styles.homeHint}>
            The CLI starts the backend, opens a browser, and drops you straight into the session.
          </div>
        </div>
      </div>
    );
  }

  if (loading) {
    return (
      <div className={styles.fullCenter}>
        <LoadingPhases phase={loadingPhase} />
      </div>
    );
  }

  if (error) {
    return (
      <div className={styles.fullCenter}>
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 12, maxWidth: 480, textAlign: "center" }}>
          <div className={styles.errorLabel}>Couldn’t load this PR</div>
          <div className={styles.errorMsg}>{error}</div>
          <button
            className={styles.retryBtn}
            onClick={() => {
              window.location.hash = "";
              window.location.reload();
            }}
          >
            Start over
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
          onClick={() => {
            if (!regenConfirm) {
              setRegenConfirm(true);
              if (regenTimerRef.current != null) window.clearTimeout(regenTimerRef.current);
              regenTimerRef.current = window.setTimeout(() => {
                setRegenConfirm(false);
                regenTimerRef.current = null;
              }, 3000);
              return;
            }
            if (regenTimerRef.current != null) {
              window.clearTimeout(regenTimerRef.current);
              regenTimerRef.current = null;
            }
            setRegenConfirm(false);
            initSession(session.plan.pr.url);
          }}
          aria-label="Regenerate session"
          title="Fresh plan + narrations for this same PR (current session is discarded)"
        >
          {regenConfirm ? "↻ Confirm — discard?" : "↻ Regenerate"}
        </button>
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

const LOADING_STAGES: { id: LoadingPhase; label: string }[] = [
  { id: "fetching_pr",   label: "Fetching the PR diff" },
  { id: "planning_tour", label: "Asking Claude to plan a tour" },
  { id: "setting_up",    label: "Setting up your session" },
];

function LoadingPhases({ phase }: { phase: LoadingPhase }) {
  const activeIdx = LOADING_STAGES.findIndex((s) => s.id === phase);
  return (
    <div className={styles.loadingPhases} aria-live="polite" aria-busy="true">
      <div className={styles.loadingTitle}>preparing your walkthrough</div>
      <ul className={styles.loadingStageList}>
        {LOADING_STAGES.map((s, i) => {
          const done = activeIdx > i;
          const active = activeIdx === i;
          return (
            <li
              key={s.id}
              className={`${styles.loadingStage} ${active ? styles.loadingStageActive : ""} ${done ? styles.loadingStageDone : ""}`}
            >
              <span className={styles.loadingStageIcon} aria-hidden>
                {done ? "✓" : active ? <span className={styles.dotPulse} /> : "○"}
              </span>
              <span className={styles.loadingStageLabel}>{s.label}</span>
            </li>
          );
        })}
      </ul>
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
