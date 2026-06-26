/**
 * Regression test for the long-line horizontal-scroll bug in DiffViewer.
 *
 * react-diff-view's default stylesheet applies `table-layout: fixed` to
 * the `.diff` table, which freezes column widths to table_width /
 * column_count and *ignores* cell content. With `fixed`, no amount of
 * `white-space: pre` / `overflow: auto` on the wrapper restores
 * horizontal scrolling — the table simply can't grow past the viewport.
 *
 * Our DiffViewer.module.css overrides `table-layout: auto !important;`
 * to fix this. If that override is ever reverted (or react-diff-view
 * changes its class), this test catches it.
 */
import { expect, test } from "@playwright/test";

const APP = "/?pr=https://github.com/example-org/auth-service/pull/142";

test("diff table uses auto layout so long lines can scroll horizontally", async ({ page }) => {
  await page.goto(APP);
  await expect(page.getByText(/Rotate session tokens/i).first()).toBeVisible({ timeout: 15_000 });

  const styles = await page.evaluate(() => {
    const table = document.querySelector('[class*="diffTable"]') as HTMLElement | null;
    const wrap = table?.closest('[class*="diffWrap"]') as HTMLElement | null;
    return {
      tableLayout: table ? getComputedStyle(table).tableLayout : null,
      tableMinWidth: table ? getComputedStyle(table).minWidth : null,
      wrapOverflowX: wrap ? getComputedStyle(wrap).overflowX : null,
    };
  });

  expect(styles.tableLayout).toBe("auto");
  expect(styles.wrapOverflowX).toBe("auto");
  // `min-width: 100%` lets short content fill the viewport while
  // long content still pushes the table wider.
  expect(styles.tableMinWidth).toMatch(/100%|\d+px/);
});
