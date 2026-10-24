from pathlib import Path
import shutil
import subprocess
import unittest


APP = Path(__file__).parents[1] / "live_gantt" / "static" / "app.js"
NODE = shutil.which("node")
HARNESS = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class Element {
  constructor() {
    this.dataset = {};
    this.hidden = true;
    this.value = "";
    this.textContent = "";
    this.classList = { toggle() {} };
  }
  addEventListener() {}
  setAttribute() {}
  removeAttribute() {}
  replaceChildren() {}
  append() {}
  querySelectorAll() { return []; }
}

async function main() {
  const scenario = process.argv[2];
  const elements = new Map();
  const streams = [];
  const requests = [];
  const timers = new Map();
  let nextTimer = 0;
  let firstDiscovery = true;
  let stateStatus = 503;
  const target = scenario === "invalid" ? "invalid/session" : "codex-target-session";
  const encodedTarget = encodeURIComponent(target);
  const other = { id: "other-session", title: "Other session", chartable: true, provider: "claude" };
  const location = { hash: `#session=${encodedTarget}`, pathname: "/", search: "" };
  class EventSource {
    static CLOSED = 2;
    constructor(url) {
      this.url = url;
      this.listeners = new Map();
      streams.push(this);
    }
    addEventListener(type, listener) { this.listeners.set(type, listener); }
    close() { this.closed = true; }
    emit(type, value) { this.listeners.get(type)({ data: JSON.stringify(value) }); }
  }
  const window = {
    addEventListener() {},
    setInterval() {},
    setTimeout(callback) { timers.set(++nextTimer, callback); return nextTimer; },
    clearTimeout(id) { timers.delete(id); },
  };
  const document = {
    hidden: false,
    addEventListener() {},
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, new Element());
      return elements.get(id);
    },
    createElement() { return new Element(); },
    querySelectorAll() { return []; },
  };
  const context = {
    AbortSignal, Date, Element, EventSource, Intl, JSON, URL, window, document, location,
    localStorage: { getItem() { return null; } },
    history: { replaceState(_state, _title, url) { location.hash = url.startsWith("#") ? url : ""; } },
    fetch: async url => {
      requests.push(url);
      if (url === "/api/lists") {
        if (firstDiscovery && scenario === "stream") {
          firstDiscovery = false;
          throw new Error("Temporary discovery failure");
        }
        return { ok: true, json: async () => ({ lists: [other] }) };
      }
      return { status: stateStatus, ok: stateStatus === 200 };
    },
  };
  vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), context);
  const settle = () => new Promise(resolve => setImmediate(resolve));
  await settle();
  if (scenario === "stream") {
    assert.equal(streams.at(-1).url, "/events/usage");
    streams.at(-1).emit("lists", { lists: [other] });
  }
  assert.equal(location.hash, `#session=${encodedTarget}`, "Partial discovery must retain the requested session hash");
  assert.equal(streams.at(-1).url, `/events?list=${encodedTarget}`, "The requested session must be checked before choosing a fallback");
  if (scenario === "missing" || scenario === "invalid") {
    stateStatus = scenario === "missing" ? 404 : 400;
    const current = streams.at(-1);
    current.readyState = EventSource.CLOSED;
    current.onerror();
    await timers.get(nextTimer)();
    await settle();
    assert.ok(requests.includes(`/api/state?list=${encodedTarget}`), "Fallback must follow a direct session check");
    assert.equal(location.hash, "#session=other-session");
    assert.equal(streams.at(-1).url, "/events?list=other-session");
  }
  if (scenario === "startup") {
    const current = streams.at(-1);
    current.readyState = EventSource.CLOSED;
    current.onerror();
    await timers.get(nextTimer)();
    await settle();
    assert.ok(requests.includes(`/api/state?list=${encodedTarget}`));
    assert.equal(location.hash, `#session=${encodedTarget}`, "A temporary state failure must not replace the requested session");
    assert.equal(streams.at(-1).url, `/events?list=${encodedTarget}`);
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
"""


class BrowserSessionTests(unittest.TestCase):
    def run_scenario(self, scenario):
        self.assertIsNotNone(NODE, "Browser JavaScript verification requires Node.js on PATH")
        result = subprocess.run([NODE, "-e", HARNESS, str(APP), scenario],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_initial_partial_discovery_and_retry_preserve_session_link(self):
        self.run_scenario("startup")

    def test_usage_stream_partial_discovery_preserves_session_link(self):
        self.run_scenario("stream")

    def test_confirmed_missing_session_uses_available_fallback(self):
        self.run_scenario("missing")

    def test_confirmed_invalid_session_uses_available_fallback(self):
        self.run_scenario("invalid")
