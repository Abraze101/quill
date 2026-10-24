const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { spawnSync } = require('node:child_process');

const source = fs.readFileSync(path.join(__dirname, 'background.js'), 'utf8');
const clone = value => JSON.parse(JSON.stringify(value));
const meetingURL = 'https://meet.google.com/abc-defg-hij';
const zoomURL = 'https://app.zoom.us/wc/12345678901/join';
function harness({ local = { client: 'client' }, session = {}, autoAck = true, initializationError } = {}) {
  let now = 1700000000000;
  let sequence = 0;
  let timerSequence = 0;
  const listeners = {};
  const frames = {};
  const childFrames = {};
  const windows = { 7: { id: 7, left: 12, top: 30, width: 1200, height: 800, state: 'normal' } };
  const tabWindows = {};
  const tabActive = {};
  const timers = new Map();
  const alarms = new Map();
  const sent = [];
  const healthSent = [];
  const ports = [];
  const errors = [];
  const state = { available: true, autoAck, connects: 0 };
  const area = data => ({
    get: async keys => {
      if (initializationError) throw initializationError;
      return Object.fromEntries((Array.isArray(keys) ? keys : [keys])
        .filter(key => key in data).map(key => [key, clone(data[key])]));
    },
    set: async values => {
      if (state.storageError) throw state.storageError;
      return Object.assign(data, clone(values));
    },
  });
  const chrome = {
    storage: { local: area(local), session: area(session) },
    runtime: {
      onMessage: { addListener: callback => listeners.message = callback },
      onStartup: { addListener: callback => listeners.startup = callback },
      sendNativeMessage: async (host, message) => {
        healthSent.push(clone(message));
        if (!state.available) throw new Error('Native host unavailable');
        if (state.healthHandler) return state.healthHandler(message);
        return { accepted: true, requestID: message.requestID, recordingStatus: { state: 'recording' } };
      },
      connectNative: () => {
        state.connects++;
        if (!state.available) throw new Error('Native host unavailable');
        const callbacks = {};
        const port = {
          disconnected: false,
          onDisconnect: { addListener: callback => callbacks.disconnect = callback },
          onMessage: { addListener: callback => callbacks.message = callback },
          disconnect() { port.disconnected = true; },
          postMessage(message) {
            if (!state.available || port.disconnected) throw new Error('Native port disconnected');
            sent.push(clone(message));
            if (state.autoAck) queueMicrotask(() => callbacks.message({ accepted: true, requestID: message.requestID }));
          },
          ack(answer) { callbacks.message(answer); },
          lose(error) {
            port.disconnected = true;
            chrome.runtime.lastError = error;
            try { callbacks.disconnect(); } finally { delete chrome.runtime.lastError; }
          },
        };
        ports.push(port);
        return port;
      },
    },
    tabs: {
      get: async tabID => ({ id: tabID, windowId: tabWindows[tabID], active: tabActive[tabID] !== false }),
      onRemoved: { addListener: callback => listeners.removed = callback },
      onActivated: { addListener: callback => listeners.activated = callback },
      onDetached: { addListener: callback => listeners.detached = callback },
      onAttached: { addListener: callback => listeners.attached = callback },
    },
    windows: { get: async windowID => {
      if (state.onWindowGet) await state.onWindowGet(windowID);
      if (!windows[windowID]) throw new Error('Window unavailable');
      return clone(windows[windowID]);
    } },
    webNavigation: {
      getFrame: async ({ tabId, frameId }) => {
        if (state.frameError) throw state.frameError;
        const frame = frameId === 0 ? frames[tabId] : childFrames[`${tabId}:${frameId}`];
        return frame ? clone(frame) : undefined;
      },
      onCommitted: { addListener: callback => listeners.committed = callback },
    },
    alarms: {
      create: async (name, info) => alarms.set(name, info),
      clear: async name => alarms.delete(name),
      onAlarm: { addListener: callback => listeners.alarm = callback },
    },
  };
  const context = vm.createContext({ chrome, URL, Date: { now: () => now },
    console: { error: (...args) => errors.push(args) },
    crypto: { randomUUID: () => `request-${++sequence}` },
    setTimeout: (callback, delay) => { const id = ++timerSequence; timers.set(id, { callback, at: now + delay }); return id; },
    clearTimeout: id => timers.delete(id),
  });
  vm.runInContext(source, context);
  async function flush() {
    for (let attempt = 0; attempt < 30; attempt++) {
      const before = vm.runInContext('pending', context);
      await before;
      await new Promise(resolve => setImmediate(resolve));
      if (before === vm.runInContext('pending', context)) return;
    }
    throw new Error('Background worker did not settle');
  }
  return {
    local, session, state, sent, healthSent, ports, alarms, errors, frames, childFrames, windows, tabWindows, tabActive, flush,
    frame(documentId, tabID = 42, url = meetingURL, lifecycle = 'active') {
      frames[tabID] = { documentId, documentLifecycle: lifecycle, url, parentFrameId: -1 };
      tabWindows[tabID] ??= 7;
    },
    child(documentId, { frameID = 5, tabID = 42, url = zoomURL, lifecycle = 'active',
      parentFrameID = 0, parentDocumentID = frames[tabID]?.documentId } = {}) {
      childFrames[`${tabID}:${frameID}`] = { documentId, documentLifecycle: lifecycle, url,
        parentFrameId: parentFrameID, parentDocumentId: parentDocumentID };
    },
    async message(documentId, meeting, eventState = 'joined', tabID = 42, extra = {}, senderExtra = {}) {
      listeners.message({ type: 'quill-meeting', documentID: 'untrusted-content-id', meeting,
        state: eventState, provider: 'google-meet', ...extra },
      { frameId: 0, tab: { id: tabID, windowId: tabWindows[tabID] }, url: meetingURL, documentId, documentLifecycle: 'active', ...senderExtra });
      await flush();
    },
    async childMessage(documentId, meeting = 'zoom-call', eventState = 'joined', extra = {}, frameID = 5) {
      return this.message(documentId, meeting, eventState, 42, { provider: 'zoom-web', ...extra },
        { frameId: frameID, url: zoomURL });
    },
    health(documentId, meeting = 'call-A', extra = {}, senderExtra = {}) {
      return new Promise(resolve => {
        const keepOpen = listeners.message({ type: 'quill-recording-health', meeting, provider: 'google-meet', ...extra },
          { frameId: 0, tab: { id: 42, windowId: 7 }, url: meetingURL, documentId, ...senderExtra }, resolve);
        assert.equal(keepOpen, true);
      });
    },
    async remove(tabID = 42) { delete frames[tabID]; listeners.removed(tabID); await flush(); },
    async activate(tabID, windowID = 7) { listeners.activated({ tabId: tabID, windowId: windowID }); await flush(); },
    async detach(tabID = 42) { listeners.detached(tabID); await flush(); },
    async attach(tabID = 42) { listeners.attached(tabID); await flush(); },
    async committed(documentId, tabID = 42, frameID = 0) { listeners.committed({ frameId: frameID, tabId: tabID, documentId }); await flush(); },
    async advance(milliseconds) {
      now += milliseconds;
      for (const [id, timer] of [...timers]) if (timer.at <= now) { timers.delete(id); timer.callback(); }
      for (const [name, alarm] of [...alarms]) if (alarm.when <= now) { alarms.delete(name); listeners.alarm({ name }); }
      await flush();
    },
    async restartEvent() { listeners.startup(); await flush(); },
  };
}

