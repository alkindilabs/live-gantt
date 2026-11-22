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
const LIVE_UPDATE_TIMEOUT = 5_000;
const HOUR_MILLISECONDS = 60 * 60 * 1000;
const DESKTOP_HEADER_HEIGHT_ALLOWANCE = 140;
const NARROW_HEADER_HEIGHT_ALLOWANCE = 240;
const TASK_COUNT = 60;
const MILESTONE_COUNT = 12;
const SCROLL_DISTANCE = 300;
const MOBILE_VIEWPORT = { width: 390, height: 844 };
const DESKTOP_VIEWPORT = { width: 1480, height: 760 };
const VIEWPORTS = [
  DESKTOP_VIEWPORT, { width: 1480, height: 500 },
  { width: 800, height: 700 }, MOBILE_VIEWPORT, { width: 390, height: 500 },
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
const forecastSessions = new Map();

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

function writeState(response, model) {
  response.write(`event: state\ndata: ${JSON.stringify(model)}\n\n`);
}

before(async () => {
  server = http.createServer(async (request, response) => {
    try {
      const url = new URL(request.url, "http://127.0.0.1");
      const pathname = url.pathname;
      const data = fixture();
      if (pathname === "/api/lists") {
        response.writeHead(200, { "Content-Type": "application/json" });
        response.end(JSON.stringify({ lists: [data.session] }));
      } else if (pathname === "/events") {
        response.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" });
        const feed = forecastSessions.get(url.searchParams.get("list"));
        if (feed) feed.response = response;
        writeState(response, feed?.model || data.model);
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

test("the largest critical-path task links to the inspector and updates with the forecast", async () => {
  const viewport = MOBILE_VIEWPORT;
  const page = await browser.newPage({ viewport });
  const session = "critical-path";
  const base = Date.now();
  const iso = hours => new Date(base + hours * HOUR_MILLISECONDS).toISOString();
  const { model } = fixture();
  const name = "Delivery";
  const task = (id, subject, intervals) => ({
    ...model.tasks[0], id, subject, milestone: name, lane: subject,
    projectedStart: iso(intervals[0][0]), projectedEnd: iso(intervals.at(-1)[1]),
    projectedSegments: intervals.map(([start, end]) => ({ start: iso(start), end: iso(end) })),
    projectionAnchored: true,
  });
  const preparation = { ...task("4", "Preparation", [[0, 0.5]]), milestone: "Foundation" };
  const integration = {
    ...task("2", "Integration", [[0.5, 1], [3, 4]]),
    quotaDelays: [{ start: iso(1), end: iso(3), provider: "claude", reason: "predicted" }],
  };
  const parallel = task("3", "Parallel work", [[0, 2]]);
  const milestone = {
    name, hasRemaining: true, hasEstimate: true, p50: iso(4), p50Anchored: true,
    criticalPathTaskIds: [preparation.id, integration.id],
  };
  const feed = {
    model: { ...model, id: session, observedAt: iso(0), tasks: [preparation, integration, parallel], milestones: [milestone] },
  };
  const update = change => {
    feed.model.milestones = [{ ...milestone, ...change }];
    writeState(feed.response, feed.model);
  };
  try {
    forecastSessions.set(session, feed);
    await page.goto(address.replace(SESSION_ID, session));
    await openOverview(page, viewport);
    const largest = page.locator(".critical-path");
    await largest.getByText("Largest task on the critical path", { exact: true }).waitFor({ timeout: LIVE_UPDATE_TIMEOUT });
    const button = largest.getByRole("button", { name: "#2 Integration" });
    await largest.getByText("1.5 h of 4 h until expected finish (38%)", { exact: true }).waitFor();
    const quotaSummary = page.getByText("2 h work · 2 h projected quota wait", { exact: true });
    await quotaSummary.waitFor({ timeout: LIVE_UPDATE_TIMEOUT });
    const majorityWait = page.locator(".milestone-quota-wait");
    assert.equal(await majorityWait.count(), 0, "Half the forecast does not constitute a majority");
    const disclosure = page.locator(".critical-path-details");
    await disclosure.getByText("Critical path · 2 tasks", { exact: true }).click({ timeout: LIVE_UPDATE_TIMEOUT });
    const steps = disclosure.getByRole("listitem");
    assert.deepEqual(await steps.getByRole("button").allTextContents(), ["#4 Preparation", "#2 Integration"]);
    await steps.nth(0).getByText("0.5 h work · Foundation", { exact: true }).waitFor();
    await steps.nth(1).getByText("1.5 h work", { exact: true }).waitFor();
    await steps.nth(1).getByText("2 h wait · Claude Code · Projected quota exhaustion", { exact: true }).waitFor({ timeout: LIVE_UPDATE_TIMEOUT });
    await steps.nth(1).getByText("Largest task", { exact: true }).waitFor();
    const preparationLink = steps.getByRole("button", { name: "#4 Preparation" });
    await preparationLink.focus();
    update({ progress: 40 });
    await page.locator('progress[aria-label="Delivery progress"][value="40"]').waitFor();
    assert.equal(await disclosure.getAttribute("open"), "");
    assert.equal(await preparationLink.evaluate(element => element === document.activeElement), true);
    await button.click();
    assert.equal(await page.locator("#overview-toggle").getAttribute("aria-expanded"), "false");
    const inspector = page.locator("#details");
    await inspector.getByRole("heading", { name: "Integration" }).waitFor();
    assert.equal(await page.locator("#close-details").evaluate(element => element === document.activeElement), true);
    await page.keyboard.press("Escape");
    const row = page.getByRole("button", { name: /Task 2: Integration.*Largest on path/ });
    await row.waitFor();
    assert.equal(await row.evaluate(element => element === document.activeElement), true);
    assert.equal(await page.locator(".bar.projected.critical-path-highlight").count(), 2);
    assert.equal(await page.locator(".quota-pause.critical-path-highlight").count(), 0);
    assert.equal(await page.locator(".task-main.critical-path-highlight").count(), 1);

    await openOverview(page, viewport);
    feed.model.tasks = [preparation, {
      ...task("2", "Integration", [[0.5, 1], [4, 5]]),
      quotaDelays: [{ start: iso(1), end: iso(4), provider: "claude", reason: "predicted" }],
    }, parallel];
    update({ p50: iso(5) });
    await majorityWait.getByText("Quota wait accounts for 60% of time until expected finish", { exact: true }).waitFor({ timeout: LIVE_UPDATE_TIMEOUT });
    await majorityWait.getByText("2 h work · 3 h projected quota wait", { exact: true }).waitFor();
    await majorityWait.getByText("Claude Code · Projected quota exhaustion", { exact: true }).waitFor();
    assert.ok((await majorityWait.boundingBox()).y < (await largest.boundingBox()).y);
    feed.model.tasks[1].quotaDelays[0].reason = "reached";
    update({ p50: iso(5) });
    await majorityWait.getByText("Claude Code · Quota limit reached", { exact: true }).waitFor();
    update({ p50: iso(5), forecastDurations: {
      p50: { workMs: 2 * HOUR_MILLISECONDS, quotaWaitMs: 3 * HOUR_MILLISECONDS }, p90: null,
    } });
    await page.locator(".expected-finish").getByRole("group", { name: "At last forecast" }).waitFor();
    assert.equal(await majorityWait.getByText("Claude Code · Quota limit reached", { exact: true }).isVisible(), true);
    assert.equal(await page.getByText("2 h work · 3 h projected quota wait", { exact: true }).count(), 0);
    update({ p50: iso(5), forecastDurations: { p50: null, p90: null }, progress: 60 });
    await page.locator('progress[aria-label="Delivery progress"][value="60"]').waitFor();
    assert.equal(await majorityWait.count(), 0, "An unavailable split cannot be inferred from task segments");
    assert.equal(await page.getByText("2 h work · 3 h projected quota wait", { exact: true }).count(), 0);
    await button.focus();
    update({ criticalPathTaskIds: [parallel.id], p50: iso(2) });
    const next = largest.getByRole("button", { name: "#3 Parallel work" });
    await next.waitFor();
    assert.equal(await quotaSummary.count(), 0);
    assert.equal(await majorityWait.count(), 0);
    assert.equal(await next.evaluate(element => element === document.activeElement), true);
    await largest.getByText("2 h of 2 h until expected finish (100%)", { exact: true }).waitFor();
    await next.click();
    await inspector.getByRole("heading", { name: "Parallel work" }).waitFor();
    assert.equal(await page.locator(".task-main.critical-path-highlight").count(), 1);
    assert.match(await page.locator(".task-main.critical-path-highlight").innerText(), /Parallel work/);
    update({ p50: null, criticalPathTaskIds: [] });
    await page.locator(".forecast-unavailable").waitFor({ state: "attached" });
    assert.equal(await largest.count(), 0);
    assert.equal(await page.locator(".critical-path-highlight").count(), 0);
  } finally {
    forecastSessions.delete(session);
    await page.close();
  }
});

test("finish estimates show their own active work and projected quota wait on live updates", async () => {
  const viewport = MOBILE_VIEWPORT;
  const page = await browser.newPage({ viewport });
  const session = "forecast-durations";
  const base = Date.now();
  const iso = hours => new Date(base + hours * HOUR_MILLISECONDS).toISOString();
  const { model } = fixture();
  const milestone = {
    name: "Delivery", hasRemaining: true, hasEstimate: true, p50: iso(2.5), p90: iso(7.75),
    forecastDurations: {
      p50: { workMs: 2.5 * HOUR_MILLISECONDS, quotaWaitMs: 0 },
      p90: { workMs: 4.5 * HOUR_MILLISECONDS, quotaWaitMs: 3.25 * HOUR_MILLISECONDS },
    },
  };
  const feed = { model: { ...model, id: session, observedAt: iso(0), milestones: [milestone] } };
  const update = change => {
    feed.model.milestones = [{ ...milestone, ...change }];
    writeState(feed.response, feed.model);
  };
  try {
    forecastSessions.set(session, feed);
    await page.goto(address.replace(SESSION_ID, session));
    await openOverview(page, viewport);
    const expected = page.locator(".expected-finish");
    const later = page.locator(".later-finish");
    const expectedSplit = expected.getByRole("group", { name: "At last forecast" });
    const laterSplit = later.getByRole("group", { name: "At last forecast" });
    await expectedSplit.getByText("2 h 30 min", { exact: true }).waitFor({ timeout: LIVE_UPDATE_TIMEOUT });
    assert.equal(await expectedSplit.getByText("Active work", { exact: true }).isVisible(), true);
    assert.equal(await expectedSplit.getByText("Quota wait", { exact: true }).isVisible(), true);
    assert.equal(await expectedSplit.getByText("0 min", { exact: true }).isVisible(), true);
    assert.equal(await laterSplit.getByText("4 h 30 min", { exact: true }).isVisible(), true);
    assert.equal(await laterSplit.getByText("3 h 15 min", { exact: true }).isVisible(), true);
    const bounds = await laterSplit.evaluate(element => ({
      available: element.clientWidth, contents: element.scrollWidth,
    }));
    assert.ok(bounds.contents <= bounds.available + GEOMETRY_TOLERANCE, "The split fits its mobile forecast column");
    const bar = laterSplit.locator(".forecast-composition");
    const waitWidth = (await bar.locator(".forecast-wait").boundingBox()).width;
    const barWidth = (await bar.boundingBox()).width;
    assert.ok(Math.abs(waitWidth - barWidth * 3.25 / 7.75) <= GEOMETRY_TOLERANCE, "Bar lengths reflect their share of time");

    update({ p50: new Date(base + 3.25 * HOUR_MILLISECONDS + 1).toISOString(), p90: null, forecastDurations: {
      p50: { workMs: 1, quotaWaitMs: 3.25 * HOUR_MILLISECONDS },
      p90: null,
    } });
    await expectedSplit.getByText("3 h 15 min", { exact: true }).waitFor({ timeout: LIVE_UPDATE_TIMEOUT });
    assert.equal(await expectedSplit.getByText("<1 min", { exact: true }).isVisible(), true);
    assert.equal(await later.getByText("Unavailable", { exact: true }).isVisible(), true);
    assert.equal(await laterSplit.count(), 0);
    update({ forecastDurations: undefined, progress: 75 });
    await page.locator('progress[aria-label="Delivery progress"][value="75"]').waitFor();
    assert.equal(await expectedSplit.count(), 0);
    assert.equal(await laterSplit.count(), 0);
    assert.equal(await later.locator("time").getAttribute("datetime") !== null, true);
  } finally {
    forecastSessions.delete(session);
    await page.close();
  }
});

test("command timings rank total elapsed time and retain disclosure after live updates", async () => {
  const viewport = MOBILE_VIEWPORT;
  const page = await browser.newPage({ viewport });
  const session = "command-timings";
  const { model } = fixture();
  const operation = (label, calls, totalMs, longestMs) => ({ label, calls, totalMs, longestMs });
  const operations = [
    operation("Repeated checks", 10, 20_000, 3_000),
    operation("One long build", 1, 15_000, 15_000),
    operation("Read logs", 2, 8_000, 4_000),
    operation("Find paths", 2, 2_000, 1_000),
    operation("Read config", 1, 500, 500),
  ];
  const feed = { model: { ...model, id: session, tasks: [], milestones: [] } };
  const update = commandTimings => {
    feed.model.commandTimings = commandTimings;
    writeState(feed.response, feed.model);
  };
  try {
    forecastSessions.set(session, feed);
    await page.goto(address.replace(SESSION_ID, session));
    await openOverview(page, viewport);
    await page.getByRole("heading", { name: "No planned tasks" }).waitFor();
    const section = page.getByRole("region", { name: "Command hot path" });
    assert.equal(await section.count(), 0);

    update({ operations: [], unmeasured: 3 });
    await section.getByText("0 measured · 3 unmeasured", { exact: true }).waitFor({ timeout: LIVE_UPDATE_TIMEOUT });
    assert.equal(await section.getByRole("listitem").count(), 0);

    update({ operations: [operation("Instant call", 1, 0, 0)], unmeasured: 2 });
    await section.getByText("Instant call", { exact: true }).waitFor();
    assert.doesNotMatch(await section.innerText(), /%|Share of/);

    update({ operations, unmeasured: 2 });
    await section.getByText("16 measured · 2 unmeasured", { exact: true }).waitFor();
    const rows = section.getByRole("listitem");
    assert.equal(await rows.count(), 4);
    assert.match(await rows.first().innerText(), /Repeated checks.*20 s total.*10 calls.*Average 2 s.*Longest 3 s/s);
    assert.match(await rows.nth(1).innerText(), /15 s total · 33%/);
    assert.deepEqual(await rows.nth(1).locator("progress").evaluate(meter => [meter.value, meter.max]), [15_000, 45_500]);
    assert.equal(await section.getByText("Share of 45.5 s measured command time", { exact: true }).isVisible(), true);
    const showAll = section.getByRole("button", { name: "Show all 5 operations" });
    await showAll.click();
    assert.equal(await rows.count(), 5);

    const revised = [operation("One long build", 2, 123_000, 112_000), ...operations.filter(item => item.label !== "One long build")];
    update({ operations: revised, unmeasured: 2 });
    await rows.first().getByText("One long build", { exact: true }).waitFor();
    assert.match(await rows.first().innerText(), /2 min 3 s total.*Longest 1 min 52 s/s);
    assert.match(await rows.first().innerText(), /2 min 3 s total · 80%/);
    assert.match(await rows.last().innerText(), /500 ms total · <1%/);
    const showFewer = section.getByRole("button", { name: "Show fewer operations" });
    assert.equal(await showFewer.getAttribute("aria-expanded"), "true");
    assert.equal(await showFewer.evaluate(button => button === document.activeElement), true);
    assert.equal(await rows.count(), 5);
    await showFewer.click();
    assert.equal(await rows.count(), 4);
    await assertFixedDocument(page, viewport);
  } finally {
    forecastSessions.delete(session);
    await page.close();
  }
});

test("the leading command exposes observed costs and keeps its disclosure through live updates", async () => {
  const viewport = MOBILE_VIEWPORT;
  const page = await browser.newPage({ viewport });
  const session = "command-diagnosis";
  const { model } = fixture();
  const first = { label: "git commit", calls: 7, totalMs: 20_000, longestMs: 8_000 };
  const second = { label: "forge test", calls: 1, totalMs: 10_000, longestMs: 10_000 };
  const detail = {
    label: first.label,
    slowestCalls: [
      { elapsedMs: 8_000, outcome: "reviewRejected", reviewProvider: "claude" },
      { elapsedMs: 4_000, outcome: "failed", reviewProvider: "codex" },
      { elapsedMs: 3_000, outcome: "completed", reviewProvider: null },
      { elapsedMs: 2_000, outcome: "denied", reviewProvider: null },
      { elapsedMs: 2_000, outcome: "completed", reviewProvider: null },
    ],
    outcomes: [
      { outcome: "reviewRejected", calls: 1, totalMs: 8_000 },
      { outcome: "completed", calls: 4, totalMs: 6_000 },
      { outcome: "failed", calls: 1, totalMs: 4_000 },
      { outcome: "denied", calls: 1, totalMs: 2_000 },
    ],
    reviews: [
      { provider: "claude", calls: 1, totalMs: 8_000, rejectedCalls: 1, rejectedMs: 8_000 },
      { provider: "codex", calls: 1, totalMs: 4_000, rejectedCalls: 0, rejectedMs: 0 },
      { provider: null, calls: 5, totalMs: 8_000, rejectedCalls: 0, rejectedMs: 0 },
    ],
    processTiming: { calls: 2, observedMs: 7_000, processMs: 3_000 },
  };
  const timings = { operations: [first, second], unmeasured: 2, leaderDetail: detail };
  const feed = { model: { ...model, id: session, tasks: [], milestones: [], commandTimings: timings } };
  function update(commandTimings) {
    feed.model.commandTimings = commandTimings;
    writeState(feed.response, feed.model);
  }
  try {
    forecastSessions.set(session, feed);
    await page.goto(address.replace(SESSION_ID, session));
    await openOverview(page, viewport);
    const section = page.getByRole("region", { name: "Command hot path" });
    const disclosure = section.locator("details");
    const summary = disclosure.locator("summary");
    await summary.waitFor({ timeout: LIVE_UPDATE_TIMEOUT });
    assert.equal(await disclosure.count(), 1);
    assert.equal(await summary.innerText(), "Call breakdown · 3 unsuccessful calls · 14 s");
    assert.equal(await disclosure.evaluate(details => details.open), false);
    await summary.focus();
    await summary.press("Enter");
    await disclosure.getByText("Slowest 5 calls accounted for 95% of this operation’s time.").waitFor();
    assert.equal(await disclosure.getByText("8 s · Review rejected · Claude Code review", { exact: true }).isVisible(), true);
    assert.equal(await disclosure.getByText("2 of 7 calls · 7 s observed · 3 s process time · 4 s unattributed").isVisible(), true);
    assert.equal(await disclosure.getByText("1 call · 8 s · 40%", { exact: true }).isVisible(), true);
    assert.equal(await disclosure.getByText("No review reported", { exact: true }).isVisible(), true);
    assert.equal(await disclosure.getByText(/Hook time was not recorded separately\./).isVisible(), true);
    for (const layout of [viewport, DESKTOP_VIEWPORT]) {
      await page.setViewportSize(layout);
      assert.ok(await disclosure.evaluate((element, tolerance) =>
        element.scrollWidth <= element.clientWidth + tolerance, GEOMETRY_TOLERANCE));
      await assertFixedDocument(page, layout);
    }

    const replacement = {
      label: second.label,
      slowestCalls: [{ elapsedMs: 30_000, outcome: "unknown", reviewProvider: null }],
      outcomes: [{ outcome: "unknown", calls: 1, totalMs: 30_000 }],
      reviews: [], processTiming: null,
    };
    update({ ...timings, operations: [{ ...second, totalMs: 30_000, longestMs: 30_000 }, first], leaderDetail: replacement });
    await disclosure.getByText("30 s · Unknown outcome", { exact: true }).waitFor();
    assert.equal(await summary.evaluate(element => element === document.activeElement), true);
    assert.equal(await disclosure.getByText(/process time|Hook time/).count(), 0);

    update({ ...timings, operations: [{ ...second, totalMs: 31_000, longestMs: 31_000 }, first] });
    await section.getByText("31 s total · 61%").waitFor();
    assert.equal(await disclosure.count(), 0);
    assert.equal(await section.locator("#command-timings-coverage").evaluate(element => element === document.activeElement), true);

    update({ operations: [], unmeasured: 2 });
    await section.getByText("0 measured · 2 unmeasured", { exact: true }).waitFor();
    assert.equal(await disclosure.count(), 0);
  } finally {
    forecastSessions.delete(session);
    await page.close();
  }
});

test("quota readings show previous windows and last known usage until a live capture replaces them", async () => {
  const viewport = MOBILE_VIEWPORT;
  const page = await browser.newPage({ viewport });
  const session = "quota-readings";
  const at = Date.now();
  const minute = 60_000;
  const resetAfter = 10_000;
  const imminentReset = new Date(at + resetAfter).toISOString();
  const iso = offset => new Date(at + offset * minute).toISOString();
  const { model, usage } = fixture();
  const provider = usage.providers[0];
  const feed = { model: { ...model, id: session } };
  const [fiveHour, weekly] = provider.windows;
  const stale = { status: "stale", basis: null, exhaustsAt: null, sampleSeconds: null };
  Object.assign(fiveHour, { label: "5 hours", usedPercent: 3, stale: true, freshnessBasis: "unknown", resetsAt: iso(-5),
    observedAt: iso(-30), capturedAt: iso(-30), forecast: stale });
  Object.assign(weekly, { label: "7 days", usedPercent: 54, stale: true, resetsAt: imminentReset,
    observedAt: iso(-8), capturedAt: iso(0), forecast: stale });
  provider.windows = [fiveHour, weekly];
  provider.status = "stale";
  const publish = () => feed.response.write(`event: usage\ndata: ${JSON.stringify({ providers: [provider] })}\n\n`);
  try {
    forecastSessions.set(session, feed);
    await page.clock.install({ time: at });
    await page.goto(address.replace(SESSION_ID, session));
    await openOverview(page, viewport);
    await page.getByRole("heading", { name: "Primary window" }).first().waitFor();
    publish();
    const card = page.locator(".usage-provider", { has: page.getByRole("heading", { name: "Claude Code" }) });
    const short = card.locator(".usage-window", { hasText: "5 hours" });
    const week = card.locator(".usage-window", { hasText: "7 days" });
    await short.getByRole("heading", { name: "5 hours" }).waitFor();
    assert.equal(await short.locator(".usage-percent").innerText(), "Unknown");
    assert.equal(await short.getByRole("progressbar").count(), 0);
    assert.equal(await week.locator(".usage-percent").innerText(), "54%");
    assert.match(await week.innerText(), /Last known/);
    assert.match(await week.innerText(), /8 min ago/);
    await card.locator("summary").click();
    const details = card.locator(".disclosure-content");
    assert.match(await details.innerText(), /Previous window.*3%/s);
    assert.doesNotMatch(await details.innerText(), /No increase observed means/);
    weekly.measuredAt = iso(-6);
    publish();
    await week.getByText(/6 min ago/).waitFor();
    await page.clock.fastForward(resetAfter);
    await week.getByText("Window ended", { exact: true }).waitFor({ timeout: 3000 });
    assert.equal(await week.locator(".usage-percent").innerText(), "Unknown");
    Object.assign(weekly, { usedPercent: 55, observedAt: imminentReset, measuredAt: imminentReset, capturedAt: imminentReset, stale: false,
      resetsAt: iso(60), forecast: { status: "insufficient_history" } });
    provider.status = "available";
    publish();
    await week.getByText("55%", { exact: true }).waitFor();
    assert.equal(await week.getByRole("progressbar").count(), 1);
    assert.doesNotMatch(await week.innerText(), /Last known|Window ended/);
    assert.equal(await card.locator("details").getAttribute("open"), "");
    await assertFixedDocument(page, viewport);
  } finally {
    forecastSessions.delete(session);
    await page.close();
  }
});

test("finish history keeps observed revisions, unknown estimates and completed milestone history across reloads", async () => {
  const page = await browser.newPage({ viewport: VIEWPORTS[0] });
  const session = "forecast-history";
  const otherSession = "other-forecast-history";
  const name = "Delivery";
  const at = Date.now();
  const minute = 60 * 1000;
  const firstFinish = at + HOUR_MILLISECONDS;
  const revisedFinish = firstFinish - 20 * minute;
  const feed = {};
  function update(offset, finish, remaining = true) {
    const observedAt = new Date(at + offset).toISOString();
    feed.model = { ...fixture().model, id: session, observedAt,
      sync: { status: "current", checkedAt: observedAt },
      milestones: [{ name, progress: remaining ? 25 : 100, hasRemaining: remaining,
        hasEstimate: remaining, p50: finish === null ? null : new Date(finish).toISOString() }] };
    if (feed.response) writeState(feed.response, feed.model);
  }
  try {
    update(0, firstFinish);
    forecastSessions.set(session, feed);
    await page.goto(address.replace(SESSION_ID, session));
    const historyName = new RegExp(`^${name} finish history$`, "i");
    const trigger = page.getByRole("button", { name: historyName });
    const dialog = page.getByRole("dialog", { name: historyName });
    const chart = dialog.getByRole("slider", { name: "Inspect forecast observations" });
    const close = dialog.getByRole("button", { name: "Close finish history" });
    const observations = dialog.locator("summary", { hasText: "Recorded observations" });
    const rows = dialog.locator("tbody tr");
    async function openHistory() {
      await trigger.click({ timeout: 3000 });
      await dialog.waitFor();
      if (!await rows.first().isVisible()) await observations.click();
      await rows.first().waitFor();
    }
    const activeValue = () => chart.getAttribute("aria-valuetext");
    const chartHasFocus = () => chart.evaluate(el => el === document.activeElement);
    const recorded = () => rows.evaluateAll(items => items.map(row =>
      [...row.cells].map(cell => cell.querySelector("time")?.dateTime || cell.innerText)));
    const iso = value => new Date(value).toISOString();
    await openHistory();
    assert.deepEqual(await recorded(), [[iso(at), iso(firstFinish)]]);
    await chart.focus();
    const firstValue = await activeValue();
    feed.response.write(`event: heartbeat\ndata: ${JSON.stringify({
      sync: { status: "current", checkedAt: iso(at + minute) },
    })}\n\n`);
    update(minute, revisedFinish);
    await rows.nth(1).waitFor();
    assert.match(await trigger.innerText(), /20 min earlier/);
    assert.equal(await chartHasFocus(), true);
    await chart.press("Home");
    assert.equal(await activeValue(), firstValue);
    await chart.press("ArrowRight");
    const revisedValue = await activeValue();
    assert.notEqual(revisedValue, firstValue);
    await chart.press("ArrowDown");
    assert.equal(await activeValue(), firstValue);
    await chart.press("ArrowUp");
    assert.equal(await activeValue(), revisedValue);
    update(2 * minute, null);
    await rows.nth(2).waitFor();
    assert.equal(await chartHasFocus(), true);
    assert.equal(await activeValue(), revisedValue);
    await chart.press("End");
    assert.match(await activeValue(), /unknown/i);
    update(3 * minute, revisedFinish);
    await rows.nth(3).waitFor();
    assert.equal(await chartHasFocus(), true);
    assert.match(await activeValue(), /unknown/i);
    await chart.press("ArrowRight");
    assert.doesNotMatch(await activeValue(), /unknown/i);
    const lineSegments = await dialog.locator("path.finish-line").evaluateAll(paths => paths.flatMap(path =>
      path.getAttribute("d").match(/M[^M]*/g) || []));
    assert.equal(lineSegments.filter(segment => /L/i.test(segment)).length, 1);
    const expected = [[iso(at + 3 * minute), iso(revisedFinish)], [iso(at + 2 * minute), "Unknown"],
      [iso(at + minute), iso(revisedFinish)], [iso(at), iso(firstFinish)]];
    update(4 * minute, at + 4 * minute, false);
    await page.getByText(/Completed milestones.*Delivery/).waitFor();
    assert.deepEqual(await recorded(), expected);
    await page.keyboard.press("Escape");
    await dialog.waitFor({ state: "hidden" });
    assert.equal(await trigger.evaluate(el => el === document.activeElement), true);
    await page.reload();
    await openHistory();
    await rows.nth(3).waitFor();
    assert.deepEqual(await recorded(), expected);
    await close.click();
    await dialog.waitFor({ state: "hidden" });
    forecastSessions.set(otherSession, { model: { ...feed.model, id: otherSession, title: "Other delivery plan",
      milestones: [{ name, hasRemaining: true, hasEstimate: true, p50: iso(firstFinish) }] } });
    await page.goto(address.replace(SESSION_ID, otherSession));
    await page.locator("#session-title").filter({ hasText: "Other delivery plan" }).waitFor({ timeout: 3000 });
    await openHistory();
    assert.deepEqual(await recorded(), [[iso(at + 4 * minute), iso(firstFinish)]]);
  } finally {
    forecastSessions.delete(session);
    forecastSessions.delete(otherSession);
    await page.close();
  }
});

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
