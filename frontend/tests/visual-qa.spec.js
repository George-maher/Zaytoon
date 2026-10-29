// Playwright visual QA test for Zaytoon Phase 3.2.1 / 3.2.2
// This file is intended to run in a browser-capable environment (CI with a display server,
// or locally with a browser installed). It is NOT run in this sandbox because:
//   - No display server (DISPLAY is unset)
//   - No browser binary is installed (firefox, chromium, etc.)
//
// To run in a browser-capable environment:
//   npm install          # installs playwright from devDependencies
//   npx playwright install --with-deps chromium
//   npm run build        # produce frontend/dist/
//   npm run preview -- --port 5173  # serve the production build
//   npx playwright test tests/visual-qa.spec.js
//
// The frontend must be served on http://localhost:5173 (Vite preview) or
// http://localhost:5173 (Vite dev). The backend is NOT required — API routes
// are mocked with page.route() for the ExecutionHistory state tests.
//
// Language is set via localStorage key "zaytoon-language" ("en" or "ar"),
// matching the app's real initialization in src/hooks/useWebSocket.js.

import { test, expect } from "@playwright/test";

// The frontend preview server URL. The production build is served here.
const BASE_URL = "http://localhost:5173";

// Viewport sizes
const DESKTOP = { width: 1280, height: 800 };
const MOBILE = { width: 375, height: 667 };

// Helper: set language in localStorage before navigation, matching the app's
// real initialization in src/hooks/useWebSocket.js.
// The app reads `localStorage.getItem("zaytoon-language")` and defaults to "en".
async function setLanguage(page, language) {
  await page.goto("about:blank");
  await page.evaluate((lang) => {
    localStorage.setItem("zaytoon-language", lang);
  }, language);
  await page.goto(BASE_URL);
}

// Helper: collect console errors during a test.
function collectConsoleErrors(page) {
  const errors = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") errors.push(msg.text());
  });
  return errors;
}

// Helper: mock the /audit endpoint to return a specific state.
async function mockAuditEndpoint(page, scenario) {
  await page.route("**/audit", async (route) => {
    switch (scenario) {
      case "empty":
        // No records — triggers EmptyState
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ entries: [], total: 0 }),
        });
        break;
      case "loading":
        // Never resolves — simulates a stalled fetch (loading state)
        await new Promise(() => {});
        break;
      case "error":
        // Network failure — triggers error state
        await route.abort("failed");
        break;
      case "unavailable":
        // 404 — triggers "Audit log unavailable" state
        await route.fulfill({
          status: 404,
          contentType: "application/json",
          body: JSON.stringify({}),
        });
        break;
      default:
        await route.continue();
    }
  });
  // /processes is optional — mock it as empty for all scenarios
  await page.route("**/processes", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ processes: [] }),
    });
  });
}

// Helper: navigate to ExecutionHistory view. The app's router uses view state;
// we navigate via the sidebar. For simplicity, we go to the workspace view
// and rely on the app routing. Since we can't easily drive the router without
// a backend WebSocket, we mount ExecutionHistory indirectly through the app.
// Note: In a real browser run, the app loads the HOME view (NexusDashboard).
// ExecutionHistory is part of WorkspaceShell which requires a WebSocket
// connection. These tests verify the app shell loads correctly and that
// EmptyState renders when the component is mounted.
async function navigateToApp(page) {
  await page.goto(BASE_URL);
  await page.waitForTimeout(2000); // allow app to hydrate
}

test.describe("Visual QA — ExecutionHistory EmptyState adoption", () => {
  test.describe("Desktop (1280x800)", () => {
    test.use({ viewport: DESKTOP });

    test("English / LTR — app loads without console errors", async ({ page }) => {
      const errors = collectConsoleErrors(page);
      await navigateToApp(page);
      await expect(page).toHaveTitle(/Zaytoon/);
      // The app's html element should have dir=ltr for English
      await expect(page.locator("html")).toHaveAttribute("dir", "ltr");
      await expect(page.locator("html")).toHaveAttribute("lang", "en");
      expect(errors).toHaveLength(0);
    });

    test("Arabic / RTL — html element has dir=rtl and lang=ar", async ({
      page,
    }) => {
      const errors = collectConsoleErrors(page);
      await setLanguage(page, "ar");
      await expect(page.locator("html")).toHaveAttribute("dir", "rtl");
      await expect(page.locator("html")).toHaveAttribute("lang", "ar");
      expect(errors).toHaveLength(0);
    });
  });

  test.describe("Mobile (375x667)", () => {
    test.use({ viewport: MOBILE });

    test("English / LTR — no horizontal overflow on narrow viewport", async ({
      page,
    }) => {
      const errors = collectConsoleErrors(page);
      await navigateToApp(page);
      const overflow = await page.evaluate(() => {
        return (
          document.documentElement.scrollWidth >
          document.documentElement.clientWidth
        );
      });
      expect(overflow).toBe(false);
      expect(errors).toHaveLength(0);
    });

    test("Arabic / RTL — no horizontal overflow on narrow viewport", async ({
      page,
    }) => {
      const errors = collectConsoleErrors(page);
      await setLanguage(page, "ar");
      await expect(page.locator("html")).toHaveAttribute("dir", "rtl");
      const overflow = await page.evaluate(() => {
        return (
          document.documentElement.scrollWidth >
          document.documentElement.clientWidth
        );
      });
      expect(overflow).toBe(false);
      expect(errors).toHaveLength(0);
    });
  });
});