test('rejects a departed document first seen after the current document, using browser identity', async () => {
  const h = harness();
  h.frame('document-B');
  await h.message('document-B', 'call-B');
  await h.message('document-A', 'call-A');
  await h.message('document-A', 'call-A', 'left');
  assert.deepEqual(h.sent.map(x => [x.meeting, x.state]), [['call-B', 'joined']]);
  assert.equal(h.session.documents['42'].documentID, 'document-B');
});

test('a known retired document and delayed navigation cannot mark the replacement call left', async () => {
  const h = harness();
  h.frame('document-A');
  await h.message('document-A', 'call-A');
  h.frame('document-B');
  await h.message('document-B', 'call-B');
  await h.message('document-A', 'call-A', 'left');
  await h.committed('document-A');
  assert.deepEqual(h.sent.map(x => [x.meeting, x.state]), [['call-A', 'joined'], ['call-A', 'left'], ['call-B', 'joined']]);
  assert.equal(h.session.documents['42'].documentID, 'document-B');
});

test('committed navigation outside meeting hosts ends only a tracked call without storing URLs', async () => {
  const h = harness();
  h.frame('document-A');
  await h.message('document-A', 'call-A');
  h.frame('document-outside', 42, 'https://example.invalid/private');
  await h.committed('document-outside');
  h.frame('unrelated', 99, 'https://example.invalid/another-private');
  await h.committed('unrelated', 99);
  assert.deepEqual(h.sent.map(x => [x.meeting, x.state]), [['call-A', 'joined'], ['call-A', 'left']]);
  assert.deepEqual(h.session.documents, {});
  assert.ok(!JSON.stringify([h.local, h.session, h.sent]).includes('https://'));
});

