import { useEffect, useRef, useState } from "react";
import { SessionProvider, useSession } from "./contexts/SessionContext";
import SessionShell from "./components/SessionShell";
import { exportTranscript } from "./lib/exportTranscript";
import type { FamiliarityLevel } from "./contracts";
import type { LoadingPhase } from "./contexts/SessionContext";
import styles from "./App.module.css";

function readBootHints(): { sid?: string; prUrl?: string } {
  const hash = window.location.hash.replace(/^#/, "");
  const sid = new URLSearchParams(hash).get("session") || undefined;
  const prUrl = new URLSearchParams(window.location.search).get("pr") || undefined;
  return { sid, prUrl };
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
    const { sid, prUrl } = readBootHints();
    if (sid) resumeSession(sid).finally(() => setBooted(true));
    else if (prUrl) initSession(prUrl).finally(() => setBooted(true));
    else setBooted(true); // no hint → show the homepage
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Homepage: shown only when boot finished with no session and no error.
  // Error case falls through to the error UI below so the user sees the
  // failure rather than a form that swallows the context.
  if (booted && !session && !loading && !error) {
    return <Homepage onSubmit={(url, familiarity, multiLevel) => {
      const next = new URL(window.location.href);
      next.searchParams.set("pr", url);
      window.history.replaceState({}, "", next.toString());
      initSession(url, familiarity, multiLevel);
    }} />;
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

// UI-only union — "all" is a sentinel for multi-level mode that the
// backend doesn't see directly (it gets translated to multi_level=true +
// a starting familiarity of "review").
type HomepageChoice = FamiliarityLevel | "all";

const FAMILIARITY_OPTIONS: { value: HomepageChoice; label: string; blurb: string }[] = [
  { value: "tutorial",  label: "Tutorial",   blurb: "New to the language/framework — explain unusual syntax too." },
  { value: "tour",      label: "Tour",       blurb: "Know the language; new to this repo — orient me to its conventions." },
  { value: "review",    label: "Review",     blurb: "Know the repo; focus on this specific change." },
  { value: "highlights",label: "Highlights", blurb: "Already familiar — just the high-impact moments." },
  { value: "all",       label: "All",        blurb: "Generate all four levels and toggle between them live in the player." },
];

function Homepage({ onSubmit }: { onSubmit: (url: string, familiarity: FamiliarityLevel, multiLevel: boolean) => void }) {
  const [value, setValue] = useState("");
  const [choice, setChoice] = useState<HomepageChoice>("review");
  const trimmed = value.trim();
  const valid = /^https?:\/\/github\.com\/[^/]+\/[^/]+\/pull\/\d+/.test(trimmed);
  const activeBlurb = FAMILIARITY_OPTIONS.find(o => o.value === choice)?.blurb ?? "";

  return (
    <div className={styles.fullCenter}>
      <form
        className={styles.homeForm}
        onSubmit={(e) => {
          e.preventDefault();
          if (!valid) return;
          if (choice === "all") onSubmit(trimmed, "review", true);
          else onSubmit(trimmed, choice, false);
        }}
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

        <fieldset className={styles.homeFieldset}>
          <legend className={styles.homeLabel}>How familiar are you?</legend>
          <div className={styles.homeSegmented} role="radiogroup" aria-label="Narration depth">
            {FAMILIARITY_OPTIONS.map(opt => (
              <button
                type="button"
                key={opt.value}
                role="radio"
                aria-checked={choice === opt.value}
                className={`${styles.homeSegment} ${choice === opt.value ? styles.homeSegmentActive : ""}`}
                onClick={() => setChoice(opt.value)}
              >
                {opt.label}
              </button>
            ))}
          </div>
          <div className={styles.homeSegmentBlurb}>{activeBlurb}</div>
        </fieldset>

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
