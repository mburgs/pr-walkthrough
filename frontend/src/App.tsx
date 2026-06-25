import { useEffect, useRef, useState } from "react";
import { SessionProvider, useSession } from "./contexts/SessionContext";
import SessionShell from "./components/SessionShell";
import { exportTranscript } from "./lib/exportTranscript";
import styles from "./App.module.css";

function readBootHints(): { sid?: string; prUrl?: string } {
  const hash = window.location.hash.replace(/^#/, "");
  const sid = new URLSearchParams(hash).get("session") || undefined;
  const prUrl = new URLSearchParams(window.location.search).get("pr") || undefined;
  return { sid, prUrl };
}

function AppContent() {
  const { session, loading, error, initSession, resumeSession } = useSession();
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
    const { sid, prUrl } = readBootHints();
    if (sid) resumeSession(sid).finally(() => setBooted(true));
    else if (prUrl) initSession(prUrl).finally(() => setBooted(true));
    else setBooted(true); // no hint → show the homepage
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Homepage: shown only when boot finished with no session and no error.
  // Error case falls through to the error UI below so the user sees the
  // failure rather than a form that swallows the context.
  if (booted && !session && !loading && !error) {
    return <Homepage onSubmit={(url) => {
      const next = new URL(window.location.href);
      next.searchParams.set("pr", url);
      window.history.replaceState({}, "", next.toString());
      initSession(url);
    }} />;
  }

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
          <button
            className={styles.retryBtn}
            onClick={() => {
              const next = new URL(window.location.href);
              next.searchParams.delete("pr");
              next.hash = "";
              window.location.href = next.toString();
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

function Homepage({ onSubmit }: { onSubmit: (url: string) => void }) {
  const [value, setValue] = useState("");
  const trimmed = value.trim();
  const valid = /^https?:\/\/github\.com\/[^/]+\/[^/]+\/pull\/\d+/.test(trimmed);

  return (
    <div className={styles.fullCenter}>
      <form
        className={styles.homeForm}
        onSubmit={(e) => { e.preventDefault(); if (valid) onSubmit(trimmed); }}
      >
        <div className={styles.homeBrand}>
          <span className={styles.brandMark}>pr</span>
          <span className={styles.homeBrandName}>walkthrough</span>
        </div>
        <div className={styles.homeTagline}>
          A narrated tour of a GitHub pull request.
        </div>
        <label className={styles.homeLabel} htmlFor="pr-url">
          Pull request URL
        </label>
        <input
          id="pr-url"
          type="url"
          autoFocus
          spellCheck={false}
          placeholder="https://github.com/owner/repo/pull/123"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          className={styles.homeInput}
        />
        <button
          type="submit"
          disabled={!valid}
          className={styles.homeSubmit}
        >
          Start walkthrough →
        </button>
        <div className={styles.homeHint}>
          Only public PRs, or PRs your local <code>gh</code> CLI can read.
        </div>
      </form>
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
