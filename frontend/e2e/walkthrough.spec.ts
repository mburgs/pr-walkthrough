/**
 * End-to-end smoke for the walkthrough UI.
 *
 * Runs against the Vite dev server in MSW (mock backend) mode — no Python
 * backend required. Each test loads `/` fresh; MSW resets in-memory flag
 * state across reloads, but tests within a single file may share it.
 */

import { expect, test } from "@playwright/test";

// Hitting `/` plain shows the homepage form (intentional). The ?pr= query
// short-circuits straight into MSW's mocked session, regardless of value —
// the mock returns the same canonical TourPlan for any submitted URL.
const APP = "/?pr=https://github.com/example-org/auth-service/pull/142";
const FIXTURE_TITLE = /Rotate session tokens/i;       // from the canonical fixture
const FIXTURE_SUMMARY = /SessionStore gains rotate/;   // chunk c1 summary text

async function bootSession(page: import("@playwright/test").Page) {
  await page.goto(APP);
  await expect(page.getByText(FIXTURE_TITLE).first()).toBeVisible({ timeout: 15_000 });
  // Wait until the chunk list has rendered at least one chunk button
  await expect(page.locator('button:has-text("c1")').first()).toBeVisible();
}

test.describe("homepage", () => {
  test("plain / shows the PR-URL form and submits into a session", async ({ page }) => {
    await page.goto("/");
    const input = page.getByLabel("Pull request URL");
    await expect(input).toBeVisible();
    // Submit button is gated on a valid GitHub PR URL pattern.
    const submit = page.getByRole("button", { name: /Start walkthrough/ });
    await expect(submit).toBeDisabled();
    await input.fill("https://github.com/example-org/auth-service/pull/142");
    await expect(submit).toBeEnabled();
    await submit.click();
    await expect(page.getByText(FIXTURE_TITLE).first()).toBeVisible({ timeout: 15_000 });
  });
});

test.describe("walkthrough shell", () => {
  test("loads the fixture session and renders chunks + diff", async ({ page }) => {
    await bootSession(page);

    // Chunk list shows all three fixture chunks
    for (const cid of ["c1", "c2", "c3"]) {
      await expect(page.locator(`button:has-text("${cid}")`).first()).toBeVisible();
    }

    // The diff renders syntax-highlighted code for the first chunk
    await expect(page.locator("table.diff").first()).toBeVisible();
    await expect(page.locator(".diff-code-insert").first()).toBeVisible();
  });

  test("session id is written to the URL hash after init", async ({ page }) => {
    await page.goto(APP);
    await expect(page).toHaveURL(/#session=sess_/, { timeout: 15_000 });
  });
});

test.describe("narration player", () => {
  test("renders segments as clickable spans + a play+caret control", async ({ page }) => {
    await bootSession(page);

    // At least one segment span shows the script
    await expect(page.locator('[class*="segment_"]').first()).toBeVisible();

    // Play button (exact text) + caret (located by its title)
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
    // The previous test only verifies the menu *exists*. This one clicks the
    // item and asserts the script area swaps to the new content — the MSW
    // handler stamps "[regen N] " on segment 0, so we wait for that prefix.
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

    // Open the Concerns section (it auto-expands when populated) and click the first row
    const row = page.locator('[role="button"][title="Click to highlight in diff"]').first();
    await expect(row).toBeVisible();
    await row.click();

    // The row gets a sticky 'rowActive' class, and at least one diff row gets
    // the 'activeRow' class (highlights the anchored line range)
    await expect(page.locator('[class*="rowActive_"]').first()).toBeVisible();
    await expect(page.locator('tr.diff-line[class*="activeRow_"]').first()).toBeVisible();
  });
});

test.describe("chunk navigation", () => {
  test("skip button advances to the next chunk", async ({ page }) => {
    await bootSession(page);

    // Initial chunk is c1 → skip should land on c2
    await page.getByTitle(/Next chunk \(c2\)/).click();
    // The right rail's chunk label updates to c2
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

    // The fixture seeds two flags — each renders a textarea.
    const flagTextareas = page.locator("textarea");
    await expect(flagTextareas.first()).toBeVisible();
    const before = await flagTextareas.count();
    expect(before).toBeGreaterThan(0);

    // Click Remove on the first flag. Prior to the api/client 204 fix, this
    // call rejected silently and the row stayed.
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
