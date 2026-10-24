const assert = require("node:assert/strict");
const { mkdir, readFile } = require("node:fs/promises");
const http = require("node:http");
const path = require("node:path");
const { after, before, test } = require("node:test");
const { chromium } = require("playwright");

const STATIC_ROOT = path.join(__dirname, "..", "live_gantt", "static");
const SESSION_ID = "layout-session";
const DESKTOP_MINIMUM_POPOVER_WIDTH = 400;
const MAXIMUM_DETAILS_WIDTH = 510;
const OVERVIEW_DRAWER_BREAKPOINT = 1000;
const MOBILE_POPOVER_MARGIN = 64;
const MINIMUM_RAIL_CONTENT_WIDTH = 240;
const GEOMETRY_TOLERANCE = 1;
const HOUR_MILLISECONDS = 60 * 60 * 1000;
const DESKTOP_HEADER_HEIGHT_ALLOWANCE = 140;
const NARROW_HEADER_HEIGHT_ALLOWANCE = 240;
const TASK_COUNT = 60;
const MILESTONE_COUNT = 12;
const SCROLL_DISTANCE = 300;
const VIEWPORTS = [
  { width: 1480, height: 760 }, { width: 1480, height: 500 },
  { width: 800, height: 700 }, { width: 390, height: 844 }, { width: 390, height: 500 },
];
const DISCLOSURES = [
  ["monitor", "#monitor-details"],
  ["usage", '.usage-details[data-provider="claude"]'],
  ["session", ".session-disclosure"],
];
const STATIC_ROUTES = new Map([
  ["/", ["index.html", "text/html"]],
  ["/style.css", ["style.css", "text/css"]],
  ["/app.js", ["app.js", "text/javascript"]],
]);

let server;
let browser;
let address;
const connections = new Set();

function fixture() {
  const now = new Date().toISOString();
  const tasks = Array.from({ length: TASK_COUNT }, (_, index) => ({
    id: String(index + 1), subject: `Inspect task ${index + 1} and its recorded work`,
    description: "A task with enough detail to exercise the work inspector.",
    status: "pending", lane: "Implementation", milestone: `Milestone ${index % MILESTONE_COUNT + 1}`,
    progress: 0, effectiveProgress: 0, updatedAt: now,
    projectedStart: now, projectedEnd: new Date(Date.now() + HOUR_MILLISECONDS).toISOString(),
    agents: [], runs: [], warnings: [], markers: [], metadata: {},
  }));
  const milestones = Array.from({ length: MILESTONE_COUNT }, (_, index) => ({
    name: `Milestone ${index + 1}`, progress: 25, hasRemaining: true, hasEstimate: true,
    p50: new Date(Date.now() + HOUR_MILLISECONDS).toISOString(),
    p90: new Date(Date.now() + HOUR_MILLISECONDS * 2).toISOString(),
  }));
  const session = {
    id: SESSION_ID, sourceId: SESSION_ID, provider: "claude", title: "Layout regression",
    project: "/example/layout", chartable: true, recent: true, active: true,
    updatedAt: now, taskCount: tasks.length, agentCount: 0,
  };
  const model = {
    ...session, now, tasks, agents: [], workflows: [], milestones, warnings: [],
    tokenUsage: { totalTokens: 1200, inputTokens: 1000, outputTokens: 200,
      cachedInputTokens: 400, cacheWriteTokens: 100, reasoningOutputTokens: 50 },
    sync: { status: "current", checkedAt: now },
    liveness: { status: "alive", checkedAt: now, detail: "Native ownership is verified." },
    monitor: {
      state: "working", lastActivityAt: now,
      summary: "Reading the selected session and checking its progress.",
      issues: ["A diagnostic sentence that should remain readable without wrapping each character."],
    },
  };
  const usage = {
    providers: ["claude", "codex"].map(provider => ({
      provider, status: "available", sourceId: SESSION_ID,
      windows: ["Primary window", "Weekly window", "Scoped window"].map((label, index) => ({
        id: `${provider}-${index}`, bucketId: provider, windowMinutes: 300,
        label, usedPercent: 25, stale: false, measuredAt: null,
        observedAt: now, capturedAt: now, freshnessBasis: "recorded_change",
        resetsAt: new Date(Date.now() + HOUR_MILLISECONDS).toISOString(),
        forecast: { status: "insufficient_history", basis: null, exhaustsAt: null, sampleSeconds: null },
      })),
    })),
  };
  return { session, model, usage };
}