test('tab-close leave survives a disconnected host and worker/browser restart until exact acknowledgment', async () => {
  const h = harness();
  h.frame('document-A');
  await h.message('document-A', 'call-A');
  h.state.available = false;
  await h.remove();
  const item = Object.values(h.local.outbox)[0];
  assert.equal(item.state, 'left');
  assert.deepEqual(h.session.documents, {});
  assert.equal(h.alarms.size, 1);
  // Browser restart clears session data and may clear alarms, but not local.
  const restarted = harness({ local: h.local, autoAck: false });
  await restarted.flush();
  assert.equal(restarted.alarms.size, 1);
  await restarted.advance(30000);
  assert.equal(restarted.sent[0].requestID, item.requestID);
  restarted.ports[0].ack({ accepted: true, requestID: 'unrelated-ack' });
  await restarted.flush();
  assert.equal(Object.values(restarted.local.outbox).length, 1);
  restarted.ports[0].ack({ accepted: true, requestID: item.requestID });
  await restarted.flush();
  assert.deepEqual(restarted.local.outbox, {});
  assert.equal(restarted.alarms.size, 0);
});

test('an old joined acknowledgment cannot erase a newer terminal signal', async () => {
  const h = harness({ autoAck: false });
  h.frame('document-A');
  await h.message('document-A', 'call-A');
  const joined = h.sent[0];
  await h.remove();
  const leave = Object.values(h.local.outbox)[0];
  h.ports[0].ack({ accepted: true, requestID: joined.requestID });
  await h.flush();
  assert.equal(Object.values(h.local.outbox)[0].requestID, leave.requestID);
  assert.equal(h.sent[1].state, 'left');
  h.ports[0].ack({ accepted: true, requestID: leave.requestID });
  await h.flush();
  assert.deepEqual(h.local.outbox, {});
});

test('offline join and leave coalesce so recovery never replays joined for a departed call', async () => {
  const h = harness();
  h.state.available = false;
  h.frame('document-A');
  await h.message('document-A', 'call-A');
  await h.message('document-A', 'call-A', 'left');
  assert.equal(Object.values(h.local.outbox).length, 1);
  assert.equal(Object.values(h.local.outbox)[0].state, 'left');
  h.state.available = true;
  await h.advance(30000);
  assert.deepEqual(h.sent.map(x => x.state), ['left']);
});

test('reconnect attempts back off, and an unacknowledged leave is retained after timeout', async () => {
  const h = harness();
  h.state.available = false;
  h.frame('document-A');
  await h.message('document-A', 'call-A', 'left');
  assert.equal(h.state.connects, 1);
  assert.deepEqual(h.errors, [['[Quill meeting detector] Native connection failed: native messaging host not found.']]);
  for (let i = 0; i < 3; i++) await h.message('document-A', 'call-A', 'left');
  assert.equal(h.state.connects, 1);
  await h.advance(30000);
  assert.equal(h.state.connects, 2);
  assert.equal(h.errors.length, 1);
  await h.advance(30000);
  assert.equal(h.state.connects, 2);
  h.state.available = true;
  h.state.autoAck = false;
  await h.advance(30000);
  assert.equal(h.state.connects, 3);
  await h.advance(10000);
  assert.equal(h.ports[0].disconnected, true);
  assert.equal(Object.values(h.local.outbox)[0].state, 'left');
  assert.equal(h.alarms.size, 1);
});

test('native disconnect reports lastError once and preserves delivery through backed-off recovery', async () => {
  const h = harness({ autoAck: false });
  h.frame('document-A');
  await h.message('document-A', 'private-call-A', 'left');
  const requestID = h.sent[0].requestID;
  h.ports[0].lose({ message: 'Specified native messaging host not found.' });
  await h.flush();
  assert.deepEqual(h.errors, [['[Quill meeting detector] Native connection failed: native messaging host not found.']]);
  assert.equal(Object.values(h.local.outbox)[0].requestID, requestID);
  assert.equal(h.alarms.size, 1);
  await h.advance(29999);
  assert.equal(h.state.connects, 1);
  await h.advance(1);
  assert.equal(h.state.connects, 2);
  h.ports[1].lose({ message: 'Specified native messaging host not found.' });
  await h.flush();
  assert.equal(h.errors.length, 1);
  h.state.autoAck = true;
  await h.advance(59999);
  assert.equal(h.state.connects, 2);
  await h.advance(1);
  assert.equal(h.state.connects, 3);
  assert.deepEqual(h.sent.map(item => item.requestID), [requestID, requestID, requestID]);
  assert.deepEqual(h.local.outbox, {});
  assert.equal(h.alarms.size, 0);
  assert.equal(h.errors.length, 1);
});

test('serialized storage failures are deduplicated without logging content or stopping later events', async () => {
  const h = harness();
  await h.flush();
  h.frame('private-document-A');
  h.state.storageError = new TypeError('Private title https://meet.google.com/private-call-A {"outbox":"private-data"}');
  for (let attempt = 0; attempt < 3; attempt++) await h.message('private-document-A', 'private-call-A');
  assert.deepEqual(h.errors, [['[Quill meeting detector] Background task failed: TypeError.']]);
  assert.deepEqual(h.sent, []);
  h.state.storageError = null;
  await h.message('private-document-A', 'private-call-A');
  assert.equal(h.sent.length, 1);
  assert.deepEqual(h.local.outbox, {});
  assert.equal(h.errors.length, 1);
});

