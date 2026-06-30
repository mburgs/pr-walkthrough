/**
 * End-to-end smoke for the walkthrough UI.
 *
 * Runs against the Vite dev server in MSW (mock backend) mode — no Python
 * backend required. The CLI is the production entry point, so the
 * browser never lands on a session-init form; tests boot straight into
 * the canonical fixture session via the `#session=` hash.
 */

import { expect, test } from "@playwright/test";

// MSW's `/sessions/:sid` handler returns the canonical fixture for this
// id. The CLI puts something similar in the URL after creating a session
// against the real backend.
const FIXTURE_SID = "sess_pr_small_001";
const APP = `/#session=${FIXTURE_SID}`;
const FIXTURE_TITLE = /Rotate session tokens/i;
const FIXTURE_SUMMARY = /SessionStore gains rotate/;

async function bootSession(page: import("@playwright/test").Page) {
  await page.goto(APP);
  await expect(page.getByText(FIXTURE_TITLE).first()).toBeVisible({ timeout: 15_000 });
  await expect(page.locator('button:has-text("c1")').first()).toBeVisible();
}

test.describe("empty state", () => {
  test("plain / shows the CLI launch hint, no form", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByText(/Launch from your terminal/i)).toBeVisible();
    await expect(page.getByText(/pr-walkthrough owner\/repo\/pull\/N/)).toBeVisible();
    // The old homepage form should be gone — no "Pull request URL" input.
    await expect(page.getByLabel("Pull request URL")).toHaveCount(0);
  });
});

test.describe("walkthrough shell", () => {
  test("loads the fixture session from #session= and renders chunks + diff", async ({ page }) => {
    await bootSession(page);

    for (const cid of ["c1", "c2", "c3"]) {
      await expect(page.locator(`button:has-text("${cid}")`).first()).toBeVisible();
    }

    await expect(page.locator("table.diff").first()).toBeVisible();
    await expect(page.locator(".diff-code-insert").first()).toBeVisible();
  });

  test("URL hash retains the session id during the session", async ({ page }) => {
    await page.goto(APP);
    await expect(page.getByText(FIXTURE_TITLE).first()).toBeVisible({ timeout: 15_000 });
    await expect(page).toHaveURL(/#session=sess_/);
  });
});

test.describe("narration player", () => {
  test("renders segments as clickable spans + a play+caret control", async ({ page }) => {
    await bootSession(page);
    await expect(page.locator('[class*="segment_"]').first()).toBeVisible();
    await expect(page.getByRole("button", { name: "▶ Play" })).toBeVisible();
    await expect(page.getByTitle("More actions")).toBeVisible();
  });

  test("caret opens a menu containing Regenerate this chunk", async ({ page }) => {
    await bootSession(page);
    await page.getByTitle("More actions").click();
    await expect(
      page.getByRole("menuitem", { name: /Regenerate this chunk/ })
    ).toBeVisible();
  });

  test("clicking Regenerate replaces the rendered narration content", async ({ page }) => {
    await bootSession(page);
    const scriptArea = page.locator('[class*="script_"]').first();
    await expect(scriptArea).not.toContainText("[regen 1]");

    await page.getByTitle("More actions").click();
    await page.getByRole("menuitem", { name: /Regenerate this chunk/ }).click();

    await expect(scriptArea).toContainText("[regen 1]", { timeout: 5000 });
  });

  test("clicking the speed button cycles 1× → 1.25×", async ({ page }) => {
    await bootSession(page);
    const speed = page.getByRole("button", { name: /Playback speed 1×/ });
    await expect(speed).toBeVisible();
    await speed.click();
    await expect(
      page.getByRole("button", { name: /Playback speed 1.25×/ })
    ).toBeVisible();
  });
});

test.describe("guided tour highlighting", () => {
  test("clicking a concern row highlights matching lines in the diff", async ({ page }) => {
    await bootSession(page);

    const row = page.locator('[role="button"][title="Click to highlight in diff"]').first();
    await expect(row).toBeVisible();
    await row.click();

    await expect(page.locator('[class*="rowActive_"]').first()).toBeVisible();
    await expect(page.locator('tr.diff-line[class*="activeRow_"]').first()).toBeVisible();
  });
});

test.describe("chunk navigation", () => {
  test("skip button advances to the next chunk", async ({ page }) => {
    await bootSession(page);

    await page.getByTitle(/Next chunk \(c2\)/).click();
    await expect(page.locator('[class*="railChunkId"]').getByText("c2")).toBeVisible();
  });

  test("clicking a chunk in the sidebar switches the diff + narration", async ({ page }) => {
    await bootSession(page);

    await page.locator('button:has-text("c3")').first().click();
    await expect(page.locator('[class*="railChunkId"]').getByText("c3")).toBeVisible();
  });
});

test.describe("flag tracker", () => {
  test("Remove button on a flag deletes it (covers the 204 No Content fix)", async ({ page }) => {
    await bootSession(page);

    const flagTextareas = page.locator("textarea");
    await expect(flagTextareas.first()).toBeVisible();
    const before = await flagTextareas.count();
    expect(before).toBeGreaterThan(0);

    await page.getByRole("button", { name: "Remove" }).first().click();
    await expect.poll(() => flagTextareas.count()).toBe(before - 1);
  });

  test("Editing a flag's body persists to PATCH", async ({ page }) => {
    await bootSession(page);
    const ta = page.locator("textarea").first();
    await expect(ta).toBeVisible();
    await ta.fill("edited via the e2e suite");
    await ta.blur();
    await expect(ta).toHaveValue("edited via the e2e suite");
  });
});

test.describe("transcript export", () => {
  test("Export transcript triggers a markdown download", async ({ page }) => {
    await bootSession(page);

    const downloadPromise = page.waitForEvent("download");
    await page.getByRole("button", { name: /Export transcript/ }).click();
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toMatch(/walkthrough\.md$/);
  });
});
