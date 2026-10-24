"use strict";
const HOST = "com.digimata.quill.meetings";
const RETRY_ALARM = "quill-native-delivery";
const ACK_TIMEOUT = 10000;
const SIGNAL_FRESHNESS = 15000;
let connection;
let inFlight;
let ackTimer;
let client;
let documents = {};
let outbox = {};
let retryAt = 0;
let retryDelay = 30000;
const reportedErrors = new Set();

function reportError(category, error) {
  // Log only fixed descriptions, once per category/cause in this worker. Raw
  // exception text and stacks can contain meeting URLs, IDs or stored content.
  const message = typeof error?.message === "string" && error.message.length <= 512 ? error.message : "";
  let cause = "unknown";
  let detail = "unrecognized error";
  const known = [
    ["host-missing", /^(?:Specified native messaging host not found|Native host unavailable)\.?$/i, "native messaging host not found"],
    ["host-forbidden", /^Access to the specified native messaging host is forbidden\.?$/i, "access to the native messaging host is forbidden"],
    ["host-start", /^Failed to start native messaging host\.?$/i, "native messaging host could not start"],
    ["host-exited", /^Native host has exited\.?$/i, "native messaging host exited"],
    ["host-communication", /^(?:Error when communicating with the native messaging host|Native port disconnected)\.?$/i, "native messaging connection was interrupted"],
    ["storage-quota", /^(?:QUOTA_BYTES|QUOTA_BYTES_PER_ITEM) quota exceeded\.?$/i, "extension storage quota exceeded"],
    ["context-invalid", /^(?:The )?extension context (?:has been )?invalidated\.?$/i, "extension context is no longer valid"],
  ];
  for (const [key, pattern, description] of known) {
    if (pattern.test(message)) { cause = key; detail = description; break; }
  }
  if (cause === "unknown" && ["TypeError", "ReferenceError", "SyntaxError", "RangeError"].includes(error?.name)) {
    cause = error.name;
    detail = error.name;
  }
  const key = category + ":" + cause;
  if (reportedErrors.has(key)) return;
  reportedErrors.add(key);
  console.error(`[Quill meeting detector] ${category}: ${detail}.`);
}

// Serialize storage changes, native acknowledgments and navigation events.
let pending = Promise.resolve();
let initializationFailed = false;
function serial(task) {
  pending = pending.then(task).catch(error => {
    if (!initializationFailed) reportError("Background task failed", error);
  });
}
const ready = (async () => {
  const saved = await chrome.storage.local.get(["client", "outbox", "retryAt"]);
  client = saved.client || crypto.randomUUID();
  outbox = saved.outbox || {};
  retryAt = saved.retryAt || 0;
  documents = (await chrome.storage.session.get("documents")).documents || {};
  // A worker/browser restart is not fresh evidence of a joined meeting. Its
  // next content-script heartbeat will recreate these replaceable signals.
  for (const [key, item] of Object.entries(outbox)) {
    if (item.state !== "left") delete outbox[key];
  }
  await chrome.storage.local.set({ client, outbox, retryAt });
})().catch(error => {
  initializationFailed = true;
  reportError("Background initialization failed; reload the extension after correcting the error", error);
  throw error;
});

function permitted(url) {
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" && (parsed.hostname === "meet.google.com" || /(^|\.)zoom\.us$/.test(parsed.hostname));
  } catch { return false; }
}

async function currentFrame(tabID, frameID = 0) {
  // webNavigation can expose navigation metadata. We query only meeting
  // senders/tracked tabs; neither URLs nor titles enter storage or the host.
  try { return await chrome.webNavigation.getFrame({ tabId: Number(tabID), frameId: frameID }); }
  catch (error) {
    const message = typeof error?.message === "string" && error.message.length <= 512 ? error.message : "";
    // A tab/frame may disappear between the event and lookup. Other failures
    // can block every joined signal, so expose their fixed diagnostic category.
    if (!/^(?:No tab with id: [0-9]+|No frame with id [0-9]+ in tab [0-9]+|Frame with ID [0-9]+ was removed)\.?$/i.test(message)) {
      reportError("Meeting tab lookup failed", error);
    }
    return null;
  }
}

function zoomMeeting(url) {
  try {
    const parsed = new URL(url);
    const match = /^\/wc\/([0-9]+)(?:\/|$)/.exec(parsed.pathname);
    if (parsed.protocol === "https:" && /(^|\.)zoom\.us$/.test(parsed.hostname) && match) {
      return { origin: parsed.origin, meeting: match[1] };
    }
  } catch {}
}