test('initialization rejection is visible once even before messages and never logs raw exception details', async () => {
  const h = harness({ initializationError: new Error('Private title https://meet.google.com/private-call-A ' + 'x'.repeat(2000)) });
  await h.flush();
  assert.deepEqual(h.errors, [[
    '[Quill meeting detector] Background initialization failed; reload the extension after correcting the error: unrecognized error.',
  ]]);
  h.frame('private-document-A');
  await h.message('private-document-A', 'private-call-A');
  await h.restartEvent();
  assert.equal(h.errors.length, 1);
  assert.equal(h.state.connects, 0);
  assert.deepEqual(h.sent, []);
});

test('unexpected frame lookup errors are visible without tab data and a later heartbeat recovers', async () => {
  const h = harness();
  h.frame('private-document-A');
  for (const message of ['No tab with id: 42.', 'No frame with id 0 in tab 42.', 'Frame with ID 0 was removed.']) {
    h.state.frameError = new Error(message);
    await h.message('private-document-A', 'private-call-A');
  }
  assert.deepEqual(h.errors, []);
  h.state.frameError = new TypeError('Cannot query https://meet.google.com/private-call-A with private-document-A');
  await h.message('private-document-A', 'private-call-A');
  await h.message('private-document-A', 'private-call-A');
  assert.deepEqual(h.errors, [['[Quill meeting detector] Meeting tab lookup failed: TypeError.']]);
  assert.deepEqual(h.sent, []);
  assert.deepEqual(h.local.outbox, {});
  h.state.frameError = null;
  await h.message('private-document-A', 'private-call-A');
  assert.equal(h.sent.length, 1);
  assert.equal(h.sent[0].state, 'joined');
  assert.deepEqual(h.local.outbox, {});
  assert.equal(h.errors.length, 1);
});

test('restart discards queued joined heartbeats until current meeting evidence returns', async () => {
  const h = harness();
  h.state.available = false;
  h.frame('document-A');
  await h.message('document-A', 'call-A');
  const restarted = harness({ local: h.local, session: h.session });
  await restarted.flush();
  await restarted.advance(30000);
  assert.deepEqual(restarted.sent, []);
  assert.deepEqual(restarted.local.outbox, {});
  restarted.frame('document-A');
  await restarted.message('document-A', 'call-A');
  assert.equal(restarted.sent[0].state, 'joined');
});

test('current inactive documents do not provide join evidence', async () => {
  const h = harness();
  h.frame('document-A', 42, meetingURL, 'cached');
  await h.message('document-A', 'call-A');
  assert.deepEqual(h.sent, []);
  assert.deepEqual(h.local.outbox, {});
});

test('window geometry comes only from Chrome and retains no titles, URLs or content fields', async () => {
  const h = harness();
  h.frame('document-A');
  Object.assign(h.windows[7], { title: 'Private title', tabs: [{ url: meetingURL }], focused: true });
  await h.message('document-A', 'call-A', 'joined', 42, {
    browserWindow: { id: 999, left: 0, top: 0, width: 2, height: 2, title: 'Spoof' },
  });
  assert.deepEqual(h.sent[0].browserWindow, { id: 7, left: 12, top: 30, width: 1200, height: 800 });
  assert.deepEqual(h.session.documents['42'].browserWindow, h.sent[0].browserWindow);
  assert.ok(!JSON.stringify([h.local, h.session, h.sent]).includes('Private title'));
  assert.ok(!JSON.stringify([h.local, h.session, h.sent]).includes('https://'));
  h.windows[7].left = -1000;
  await h.message('document-A', 'call-A', 'unknown');
  assert.equal(h.sent[1].browserWindow.left, -1000);
  await h.message('document-A', 'call-A', 'left');
  assert.ok(!('browserWindow' in h.sent[2]));
});

test('missing or minimized window metadata clears an earlier valid capture target', async () => {
  const h = harness();
  h.frame('document-A');
  await h.message('document-A', 'call-A');
  h.windows[7].state = 'minimized';
  await h.message('document-A', 'call-A');
  assert.ok(!('browserWindow' in h.sent[1]));
  assert.ok(!('browserWindow' in h.session.documents['42']));
  delete h.windows[7];
  await h.message('document-A', 'call-A', 'unknown');
  assert.ok(!('browserWindow' in h.sent[2]));
});

