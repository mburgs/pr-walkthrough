import { expect, test } from "@playwright/test";

const APP = "/?pr=https://github.com/example-org/auth-service/pull/142";

test.describe("expand-context buttons", () => {
  test("▲ pulls more context lines above the first hunk and updates the @@ header", async ({ page }) => {
    await page.goto(APP);
    await expect(page.getByText(/Rotate session tokens/i).first()).toBeVisible({ timeout: 15_000 });

    // Pick chunk c1 — fixture hunk is src/auth/session.py @@ -42,12 +42,28 @@
    await page.locator('button:has-text("c1")').first().click();

    // The first hunk's decoration shows the raw @@ header. Ensure it's present.
    const hunkHeader = page.locator(".diff-decoration").first();
    await expect(hunkHeader).toContainText("@@ -42,12 +42,28 @@");

    // The buttons live inside the decoration. The first hunk has no prev
    // hunk, so only the ▲ should be visible.
    const upBtn = hunkHeader.locator('button[aria-label="Expand context up"]');
    await expect(upBtn).toBeVisible();

    // Track the file fetch fires.
    const filesReq = page.waitForRequest((r) =>
      r.url().includes("/files?path=") && r.method() === "GET"
    );

    await upBtn.click();
    const req = await filesReq;
    // URL is percent-encoded by the client; decode to compare.
    expect(decodeURIComponent(req.url())).toContain("src/auth/session.py");

    // After expansion, the header should reflect a new range starting earlier.
    // 10 lines pulled up: -32,22 +32,38
    await expect(hunkHeader).toContainText("@@ -32,22 +32,38 @@", { timeout: 5_000 });
  });

  test("▼ pulls more context below the last hunk", async ({ page }) => {
    await page.goto(APP);
    await expect(page.getByText(/Rotate session tokens/i).first()).toBeVisible({ timeout: 15_000 });
    await page.locator('button:has-text("c1")').first().click();

    // The trailing decoration (after the last hunk) labelled "end of hunk".
    const tail = page.locator(".diff-decoration:has-text('end of hunk')");
    await expect(tail).toBeVisible();
    const downBtn = tail.locator('button[aria-label="Expand context down"]');
    await expect(downBtn).toBeVisible();

    await downBtn.click();

    // First hunk header should now have a larger new-side count.
    const hunkHeader = page.locator(".diff-decoration").first();
    // Original was +42,28 → +42,38 after 10 more lines down (capped by file).
    await expect(hunkHeader).toContainText("+42,38", { timeout: 5_000 });
  });
});