before(async () => {
  server = http.createServer(async (request, response) => {
    try {
      const pathname = new URL(request.url, "http://127.0.0.1").pathname;
      const data = fixture();
      if (pathname === "/api/lists") {
        response.writeHead(200, { "Content-Type": "application/json" });
        response.end(JSON.stringify({ lists: [data.session] }));
      } else if (pathname === "/events") {
        response.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" });
        response.write(`event: state\ndata: ${JSON.stringify(data.model)}\n\n`);
        response.write(`event: usage\ndata: ${JSON.stringify(data.usage)}\n\n`);
      } else if (STATIC_ROUTES.has(pathname)) {
        const [filename, contentType] = STATIC_ROUTES.get(pathname);
        response.writeHead(200, { "Content-Type": contentType });
        response.end(await readFile(path.join(STATIC_ROOT, filename)));
      } else {
        response.writeHead(404);
        response.end();
      }
    } catch (error) {
      response.destroy(error);
    }
  });
  server.on("connection", connection => {
    connections.add(connection);
    connection.on("close", () => connections.delete(connection));
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  address = `http://127.0.0.1:${server.address().port}/#session=${SESSION_ID}`;
  const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;
  browser = await chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}) });
  if (process.env.LIVE_GANTT_SCREENSHOTS) await mkdir(process.env.LIVE_GANTT_SCREENSHOTS, { recursive: true });
});

after(async () => {
  if (browser) await browser.close();
  if (server) {
    for (const connection of connections) connection.destroy();
    await new Promise(resolve => server.close(resolve));
  }
});

async function openOverview(page, viewport) {
  if (viewport.width <= OVERVIEW_DRAWER_BREAKPOINT) {
    await page.locator("#overview-toggle").click();
    await page.waitForFunction(() => document.querySelector("#overview-panel").classList.contains("is-open"));
  }
}

async function assertFixedDocument(page, viewport) {
  const dimensions = await page.evaluate(() => ({
    width: document.documentElement.scrollWidth,
    height: document.documentElement.scrollHeight,
    x: window.scrollX, y: window.scrollY,
  }));
  assert.ok(dimensions.width <= viewport.width + GEOMETRY_TOLERANCE, "The document must not scroll horizontally");
  assert.ok(dimensions.height <= viewport.height + GEOMETRY_TOLERANCE, "The document must not scroll vertically");
  assert.equal(dimensions.x, 0);
  assert.equal(dimensions.y, 0);
}