test('capture target requires the meeting tab to be active within its window, not the focused window', async () => {
  const h = harness();
  h.frame('document-A');
  h.windows[7].focused = false;
  await h.message('document-A', 'call-A');
  assert.equal(h.sent[0].browserWindow.id, 7);
  h.tabActive[42] = false;
  await h.message('document-A', 'call-A');
  assert.equal(h.sent[1].state, 'joined');
  assert.ok(!('browserWindow' in h.sent[1]));
});

test('selecting a different tab immediately invalidates screen metadata without reporting meeting leave', async () => {
  const h = harness();
  h.frame('document-A');
  await h.message('document-A', 'call-A');
  h.tabActive[42] = false;
  await h.activate(99);
  assert.equal(h.sent[1].state, 'unknown');
  assert.ok(!('browserWindow' in h.sent[1]));
  h.tabActive[42] = true;
  await h.activate(42);
  assert.equal(h.sent[2].state, 'unknown');
  assert.equal(h.sent[2].browserWindow.id, 7);
  await h.message('document-A', 'call-A');
  assert.equal(h.sent[3].state, 'joined');
});

test('tab transfer invalidates old bounds before reporting its new window without fabricating join evidence', async () => {
  const h = harness();
  h.frame('document-A');
  await h.message('document-A', 'call-A');
  await h.detach();
  assert.equal(h.sent[1].state, 'unknown');
  assert.ok(!('browserWindow' in h.sent[1]));
  h.windows[8] = { id: 8, left: -1200, top: 10, width: 1000, height: 900, state: 'normal' };
  h.tabWindows[42] = 8;
  await h.attach();
  assert.equal(h.sent[2].state, 'unknown');
  assert.equal(h.sent[2].browserWindow.id, 8);
  assert.ok(!JSON.stringify(h.sent).includes('windowUnavailable'));
});

test('activation in an unrelated window and a retired tracked document cannot update the capture target', async () => {
  const h = harness();
  h.frame('document-A');
  await h.message('document-A', 'call-A');
  await h.activate(99, 8);
  assert.equal(h.sent.length, 1);
  h.frame('document-B');
  await h.activate(42);
  await h.detach();
  assert.equal(h.sent.length, 1);
  assert.equal(h.session.documents['42'].documentID, 'document-A');
});

test('invalid browser geometry is omitted without replacing meeting state with guessed bounds', async () => {
  const h = harness();
  h.frame('document-A');
  for (const invalid of [-1, 0, 2.5, 65537, null, '1200']) {
    h.windows[7].width = invalid;
    await h.message('document-A', 'call-A');
    assert.equal(h.sent.at(-1).state, 'joined');
    assert.ok(!('browserWindow' in h.sent.at(-1)));
  }
});

test('document replacement during a window lookup cannot contaminate the new target', async () => {
  const h = harness();
  h.frame('document-A');
  h.state.onWindowGet = () => {
    h.frame('document-B');
    h.state.onWindowGet = null;
  };
  await h.message('document-A', 'call-A');
  assert.deepEqual(h.sent, []);
  assert.deepEqual(h.session.documents, {});
  h.windows[7].left = 444;
  await h.message('document-B', 'call-B');
  h.windows[7].left = 999;
  await h.message('document-A', 'call-A');
  assert.equal(h.sent.length, 1);
  assert.equal(h.sent[0].browserWindow.left, 444);
  assert.equal(h.session.documents['42'].browserWindow.left, 444);
});

test('queued delivery refreshes its exact tab window after a transfer rather than using saved bounds', async () => {
  const h = harness({ autoAck: false });
  h.frame('document-A');
  await h.message('document-A', 'call-A');
  const first = h.sent[0];
  await h.message('document-A', 'call-A');
  h.windows[8] = { id: 8, left: -1200, top: 10, width: 1000, height: 900, state: 'normal' };
  h.tabWindows[42] = 8;
  h.ports[0].ack({ accepted: true, requestID: first.requestID });
  await h.flush();
  assert.deepEqual(h.sent[1].browserWindow, { id: 8, left: -1200, top: 10, width: 1000, height: 900 });
});

test('direct Zoom client frame is bound to its current same-origin numeric meeting root without storing URLs', async () => {
  const h = harness();
  h.frame('zoom-root', 42, zoomURL + '?private=not-stored');
  h.child('zoom-child');
  await h.childMessage('zoom-child', 'zoom-call', 'joined', { rootDocumentID: 'spoofed-root', frameID: 999 });
  assert.equal(h.sent.length, 1);
  assert.equal(h.sent[0].provider, 'zoom-web');
  assert.equal(h.sent[0].state, 'joined');
  assert.equal(h.sent[0].browserWindow.id, 7);
  assert.equal(h.session.documents['42'].documentID, 'zoom-child');
  assert.equal(h.session.documents['42'].rootDocumentID, 'zoom-root');
  assert.equal(h.session.documents['42'].frameID, 5);
  assert.ok(!JSON.stringify([h.local, h.session, h.sent]).includes('https://'));
  assert.ok(!JSON.stringify([h.local, h.session, h.sent]).includes('12345678901'));
  assert.ok(!('rootDocumentID' in h.sent[0]));
  assert.ok(!('frameID' in h.sent[0]));
});