async function currentIdentity(tabID, frameID, documentID, provider, rootDocumentID) {
  const frame = await currentFrame(tabID, frameID);
  if (frame?.documentId !== documentID || frame.documentLifecycle !== "active" || !permitted(frame.url)) return;
  if (frameID === 0) {
    if (rootDocumentID != null && rootDocumentID !== documentID) return;
    return { rootDocumentID: documentID };
  }
  // Only the observed Zoom web-client shape is trusted: a direct child of
  // the current root, with the same origin and numeric meeting in both URLs.
  if (provider !== "zoom-web" || frame.parentFrameId !== 0) return;
  const root = await currentFrame(tabID);
  if (!root?.documentId || root.documentLifecycle !== "active" || frame.parentDocumentId !== root.documentId ||
      (rootDocumentID != null && root.documentId !== rootDocumentID)) return;
  const childMeeting = zoomMeeting(frame.url);
  const rootMeeting = zoomMeeting(root.url);
  if (!childMeeting || !rootMeeting || childMeeting.origin !== rootMeeting.origin || childMeeting.meeting !== rootMeeting.meeting) return;
  return { rootDocumentID: root.documentId };
}

function currentEvent(event) {
  // Persisted records from before child-frame support describe frame zero.
  return currentIdentity(event.tabID, event.frameID ?? 0, event.documentID,
    event.provider, event.rootDocumentID ?? event.documentID);
}

function sameDocument(left, right) {
  return left.documentID === right.documentID && (left.frameID ?? 0) === (right.frameID ?? 0) &&
    (left.rootDocumentID ?? left.documentID) === (right.rootDocumentID ?? right.documentID);
}

async function browserWindowFor(tabID, expectedWindowID) {
  // Tab/window identifiers and geometry come from Chrome, never the content
  // script. A minimized window has no currently capturable bounds.
  try {
    const tab = await chrome.tabs.get(Number(tabID));
    if (tab.active !== true) return;
    if (expectedWindowID != null && tab.windowId !== expectedWindowID) return;
    const window = await chrome.windows.get(tab.windowId);
    const currentTab = await chrome.tabs.get(Number(tabID));
    if (currentTab.active !== true || currentTab.windowId !== tab.windowId || window.id !== tab.windowId || window.state === "minimized") return;
    const limits = { id: [1, 2147483647], left: [-131072, 131072], top: [-131072, 131072], width: [1, 65536], height: [1, 65536] };
    const geometry = {};
    for (const [key, [minimum, maximum]] of Object.entries(limits)) {
      const value = window[key];
      if (!Number.isInteger(value) || value < minimum || value > maximum) return;
      geometry[key] = value;
    }
    return geometry;
  } catch { return; }
}

function keyFor(event) { return event.source + "|" + event.meeting; }
function queue(event) {
  const key = keyFor(event);
  // A repeated terminal heartbeat must not displace an unacknowledged leave.
  if (outbox[key]?.state === "left" && event.state === "left") return;
  outbox[key] = { ...event, requestID: crypto.randomUUID(), queuedAt: Date.now() };
  if (event.state === "left") delete outbox[key].browserWindow;
}

async function save() {
  // Persist terminal delivery before removing the corresponding tab state.
  await chrome.storage.local.set({ outbox, retryAt });
  await chrome.storage.session.set({ documents });
}

async function scheduleRetry() {
  if (!Object.keys(outbox).length) {
    await chrome.alarms.clear(RETRY_ALARM);
    return;
  }
  // Alarms survive worker suspension. Reconnection is limited to once every
  // 30 seconds, backing off to five minutes while the native host is missing.
  await chrome.alarms.create(RETRY_ALARM, { when: Math.max(Date.now() + 30000, retryAt) });
}

async function failed(port) {
  if (port && port !== connection) return;
  clearTimeout(ackTimer);
  ackTimer = null;
  inFlight = null;
  const previous = connection;
  connection = null;
  try { previous?.disconnect(); } catch {}
  retryAt = Date.now() + retryDelay;
  retryDelay = Math.min(retryDelay * 2, 300000);
  await save();
  await scheduleRetry();
}

function host() {
  if (connection) return connection;
  const port = chrome.runtime.connectNative(HOST);
  connection = port;
  port.onDisconnect.addListener(() => {
    // lastError is defined only inside this callback.
    const error = chrome.runtime.lastError;
    if (port === connection) reportError("Native connection failed", error);
    serial(async () => { await ready; await failed(port); });
  });
  port.onMessage.addListener(answer => serial(async () => {
    await ready;
    if (port !== connection || !inFlight || answer?.requestID !== inFlight.requestID) return;
    if (answer.accepted !== true) { await failed(port); return; }
    const delivered = inFlight;
    inFlight = null;
    clearTimeout(ackTimer);
    ackTimer = null;
    // An acknowledgment for joined must not delete a newer queued leave.
    const key = keyFor(delivered);
    if (outbox[key]?.requestID === delivered.requestID) delete outbox[key];
    retryAt = 0;
    retryDelay = 30000;
    await save();
    await pump();
  }));
  return port;
}

