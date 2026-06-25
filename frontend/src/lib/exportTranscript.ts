/**
 * Build a Markdown transcript of the current session and trigger a browser download.
 *
 * Walks every chunk in the plan, fetches its narration, and emits a section per
 * chunk with the spoken text, highlights, concerns, and look-closer items.
 */

import type { TourPlan } from "../contracts";
import { getChunkNarration } from "../api/client";

export async function exportTranscript(sessionId: string, plan: TourPlan): Promise<void> {
  const lines: string[] = [];
  lines.push(`# ${plan.pr.title}`);
  lines.push("");
  lines.push(`**${plan.pr.repo}#${plan.pr.number}** — by @${plan.pr.author}`);
  lines.push(`Base \`${plan.pr.base_ref}\` ← head \`${plan.pr.head_ref}\``);
  lines.push("");
  lines.push(`Review session: \`${sessionId}\``);
  lines.push("");
  lines.push("---");
  lines.push("");

  for (const chunk of plan.chunks) {
    lines.push(`## ${chunk.chunk_id} — ${chunk.files.join(", ")}`);
    lines.push("");
    lines.push(`_Concern: ${chunk.est_concern_level}_ — ${chunk.summary}`);
    lines.push("");

    let narration;
    try {
      narration = await getChunkNarration(sessionId, chunk.chunk_id);
    } catch {
      lines.push("> _(narration not yet available)_");
      lines.push("");
      continue;
    }

    lines.push(narration.narration);
    lines.push("");

    if (narration.concerns.length) {
      lines.push("### Concerns");
      for (const c of narration.concerns) {
        lines.push(`- **[${c.severity}]** ${c.text}`);
        if (c.suggested_question) {
          lines.push(`  > ${c.suggested_question}`);
        }
      }
      lines.push("");
    }

    if (narration.look_closer_for.length) {
      lines.push("### Look closer");
      for (const item of narration.look_closer_for) {
        lines.push(`- ${item}`);
      }
      lines.push("");
    }
  }

  const blob = new Blob([lines.join("\n")], { type: "text/markdown" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `pr-${plan.pr.repo.replace("/", "-")}-${plan.pr.number}-walkthrough.md`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