test('embedded Zoom trust rejects different origin, meeting, path, provider, parent, and inactive documents', async () => {
  for (const entry of [
    { rootURL: 'https://us02web.zoom.us/wc/12345678901/join' },
    { rootURL: 'https://app.zoom.us/wc/99999999999/join' },
    { rootURL: 'https://app.zoom.us/j/12345678901' },
    { rootURL: meetingURL },
    { child: { url: 'https://app.zoom.us/wc/99999999999/join' } },
    { child: { url: 'https://app.zoom.us/wc/not-numeric/join' } },
    { child: { url: 'https://app.zoom.us.attacker.test/wc/12345678901/join' } },
    { child: { parentFrameID: 4 } },
    { child: { parentDocumentID: 'retired-root' } },
    { child: { parentDocumentID: null } },
    { child: { lifecycle: 'cached' } },
    { rootLifecycle: 'cached' },
    { provider: 'google-meet' },
  ]) {
    const h = harness();
    h.frame('zoom-root', 42, entry.rootURL || zoomURL, entry.rootLifecycle || 'active');
    h.child('zoom-child', entry.child);
    await h.childMessage('zoom-child', 'zoom-call', 'joined', { provider: entry.provider || 'zoom-web' });
    assert.deepEqual(h.sent, [], JSON.stringify(entry));
    assert.deepEqual(h.session.documents, {}, JSON.stringify(entry));
    assert.deepEqual(h.local.outbox, {}, JSON.stringify(entry));
  }
});

test('retired child and root identities cannot replace or end the current Zoom call', async () => {
  const h = harness();
  h.frame('root-A', 42, zoomURL);
  h.child('child-A');
  await h.childMessage('child-A', 'call-A');
  h.frame('root-B', 42, zoomURL);
  // Chrome's stale child lookup must not be associated with a new root.
  await h.childMessage('child-A', 'call-A');
  assert.equal(h.sent.length, 1);
  h.child('child-B');
  await h.childMessage('child-B', 'call-B');
  await h.childMessage('child-A', 'call-A', 'left');
  await h.committed('root-A');
  await h.committed('child-A', 42, 5);
  assert.deepEqual(h.sent.map(x => [x.meeting, x.state]), [['call-A', 'joined'], ['call-A', 'left'], ['call-B', 'joined']]);
  assert.equal(h.session.documents['42'].rootDocumentID, 'root-B');
  assert.equal(h.session.documents['42'].documentID, 'child-B');
});

test('root or child replacement during Zoom geometry lookup cannot contaminate the new target', async () => {
  for (const replaceRoot of [false, true]) {
    const h = harness();
    h.frame('root-A', 42, zoomURL);
    h.child('child-A');
    h.state.onWindowGet = () => {
      if (replaceRoot) h.frame('root-B', 42, zoomURL);
      h.child('child-B');
      h.state.onWindowGet = null;
    };
    await h.childMessage('child-A', 'call-A');
    assert.deepEqual(h.sent, []);
    assert.deepEqual(h.session.documents, {});
    await h.childMessage('child-B', 'call-B');
    assert.equal(h.sent.length, 1);
    assert.equal(h.session.documents['42'].rootDocumentID, replaceRoot ? 'root-B' : 'root-A');
  }
});

test('tracked child navigation and root navigation retire Zoom calls while unrelated child commits do nothing', async () => {
  const h = harness();
  h.frame('zoom-root', 42, zoomURL);
  h.child('child-A');
  await h.childMessage('child-A', 'call-A');
  h.child('unrelated', { frameID: 8 });
  await h.committed('unrelated', 42, 8);
  assert.equal(h.sent.length, 1);
  h.child('child-outside', { url: 'https://example.invalid/departed' });
  await h.committed('child-outside', 42, 5);
  assert.deepEqual(h.sent.map(x => x.state), ['joined', 'left']);
  assert.deepEqual(h.session.documents, {});
  h.child('child-B');
  await h.childMessage('child-B', 'call-B');
  await h.committed('child-outside', 42, 5);
  assert.equal(h.sent.length, 3);
  h.frame('root-outside', 42, 'https://example.invalid/departed');
  await h.committed('root-outside');
  assert.deepEqual(h.sent.map(x => [x.meeting, x.state]), [['call-A', 'joined'], ['call-A', 'left'], ['call-B', 'joined'], ['call-B', 'left']]);
  assert.deepEqual(h.session.documents, {});
});