async function pump() {
  if (inFlight) {
    if (Date.now() - inFlight.sentAt >= ACK_TIMEOUT) await failed(connection);
    return;
  }
  if (Date.now() < retryAt) { await scheduleRetry(); return; }
  let event;
  // Leaves are durable; joined/unknown are short-lived heartbeats. Never
  // replay an old joined signal with a fresh host timestamp after departure.
  const candidates = Object.values(outbox).sort((a, b) =>
    (a.state === "left" ? 0 : 1) - (b.state === "left" ? 0 : 1) || a.queuedAt - b.queuedAt);
  for (const candidate of candidates) {
    if (candidate.state !== "left") {
      if (Date.now() - candidate.queuedAt > SIGNAL_FRESHNESS || !await currentEvent(candidate)) {
        delete outbox[keyFor(candidate)];
        continue;
      }
      // Refresh geometry immediately before delivery: a queued heartbeat may
      // outlive a move/resize or a tab transfer to a different Chrome window.
      const browserWindow = candidate.windowUnavailable ? undefined : await browserWindowFor(candidate.tabID);
      if (!await currentEvent(candidate)) {
        delete outbox[keyFor(candidate)];
        continue;
      }
      delete candidate.browserWindow;
      if (browserWindow) candidate.browserWindow = browserWindow;
    }
    event = candidate;
    break;
  }
  await save();
  if (!event) { await chrome.alarms.clear(RETRY_ALARM); return; }
  // The alarm is a backup if the worker is suspended before its short timer.
  await scheduleRetry();
  try {
    const port = host();
    inFlight = { ...event, sentAt: Date.now() };
    ackTimer = setTimeout(() => serial(async () => {
      if (connection === port && inFlight?.requestID === event.requestID) await failed(port);
    }), ACK_TIMEOUT);
    port.postMessage({ source: event.source, meeting: event.meeting,
      provider: event.provider, state: event.state, requestID: event.requestID,
      ...(event.state !== "left" && event.browserWindow ? { browserWindow: event.browserWindow } : {}) });
  } catch (error) {
    reportError("Native connection failed", error);
    await failed(connection);
  }
}

// Health probes are read-only and independent of the durable signal outbox.
// A missing host, failed storage, or an unsupported capture route cannot turn
// an absent recording confirmation into a positive one.
const healthRequests = new Map();
async function recordingHealth(message, sender) {
  const unavailable = { recordingStatus: { state: "unavailable" } };
  if (!Number.isInteger(sender.frameId) || sender.frameId < 0 || sender.tab?.id == null ||
      !permitted(sender.url) || !["google-meet", "zoom-web"].includes(message.provider) ||
      ![message.meeting, sender.documentId].every(x => typeof x === "string" && /^[a-zA-Z0-9-]{1,64}$/.test(x))) return unavailable;
  if ((new URL(sender.url).hostname === "meet.google.com") !== (message.provider === "google-meet")) return unavailable;
  const identity = await currentIdentity(sender.tab.id, sender.frameId, sender.documentId, message.provider);
  if (!identity) return unavailable;
  const key = `${sender.tab.id}:${sender.frameId}:${sender.documentId}`;
  const previous = healthRequests.get(key);
  if (previous && previous.meeting === message.meeting && Date.now() - previous.started < 4000) return previous.promise;
  if (healthRequests.size >= 64) healthRequests.clear();
  const entry = { meeting: message.meeting, started: Date.now() };
  healthRequests.set(key, entry);
  entry.promise = (async () => {
    const requestID = crypto.randomUUID();
    let timeout;
    try {
      const answer = await Promise.race([
        Promise.resolve().then(() => chrome.runtime.sendNativeMessage(HOST, {
          type: "recording-status", requestID, meeting: message.meeting,
        })),
        new Promise(resolve => { timeout = setTimeout(() => resolve(null), 4000); }),
      ]);
      if (healthRequests.get(key) !== entry || answer?.accepted !== true || answer.requestID !== requestID ||
          !await currentIdentity(sender.tab.id, sender.frameId, sender.documentId, message.provider, identity.rootDocumentID)) return unavailable;
      const state = answer.recordingStatus?.state;
      if (!["not-recording", "starting", "recording", "paused", "skipped", "unavailable"].includes(state)) return unavailable;
      return { recordingStatus: { state } };
    } catch { return unavailable; }
    finally { clearTimeout(timeout); }
  })();
  return entry.promise;
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "quill-recording-health") {
    recordingHealth(message, sender).then(sendResponse, () => sendResponse({ recordingStatus: { state: "unavailable" } }));
    return true;
  }
  if (message?.type !== "quill-meeting" || !Number.isInteger(sender.frameId) || sender.frameId < 0 || sender.tab?.id == null || !permitted(sender.url)) return;
  if (!["joined", "left", "unknown"].includes(message.state) || !["google-meet", "zoom-web"].includes(message.provider)) return;
  if (![message.meeting, sender.documentId].every(x => typeof x === "string" && /^[a-zA-Z0-9-]{1,64}$/.test(x))) return;
  serial(async () => {
    await ready;
    const tab = String(sender.tab.id);
    const identity = await currentIdentity(tab, sender.frameId, sender.documentId, message.provider);
    // Compare current browser-owned child AND root identities, again after
    // geometry lookup. Retired documents cannot refresh the replacement call.
    if (!identity) return;
    const browserWindow = message.state === "left" ? undefined : await browserWindowFor(tab, sender.tab.windowId);
    if (!await currentIdentity(tab, sender.frameId, sender.documentId, message.provider, identity.rootDocumentID)) return;
    const source = client + "-" + tab;
    const event = { source, meeting: message.meeting, provider: message.provider,
      state: message.state, documentID: sender.documentId, frameID: sender.frameId,
      rootDocumentID: identity.rootDocumentID, tabID: tab,
      ...(browserWindow ? { browserWindow } : {}) };
    const previous = documents[tab];
    if (previous && !sameDocument(previous, event) && previous.state !== "left") {
      queue({ ...previous, state: "left" });
    }
    documents[tab] = event;
    queue(event);
    await save();
    await pump();
  });
});

