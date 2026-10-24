"use strict";

const PERCENT_SCALE = 100;
const SECOND = 1000;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
const MINUTES_PER_HOUR = HOUR / MINUTE;
const MINIMUM_FIT_SPAN = 12 * HOUR;
const FIT_PADDING_SHARE = 0.06;
const PAST_SHARE = 0.25;
const FOLLOW_REBASE_INTERVAL = 30 * SECOND;
const CONNECTION_STALE_AFTER = 40 * SECOND;
const DISCOVERY_RETRY_DELAY = 3 * SECOND;
const REQUEST_TIMEOUT = 10 * SECOND;
const MAX_MONITOR_ISSUES = 8;
const MAX_MONITOR_TEXT = 500;
const MONITOR_LABELS = {
  working: "Working",
  needs_you: "Needs you",
  waiting_for_quota: "Waiting for quota",
  idle: "Idle",
  plan_complete: "Plan complete",
  attention: "Needs attention",
  unverified: "Unable to verify",
};
const MINIMUM_PLOT_WIDTH = 540;
const MINIMUM_BAR_WIDTH = 3;
const BAR_LABEL_GAP = 7;
const DESIRED_TICK_WIDTH = 100;
const DEFAULT_ZOOM = "12h";
const ZOOM_STORAGE_KEY = "live-gantt.zoom";
const EXPANDED_STORAGE_KEY = "live-gantt.expanded";
const RECENTLY_FINISHED_WINDOW = 2 * HOUR;
const LIVE_AGENT_STATUSES = new Set(["running", "silent"]);
const SESSION_HASH_PREFIX = "#session=";
const RECENT_PICKER_SESSIONS = 10;
const MAX_LISTED_SESSIONS = 200;
const OPTION_PAGE_STEP = 8;
const PROJECT_PATH_SEGMENTS = 2;
const ABSOLUTE_DATE_AFTER = 30 * DAY;
const AGE_UNITS = [[DAY, "d"], [HOUR, "h"], [MINUTE, "min"]];
const FOLDED_DISCLOSURE = "\u25b8";
const EXPANDED_DISCLOSURE = "\u25be";
const TICK_STEPS = [15 * MINUTE, 30 * MINUTE, HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR, DAY, 2 * DAY, 7 * DAY, 30 * DAY];
const ZOOMS = {
  "12h": { span: 12 * HOUR, pixels: 1008 },
  "3d": { span: 3 * DAY, pixels: 1440 },
  week: { span: 7 * DAY, pixels: 1512 },
};

function savedZoom() {
  try {
    const zoom = localStorage.getItem(ZOOM_STORAGE_KEY);
    if (zoom === "fit" || Object.hasOwn(ZOOMS, zoom)) return zoom;
  } catch {}
  return DEFAULT_ZOOM;
}

function savedExpanded() {
  try {
    const keys = JSON.parse(localStorage.getItem(EXPANDED_STORAGE_KEY));
    if (Array.isArray(keys)) return new Set(keys.filter(key => typeof key === "string"));
  } catch {}
  return new Set();
}

const elements = Object.fromEntries(
  ["overview-panel", "overview-toggle", "monitor-status", "monitor-title", "monitor-summary", "monitor-activity", "monitor-details", "monitor-last-known", "monitor-issues", "usage-providers", "session-tokens", "token-values", "session-provider", "session", "session-heading", "session-name", "session-description", "session-control", "session-title", "session-meta", "session-panel", "session-filter", "session-all", "session-options", "session-summary", "project", "connection", "connection-text", "connection-age", "freshness", "overview", "overview-title", "harness-health", "forecast-health", "milestones", "timeline", "timeline-content", "notice", "follow", "task-count", "warnings", "details", "details-content", "close-details"].map(id => [id, document.getElementById(id)]),
);
const state = {
  model: null,
  usage: null,
  usageAges: [],
  usageForecasts: [],
  usagePaceHeadings: [],
  pageInactive: false,
  lists: [],
  selected: null,
  stream: null,
  zoom: savedZoom(),
  expanded: savedExpanded(),
  pickerAll: false,
  pickerActive: null,
  pickerShown: [],
  follow: true,
  connected: false,
  lastReceived: null,
  streamStarted: null,
  reconnectTimer: null,
  sync: null,
  liveness: null,
  monitor: null,
  monitorReceivedAt: null,
  range: null,
  plotWidth: MINIMUM_PLOT_WIDTH,
  lastRebase: 0,
  programmaticScroll: false,
  lastScrollLeft: 0,
  tracks: [],
  milestoneDates: [],
  detailsOpen: false,
  detailKey: null,
  detailEntries: new Map(),
  focusTargets: new Map(),
  observations: new Map(),
};

function node(tag, className, text) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text !== undefined) result.textContent = String(text);
  return result;
}

function timestamp(value) {
  if (!value) return null;
  const result = new Date(value).getTime();
  return Number.isFinite(result) ? result : null;
}