test('queued Zoom delivery revalidates both root and child before replaying a fresh heartbeat', async () => {
  for (const replacement of ['root', 'child', 'meeting-path']) {
    const h = harness({ autoAck: false });
    h.frame('zoom-root', 42, zoomURL);
    h.child('zoom-child');
    await h.childMessage('zoom-child');
    const first = h.sent[0];
    await h.childMessage('zoom-child');
    if (replacement === 'root') h.frame('new-root', 42, zoomURL);
    if (replacement === 'child') h.child('new-child');
    if (replacement === 'meeting-path') h.child('zoom-child', { url: 'https://app.zoom.us/wc/99999999999/join' });
    h.ports[0].ack({ accepted: true, requestID: first.requestID });
    await h.flush();
    assert.equal(h.sent.length, 1, replacement);
    assert.deepEqual(h.local.outbox, {}, replacement);
  }
});

test('Zoom child window updates retain both identities and reject a retired root during refresh', async () => {
  const h = harness();
  h.frame('zoom-root', 42, zoomURL);
  h.child('zoom-child');
  await h.childMessage('zoom-child');
  h.tabActive[42] = false;
  await h.activate(99);
  assert.equal(h.sent[1].state, 'unknown');
  assert.ok(!('browserWindow' in h.sent[1]));
  h.tabActive[42] = true;
  h.windows[8] = { id: 8, left: -900, top: 30, width: 1000, height: 700 };
  h.tabWindows[42] = 8;
  await h.attach();
  assert.equal(h.sent[2].browserWindow.id, 8);
  assert.equal(h.session.documents['42'].frameID, 5);
  assert.equal(h.session.documents['42'].rootDocumentID, 'zoom-root');
  h.state.onWindowGet = () => {
    h.frame('new-root', 42, zoomURL);
    h.state.onWindowGet = null;
  };
  await h.activate(42, 8);
  await h.detach();
  assert.equal(h.sent.length, 3);
  assert.equal(h.session.documents['42'].rootDocumentID, 'zoom-root');
});

test('Zoom child leave remains durable across native failure and worker restart', async () => {
  const h = harness();
  h.frame('zoom-root', 42, zoomURL);
  h.child('zoom-child');
  await h.childMessage('zoom-child');
  h.state.available = false;
  h.child('child-departed', { url: 'https://app.zoom.us/wc/leave' });
  await h.committed('child-departed', 42, 5);
  const leave = Object.values(h.local.outbox)[0];
  assert.equal(leave.state, 'left');
  assert.equal(leave.rootDocumentID, 'zoom-root');
  assert.equal(leave.frameID, 5);
  const restarted = harness({ local: h.local });
  await restarted.flush();
  await restarted.advance(30000);
  assert.equal(restarted.sent[0].state, 'left');
  assert.equal(restarted.sent[0].requestID, leave.requestID);
  assert.deepEqual(restarted.local.outbox, {});
});

test('legacy top-only tab state remains valid for refresh and navigation after the frame upgrade', async () => {
  const legacy = { source: 'client-42', meeting: 'call-A', provider: 'google-meet', state: 'joined', documentID: 'document-A', tabID: '42' };
  const h = harness({ session: { documents: { '42': legacy } } });
  h.frame('document-A');
  await h.activate(42);
  await h.message('document-A', 'call-A');
  assert.deepEqual(h.sent.map(x => x.state), ['unknown', 'joined']);
  h.frame('document-B');
  await h.committed('document-B');
  assert.deepEqual(h.sent.map(x => x.state), ['unknown', 'joined', 'left']);
});