for (const viewport of VIEWPORTS) {
  const dimensions = `${viewport.width}x${viewport.height}`;
  for (const [name, selector] of DISCLOSURES) {
    test(`${name} Details stays readable and supports native toggling at ${dimensions}`, async () => {
      const page = await browser.newPage({ viewport, colorScheme: "dark" });
      const errors = [];
      page.on("pageerror", error => errors.push(error.message));
      try {
        await page.goto(address);
        await page.locator(".task-main").first().waitFor();
        if (name !== "monitor") await openOverview(page, viewport);
        const disclosure = page.locator(selector);
        const summary = disclosure.locator("summary");
        const content = disclosure.locator(".disclosure-content");
        await summary.click();
        await content.waitFor({ state: "visible" });
        const bounds = await content.boundingBox();
        assert.ok(bounds, "Opened Details must have a visible content box");
        const minimumWidth = name !== "monitor" ? MINIMUM_RAIL_CONTENT_WIDTH
          : viewport.width <= OVERVIEW_DRAWER_BREAKPOINT
            ? Math.min(DESKTOP_MINIMUM_POPOVER_WIDTH, viewport.width - MOBILE_POPOVER_MARGIN)
            : DESKTOP_MINIMUM_POPOVER_WIDTH;
        assert.ok(bounds.width >= minimumWidth,
          `${name} Details width ${bounds.width}px must be at least ${minimumWidth}px`);
        assert.ok(bounds.width <= MAXIMUM_DETAILS_WIDTH, `${name} Details must keep a readable line length`);
        assert.ok(bounds.x >= 0, `${name} Details must stay inside the left viewport edge`);
        assert.ok(bounds.x + bounds.width <= viewport.width + GEOMETRY_TOLERANCE,
          `${name} Details must stay inside the right viewport edge`);
        if (name !== "monitor") {
          const position = await content.evaluate(element => getComputedStyle(element).position);
          assert.equal(position, "static", "Overview Details must participate in its scrollable content flow");
          const overflow = await content.evaluate(element => element.scrollWidth - element.clientWidth);
          assert.ok(overflow <= GEOMETRY_TOLERANCE, "Details text must fit the available rail width");
        }
        await assertFixedDocument(page, viewport);

        await summary.focus();
        await page.keyboard.press("Enter");
        await content.waitFor({ state: "hidden" });
        assert.equal(await disclosure.evaluate(element => element.open), false);
        await page.keyboard.press("Space");
        await content.waitFor({ state: "visible" });
        assert.equal(await disclosure.evaluate(element => element.open), true);
        await summary.click();
        await content.waitFor({ state: "hidden" });
        assert.deepEqual(errors, [], "Rendering must not throw browser errors");
      } finally {
        await page.close();
      }
    });
  }

  test(`timeline owns viewport scrolling at ${dimensions}`, async () => {
    const page = await browser.newPage({ viewport, colorScheme: "dark" });
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    try {
      await page.goto(address);
      await page.locator(".task-main").first().waitFor();
      const timeline = page.locator("#timeline");
      const initial = await timeline.boundingBox();
      const headerAllowance = viewport.width <= OVERVIEW_DRAWER_BREAKPOINT
        ? NARROW_HEADER_HEIGHT_ALLOWANCE : DESKTOP_HEADER_HEIGHT_ALLOWANCE;
      assert.ok(initial.height >= viewport.height - headerAllowance,
        `Timeline height ${initial.height}px must use the viewport remaining below compact controls`);
      assert.ok(initial.y + initial.height <= viewport.height + GEOMETRY_TOLERANCE,
        "Timeline bottom must stay in the viewport");
      await assertFixedDocument(page, viewport);
      if (process.env.LIVE_GANTT_SCREENSHOTS) {
        await page.screenshot({ path: path.join(process.env.LIVE_GANTT_SCREENSHOTS, `workspace-${dimensions}.png`) });
      }
      await page.locator('[data-zoom="week"]').click();
      await timeline.evaluate((element, distance) => {
        element.scrollTop = distance;
        element.scrollLeft = distance;
      }, SCROLL_DISTANCE);
      const scrolled = await timeline.evaluate(element => ({ top: element.scrollTop, left: element.scrollLeft }));
      assert.ok(scrolled.top > 0, "Many tasks must scroll inside the timeline");
      assert.ok(scrolled.left > 0, "A wide time range must scroll inside the timeline");
      const axis = await page.locator(".axis-row").boundingBox();
      const label = await page.locator(".axis-row .row-label").boundingBox();
      const bounds = await timeline.boundingBox();
      assert.ok(Math.abs(axis.y - bounds.y) <= GEOMETRY_TOLERANCE, "The time axis must remain at the top while scrolling");
      assert.ok(Math.abs(label.x - bounds.x) <= GEOMETRY_TOLERANCE, "Row labels must remain at the left while scrolling");
      await assertFixedDocument(page, viewport);

      const toggle = page.locator("#overview-toggle");
      const overview = page.locator("#overview-panel");
      if (viewport.width <= OVERVIEW_DRAWER_BREAKPOINT) {
        assert.equal(await toggle.getAttribute("aria-expanded"), "false");
        assert.equal(await overview.evaluate(element => element.classList.contains("is-open")), false);
      }
      await openOverview(page, viewport);
      if (viewport.width <= OVERVIEW_DRAWER_BREAKPOINT) {
        assert.equal(await toggle.getAttribute("aria-expanded"), "true");
        assert.equal(await page.locator(".work-area").isVisible(), false,
          "Overview must hide covered work controls from keyboard navigation");
      }
      const beforeOverviewScroll = await timeline.evaluate(element => element.scrollTop);
      const overviewScroll = await overview.evaluate((element, distance) => {
        element.scrollTop = distance;
        return element.scrollTop;
      }, SCROLL_DISTANCE);
      assert.ok(overviewScroll > 0, "Long overview content must have its own scroll region");
      assert.equal(await timeline.evaluate(element => element.scrollTop), beforeOverviewScroll,
        "Overview scrolling must not move the timeline");
      if (viewport.width <= OVERVIEW_DRAWER_BREAKPOINT) {
        await overview.locator("summary").first().focus();
        await page.keyboard.press("Escape");
        assert.equal(await toggle.getAttribute("aria-expanded"), "false");
        assert.equal(await overview.evaluate(element => element.classList.contains("is-open")), false);
        assert.equal(await toggle.evaluate(element => document.activeElement === element), true,
          "Closing Overview must return focus to its toggle");
        await toggle.click();
        assert.equal(await toggle.getAttribute("aria-expanded"), "true");
        await toggle.click();
        assert.equal(await toggle.getAttribute("aria-expanded"), "false");
      }
      assert.equal(await page.locator(".work-area").isVisible(), true);
      await timeline.evaluate(element => { element.scrollTop = 0; element.scrollLeft = 0; });
      const beforeInspector = await timeline.boundingBox();
      const task = page.locator(".task-main").first();
      await task.click();
      const inspector = page.locator("#details");
      await inspector.waitFor({ state: "visible" });
      const afterInspector = await timeline.boundingBox();
      assert.ok(Math.abs(beforeInspector.height - afterInspector.height) <= GEOMETRY_TOLERANCE,
        "Opening the work inspector must not reduce timeline height");
      const inspectorBounds = await inspector.boundingBox();
      assert.ok(inspectorBounds.y >= 0 && inspectorBounds.y + inspectorBounds.height <= viewport.height + GEOMETRY_TOLERANCE,
        "The work inspector must remain bounded to the viewport");
      await page.keyboard.press("Escape");
      await inspector.waitFor({ state: "hidden" });
      await assertFixedDocument(page, viewport);
      assert.deepEqual(errors, [], "Workspace interactions must not throw browser errors");
    } finally {
      await page.close();
    }
  });
}