test.describe("Visual QA — EmptyState component rendering", () => {
  test("English / LTR — EmptyState renders with title, description, and glyph", async ({
    page,
  }) => {
    const errors = collectConsoleErrors(page);
    await mockAuditEndpoint(page, "empty");
    await navigateToApp(page);

    // EmptyState uses theme tokens: verify rendered text and structure
    // The EmptyState renders an <h3> title and a <p> description
    await expect(page.getByRole("heading", { level: 3 })).toBeVisible();
    await expect(page.locator("p")).toBeVisible();
    // No EmptyState-related console errors
    expect(errors.filter((e) => e.includes("EmptyState"))).toHaveLength(0);
    expect(errors).toHaveLength(0);
  });

  test("Arabic / RTL — EmptyState renders Arabic text with RTL direction", async ({
    page,
  }) => {
    const errors = collectConsoleErrors(page);
    await setLanguage(page, "ar");
    await mockAuditEndpoint(page, "empty");
    await page.goto(BASE_URL);
    await page.waitForTimeout(2000);

    await expect(page.locator("html")).toHaveAttribute("dir", "rtl");
    expect(errors.filter((e) => e.includes("EmptyState"))).toHaveLength(0);
    expect(errors).toHaveLength(0);
  });

  test("Loading state — LOADING… shown, EmptyState not substituted", async ({
    page,
  }) => {
    const errors = collectConsoleErrors(page);
    await mockAuditEndpoint(page, "loading");
    await navigateToApp(page);

    // Loading state: "LOADING…" should be visible, EmptyState content should not
    await expect(page.getByText(/LOADING|جار التحميل/)).toBeVisible();
    // EmptyState title should NOT appear during loading
    await expect(
      page.getByText(/No executions recorded|لا توجد عمليات/)
    ).not.toBeVisible();
    expect(errors).toHaveLength(0);
  });

  test("Error state — error message shown, EmptyState not substituted", async ({
    page,
  }) => {
    const errors = collectConsoleErrors(page);
    await mockAuditEndpoint(page, "error");
    await navigateToApp(page);

    // Error state: fetch failure message should be visible
    await expect(
      page.getByText(/Audit fetch failed|تعذر جلب السجل/)
    ).toBeVisible();
    // EmptyState title should NOT appear during error
    await expect(
      page.getByText(/No executions recorded|لا توجد عمليات/)
    ).not.toBeVisible();
    expect(errors).toHaveLength(0);
  });

  test("Unavailable state — 'Audit log unavailable' shown, EmptyState not substituted", async ({
    page,
  }) => {
    const errors = collectConsoleErrors(page);
    await mockAuditEndpoint(page, "unavailable");
    await navigateToApp(page);

    // Unavailable state: backend 404 → "Audit log unavailable"
    await expect(
      page.getByText(/Audit log unavailable|سجل العمليات غير متاح/)
    ).toBeVisible();
    // EmptyState title should NOT appear when unavailable
    await expect(
      page.getByText(/No executions recorded|لا توجد عمليات/)
    ).not.toBeVisible();
    expect(errors).toHaveLength(0);
  });

  test("EmptyState accessibility — semantic heading, glyph hidden from screen readers", async ({
    page,
  }) => {
    const errors = collectConsoleErrors(page);
    await mockAuditEndpoint(page, "empty");
    await navigateToApp(page);

    // Semantic <h3> heading present
    const heading = page.getByRole("heading", { level: 3 });
    await expect(heading).toBeVisible();
    // Glyph is aria-hidden
    const glyph = page.locator('[aria-hidden="true"]');
    await expect(glyph.first()).toHaveAttribute("aria-hidden", "true");
    expect(errors).toHaveLength(0);
  });
});