test('native host acknowledges the delivery identity after writing only allowed signal fields', () => {
  const script = String.raw`
import importlib.util, io, json, pathlib, struct, sys, tempfile
root = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(root))
spec = importlib.util.spec_from_file_location('native', root / 'native-host.py')
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)
message = {'source':'test-tab', 'meeting':'call', 'state':'left', 'provider':'google-meet', 'requestID':'delivery-123', 'url':'https://example.invalid/private'}
with tempfile.TemporaryDirectory() as directory:
    payload = json.dumps(message).encode()
    target = io.BytesIO()
    native.serve(io.BytesIO(struct.pack('=I', len(payload)) + payload), target, directory)
    reply = target.getvalue()
    print(json.dumps({'answer':json.loads(reply[4:]), 'signal':json.loads(next(pathlib.Path(directory).glob('*.json')).read_text())}))
`;
  const result = spawnSync('/usr/bin/python3', ['-c', script, path.resolve(__dirname, '..')], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  const output = JSON.parse(result.stdout);
  assert.deepEqual(output.answer, { accepted: true, requestID: 'delivery-123', recordingStatus: { state: 'unavailable' } });
  assert.deepEqual(Object.keys(output.signal).sort(), ['source', 'meeting', 'state', 'provider', 'timestamp'].sort());
});

test('read-only health probes work on an unknown Meet route and never queue a recording signal', async () => {
  const h = harness();
  const url = 'https://meet.google.com/future/abc-defg-hij';
  h.frame('document-A', 42, url);
  const answer = await h.health('document-A', 'warning-call', {}, { url });
  assert.equal(answer.recordingStatus.state, 'recording');
  assert.deepEqual(h.healthSent.map(x => ({...x, requestID: 'opaque'})), [
    {type: 'recording-status', requestID: 'opaque', meeting: 'warning-call'},
  ]);
  assert.deepEqual(h.sent, []);
  assert.deepEqual(h.session.documents, {});
  assert.ok(!JSON.stringify(h.healthSent).includes('https://'));
});

test('health rejects stale documents, mismatched providers, unsupported child frames, and wrong hosts', async () => {
  const h = harness();
  h.frame('document-A');
  for (const [document, extra, sender] of [
    ['document-retired', {}, {}],
    ['document-A', {provider: 'zoom-web'}, {}],
    ['document-A', {}, {frameId: 5}],
    ['document-A', {}, {url: 'https://meet.google.com.attacker.test/abc-defg-hij'}],
  ]) {
    assert.equal((await h.health(document, 'call-A', extra, sender)).recordingStatus.state, 'unavailable');
  }
  assert.deepEqual(h.healthSent, []);
  h.frame('document-A', 42, meetingURL, 'cached');
  assert.equal((await h.health('document-A')).recordingStatus.state, 'unavailable');
});

test('health replies revalidate document and Zoom root identity after native response', async () => {
  for (const zoom of [false, true]) {
    const h = harness();
    let reply;
    h.state.healthHandler = message => new Promise(resolve => { reply = () => resolve({accepted: true, requestID: message.requestID, recordingStatus: {state: 'recording'}}); });
    h.frame('document-A', 42, zoom ? zoomURL : meetingURL);
    if (zoom) h.child('child-A');
    const answer = zoom ? h.health('child-A', 'call-A', {provider: 'zoom-web'}, {frameId: 5, url: zoomURL}) : h.health('document-A');
    await h.flush();
    assert.equal(typeof reply, 'function');
    h.frame('document-B', 42, zoom ? zoomURL : meetingURL);
    reply();
    assert.equal((await answer).recordingStatus.state, 'unavailable');
  }
});

test('health rejects wrong reply identity, absent health state, and native failure without affecting leaves', async () => {
  for (const failure of ['wrong-id', 'missing-state', 'unavailable']) {
    const h = harness();
    h.frame('document-A');
    h.state.healthHandler = message => ({accepted: true, requestID: failure === 'wrong-id' ? 'other' : message.requestID,
      ...(failure === 'missing-state' ? {} : {recordingStatus: {state: 'recording'}})});
    h.state.available = failure !== 'unavailable';
    assert.equal((await h.health('document-A')).recordingStatus.state, 'unavailable');
    h.state.available = true;
    await h.message('document-A', 'call-A');
    await h.remove();
    assert.deepEqual(h.sent.map(x => x.state), ['joined', 'left']);
  }
});

test('a stalled health probe times out while signal delivery continues independently', async () => {
  const h = harness();
  h.frame('document-A');
  h.state.healthHandler = () => new Promise(() => {});
  const answer = h.health('document-A');
  await h.flush();
  await h.message('document-A', 'call-A');
  assert.equal(h.sent[0].state, 'joined');
  await h.advance(4000);
  assert.equal((await answer).recordingStatus.state, 'unavailable');
});

test('health deduplicates a rapid request and cannot reuse an older response for a newer session', async () => {
  const h = harness();
  h.frame('document-A');
  const replies = [];
  h.state.healthHandler = message => new Promise(resolve => replies.push(() => resolve({accepted: true, requestID: message.requestID, recordingStatus: {state: 'recording'}})));
  const first = h.health('document-A', 'call-A');
  const repeated = h.health('document-A', 'call-A');
  await h.flush();
  assert.equal(h.healthSent.length, 1);
  const replacement = h.health('document-A', 'call-B');
  await h.flush();
  assert.equal(h.healthSent.length, 2);
  replies[0]();
  assert.equal((await first).recordingStatus.state, 'unavailable');
  assert.equal((await repeated).recordingStatus.state, 'unavailable');
  replies[1]();
  assert.equal((await replacement).recordingStatus.state, 'recording');
});

test('health reports only the allowed state and remains usable when recording outbox storage fails', async () => {
  const h = harness({ initializationError: new Error('Storage unavailable') });
  h.frame('document-A');
  h.state.healthHandler = message => ({accepted: true, requestID: message.requestID,
    recordingStatus: {state: 'paused', privatePath: '/private/location'}, secret: 'not forwarded'});
  const answer = await h.health('document-A');
  assert.deepEqual(clone(answer), {recordingStatus: {state: 'paused'}});
});