chrome.tabs.onRemoved.addListener(tabID => serial(async () => {
  await ready;
  const tab = String(tabID);
  if (documents[tab]) queue({ ...documents[tab], state: "left" });
  delete documents[tab];
  await save();
  await pump();
}));

async function refreshWindowSignal(tabID, unavailable = false) {
  const tab = String(tabID);
  const previous = documents[tab];
  if (!previous || previous.state === "left") return;
  if (!await currentEvent(previous)) return;
  const browserWindow = unavailable ? undefined : await browserWindowFor(tab);
  if (!await currentEvent(previous)) return;
  // Window events prove an association change, not that the call is still
  // joined. Preserve leave detection and let the next DOM heartbeat restore
  // joined evidence. Unknown must never end the audio recording.
  const event = { ...previous, state: "unknown" };
  delete event.browserWindow;
  delete event.windowUnavailable;
  if (browserWindow) event.browserWindow = browserWindow;
  if (unavailable) event.windowUnavailable = true;
  documents[tab] = event;
  queue(event);
  await save();
  await pump();
}

chrome.tabs.onActivated.addListener(info => serial(async () => {
  await ready;
  // `active` is per Chrome window. Focusing another window does not invalidate
  // the meeting window; choosing another tab inside it does.
  const affected = Object.keys(documents).filter(tab => Number(tab) === info.tabId || documents[tab].browserWindow?.id === info.windowId);
  for (const tab of affected) await refreshWindowSignal(tab);
}));
chrome.tabs.onDetached.addListener(tabID => serial(async () => {
  await ready;
  // During transfer, Chrome may briefly report the old association. Invalidate
  // it explicitly until the attached event or a fresh content heartbeat.
  await refreshWindowSignal(tabID, true);
}));
chrome.tabs.onAttached.addListener(tabID => serial(async () => {
  await ready;
  await refreshWindowSignal(tabID);
}));

chrome.webNavigation.onCommitted.addListener(details => {
  if (!Number.isInteger(details.frameId) || details.frameId < 0) return;
  serial(async () => {
    await ready;
    const tab = String(details.tabId);
    const previous = documents[tab];
    if (!previous || (details.frameId !== 0 && details.frameId !== (previous.frameID ?? 0))) return;
    const previousID = details.frameId === 0 ? (previous.rootDocumentID ?? previous.documentID) : previous.documentID;
    if (previousID === details.documentId) return;
    const frame = await currentFrame(tab, details.frameId);
    // Ignore delayed navigation callbacks too. An actual document replacement
    // ends the tracked call even when the destination has no host permission.
    if (frame?.documentId !== details.documentId || frame.documentLifecycle !== "active") return;
    if (details.frameId !== 0) {
      const root = await currentFrame(tab);
      if (root?.documentId !== previous.rootDocumentID || root.documentLifecycle !== "active" ||
          frame.parentFrameId !== 0 || frame.parentDocumentId !== root.documentId) return;
    }
    queue({ ...previous, state: "left" });
    delete documents[tab];
    await save();
    await pump();
  });
});

chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === RETRY_ALARM) serial(async () => { await ready; await pump(); });
});
// Chrome may clear alarms on browser restart; this event recreates any needed
// alarm from the durable terminal outbox even with no meeting tabs open.
chrome.runtime.onStartup.addListener(() => serial(async () => { await ready; await pump(); }));
serial(async () => { await ready; await pump(); });