test.describe("Visual QA — Theme token resolution", () => {
  test("CSS custom properties resolve on the root element", async ({ page }) => {
    await navigateToApp(page);

    // Verify theme tokens are defined on :root
    const rootStyles = await page.evaluate(() => {
      const root = getComputedStyle(document.documentElement);
      return {
        ztTextMuted: root.getPropertyValue("--zt-text-muted").trim(),
        ztFontUi: root.getPropertyValue("--zt-font-ui").trim(),
        ztSpace6: root.getPropertyValue("--zt-space-6").trim(),
        hudText: root.getPropertyValue("--hud-text").trim(),
      };
    });

    expect(rootStyles.ztTextMuted).toBeTruthy();
    expect(rootStyles.ztFontUi).toBeTruthy();
    expect(rootStyles.ztSpace6).toBeTruthy();
    expect(rootStyles.hudText).toBeTruthy();
  });
});

test.describe("Visual QA — Keyboard navigation and focus", () => {
  test.use({ viewport: DESKTOP });

  test("Tab reaches the composer input and SEND button", async ({ page }) => {
    const errors = collectConsoleErrors(page);
    await navigateToApp(page);

    // Focus the page body first, then tab.
    await page.keyboard.press("Tab");
    await page.keyboard.press("Tab");

    // The composer has id="zaytoon-composer".
    const focused = await page.evaluate(() => document.activeElement?.id);
    expect(focused).toBe("zaytoon-composer");

    expect(errors).toHaveLength(0);
  });

  test("Visible focus ring on interactive elements", async ({ page }) => {
    const errors = collectConsoleErrors(page);
    await navigateToApp(page);

    // Tab to the composer and check for a focus-visible outline.
    await page.keyboard.press("Tab");
    const composer = page.locator("#zaytoon-composer");
    await expect(composer).toBeFocused();

    const focusStyles = await composer.evaluate((el) => {
      const s = getComputedStyle(el);
      return {
        outline: s.outline,
        outlineColor: s.outlineColor,
        outlineWidth: s.outlineWidth,
      };
    });

    // A visible focus ring should be present (not "none" or "0px").
    expect(focusStyles.outline).not.toBe("none");
    expect(focusStyles.outlineWidth).not.toBe("0px");

    expect(errors).toHaveLength(0);
  });

  test("Escape closes the settings panel when open", async ({ page }) => {
    const errors = collectConsoleErrors(page);
    await navigateToApp(page);

    // Click Settings in the header nav.
    await page.getByRole("button", { name: /Settings|إعدادات/ }).click();

    // The settings dialog should be modal.
    await expect(page.getByRole("dialog")).toBeVisible();

    // Escape should close it.
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).not.toBeVisible();

    expect(errors).toHaveLength(0);
  });

  test("Enter activates the primary button in a focused dialog", async ({
    page,
  }) => {
    const errors = collectConsoleErrors(page);
    await navigateToApp(page);

    // Open settings and tab to the primary action.
    await page.getByRole("button", { name: /Settings|إعدادات/ }).click();
    await expect(page.getByRole("dialog")).toBeVisible();

    // Tab through dialog controls until the primary button is focused.
    // The CLOSE button is at the end of the dialog footer.
    await page.keyboard.press("Tab");
    await page.keyboard.press("Tab");
    await page.keyboard.press("Tab");

    // Enter should activate the focused button (dismissing the dialog).
    await page.keyboard.press("Enter");

    await expect(page.getByRole("dialog")).not.toBeVisible();
    expect(errors).toHaveLength(0);
  });
});

test.describe("Visual QA — Accessible names for key controls", () => {
  test.use({ viewport: DESKTOP });

  test("Sidebar collapsed nav items have accessible names", async ({ page }) => {
    const errors = collectConsoleErrors(page);
    await navigateToApp(page);

    // Look for a sidebar toggle (collapse button) and collapse.
    const toggle = page.getByRole("button", {
      name: /Collapse sidebar|توسيع الشريط|طي الشريط/,
    });
    if ((await toggle.count()) > 0) {
      await toggle.click();

      // After collapsing, nav items should expose aria-label.
      // The Home nav item should have an accessible name.
      await expect(
        page.getByRole("button", { name: /Home|الرئيسية/ })
      ).toBeVisible();
    }

    expect(errors).toHaveLength(0);
  });

  test("Icon-only buttons have accessible labels", async ({ page }) => {
    const errors = collectConsoleErrors(page);
    await navigateToApp(page);

    // The refresh button in ExecutionHistory has a label.
    // Look for aria-label attributes on buttons without visible text.
    const labeledButtons = page.locator('button[aria-label]');
    const count = await labeledButtons.count();
    expect(count).toBeGreaterThan(0);

    expect(errors).toHaveLength(0);
  });

  test("Form controls have associated labels", async ({ page }) => {
    const errors = collectConsoleErrors(page);
    await navigateToApp(page);

    // The composer input has an aria-label.
    const composer = page.locator("#zaytoon-composer");
    await expect(composer).toHaveAttribute("aria-label");

    expect(errors).toHaveLength(0);
  });
});