function number(value, fallback = 0) {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function percent(value) {
  return `${Math.round(Math.max(0, Math.min(PERCENT_SCALE, number(value))))}%`;
}

function hours(value) {
  return `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(number(value))} h`;
}

function duration(milliseconds) {
  const minutes = Math.floor(Math.max(0, milliseconds) / MINUTE);
  return minutes < MINUTES_PER_HOUR
    ? `${minutes} min`
    : `${Math.floor(minutes / MINUTES_PER_HOUR)} h ${minutes % MINUTES_PER_HOUR} min`;
}

function dateTime(value, full = false) {
  const time = typeof value === "number" ? value : timestamp(value);
  if (time === null) return "Unknown";
  return new Intl.DateTimeFormat(undefined, full
    ? { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" }
    : { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(time);
}

function forecastTime(time, now) {
  if (new Date(time).toDateString() !== new Date(now).toDateString()) return dateTime(time);
  const clock = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(time);
  return `Today, ${clock}`;
}

function titleCase(value) {
  return String(value || "unknown").replaceAll("_", " ").replace(/^./, letter => letter.toUpperCase());
}

function modelAge(now) {
  return Math.max(0, now - (timestamp(state.model?.observedAt) ?? now));
}

function projectedTime(value, now, anchored = false) {
  const result = timestamp(value);
  return result === null ? null : result + (anchored ? 0 : modelAge(now));
}

function taskSegments(task, now) {
  const anchored = task.projectionAnchored === true;
  const explicit = Array.isArray(task.projectedSegments);
  const segments = explicit ? task.projectedSegments : [{
    start: task.projectedStart,
    end: task.projectedEnd,
  }];
  return segments.filter(segment => segment && typeof segment === "object").map(segment => ({
    start: task.status === "in_progress" && !explicit
      ? now : projectedTime(segment.start, now, anchored),
    end: projectedTime(segment.end, now, anchored),
  })).filter(segment => segment.start !== null && segment.end !== null && segment.end >= segment.start);
}

function taskQuotaDelays(task) {
  return (Array.isArray(task.quotaDelays) ? task.quotaDelays : [])
    .filter(delay => delay && typeof delay === "object" && (delay.end == null || timestamp(delay.end) !== null))
    .map(delay => ({
    ...delay, start: timestamp(delay.start), end: timestamp(delay.end),
    })).filter(delay => delay.start !== null && (delay.end === null || delay.end >= delay.start));
}

function currentQuotaDelay(task, now) {
  return task.status === "completed" ? null : taskQuotaDelays(task).find(delay =>
    delay.start <= now && (delay.end === null || now < delay.end));
}

function quotaDelayDescription(delay) {
  const reason = delay.reason === "predicted" ? "Projected quota exhaustion" : "Quota limit reached";
  const resume = delay.end === null ? "Resume time unavailable" : `Resume ${dateTime(delay.end)}`;
  return `${providerName(delay.provider)} · ${resume} · ${reason}`;
}

function taskDescription(task, now = Date.now()) {
  const delay = currentQuotaDelay(task, now);
  return taskSubtitle(task, now) + (delay ? ` · ${quotaDelayDescription(delay)}` : "");
}

function setNotice(message) {
  elements.notice.textContent = message;
  elements.notice.hidden = !message;
}

function plural(count, noun) {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

function elapsedLabel(value, now) {
  const time = timestamp(value);
  if (time === null) return "Unknown date";
  const elapsed = Math.max(0, now - time);
  if (elapsed >= ABSOLUTE_DATE_AFTER) return new Date(time).toLocaleDateString();
  const [unit, label] = AGE_UNITS.find(([size]) => elapsed >= size) || [];
  return unit ? `${Math.floor(elapsed / unit)} ${label} ago` : "just now";
}

function shortProject(project) {
  return String(project || "").split("/").filter(Boolean).slice(-PROJECT_PATH_SEGMENTS).join("/");
}

function sessionIdentity(list) {
  if (list.id !== state.selected || !state.model) return list;
  const { title, prompt, project } = state.model;
  return { ...list, title, prompt, project };
}

function providerName(provider) {
  return provider === "codex" ? "Codex" : provider === "claude" ? "Claude Code" : "Unknown provider";
}

function renderFacts(container, list, now) {
  const facts = [node("span", "provider-label", providerName(list.provider)), list.active ? node("span", "session-active", "Active") : node("span", "", elapsedLabel(list.updatedAt, now))];
  const project = shortProject(list.project);
  if (project) facts.push(node("span", "", project));
  const work = [list.taskCount && plural(list.taskCount, "task"), list.agentCount && plural(list.agentCount, "agent")].filter(Boolean);
  if (work.length) facts.push(node("span", "", work.join(", ")));
  if (list.headless) facts.push(node("span", "headless", "headless"));
  container.replaceChildren(...facts);
}

function renderPicker() {
  const selected = state.lists.find(list => list.id === state.selected);
  elements.session.disabled = !state.lists.length;
  if (!selected) {
    elements["session-title"].textContent = state.lists.length ? "Choose a session" : "No sessions";
    elements["session-meta"].replaceChildren();
    return;
  }
  const identity = sessionIdentity(selected);
  elements["session-title"].textContent = identity.title || identity.id;
  renderFacts(elements["session-meta"], identity, Date.now());
  elements.session.title = [identity.title, identity.project, "Switch session (/)"].filter(Boolean).join("\n");
}

function optionId(id) {
  return `session-option-${id}`;
}

function isWorkSession(list) {
  return list.chartable && !list.headless;
}

function pickerSessions() {
  const words = elements["session-filter"].value.toLowerCase().split(/\s+/).filter(Boolean);
  return state.lists.map(sessionIdentity).filter(list => {
    if (!state.pickerAll && !isWorkSession(list) && list.id !== state.selected) return false;
    const text = [list.title, list.prompt, list.project, list.id, providerName(list.provider)].join(" ").toLowerCase();
    return words.every(word => text.includes(word));
  });
}

function sessionOption(list, now) {
  const option = node("li", "session-option");
  option.id = optionId(list.id);
  option.dataset.id = list.id;
  option.setAttribute("role", "option");
  option.setAttribute("aria-selected", String(list.id === state.selected));
  option.title = [list.title, list.prompt, list.project].filter(Boolean).join("\n");
  option.append(node("span", "session-option-title", list.title || list.id));
  if (list.prompt) option.append(node("span", "session-option-prompt", list.prompt));
  const facts = node("span", "session-meta");
  renderFacts(facts, list, now);
  option.append(facts);
  return option;
}

function pickerSummary(matching, shown, compact) {
  const parts = [];
  if (shown < matching) parts.push(`Showing ${shown} of ${matching}; ${compact ? "search or choose All sessions" : "type to narrow"}.`);
  if (!state.pickerAll) parts.push("Background runs and other chats are in All sessions.");
  return parts.join(" ") || `${plural(matching, "session")}.`;
}

function renderOptions() {
  const matching = pickerSessions();
  const compact = !state.pickerAll && !elements["session-filter"].value.trim();
  const limit = compact ? RECENT_PICKER_SESSIONS : MAX_LISTED_SESSIONS;
  const shown = matching.filter((list, index) => index < limit || (compact && list.id === state.selected));
  state.pickerShown = shown.map(list => list.id);
  if (!state.pickerShown.includes(state.pickerActive)) {
    state.pickerActive = state.pickerShown.includes(state.selected) ? state.selected : state.pickerShown[0] ?? null;
  }
  const now = Date.now();
  elements["session-options"].replaceChildren(...shown.map(list => sessionOption(list, now)));
  if (!shown.length) {
    const empty = node("li", "session-empty", "No matching sessions");
    empty.setAttribute("role", "presentation");
    elements["session-options"].append(empty);
  }
  elements["session-summary"].textContent = pickerSummary(matching.length, shown.length, compact);
  renderActiveOption(false);
}

function renderActiveOption(reveal) {
  for (const option of elements["session-options"].querySelectorAll("[role=option]")) {
    option.classList.toggle("active", option.dataset.id === state.pickerActive);
  }
  const active = state.pickerActive === null ? null : document.getElementById(optionId(state.pickerActive));
  if (active) {
    elements["session-filter"].setAttribute("aria-activedescendant", active.id);
    if (reveal) active.scrollIntoView({ block: "nearest" });
  } else {
    elements["session-filter"].removeAttribute("aria-activedescendant");
  }
}

function pickerOpen() {
  return !elements["session-panel"].hidden;
}

function openPicker() {
  if (elements.session.disabled) return;
  elements["session-panel"].hidden = false;
  elements.session.setAttribute("aria-expanded", "true");
  state.pickerActive = state.selected;
  elements["session-filter"].value = "";
  renderOptions();
  renderActiveOption(true);
  elements["session-filter"].focus();
}

function closePicker(returnFocus) {
  if (!pickerOpen()) return;
  elements["session-panel"].hidden = true;
  elements.session.setAttribute("aria-expanded", "false");
  if (returnFocus) elements.session.focus();
}

function chooseSession(id) {
  closePicker(true);
  if (id && id !== state.selected) selectSession(id);
}

function moveActiveOption(step) {
  if (!state.pickerShown.length) return;
  const index = state.pickerShown.indexOf(state.pickerActive);
  const next = index < 0 ? 0 : Math.max(0, Math.min(state.pickerShown.length - 1, index + step));
  state.pickerActive = state.pickerShown[next];
  renderActiveOption(true);
}

function renderLists(lists) {
  state.lists = Array.isArray(lists) ? lists : [];
  renderPicker();
  if (pickerOpen()) renderOptions();
}

function linkedSession() {
  if (!location.hash.startsWith(SESSION_HASH_PREFIX)) return null;
  try {
    return decodeURIComponent(location.hash.slice(SESSION_HASH_PREFIX.length));
  } catch {
    return null;
  }
}

function preferredSession() {
  return linkedSession() || state.lists.find(isWorkSession)?.id || null;
}

function linkSession(id) {
  const address = id === null ? `${location.pathname}${location.search}` : `${SESSION_HASH_PREFIX}${encodeURIComponent(id)}`;
  if (linkedSession() !== id) history.replaceState(null, "", address);
}

function closeStream() {
  window.clearTimeout(state.reconnectTimer);
  state.reconnectTimer = null;
  if (state.stream) state.stream.close();
  state.stream = null;
  state.connected = false;
}

function readLocal(url) {
  return fetch(url, { cache: "no-store", signal: AbortSignal.timeout(REQUEST_TIMEOUT) });
}

function recoverStream() {
  closeStream();
  renderConnection();
  if (document.hidden || state.pageInactive) return;
  state.reconnectTimer = window.setTimeout(async () => {
    const attempt = state.reconnectTimer;
    try {
      const response = await readLocal("/api/lists");
      if (response.ok) {
        const { lists } = await response.json();
        if (state.reconnectTimer !== attempt) return;
        renderLists(lists);
        if (state.selected && !state.lists.some(list => list.id === state.selected)) {
          const selected = await readLocal(`/api/state?list=${encodeURIComponent(state.selected)}`);
          if (state.reconnectTimer !== attempt) return;
          if (selected.status === 400 || selected.status === 404) {
            selectSession(state.lists.find(isWorkSession)?.id ?? null);
            return;
          }
        }
      }
    } catch {}
    if (state.reconnectTimer === attempt) connectStream();
  }, DISCOVERY_RETRY_DELAY);
}

function markReceived() {
  state.connected = true;
  state.lastReceived = Date.now();
  renderConnection();
}

function renderConnection() {
  const age = state.lastReceived === null ? null : Math.max(0, Math.floor((Date.now() - state.lastReceived) / SECOND));
  const live = state.connected && age !== null && age * SECOND < CONNECTION_STALE_AFTER;
  const checked = timestamp(state.sync?.checkedAt);
  const current = live && state.sync?.status === "current" && checked !== null && Date.now() - checked < CONNECTION_STALE_AFTER;
  elements.connection.classList.toggle("disconnected", !current);
  const label = !live ? "Disconnected" : current ? "Synced" : state.sync?.status === "retrying" ? "Retrying" : "Connecting";
  if (elements["connection-text"].textContent !== label) elements["connection-text"].textContent = label;
  elements["connection-age"].textContent = checked === null ? "" : `${Math.max(0, Math.floor((Date.now() - checked) / SECOND))}s ago`;
  elements.connection.title = `${checked === null ? "No successful source read yet." : `Last successful source read: ${dateTime(checked, true)}.`} ${state.sync?.error || "Local session files are checked every two seconds."}`;
  renderHarnessHealth(live);
  renderForecastHealth();
  renderMonitor(live);
}

function receiveMonitor(monitor) {
  if (!monitor || !Object.hasOwn(MONITOR_LABELS, monitor.state) || typeof monitor.summary !== "string"
      || !Array.isArray(monitor.issues) || !monitor.issues.every(issue => typeof issue === "string")
      || !(monitor.lastActivityAt === null || typeof monitor.lastActivityAt === "string"
        && timestamp(monitor.lastActivityAt) !== null)) return;
  state.monitor = {
    state: monitor.state,
    summary: monitor.summary.slice(0, MAX_MONITOR_TEXT),
    lastActivityAt: monitor.lastActivityAt,
    issues: monitor.issues.slice(0, MAX_MONITOR_ISSUES).map(issue => issue.slice(0, MAX_MONITOR_TEXT)),
  };
  state.monitorReceivedAt = Date.now();
}

function renderMonitor(connected) {
  const current = state.monitor;
  const age = state.monitorReceivedAt === null ? null : Date.now() - state.monitorReceivedAt;
  const verified = connected && current !== null && age !== null && age >= 0 && age < CONNECTION_STALE_AFTER;
  const status = verified ? current.state : "unverified";
  elements["monitor-status"].hidden = state.selected === null;
  elements["monitor-status"].dataset.state = status;
  const summary = verified ? current.summary : !connected
    ? "Connection lost. Reconnecting automatically."
    : age === null ? "Waiting for current session evidence."
    : "Session evidence expired. Waiting for a fresh monitor update.";
  for (const [id, text] of [["monitor-title", MONITOR_LABELS[status]], ["monitor-summary", summary]]) {
    if (elements[id].textContent !== text) elements[id].textContent = text;
  }
  elements["monitor-summary"].title = summary;
  const activity = timestamp(current?.lastActivityAt);
  elements["monitor-activity"].textContent = activity !== null && activity <= Date.now()
    ? `Last activity ${elapsedLabel(current.lastActivityAt, Date.now())}` : "No verified activity time";
  const lastKnown = elements["monitor-last-known"];
  lastKnown.hidden = verified || current === null;
  lastKnown.textContent = current === null ? "" : `Last reported: ${MONITOR_LABELS[current.state]}. ${current.summary}`;
  const issues = current?.issues || [];
  const issueList = elements["monitor-issues"];
  const content = JSON.stringify(issues);
  issueList.hidden = !issues.length;
  if (issueList.dataset.content !== content) {
    issueList.dataset.content = content;
    issueList.replaceChildren(...issues.map(issue => node("li", "", issue)));
  }
}

function parseEvent(event, receive) {
  try {
    receive(JSON.parse(event.data));
    markReceived();
  } catch {
    setNotice("An update could not be read. Reconnecting for a fresh snapshot.");
    recoverStream();
  }
}

function resetSession(id) {
  const hadSelection = state.selected !== null;
  state.selected = id;
  if (id !== null || hadSelection) linkSession(id);
  state.model = null;
  state.monitor = null;
  state.monitorReceivedAt = null;
  state.follow = true;
  state.range = null;
  state.detailsOpen = false;
  elements.details.hidden = true;
  elements["timeline-content"].replaceChildren();
  elements.milestones.replaceChildren();
  elements.overview.hidden = true;
  elements["session-tokens"].hidden = true;
  renderFreshness(null);
  elements.warnings.hidden = true;
  elements["task-count"].textContent = "";
  elements.project.textContent = shortProject(state.lists.find(list => list.id === id)?.project) || "Claude Code and Codex sessions";
  elements.project.title = "";
  elements["session-heading"].hidden = true;
  document.title = "Live Gantt";
  renderPicker();
  if (pickerOpen()) renderOptions();
  renderFollow();
}

function selectSession(id) {
  resetSession(id);
  setNotice(id ? "Reading session…" : "No sessions with recorded work. Waiting for local activity.");
  connectStream();
}

function connectStream() {
  closeStream();
  state.connected = false;
  state.lastReceived = null;
  state.sync = null;
  state.liveness = null;
  state.monitorReceivedAt = null;
  state.streamStarted = Date.now();
  renderConnection();
  if (document.hidden || state.pageInactive) return;
  const id = state.selected;
  const stream = new EventSource(id ? `/events?list=${encodeURIComponent(id)}` : "/events/usage");
  state.stream = stream;
  stream.addEventListener("state", event => {
    if (state.stream !== stream || !id) return;
    parseEvent(event, model => {
      state.model = model;
      state.sync = model.sync;
      state.liveness = model.liveness;
      receiveMonitor(model.monitor);
      renderLists(state.lists);
      renderModel();
    });
  });
  stream.addEventListener("usage", event => {
    if (state.stream === stream) parseEvent(event, renderUsage);
  });
  stream.addEventListener("lists", event => {
    if (state.stream !== stream) return;
    parseEvent(event, data => {
      renderLists(data.lists);
      if (!state.selected) {
        const next = preferredSession();
        if (next) selectSession(next);
      }
    });
  });
  stream.addEventListener("heartbeat", event => {
    if (state.stream === stream) parseEvent(event, data => {
      state.sync = data.sync;
      state.liveness = data.liveness;
      receiveMonitor(data.monitor);
    });
  });
  stream.addEventListener("missing", () => {
    if (state.stream !== stream) return;
    renderLists(state.lists.filter(list => list.id !== id));
    const next = state.lists.find(isWorkSession);
    selectSession(next?.id ?? null);
    if (!next) setNotice("This session is no longer available. Waiting for local activity; use All sessions to open another.");
  });
  stream.onopen = () => {
    if (state.stream === stream) markReceived();
  };
  stream.onerror = () => {
    if (state.stream !== stream) return;
    state.connected = false;
    renderConnection();
    if (id && !state.model) setNotice("Disconnected. Reconnecting to the local server…");
    if (stream.readyState === EventSource.CLOSED) recoverStream();
  };
}

function usageForecastPassed(window, now) {
  const exhaustion = timestamp(window.forecast?.exhaustsAt);
  return window.forecast?.status === "predicted" && exhaustion !== null && exhaustion <= now;
}

function usageForecast(window, now) {
  const forecast = window.forecast || {};
  const exhaustion = timestamp(forecast.exhaustsAt);
  if (window.stale || forecast.status === "stale") return "Stale; no current estimate";
  if (forecast.status === "reached") return "Limit reached";
  if (forecast.status === "predicted" && exhaustion !== null) return usageForecastPassed(window, now)
    ? "Awaiting fresh usage" : `Runs out ${forecastTime(exhaustion, now)}`;
  if (forecast.status === "resets_first") return "Reset comes first";
  if (forecast.status === "insufficient_history") return "Collecting history";
  if (forecast.status === "no_observed_pace") return "No increase observed";
  return "Estimate unavailable";
}

function usagePaceTooltip(window, now) {
  const forecast = window.forecast || {};
  if (window.stale || forecast.status === "stale") return "Observation window unavailable: usage is stale.";
  const passed = usageForecastPassed(window, now);
  if (forecast.basis === "provider") return passed ? "Previous provider estimate has passed; awaiting fresh usage."
    : "Forecast supplied by provider; no local observation window used.";
  const seconds = forecast.sampleSeconds;
  if (forecast.basis !== "recorded_changes" || typeof seconds !== "number" || !Number.isFinite(seconds) || seconds <= 0) return passed
    ? "Estimate time has passed; awaiting fresh usage." : "Observation window unavailable.";
  const roundedSeconds = Math.round(seconds);
  const minutes = Math.floor(roundedSeconds * SECOND / MINUTE);
  const remainder = roundedSeconds - minutes * MINUTE / SECOND;
  const span = [minutes ? `${minutes} min` : "", remainder ? `${remainder} sec` : ""].filter(Boolean).join(" ");
  return `${passed ? "Previous estimate" : "At current pace"}, based on ${span || "less than 1 sec"} of observed usage.${passed ? " Awaiting fresh usage." : ""}`;
}

function paintUsageForecasts(now) {
  for (const { field, window } of state.usageForecasts) {
    field.firstElementChild.textContent = usageForecast(window, now);
    field.title = usagePaceTooltip(window, now);
    field.classList.toggle("is-warning", Boolean(window.stale || window.forecast?.status === "stale"
      || window.forecast?.status === "reached" || usageForecastPassed(window, now)));
  }
  for (const { field, windows } of state.usagePaceHeadings) {
    field.title = windows.map(window => `${window.label || "Usage window"}: ${usagePaceTooltip(window, now)}`).join("\n");
  }
}

function addUsageAge(container, label, value, now) {
  const field = node("span");
  container.append(field);
  state.usageAges.push({ field, label, value });
  field.textContent = `${label}: ${elapsedLabel(value, now)}`;
  field.title = dateTime(value, true);
}

function renderUsage(data) {
  state.usage = data;
  state.usageAges = [];
  state.usageForecasts = [];
  state.usagePaceHeadings = [];
  const now = Date.now();
  const providers = Array.isArray(data?.providers) ? data.providers : [];
  const openProviders = new Set([...elements["usage-providers"].querySelectorAll("details[open]")].map(details => details.dataset.provider));
  const providerScroll = new Map([...elements["usage-providers"].querySelectorAll("details[open]")].map(details => [details.dataset.provider, details.querySelector(".disclosure-content").scrollTop]));
  const focusedProvider = document.activeElement?.closest(".usage-details")?.dataset.provider;
  elements["usage-providers"].replaceChildren();
  for (const provider of providers) {
    const windows = Array.isArray(provider.windows) ? provider.windows : [];
    const card = node("article", "usage-provider");
    const heading = node("div", "usage-provider-heading");
    heading.append(node("h3", "", providerName(provider.provider)));
    const status = provider.status === "available" ? "Recorded locally" : provider.status === "stale" ? "Stale" : "Unavailable";
    heading.append(node("span", `usage-status ${provider.status === "stale" ? "is-stale" : ""}`, status));
    card.append(heading);
    const disclosure = node("details", "compact-disclosure usage-details");
    disclosure.dataset.provider = provider.provider;
    disclosure.append(node("summary", "", "Details"));
    const provenance = node("div", "disclosure-content");
    disclosure.append(provenance);
    const columns = node("div", "usage-columns");
    for (const label of ["Window", "Used", "Resets"]) columns.append(node("span", "", label));
    const outlook = node("span", "", "Exhaustion ");
    const pace = node("span", "usage-pace", "at current pace");
    state.usagePaceHeadings.push({ field: pace, windows });
    outlook.append(pace);
    columns.append(outlook);
    if (provider.message) {
      const message = node("p", "usage-message", provider.message);
      if (provider.status === "available") provenance.append(message);
      else card.append(message);
    }
    if (windows.length) card.append(columns);
    for (const window of windows) {
      const section = node("section", "usage-window");
      const title = node("div", "usage-window-heading");
      title.append(node("h4", "", window.label || "Usage window"));
      if (window.stale) title.append(node("span", "usage-status is-stale", "Stale"));
      section.append(title);
      const known = typeof window.usedPercent === "number" && Number.isFinite(window.usedPercent);
      const amount = node("div", "usage-amount");
      amount.append(node("strong", "usage-percent", known ? `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(window.usedPercent)}%` : "Unknown"));
      if (known) {
        const meter = node("progress", "usage-meter");
        meter.max = PERCENT_SCALE;
        meter.value = window.usedPercent;
        meter.setAttribute("aria-label", `${providerName(provider.provider)}, ${window.label || "usage window"}: ${window.usedPercent}% used`);
        amount.append(meter);
      }
      section.append(amount);
      const reset = timestamp(window.resetsAt);
      section.append(node("p", "usage-reset", reset === null ? "Unknown" : forecastTime(reset, now)));
      const forecast = node("div", "usage-forecast");
      const basis = window.forecast?.basis;
      forecast.append(node("p"));
      state.usageForecasts.push({ field: forecast, window });
      section.append(forecast);
      const ages = node("div", "usage-ages");
      ages.append(node("strong", "", window.label || "Usage window"));
      ages.append(node("span", "", basis === "provider" ? "Forecast supplied by provider" : basis === "recorded_changes" ? "Forecast estimated from observed usage over time" : "No recorded forecast basis"));
      ages.append(node("span", "", "No increase observed means there is no exhaustion estimate; collecting history requires more observations."));
      if (timestamp(window.capturedAt) !== null) addUsageAge(ages, "Captured locally", window.capturedAt, now);
      else ages.append(node("span", "", "Local capture time unknown"));
      if (timestamp(window.measuredAt) !== null) addUsageAge(ages, "Provider measurement", window.measuredAt, now);
      else {
        ages.append(node("span", "", "Provider measurement age unknown"));
        const observation = window.observedAt || window.capturedAt;
        if (timestamp(observation) !== null) addUsageAge(ages, "Last distinct observation", observation, now);
      }
      if (window.freshnessBasis === "recorded_change") ages.append(node("span", "", "Freshness follows recorded changes, not repeat captures."));
      provenance.append(ages);
      card.append(section);
    }
    if (!windows.length && !provider.message) card.append(node("p", "usage-message", "No local usage record available."));
    if (provider.sourceId) provenance.append(node("p", "usage-source", `Source session: ${provider.sourceId}`));
    if (windows.length || provider.sourceId) {
      disclosure.open = openProviders.has(provider.provider);
      card.append(disclosure);
    }
    elements["usage-providers"].append(card);
    provenance.scrollTop = providerScroll.get(provider.provider) || 0;
  }
  if (!providers.length) elements["usage-providers"].append(node("p", "usage-empty", "Local provider usage is unavailable."));
  paintUsageForecasts(now);
  if (focusedProvider) {
    const disclosure = [...elements["usage-providers"].querySelectorAll(".usage-details")].find(details => details.dataset.provider === focusedProvider);
    disclosure?.querySelector("summary").focus({ preventScroll: true });
  }
}

function renderTokens() {
  const usage = state.model?.tokenUsage || {};
  const fields = [["totalTokens", "Total"], ["inputTokens", "Input"], ["outputTokens", "Output"], ["cachedInputTokens", "Cached input"], ["cacheWriteTokens", "Cache writes"], ["reasoningOutputTokens", "Reasoning output"]];
  elements["token-values"].replaceChildren();
  for (const [key, label] of fields) {
    const field = node("div");
    const known = typeof usage[key] === "number" && Number.isFinite(usage[key]);
    field.append(node("dt", "", label), node("dd", "", known ? new Intl.NumberFormat().format(usage[key]) : "Unknown"));
    elements["token-values"].append(field);
  }
  elements["session-tokens"].hidden = false;
}

function fitRange(now) {
  const times = [now - MINIMUM_FIT_SPAN * PAST_SHARE, now + MINIMUM_FIT_SPAN * (1 - PAST_SHARE)];
  const workflows = state.model?.workflows || [];
  const activities = [...workflows];
  const agents = state.model?.agents || [];
  const span = activitySpan(agents, now);
  if (span.start !== null) times.push(span.start);
  if (span.end !== null) times.push(span.end);
  for (const task of state.model?.tasks || []) {
    for (const value of [timestamp(task.started), timestamp(task.ended),
      projectedTime(task.projectedStart, now, task.projectionAnchored === true),
      projectedTime(task.projectedEnd, now, task.projectionAnchored === true),
      ...taskSegments(task, now).flatMap(segment => [segment.start, segment.end]),
      ...taskQuotaDelays(task).flatMap(delay => [delay.start, delay.end])]) {
      if (value !== null) times.push(value);
    }
    const taskVisible = !isFinishedLongAgo(task, now) || state.expanded.has(expansionKey("done", task.lane, task.milestone ?? null));
    if (taskVisible && state.expanded.has(expansionKey("task-details", task.id))) {
      activities.push(...(task.agents || []), ...(task.runs || []));
    }
  }
  for (const workflow of workflows) {
    if (state.expanded.has(expansionKey("workflow-details", workflow.id))) activities.push(...(workflow.agents || []));
  }
  for (const activity of activities) {
    const start = timestamp(activity.started);
    const end = agentEnd(activity, now);
    if (start !== null) times.push(start);
    if (end !== null) times.push(end);
  }
  const start = Math.min(...times);
  const end = Math.max(...times);
  const padding = (end - start) * FIT_PADDING_SHARE;
  return { start: start - padding, end: end + padding };
}

function setRange(now) {
  const labelWidth = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--label-width"));
  const available = Math.max(MINIMUM_PLOT_WIDTH, elements.timeline.clientWidth - labelWidth);
  if (state.zoom === "fit") {
    state.range = fitRange(now);
    state.plotWidth = available;
  } else {
    const zoom = ZOOMS[state.zoom];
    state.range = { start: now - zoom.span * PAST_SHARE, end: now + zoom.span * (1 - PAST_SHARE) };
    state.plotWidth = Math.max(available, zoom.pixels);
  }
  elements["timeline-content"].style.setProperty("--plot-width", `${state.plotWidth}px`);
  state.lastRebase = now;
}

function xPosition(time) {
  return (time - state.range.start) / (state.range.end - state.range.start) * state.plotWidth;
}

function tickStep() {
  const desired = (state.range.end - state.range.start) * DESIRED_TICK_WIDTH / state.plotWidth;
  return TICK_STEPS.find(step => step >= desired) || TICK_STEPS[TICK_STEPS.length - 1];
}

function makeTrack(className = "") {
  const track = node("div", `row-plot ${className}`.trim());
  const step = tickStep();
  const firstTick = Math.ceil(state.range.start / step) * step;
  track.style.setProperty("--tick-width", `${step / (state.range.end - state.range.start) * state.plotWidth}px`);
  track.style.setProperty("--tick-offset", `${xPosition(firstTick)}px`);
  track.append(node("span", "now-line"));
  return track;
}

function makeRow(label, track, className = "") {
  const row = node("div", `timeline-row ${className}`.trim());
  row.append(label, track);
  elements["timeline-content"].append(row);
  return row;
}

function renderAxis() {
  const label = node("div", "row-label", "Work / local time");
  const track = makeTrack();
  const step = tickStep();
  const firstTick = Math.ceil(state.range.start / step) * step;
  for (let time = firstTick; time < state.range.end; time += step) {
    const tick = node("span", "axis-tick");
    tick.style.left = `${xPosition(time)}px`;
    tick.append(node("strong", "", new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" }).format(time)));
    tick.append(node("span", "", new Intl.DateTimeFormat(undefined, { weekday: "short", month: "short", day: "numeric" }).format(time)));
    track.append(tick);
  }
  track.querySelector(".now-line").append(node("span", "now-label", "Now"));
  makeRow(label, track, "axis-row");
}

function renderLane(name, count) {
  const label = node("div", "row-label", name);
  label.append(node("span", "lane-count", count));
  makeRow(label, makeTrack(), "lane-row");
}

function detailField(list, name, value) {
  if (value === undefined || value === null || value === "") return;
  list.append(node("dt", "", name), node("dd", "", value));
}

function showDetails(item, kind, key) {
  const content = elements["details-content"];
  const sameSelection = state.detailsOpen && state.detailKey === key;
  const previousMetadata = sameSelection ? content.querySelector(".task-metadata") : null;
  const metadataOpen = previousMetadata?.open || false;
  const metadataFocused = previousMetadata?.contains(document.activeElement) || false;
  const previousScroll = sameSelection ? elements.details.scrollTop : 0;
  state.detailsOpen = true;
  state.detailKey = key;
  content.replaceChildren();
  const heading = item.subject || item.label || item.name || item.description || `Agent ${item.id}`;
  content.append(node("h2", "", heading));
  if (item.description && item.description !== heading) content.append(node("p", "", item.description));
  const fields = node("dl");
  detailField(fields, "Status", kind === "task" ? taskState(item) : titleCase(item.status));
  detailField(fields, "Waiting", kind === "task" && item.queued ? waitingLabel(item) : undefined);
  detailField(fields, "Task", kind === "task" ? item.id : undefined);
  detailField(fields, "Lane", item.lane);
  detailField(fields, "Milestone", item.milestone);
  detailField(fields, "Progress", kind === "task" ? percent(item.effectiveProgress ?? item.progress) : undefined);
  const now = Date.now();
  const expectedFinish = kind === "task" ? projectedTime(item.projectedEnd, now, item.projectionAnchored === true) : null;
  detailField(fields, "Expected finish", expectedFinish !== null ? dateTime(expectedFinish) : undefined);
  if (kind === "task" && item.status !== "completed") {
    for (const delay of taskQuotaDelays(item).filter(delay => delay.end === null || delay.end > now)) {
      detailField(fields, delay.start <= now ? "Waiting for quota" : "Quota pause projected", quotaDelayDescription(delay));
    }
  }
  detailField(fields, "Last update", item.updatedAt ? dateTime(item.updatedAt, true) : undefined);
  detailField(fields, "Started", item.started ? dateTime(item.started, true) : undefined);
  detailField(fields, "Ended", item.ended ? dateTime(item.ended, true) : undefined);
  detailField(fields, "Workflow", item.workflow);
  detailField(fields, "Phase", item.phase);
  detailField(fields, "Agent type", item.agentType);
  detailField(fields, "Branch", item.worktreeBranch);
  detailField(fields, "Last activity", item.lastActivityAt ? dateTime(item.lastActivityAt, true) : undefined);
  detailField(fields, "Activity", item.activity);
  detailField(fields, "Log", item.log);
  if (item.estimate) {
    detailField(fields, "Task estimate", `${hours(item.estimate.likely)} likely (${hours(item.estimate.low)}–${hours(item.estimate.high)})`);
  }
  content.append(fields);
  for (const warning of [...(item.warnings || []), ...(item.markers || [])]) content.append(node("p", "detail-warning", warning));
  if (item.history?.length) {
    content.append(node("h3", "", "History"));
    const history = node("ul");
    for (const entry of item.history) history.append(node("li", "", `${dateTime(entry.time, true)} · ${titleCase(entry.event)}${entry.value !== undefined ? `: ${typeof entry.value === "object" ? JSON.stringify(entry.value) : entry.value}` : ""}`));
    content.append(history);
  }
  if (item.metadata && Object.keys(item.metadata).length) {
    const metadata = node("details", "task-metadata");
    metadata.open = metadataOpen;
    metadata.append(node("summary", "", "Task metadata"), node("pre", "", JSON.stringify(item.metadata, null, 2)));
    content.append(metadata);
    if (metadataFocused) metadata.querySelector("summary").focus({ preventScroll: true });
  }
  elements.details.hidden = false;
  elements.details.scrollTop = previousScroll;
  for (const [entryKey, entry] of state.detailEntries) {
    entry.target.setAttribute("aria-expanded", String(entryKey === key));
  }
}

function registerFocus(target, key) {
  target.dataset.focusKey = key;
  state.focusTargets.set(key, target);
}

function toggleExpanded(key) {
  if (!state.expanded.delete(key)) state.expanded.add(key);
  try { localStorage.setItem(EXPANDED_STORAGE_KEY, JSON.stringify([...state.expanded])); } catch {}
  renderTimeline(Date.now(), state.zoom === "fit");
}

function closeDetails(restoreFocus = true) {
  const key = state.detailKey;
  state.detailsOpen = false;
  state.detailKey = null;
  elements.details.hidden = true;
  if (state.model) renderTimeline(Date.now(), state.follow);
  if (restoreFocus) state.focusTargets.get(key)?.focus();
}

function attachDetails(target, item, kind, key) {
  registerFocus(target, key);
  state.detailEntries.set(key, { target, item, kind });
  target.setAttribute("aria-controls", "details");
  target.setAttribute("aria-expanded", String(state.detailsOpen && state.detailKey === key));
  target.addEventListener("click", () => {
    showDetails(item, kind, key);
    renderTimeline(Date.now(), state.follow);
    elements["close-details"].focus();
  });
}

function isOverrun(task, now) {
  const deadline = timestamp(task.overrunAt);
  return deadline !== null && now > deadline;
}

function taskState(task, now = Date.now()) {
  if (currentQuotaDelay(task, now)) return "Waiting for quota";
  return isOverrun(task, now) ? "Overrun" : task.blocked ? "Blocked" : task.queued ? "Queued" : titleCase(task.status);
}

function taskSubtitle(task, now = Date.now()) {
  const labels = [taskState(task, now)];
  if (task.queued) labels.push(waitingLabel(task));
  if (task.status === "in_progress") labels.push(percent(task.effectiveProgress ?? task.progress));
  const updated = timestamp(task.updatedAt);
  if (task.status === "in_progress" && updated !== null) labels.push(`updated ${duration(now - updated)} ago`);
  for (const observed of state.observations.get(task.id) || []) labels.push(`declared running; observed: ${observed}`);
  if (task.estimateMean !== null && task.estimateMean !== undefined) labels.push(hours(task.estimateMean));
  if (task.warnings?.length) labels.push("Warning");
  if (task.markers?.length) labels.push(...task.markers);
  return labels.join(" · ");
}

function waitingLabel(task) {
  return `after ${task.waitingFor.map(id => `#${id}`).join(", ")}`;
}

function expansionKey(...parts) {
  return JSON.stringify([state.selected, ...parts]);
}

function renderTask(task) {
  const agents = task.agents || [];
  const runs = task.runs || [];
  const key = expansionKey("task-details", task.id);
  const shown = state.expanded.has(key);
  const label = node("div", "row-label task-label");
  const main = node("button", "task-main");
  main.type = "button";
  const title = node("span", "task-title");
  title.append(node("span", "task-id", `#${task.id} `), document.createTextNode(task.subject || "Untitled task"));
  const meta = node("span", "task-meta", taskSubtitle(task));
  main.append(title, meta);
  main.title = taskDescription(task);
  main.setAttribute("aria-label", `Task ${task.id}: ${task.subject || "Untitled task"}. ${taskDescription(task)}. Show details.`);
  attachDetails(main, task, "task", `task:${task.id}`);
  label.append(main);
  if (agents.length || runs.length) {
    const counts = [agents.length ? plural(agents.length, "recorded agent") : "", runs.length ? plural(runs.length, "run") : ""].filter(Boolean).join(" and ");
    const detailCount = agents.length + runs.length;
    const noun = runs.length ? "detail" : "agent";
    label.append(foldBadge(`${shown ? "Hide" : "Show"} ${plural(detailCount, noun)}`, `all ${counts} for task ${task.id}`, key, shown));
  }
  const track = makeTrack();
  state.tracks.push({ track, item: task, kind: "task", main, meta });
  makeRow(label, track);
  if (shown) {
    for (const agent of agents) renderChild(agent, "agent", `task:${task.id}:agent:${agent.id}`);
    for (const [index, run] of runs.entries()) renderChild(run, "run", `task:${task.id}:run:${index}`);
  }
}

function foldBadge(text, folded, key, shown) {
  const badge = node("button", "detail-toggle", text);
  badge.type = "button";
  badge.setAttribute("aria-expanded", String(shown));
  badge.setAttribute("aria-label", `${shown ? "Hide" : "Show"} ${folded}`);
  registerFocus(badge, key);
  badge.addEventListener("click", () => toggleExpanded(key));
  return badge;
}

function renderWorkflow(workflow) {
  const agents = workflow.agents || [];
  const key = expansionKey("workflow-details", workflow.id);
  const shown = state.expanded.has(key);
  const label = node("div", "row-label task-label");
  const main = node("button", "task-main");
  main.type = "button";
  main.append(node("span", "task-title", workflow.name));
  const summary = `${titleCase(workflow.status)} · ${recordedAgentCounts(agents)}`;
  main.append(node("span", "task-meta", summary));
  main.title = [workflow.description || workflow.name, summary].join("\n");
  main.setAttribute("aria-label", `Workflow ${workflow.name}: ${summary}. Show details.`);
  attachDetails(main, workflow, "workflow", `workflow:${workflow.id}`);
  label.append(main);
  if (agents.length) label.append(foldBadge(`${shown ? "Hide" : "Show"} ${plural(agents.length, "agent")}`, `all ${plural(agents.length, "recorded agent")} for workflow ${workflow.name}`, key, shown));
  const track = makeTrack();
  state.tracks.push({ track, item: workflow, kind: "workflow" });
  makeRow(label, track, "overview-row");
  if (shown) for (const agent of agents) renderChild(agent, "agent", `workflow:${workflow.id}:agent:${agent.id}`);
}

function recordedAgentCounts(agents) {
  const counts = new Map();
  for (const agent of agents) {
    const status = agent.status || "unknown";
    counts.set(status, (counts.get(status) || 0) + 1);
  }
  const statuses = [...counts].sort(([left], [right]) => left.localeCompare(right))
    .map(([status, count]) => `${count} ${status.replaceAll("_", " ")}`).join(", ");
  return `${plural(agents.length, "recorded agent")}${statuses ? `: ${statuses}` : ""}`;
}

function activitySpan(agents, now) {
  const starts = agents.map(agent => timestamp(agent.started)).filter(value => value !== null);
  const ends = agents.map(agent => agentEnd(agent, now)).filter(value => value !== null);
  return { start: starts.length ? Math.min(...starts) : null, end: ends.length ? Math.max(...ends) : null };
}

function renderOtherAgents(agents) {
  const key = expansionKey("other-agents");
  const shown = state.expanded.has(key);
  const label = node("div", "row-label task-label");
  const main = node("div", "activity-main");
  const summary = recordedAgentCounts(agents);
  main.append(node("span", "task-title", "Other agent activity"), node("span", "task-meta", summary));
  main.title = summary;
  label.append(main, foldBadge(`${shown ? "Hide" : "Show"} ${plural(agents.length, "agent")}`, `all ${plural(agents.length, "recorded agent")}`, key, shown));
  const track = makeTrack();
  state.tracks.push({ track, item: agents, kind: "activity" });
  makeRow(label, track, "overview-row");
  if (shown) for (const agent of agents) renderChild(agent, "agent", `agent:${agent.id}`);
}

function renderSummary(lane, milestone, tasks) {
  const key = expansionKey("done", lane, milestone ?? null);
  const shown = state.expanded.has(key);
  const spans = tasks.map(task => [timestamp(task.started), timestamp(task.ended)]).filter(([start, end]) => start !== null && end !== null);
  const worked = spans.reduce((total, [start, end]) => total + end - start, 0);
  const text = `${milestone ? `${milestone} \u00b7 ` : ""}${tasks.length} done${worked ? `, ${hours(worked / HOUR)}` : ""}`;
  const label = node("button", "row-label");
  label.type = "button";
  label.append(node("span", "task-title", `${shown ? EXPANDED_DISCLOSURE : FOLDED_DISCLOSURE} ${text}`));
  label.title = `${shown ? "Fold" : "Show"} finished work: ${text}`;
  label.setAttribute("aria-expanded", String(shown));
  registerFocus(label, key);
  label.addEventListener("click", () => toggleExpanded(key));
  const track = makeTrack();
  const starts = spans.map(([start]) => start);
  const ends = spans.map(([, end]) => end);
  state.tracks.push({ track, item: { started: Math.min(...starts), ended: Math.max(...ends) }, kind: "summary" });
  makeRow(label, track, "summary-row");
  if (shown) for (const task of tasks) renderTask(task);
}

function renderChild(item, kind, key, className = "child-row") {
  const label = node("button", "row-label");
  label.type = "button";
  const title = kind === "run" ? item.label || "Run" : item.description || (item.agentType === "session" ? "Session activity" : `Agent ${item.id}`);
  label.append(node("span", "task-title", `${titleCase(item.status)} \u00b7 ${title}`));
  label.title = title;
  attachDetails(label, item, kind, key);
  const track = makeTrack();
  state.tracks.push({ track, item, kind });
  makeRow(label, track, className);
}

function createBar(track, className, start, end, text, pulse = false) {
  if (start === null || end === null || end < state.range.start || start > state.range.end) return null;
  const left = Math.max(0, xPosition(start));
  const right = Math.min(state.plotWidth, xPosition(Math.max(start, end)));
  const bar = node("span", `bar ${className}`);
  bar.style.left = `${left}px`;
  bar.style.width = `${Math.max(MINIMUM_BAR_WIDTH, right - left)}px`;
  if (pulse) bar.append(node("span", "live-dot"));
  track.append(bar);
  if (text) {
    const label = node("span", "bar-label", text);
    track.append(label);
    const dotWidth = pulse ? bar.querySelector(".live-dot").offsetWidth + BAR_LABEL_GAP : 0;
    const labelWidth = label.offsetWidth;
    const fits = labelWidth + dotWidth + BAR_LABEL_GAP * 2 <= bar.clientWidth;
    const insideOffset = bar.clientWidth - labelWidth - BAR_LABEL_GAP;
    const outsideOffset = bar.offsetWidth + BAR_LABEL_GAP;
    label.style.left = `${left + (fits ? insideOffset : outsideOffset)}px`;
  }
  return bar;
}

function paintTask(track, task, now) {
  const started = timestamp(task.started);
  const ended = timestamp(task.ended);
  const projectedEnd = projectedTime(task.projectedEnd, now, task.projectionAnchored === true);
  const segments = taskSegments(task, now);
  if (task.status === "completed") {
    if (started !== null && ended !== null) createBar(track, "done", started, ended, "Done");
    else track.append(node("span", "row-marker", "No history"));
  } else if (task.status === "in_progress") {
    const hasTail = segments.some(segment => segment.start <= now && segment.end > now);
    if (started !== null) createBar(track, `${isOverrun(task, now) ? "overrun" : "running"}${hasTail ? " has-tail" : ""}`, started, now, percent(task.effectiveProgress ?? task.progress), true);
    else track.append(node("span", "row-marker", "No history"));
  }
  if (task.status !== "completed") {
    for (const segment of segments) {
      const running = task.status === "in_progress";
      const start = running ? Math.max(now, segment.start) : segment.start;
      if (segment.end < start || (running && segment.end === start)) continue;
      const tail = running && segment.start <= now ? " projected-tail" : "";
      createBar(track, `projected${tail}${task.queued ? " queued" : ""}`, start, segment.end,
        task.queued ? waitingLabel(task) : "Projected");
    }
    for (const delay of taskQuotaDelays(task)) {
      const pause = createBar(track, `quota-pause${delay.end === null ? " open-end" : ""}`,
        delay.start, delay.end ?? state.range.end);
      if (pause) {
        pause.textContent = "Quota pause";
        pause.title = quotaDelayDescription(delay);
        pause.setAttribute("aria-label", `Quota pause: ${pause.title}`);
      }
    }
  }
  if (task.status !== "completed" && projectedEnd === null && !currentQuotaDelay(task, now)) {
    const reason = (task.markers || []).join(" · ") || (task.blocked ? "unresolved prerequisite" : "Unestimated");
    const marker = node("span", `row-marker ${task.blocked ? "blocked-marker" : "warning"}`, `${task.blocked ? "Blocked · " : ""}${reason}`);
    if (started !== null) marker.style.left = `${Math.max(10, Math.min(state.plotWidth - 140, xPosition(now) + 10))}px`;
    track.append(marker);
  }
}

function agentClass(item) {
  return item.status === "running" ? "running" : item.status === "silent" || item.status === "idle" ? "silent" : item.status === "completed" ? "done" : "blocked";
}

function agentEnd(item, now) {
  return timestamp(item.ended) ?? (LIVE_AGENT_STATUSES.has(item.status) ? now : timestamp(item.lastActivityAt));
}

function childBarLabel(item) {
  if (item.status === "running") return item.activity || "Running";
  return item.status === "completed" ? "Done" : titleCase(item.status);
}

function paintChild(track, item, now) {
  createBar(track, agentClass(item), timestamp(item.started), agentEnd(item, now), childBarLabel(item), item.status === "running");
}

function paintTimeline(now) {
  if (!state.range) return;
  for (const { track, item, kind, main, meta } of state.tracks) {
    for (const old of track.querySelectorAll(".bar, .bar-label, .row-marker")) old.remove();
    if (kind === "task") {
      paintTask(track, item, now);
      main.title = taskDescription(item, now);
      meta.textContent = taskSubtitle(item, now);
    } else if (kind === "summary") createBar(track, "done summary-bar", timestamp(item.started), timestamp(item.ended));
    else if (kind === "activity") {
      const span = activitySpan(item, now);
      const running = item.some(agent => agent.status === "running");
      createBar(track, running ? "running" : "activity-span", span.start, span.end, "Activity span", running);
      if (span.start === null || span.end === null) track.append(node("span", "row-marker", "No recorded timing"));
    } else paintChild(track, item, now);
  }
  for (const line of elements["timeline-content"].querySelectorAll(".now-line")) {
    line.style.left = `${xPosition(now)}px`;
    line.hidden = now < state.range.start || now > state.range.end;
  }
}

function renderHarnessHealth(connected) {
  const health = elements["harness-health"];
  const checked = timestamp(state.liveness?.checkedAt);
  const age = checked === null ? null : Date.now() - checked;
  const fresh = age !== null && age >= 0 && age < CONNECTION_STALE_AFTER;
  const reported = state.liveness?.status;
  const status = connected && fresh && (reported === "alive" || reported === "exited") ? reported : "unknown";
  health.textContent = `Harness: ${status}`;
  health.dataset.status = status;
  const reason = !connected ? "Connection disconnected; harness status is unknown."
    : !fresh ? checked === null ? "No valid harness check received." : "Harness check expired or has an invalid time."
    : state.liveness?.detail || "No verified harness state is available.";
  health.title = `${reason}${checked === null ? "" : ` Checked: ${dateTime(checked, true)}.`}`;
}

function renderForecastHealth() {
  const stale = state.model?.freshness?.stale;
  const changed = state.model?.freshness?.sinceChangeSeconds;
  const updated = (state.model?.tasks || []).reduce((latest, task) => {
    const time = timestamp(task.updatedAt);
    if (time === null) return latest;
    return latest === null ? time : Math.max(latest, time);
  }, null);
  const age = Number.isFinite(changed) ? changed * SECOND + modelAge(Date.now())
    : updated === null ? null : Math.max(0, Date.now() - updated);
  const message = !state.model ? "" : stale ? "Plan: stale"
    : age === null ? "Plan: no progress report"
    : `Plan: updated ${duration(age)} ago`;
  const health = elements["forecast-health"];
  if (health.textContent !== message) health.textContent = message;
  health.title = age === null ? "No recorded plan update is available."
    : `Last recorded plan update was ${duration(age)} ago.${stale ? " Estimates need attention." : ""}`;
  health.classList.toggle("is-warning", Boolean(state.model && stale));
}

function renderMilestones(now) {
  elements.milestones.replaceChildren();
  state.milestoneDates = [];
  const cards = node("div", "milestone-cards");
  const done = [];
  const milestones = state.model.milestones || [];
  for (const milestone of milestones) {
    if (!milestone.hasRemaining) {
      done.push(milestone.name);
      continue;
    }
    const name = milestone.name || "Ungrouped";
    const card = node("article", "milestone");
    const heading = node("div", "milestone-heading");
    heading.append(node("h3", "", name));
    const progress = node("div", "milestone-progress");
    const amount = node("div", "milestone-amount");
    amount.append(node("strong", "milestone-percentage", percent(milestone.progress)), node("span", "", "progress"));
    const meter = node("progress", "milestone-meter");
    meter.max = PERCENT_SCALE;
    meter.value = parseFloat(percent(milestone.progress));
    meter.setAttribute("aria-label", `${name} progress`);
    progress.append(amount, meter);
    const measures = node("div", "milestone-measures");
    measures.append(progress);
    for (const [key, title] of [["p50", "Expected finish"], ["p90", "Later estimate"]]) {
      const field = node("div", `milestone-date ${key === "p50" ? "expected-finish" : "later-finish"}`);
      field.title = key === "p50"
        ? "50th-percentile planning estimate (P50), based on the remaining task estimates"
        : "90th-percentile planning estimate (P90), a later finish allowing for task uncertainty; not a guarantee";
      const value = node("time", "forecast-time");
      const remaining = key === "p50" ? node("span", "forecast-remaining") : null;
      field.append(node("span", "forecast-label", title), value);
      if (remaining) field.append(remaining);
      measures.append(field);
      state.milestoneDates.push({ node: value, remaining, milestone, key });
    }
    card.append(heading, measures);
    if (!milestone.p50) {
      card.classList.add("forecast-unavailable");
      card.append(node("p", "forecast-explanation", milestone.hasEstimate
        ? "Forecast unavailable. Review the plan for missing estimates or unresolved dependencies."
        : "Needs estimates. Ask your coding agent to estimate the remaining tasks."));
    }
    cards.append(card);
  }
  if (cards.childElementCount) elements.milestones.append(cards);
  if (done.length) elements.milestones.append(node("p", "milestone-summary", `Completed milestones: ${done.join(", ")}`));
  if (!milestones.length) {
    const hasAgents = (state.model.agents || []).length || (state.model.workflows || []).length;
    elements.milestones.append(node("p", "overview-empty", state.model.provider === "codex"
      ? "No supported task plan is recorded for this Codex session. Activity and usage are shown."
      : hasAgents
      ? "Agent activity is shown below. Add planned tasks with estimates to get progress and finish forecasts."
      : "Add planned tasks with estimates to see progress and a finish forecast."));
  }
  paintMilestones(now);
}

function paintMilestones(now) {
  for (const item of state.milestoneDates) {
    const anchored = !item.milestone.hasRemaining || item.milestone[`${item.key}Anchored`] === true;
    const time = projectedTime(item.milestone[item.key], now, anchored);
    item.node.textContent = time === null ? (item.key === "p50" ? "Unknown" : "Unavailable") : forecastTime(time, now);
    if (time === null) item.node.removeAttribute("datetime");
    else item.node.dateTime = new Date(time).toISOString();
    if (item.remaining) {
      item.remaining.textContent = time === null ? "" : time <= now
        ? "Expected now" : `About ${duration(Math.ceil((time - now) / MINUTE) * MINUTE)} left`;
    }
  }
}

function isFinishedLongAgo(task, now) {
  if (task.status !== "completed") return false;
  const ended = timestamp(task.ended);
  return ended === null || now - ended > RECENTLY_FINISHED_WINDOW;
}

function renderTimeline(now, resetRange = false) {
  if (!state.range || resetRange) setRange(now);
  const previousScroll = elements.timeline.scrollLeft;
  const previousTop = elements.timeline.scrollTop;
  const focusedKey = document.activeElement?.dataset.focusKey;
  elements["timeline-content"].replaceChildren();
  state.tracks = [];
  state.detailEntries = new Map();
  state.focusTargets = new Map();
  renderAxis();
  const lanes = new Map();
  for (const task of state.model.tasks || []) {
    const lane = task.lane;
    if (!lanes.has(lane)) lanes.set(lane, []);
    lanes.get(lane).push(task);
  }
  for (const [name, tasks] of lanes) {
    renderLane(name, tasks.length);
    const folded = new Map();
    const open = [];
    for (const task of tasks) {
      if (!isFinishedLongAgo(task, now)) open.push(task);
      else if (folded.has(task.milestone)) folded.get(task.milestone).push(task);
      else folded.set(task.milestone, [task]);
    }
    for (const [milestone, finished] of folded) renderSummary(name, milestone, finished);
    for (const task of open) renderTask(task);
  }
  const agents = state.model.agents || [];
  const sessions = agents.filter(agent => agent.agentType === "session");
  const otherAgents = agents.filter(agent => agent.agentType !== "session");
  if (sessions.length || otherAgents.length) {
    renderLane("Session activity", sessions.length + (otherAgents.length ? 1 : 0));
    for (const session of sessions) renderChild(session, "agent", `agent:${session.id}`, "session-row");
    if (otherAgents.length) renderOtherAgents(otherAgents);
  }
  const workflows = state.model.workflows || [];
  if (workflows.length) {
    renderLane("Workflows", workflows.length);
    for (const workflow of workflows) renderWorkflow(workflow);
  }
  paintTimeline(now);
  elements.timeline.scrollTop = previousTop;
  if (state.follow) followNow(now);
  else setScroll(previousScroll);
  state.focusTargets.get(focusedKey)?.focus({ preventScroll: true });
  if (state.detailsOpen) {
    const selected = state.detailEntries.get(state.detailKey);
    if (selected) showDetails(selected.item, selected.kind, state.detailKey);
    else {
      state.detailsOpen = false;
      elements.details.hidden = true;
    }
  }
}

function setScroll(value) {
  state.programmaticScroll = true;
  elements.timeline.scrollLeft = value;
  state.lastScrollLeft = elements.timeline.scrollLeft;
  requestAnimationFrame(() => { state.programmaticScroll = false; });
}

function followNow(now) {
  if (state.zoom === "fit") setScroll(0);
  else {
    const labelWidth = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--label-width"));
    const visiblePlot = elements.timeline.clientWidth - labelWidth;
    setScroll(Math.max(0, xPosition(now) - visiblePlot * PAST_SHARE));
  }
}

function renderFollow() {
  elements.follow.setAttribute("aria-pressed", String(state.follow));
  elements.follow.textContent = state.follow ? "Following now" : "Follow now";
}

function renderFreshness(freshness) {
  const findings = freshness?.stale ? freshness.findings || [] : [];
  state.observations = new Map();
  for (const finding of findings) {
    if (!finding.observed) continue;
    if (!state.observations.has(finding.taskId)) state.observations.set(finding.taskId, []);
    state.observations.get(finding.taskId).push(finding.observed);
  }
  elements.freshness.replaceChildren();
  elements.freshness.hidden = !findings.length;
  if (!findings.length) return;
  elements.freshness.append(node("h2", "", "This plan is stale"));
  const list = node("ul");
  for (const finding of findings) list.append(node("li", "", finding.message));
  elements.freshness.append(list);
}

function renderModel() {
  const now = Date.now();
  const model = state.model;
  elements.project.textContent = shortProject(model.project) || "Unknown project";
  elements.project.title = model.project || "";
  elements["session-heading"].hidden = false;
  elements["session-provider"].textContent = providerName(model.provider);
  renderTokens();
  elements["session-name"].textContent = shortProject(model.project) || model.title || "Session";
  elements["session-description"].textContent = model.title || "";
  elements["session-description"].hidden = !model.title || model.title === shortProject(model.project);
  document.title = `${model.title || "Session"} · Live Gantt`;
  const tasks = model.tasks || [];
  const completed = tasks.filter(task => task.status === "completed").length;
  elements.overview.hidden = false;
  elements["overview-title"].textContent = !tasks.length ? "No planned tasks"
    : completed === tasks.length ? "Planned tasks complete" : "Plan progress";
  elements["task-count"].textContent = tasks.length ? `${completed} of ${tasks.length} planned tasks complete` : "";
  renderFreshness(model.freshness);
  renderMilestones(now);
  renderTimeline(now, state.follow || !state.range);
  setNotice(state.tracks.length ? "" : "No tasks or active agents in this time window.");
  elements.warnings.replaceChildren();
  for (const warning of model.warnings || []) elements.warnings.append(node("p", "", warning));
  elements.warnings.hidden = !(model.warnings || []).length;
}

function tick() {
  const now = Date.now();
  const lastStreamActivity = state.lastReceived ?? state.streamStarted;
  if (state.stream && lastStreamActivity !== null && now - lastStreamActivity >= CONNECTION_STALE_AFTER) recoverStream();
  renderConnection();
  renderPicker();
  for (const { field, label, value } of state.usageAges) field.textContent = `${label}: ${elapsedLabel(value, now)}`;
  paintUsageForecasts(now);
  if (!state.model) return;
  if (state.follow && now - state.lastRebase >= FOLLOW_REBASE_INTERVAL) renderTimeline(now, true);
  else paintTimeline(now);
  paintMilestones(now);
}

function setOverviewOpen(open) {
  const panel = elements["overview-panel"];
  const toggle = elements["overview-toggle"];
  panel.classList.toggle("is-open", open);
  toggle.setAttribute("aria-expanded", String(open));
  if (open) panel.focus({ preventScroll: true });
  else if (panel.contains(document.activeElement)) toggle.focus({ preventScroll: true });
}

elements["overview-toggle"].addEventListener("click", () => {
  setOverviewOpen(elements["overview-toggle"].getAttribute("aria-expanded") !== "true");
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && elements["overview-toggle"].getAttribute("aria-expanded") === "true") {
    setOverviewOpen(false);
  }
});

elements.session.addEventListener("click", () => {
  if (pickerOpen()) closePicker(true);
  else openPicker();
});
elements["session-filter"].addEventListener("input", () => {
  renderOptions();
  renderActiveOption(true);
});
elements["session-filter"].addEventListener("keydown", event => {
  const steps = { ArrowDown: 1, ArrowUp: -1, PageDown: OPTION_PAGE_STEP, PageUp: -OPTION_PAGE_STEP };
  if (Object.hasOwn(steps, event.key)) {
    event.preventDefault();
    moveActiveOption(steps[event.key]);
  } else if (event.key === "Enter" && state.pickerActive !== null) {
    event.preventDefault();
    chooseSession(state.pickerActive);
  }
});
elements["session-all"].addEventListener("change", () => {
  state.pickerAll = elements["session-all"].checked;
  renderOptions();
});
elements["session-options"].addEventListener("mousedown", event => event.preventDefault());
elements["session-options"].addEventListener("click", event => {
  const option = event.target.closest("[role=option]");
  if (option) chooseSession(option.dataset.id);
});
elements["session-options"].addEventListener("mousemove", event => {
  const option = event.target.closest("[role=option]");
  if (option && option.dataset.id !== state.pickerActive) {
    state.pickerActive = option.dataset.id;
    renderActiveOption(false);
  }
});
elements["session-control"].addEventListener("keydown", event => {
  if (event.key === "Escape" && pickerOpen()) {
    event.preventDefault();
    event.stopPropagation();
    closePicker(true);
  }
});
elements["session-control"].addEventListener("focusout", event => {
  if (!elements["session-control"].contains(event.relatedTarget)) closePicker(false);
});
document.addEventListener("keydown", event => {
  const typing = event.target instanceof Element && event.target.closest("input, textarea, select, [contenteditable]");
  if (event.key === "/" && !typing && !event.ctrlKey && !event.metaKey && !event.altKey) {
    event.preventDefault();
    openPicker();
  }
});
window.addEventListener("hashchange", () => {
  const id = linkedSession();
  if (id && id !== state.selected && state.lists.some(list => list.id === id)) selectSession(id);
});
elements.follow.addEventListener("click", () => {
  state.follow = !state.follow;
  renderFollow();
  if (state.follow && state.model) renderTimeline(Date.now(), true);
});
function renderZoom() {
  for (const button of document.querySelectorAll("[data-zoom]")) {
    button.setAttribute("aria-pressed", String(button.dataset.zoom === state.zoom));
  }
}

for (const button of document.querySelectorAll("[data-zoom]")) {
  button.addEventListener("click", () => {
    state.zoom = button.dataset.zoom;
    state.follow = true;
    try { localStorage.setItem(ZOOM_STORAGE_KEY, state.zoom); } catch {}
    renderZoom();
    renderFollow();
    if (state.model) renderTimeline(Date.now(), true);
  });
}
elements.timeline.addEventListener("scroll", () => {
  const left = elements.timeline.scrollLeft;
  if (!state.programmaticScroll && Math.abs(left - state.lastScrollLeft) > 1) {
    state.follow = false;
    renderFollow();
  }
  state.lastScrollLeft = left;
}, { passive: true });
elements["close-details"].addEventListener("click", () => closeDetails());
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && state.detailsOpen) closeDetails();
});
window.addEventListener("resize", () => {
  if (state.model) renderTimeline(Date.now(), state.follow);
});
window.addEventListener("pagehide", () => {
  state.pageInactive = true;
  closeStream();
});
window.addEventListener("pageshow", () => {
  state.pageInactive = false;
  if (!state.stream) connectStream();
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden) closeStream();
  else if (!state.stream) connectStream();
});

async function start() {
  try {
    const response = await readLocal("/api/lists");
    if (!response.ok) throw new Error("Session discovery failed");
    const result = await response.json();
    renderLists(result.lists);
    selectSession(preferredSession());
  } catch {
    setNotice("Cannot reach the local server. Retrying…");
    if (!state.stream) connectStream();
    window.setTimeout(start, DISCOVERY_RETRY_DELAY);
  }
}

renderZoom();
window.setInterval(tick, SECOND);
start();
