"use strict";

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
const MINIMUM_PLOT_WIDTH = 540;
const MINIMUM_BAR_WIDTH = 3;
const BAR_LABEL_GAP = 7;
const DESIRED_TICK_WIDTH = 100;
const DEFAULT_ZOOM = "12h";
const ZOOM_STORAGE_KEY = "live-gantt.zoom";
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

const elements = Object.fromEntries(
  ["session", "project", "connection", "connection-text", "freshness", "milestones", "timeline", "timeline-content", "notice", "follow", "task-count", "warnings", "details", "details-content", "close-details"].map(id => [id, document.getElementById(id)]),
);
const state = {
  model: null,
  lists: [],
  selected: null,
  stream: null,
  zoom: savedZoom(),
  showOlder: false,
  follow: true,
  connected: false,
  lastReceived: null,
  range: null,
  plotWidth: MINIMUM_PLOT_WIDTH,
  lastRebase: 0,
  programmaticScroll: false,
  lastScrollLeft: 0,
  tracks: [],
  milestoneDates: [],
  detailPinned: false,
  detailKey: null,
  detailEntries: new Map(),
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
  return `${Math.round(Math.max(0, Math.min(100, number(value))))}%`;
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

function titleCase(value) {
  return String(value || "unknown").replaceAll("_", " ").replace(/^./, letter => letter.toUpperCase());
}

function modelAge(now) {
  return Math.max(0, now - (timestamp(state.model?.observedAt) ?? now));
}

function projectedTime(value, now) {
  const result = timestamp(value);
  return result === null ? null : result + modelAge(now);
}

function setNotice(message) {
  elements.notice.textContent = message;
  elements.notice.hidden = !message;
}

function renderLists(lists) {
  state.lists = Array.isArray(lists) ? lists : [];
  elements.session.replaceChildren();
  if (!state.lists.length) {
    elements.session.append(node("option", "", "No sessions"));
    elements.session.disabled = true;
    return;
  }
  elements.session.disabled = false;
  if (state.lists.some(list => list.id === state.selected && !list.recent)) state.showOlder = true;
  if (!state.selected && !state.lists.some(list => list.recent)) {
    const placeholder = node("option", "", "Choose a session");
    placeholder.value = "";
    placeholder.disabled = true;
    elements.session.append(placeholder);
  }
  const recent = state.lists.filter(list => list.recent);
  const older = state.lists.filter(list => !list.recent);
  for (const list of [...recent, ...(state.showOlder ? older : [])]) {
    const identity = list.id === state.selected && state.model ? state.model : list;
    const title = identity.title || list.id;
    const updated = timestamp(list.updatedAt);
    const date = updated === null ? "Unknown date" : new Date(updated).toLocaleDateString();
    const option = node("option", "", list.recent ? title : `${title} · ${date}`);
    option.value = list.id;
    option.title = `${title}${identity.project ? ` · ${identity.project}` : ""}`;
    elements.session.append(option);
  }
  if (older.length && !state.showOlder) {
    const reveal = node("option", "", "Show older sessions");
    reveal.value = "";
    reveal.dataset.action = "show-older";
    elements.session.append(reveal);
  }
  if (state.lists.some(list => list.id === state.selected)) elements.session.value = state.selected;
  else elements.session.selectedIndex = 0;
}

function closeStream() {
  if (state.stream) state.stream.close();
  state.stream = null;
}

function markReceived() {
  state.connected = true;
  state.lastReceived = Date.now();
  renderConnection();
}

function renderConnection() {
  const age = state.lastReceived === null ? null : Math.max(0, Math.floor((Date.now() - state.lastReceived) / SECOND));
  const live = state.connected && age !== null && age * SECOND < CONNECTION_STALE_AFTER;
  elements.connection.classList.toggle("disconnected", !live);
  elements["connection-text"].textContent = live ? `Live · ${age}s ago` : "Disconnected";
}

function parseEvent(event, receive) {
  try {
    receive(JSON.parse(event.data));
    markReceived();
  } catch {
    setNotice("An update could not be read. Waiting for the next update.");
  }
}

function selectSession(id) {
  closeStream();
  state.selected = id;
  state.model = null;
  state.connected = false;
  state.lastReceived = null;
  state.follow = true;
  state.detailPinned = false;
  elements.details.hidden = true;
  elements["timeline-content"].replaceChildren();
  elements.milestones.replaceChildren();
  renderFreshness(null);
  elements.warnings.hidden = true;
  elements["task-count"].textContent = "";
  elements.project.textContent = state.lists.find(list => list.id === id)?.project || "Claude Code sessions";
  renderFollow();
  renderConnection();
  setNotice("Reading session…");
  const stream = new EventSource(`/events?list=${encodeURIComponent(id)}`);
  state.stream = stream;
  stream.addEventListener("state", event => {
    if (state.stream !== stream) return;
    parseEvent(event, model => {
      state.model = model;
      if (!state.lists.find(list => list.id === id)?.recent) state.showOlder = true;
      renderLists(state.lists);
      renderModel();
    });
  });
  stream.addEventListener("lists", event => {
    if (state.stream === stream) parseEvent(event, data => renderLists(data.lists));
  });
  stream.addEventListener("heartbeat", event => {
    if (state.stream === stream) parseEvent(event, () => {});
  });
  stream.addEventListener("missing", () => {
    if (state.stream !== stream) return;
    renderLists(state.lists.filter(list => list.id !== id));
    if (state.lists.length) {
      selectSession(state.lists[0].id);
      return;
    }
    closeStream();
    state.model = null;
    state.selected = null;
    state.connected = false;
    state.detailPinned = false;
    elements.details.hidden = true;
    elements["timeline-content"].replaceChildren();
    elements.milestones.replaceChildren();
    renderFreshness(null);
    elements["task-count"].textContent = "";
    elements.warnings.hidden = true;
    renderConnection();
    setNotice("This session is no longer available. No recent sessions remain.");
  });
  stream.onopen = () => {
    if (state.stream === stream) markReceived();
  };
  stream.onerror = () => {
    if (state.stream === stream) {
      state.connected = false;
      renderConnection();
      if (!state.model) setNotice("Disconnected. Reconnecting to the local server…");
    }
  };
}

function fitRange(now) {
  const times = [now - MINIMUM_FIT_SPAN * PAST_SHARE, now + MINIMUM_FIT_SPAN * (1 - PAST_SHARE)];
  for (const task of state.model?.tasks || []) {
    for (const value of [timestamp(task.started), timestamp(task.ended), projectedTime(task.projectedStart, now), projectedTime(task.projectedEnd, now)]) {
      if (value !== null) times.push(value);
    }
    for (const child of [...(task.agents || []), ...(task.runs || [])]) {
      const start = timestamp(child.started);
      const end = timestamp(child.ended);
      if (start !== null) times.push(start);
      if (end !== null) times.push(end);
    }
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

function showDetails(item, kind, pinned = false, key = null) {
  if (state.detailPinned && !pinned) return;
  state.detailPinned = pinned;
  state.detailKey = key;
  const content = elements["details-content"];
  content.replaceChildren();
  const heading = item.subject || item.label || item.description || `Agent ${item.id}`;
  content.append(node("h2", "", heading));
  if (item.description && item.description !== heading) content.append(node("p", "", item.description));
  const fields = node("dl");
  detailField(fields, "Status", kind === "task" ? taskState(item) : titleCase(item.status));
  detailField(fields, "Waiting", kind === "task" && item.queued ? waitingLabel(item) : undefined);
  detailField(fields, "Task", kind === "task" ? item.id : undefined);
  detailField(fields, "Lane", item.lane);
  detailField(fields, "Milestone", item.milestone);
  detailField(fields, "Progress", kind === "task" ? percent(item.effectiveProgress ?? item.progress) : undefined);
  detailField(fields, "Weight", item.weight);
  detailField(fields, "Created", item.created ? dateTime(item.created, true) : undefined);
  detailField(fields, "Started", item.started ? dateTime(item.started, true) : undefined);
  detailField(fields, "Ended", item.ended ? dateTime(item.ended, true) : undefined);
  detailField(fields, "Agent type", item.agentType);
  detailField(fields, "Branch", item.worktreeBranch);
  detailField(fields, "Last activity", item.lastActivityAt ? dateTime(item.lastActivityAt, true) : undefined);
  detailField(fields, "Activity", item.activity);
  detailField(fields, "Log", item.log);
  if (item.estimate) {
    detailField(fields, "Estimate", `${hours(item.estimate.low)} low · ${hours(item.estimate.likely)} likely · ${hours(item.estimate.high)} high`);
    detailField(fields, "Mean", hours(item.estimateMean));
    detailField(fields, "Deviation", hours(item.estimateSd));
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
    content.append(node("h3", "", "Metadata"), node("pre", "", JSON.stringify(item.metadata, null, 2)));
  }
  elements.details.hidden = false;
}

function attachDetails(target, item, kind, key) {
  target.dataset.detailKey = key;
  state.detailEntries.set(key, { target, item, kind });
  target.addEventListener("mouseenter", () => showDetails(item, kind, false, key));
  target.addEventListener("mouseleave", () => {
    if (!state.detailPinned && !target.contains(document.activeElement)) elements.details.hidden = true;
  });
  target.addEventListener("focus", () => showDetails(item, kind, false, key));
  target.addEventListener("blur", event => {
    if (!state.detailPinned && !elements.details.contains(event.relatedTarget)) elements.details.hidden = true;
  });
  target.addEventListener("click", () => showDetails(item, kind, true, key));
}

function isOverrun(task, now) {
  const deadline = timestamp(task.overrunAt);
  return deadline !== null && now > deadline;
}

function taskState(task, now = Date.now()) {
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

function renderTask(task) {
  const label = node("button", "row-label");
  label.type = "button";
  const title = node("span", "task-title");
  title.append(node("span", "task-id", `#${task.id} `), document.createTextNode(task.subject || "Untitled task"));
  const subtitle = node("span", "task-subtitle", taskSubtitle(task));
  label.append(title, subtitle);
  label.setAttribute("aria-label", `Task ${task.id}: ${task.subject || "Untitled task"}. ${taskSubtitle(task)}. Show details.`);
  attachDetails(label, task, "task", `task:${task.id}`);
  const track = makeTrack();
  state.tracks.push({ track, item: task, kind: "task", subtitle });
  makeRow(label, track);
  for (const agent of task.agents || []) renderChild(agent, "agent", `task:${task.id}:agent:${agent.id}`);
  for (const [index, run] of (task.runs || []).entries()) renderChild(run, "run", `task:${task.id}:run:${index}`);
}

function renderChild(item, kind, key) {
  const label = node("button", "row-label");
  label.type = "button";
  const title = kind === "run" ? item.label || "Run" : item.description || `Agent ${item.id}`;
  label.append(node("span", "task-title", `${titleCase(item.status)} · ${title}`));
  label.title = title;
  attachDetails(label, item, kind, key);
  const track = makeTrack();
  state.tracks.push({ track, item, kind });
  makeRow(label, track, "child-row");
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
  const projectedStart = projectedTime(task.projectedStart, now);
  const projectedEnd = projectedTime(task.projectedEnd, now);
  if (task.status === "completed") {
    if (started !== null && ended !== null) createBar(track, "done", started, ended, "Done");
    else track.append(node("span", "row-marker", "No history"));
  } else if (task.status === "in_progress") {
    if (started !== null) createBar(track, `${isOverrun(task, now) ? "overrun" : "running"}${projectedEnd !== null && projectedEnd > now ? " has-tail" : ""}`, started, now, percent(task.effectiveProgress ?? task.progress), true);
    else track.append(node("span", "row-marker", "No history"));
    if (projectedEnd !== null && projectedEnd > now) createBar(track, "projected projected-tail", now, projectedEnd, "Projected");
  } else if (projectedStart !== null && projectedEnd !== null) {
    createBar(track, task.queued ? "projected queued" : "projected", projectedStart, projectedEnd, task.queued ? waitingLabel(task) : "Projected");
  }
  if (task.status !== "completed" && projectedEnd === null) {
    const reason = (task.markers || []).join(" · ") || (task.blocked ? "unresolved prerequisite" : "Unestimated");
    const marker = node("span", `row-marker ${task.blocked ? "blocked-marker" : "warning"}`, `${task.blocked ? "Blocked · " : ""}${reason}`);
    if (started !== null) marker.style.left = `${Math.max(10, Math.min(state.plotWidth - 140, xPosition(now) + 10))}px`;
    track.append(marker);
  }
}

function paintChild(track, item, now) {
  const start = timestamp(item.started);
  const end = timestamp(item.ended);
  const live = item.status === "running";
  const className = live ? "running" : item.status === "silent" ? "silent" : item.status === "completed" ? "done" : "blocked";
  const latest = end ?? (live || item.status === "silent" ? now : timestamp(item.lastActivityAt));
  createBar(track, className, start, latest, item.activity, live);
}

function paintTimeline(now) {
  if (!state.range) return;
  for (const { track, item, kind, subtitle } of state.tracks) {
    for (const old of track.querySelectorAll(".bar, .bar-label, .row-marker")) old.remove();
    if (kind === "task") {
      paintTask(track, item, now);
      subtitle.textContent = taskSubtitle(item, now);
    }
    else paintChild(track, item, now);
  }
  for (const line of elements["timeline-content"].querySelectorAll(".now-line")) {
    line.style.left = `${xPosition(now)}px`;
    line.hidden = now < state.range.start || now > state.range.end;
  }
}

function renderMilestones(now) {
  elements.milestones.replaceChildren();
  state.milestoneDates = [];
  const cards = node("div", "milestone-cards");
  const done = [];
  const unestimated = [];
  for (const milestone of state.model.milestones || []) {
    if (!milestone.hasRemaining) {
      done.push(milestone.name);
      continue;
    }
    if (!milestone.hasEstimate) {
      unestimated.push(milestone.name);
      continue;
    }
    const card = node("article", "milestone");
    const heading = node("div", "milestone-heading");
    heading.append(node("h2", "", milestone.name || "Ungrouped"), node("span", "milestone-percentage", percent(milestone.progress)));
    const dates = node("div", "milestone-dates");
    for (const [key, title] of [["p50", "P50"], ["p90", "P90"]]) {
      const field = node("span", "", `${title} `);
      field.title = key === "p50" ? "50th-percentile projected finish (P50): the mean estimate" : "90th-percentile projected finish (P90): a one-sided normal approximation";
      const value = node("strong");
      field.append(value);
      dates.append(field);
      state.milestoneDates.push({ node: value, milestone, key });
    }
    card.append(heading, dates);
    cards.append(card);
  }
  if (cards.childElementCount) elements.milestones.append(cards);
  if (done.length) elements.milestones.append(node("p", "milestone-summary", `Done: ${done.join(", ")}`));
  if (unestimated.length) elements.milestones.append(node("p", "milestone-summary", `Unestimated: ${unestimated.join(", ")}`));
  paintMilestones(now);
}

function paintMilestones(now) {
  for (const item of state.milestoneDates) {
    const base = timestamp(item.milestone[item.key]);
    const time = base === null ? null : base + (item.milestone.hasRemaining ? modelAge(now) : 0);
    item.node.textContent = time === null ? "Unestimated" : dateTime(time);
  }
}

function activeInWindow(agent, now) {
  const start = timestamp(agent.started);
  const end = timestamp(agent.ended) ?? (agent.status === "running" || agent.status === "silent" ? now : timestamp(agent.lastActivityAt));
  return start !== null && end !== null && start <= state.range.end && end >= state.range.start;
}

function renderTimeline(now, resetRange = false) {
  if (!state.range || resetRange) setRange(now);
  const previousScroll = elements.timeline.scrollLeft;
  const previousTop = elements.timeline.scrollTop;
  const focusedKey = document.activeElement?.dataset.detailKey;
  elements["timeline-content"].replaceChildren();
  state.tracks = [];
  state.detailEntries = new Map();
  renderAxis();
  const lanes = new Map();
  for (const task of state.model.tasks || []) {
    const lane = task.lane;
    if (!lanes.has(lane)) lanes.set(lane, []);
    lanes.get(lane).push(task);
  }
  for (const [name, tasks] of lanes) {
    renderLane(name, tasks.length);
    for (const task of tasks) renderTask(task);
  }
  const agents = (state.model.agents || []).filter(agent => activeInWindow(agent, now));
  if (agents.length) {
    renderLane("Agents", agents.length);
    for (const agent of agents) renderChild(agent, "agent", `agent:${agent.id}`);
  }
  paintTimeline(now);
  elements.timeline.scrollTop = previousTop;
  if (state.follow) followNow(now);
  else setScroll(previousScroll);
  state.detailEntries.get(focusedKey)?.target.focus({ preventScroll: true });
  if (state.detailPinned) {
    const selected = state.detailEntries.get(state.detailKey);
    if (selected) showDetails(selected.item, selected.kind, true, state.detailKey);
    else {
      state.detailPinned = false;
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
  elements.project.textContent = model.project || "Unknown project";
  document.title = `${model.title || "Session"} · Live Gantt`;
  const tasks = model.tasks || [];
  const completed = tasks.filter(task => task.status === "completed").length;
  elements["task-count"].textContent = `${completed}/${tasks.length} tasks done`;
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
  renderConnection();
  if (!state.model) return;
  if (state.follow && now - state.lastRebase >= FOLLOW_REBASE_INTERVAL) renderTimeline(now, true);
  else paintTimeline(now);
  paintMilestones(now);
}

elements.session.addEventListener("change", () => {
  if (elements.session.selectedOptions[0]?.dataset.action === "show-older") {
    state.showOlder = true;
    renderLists(state.lists);
  } else if (elements.session.value) selectSession(elements.session.value);
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
elements["close-details"].addEventListener("click", () => {
  state.detailPinned = false;
  elements.details.hidden = true;
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape") {
    state.detailPinned = false;
    elements.details.hidden = true;
  }
});
window.addEventListener("resize", () => {
  if (state.model) renderTimeline(Date.now(), state.follow);
});
window.addEventListener("pagehide", closeStream);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) closeStream();
  else if (state.selected && !state.stream) selectSession(state.selected);
});

async function start() {
  try {
    const response = await fetch("/api/lists", { cache: "no-store" });
    if (!response.ok) throw new Error("Session discovery failed");
    const result = await response.json();
    renderLists(result.lists);
    const firstRecent = state.lists.find(list => list.recent);
    if (firstRecent) selectSession(firstRecent.id);
    else if (state.lists.length) setNotice("Choose Show older sessions to browse earlier work.");
    else setNotice("No sessions found. Start a Claude Code session with tasks, then refresh this page.");
  } catch {
    setNotice("Cannot reach the local server. Retrying…");
    window.setTimeout(start, DISCOVERY_RETRY_DELAY);
  }
}

renderZoom();
window.setInterval(tick, SECOND);
start();
