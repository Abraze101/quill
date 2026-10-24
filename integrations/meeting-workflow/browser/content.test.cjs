const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const detectorSource = fs.readFileSync(path.join(__dirname, 'detector.js'), 'utf8');
const contentSource = fs.readFileSync(path.join(__dirname, 'content.js'), 'utf8');
const warningSource = fs.readFileSync(path.join(__dirname, 'recording-warning.js'), 'utf8');
const clone = value => JSON.parse(JSON.stringify(value));

// Run both shipped scripts, including the actual scan and page lifecycle,
// against DOM-shaped controls matching the observed muted Meet toolbar.
function harness(initialFailure = null, initialURL = 'https://meet.google.com/abc-defg-hij', withWarning = false) {
  let sequence = 0;
  let timerSequence = 0;
  const timers = new Map();
  const listeners = new Map();
  const attempts = [];
  const delivered = [];
  const state = { failure: initialFailure, toolbar: true, now: 0, healthState: 'not-recording' };
  const warningNodes = [];
  const mountedWarnings = [];
  const observer = { active: false, disconnects: 0, callback: null };
  const ids = new Map();
  const document = {
    documentElement: { append: node => mountedWarnings.push(node) },
    createElement: tag => {
      const node = { tag, attributes: {}, style: {}, children: [],
        setAttribute(name, value) { this.attributes[name] = value; },
        append(...children) { this.children.push(...children); },
        attachShadow() { return this.shadow = document.createElement('shadow'); },
        addEventListener(name, callback) { this[name] = callback; },
        remove() { this.removed = true; },
      };
      warningNodes.push(node); return node;
    },
    getElementById: id => ids.get(id) ?? null,
    querySelectorAll: selector => selector.startsWith('button') && state.toolbar ? controls : [],
  };
  function element(tag, attributes = {}, text) {
    const node = {
      nodeType: 1, tagName: tag.toUpperCase(), ownerDocument: document,
      childNodes: text === undefined ? [] : [{ nodeType: 3, nodeValue: text }],
      disabled: false,
      getAttribute: name => attributes[name] ?? null,
      closest: () => null,
      getClientRects: () => [{}],
    };
    if (attributes.id) ids.set(attributes.id, node);
    return node;
  }
  element('span', { id: 'DPUxh-nav9Xe' }, 'People');
  const controls = [
    element('button', { 'aria-label': 'Leave call' }),
    element('button', { 'aria-labelledby': 'DPUxh-nav9Xe' }),
    element('button', { 'aria-label': 'Turn on microphone' }),
  ];
  function timer(kind, callback, delay) {
    const id = ++timerSequence;
    timers.set(id, { kind, callback, delay });
    return id;
  }
  const location = { href: initialURL };
  const context = vm.createContext({
    document, location, URL, Date: { now: () => state.now },
    crypto: { randomUUID: () => `opaque-${++sequence}` },
    getComputedStyle: () => ({ display: 'block', visibility: 'visible' }),
    MutationObserver: class {
      constructor(callback) { observer.callback = callback; }
      observe() { observer.active = true; }
      disconnect() { observer.active = false; observer.disconnects++; }
    },
    window: {
      addEventListener: (event, callback) => listeners.set(event, callback),
      removeEventListener: (event, callback) => {
        if (listeners.get(event) === callback) listeners.delete(event);
      },
    },
    setTimeout: (callback, delay) => timer('timeout', callback, delay),
    clearTimeout: id => timers.delete(id),
    setInterval: (callback, delay) => timer('interval', callback, delay),
    clearInterval: id => timers.delete(id),
    chrome: { runtime: { sendMessage(message) {
      attempts.push(clone(message));
      if (state.failure?.kind === 'sync') throw new Error(state.failure.message);
      if (state.failure?.kind === 'async') return Promise.reject(new Error(state.failure.message));
      delivered.push(clone(message));
      if (message.type === 'quill-recording-health') return Promise.resolve({ recordingStatus: { state: state.healthState } });
      return Promise.resolve();
    } } },
  });
  vm.runInContext(detectorSource, context, { filename: 'detector.js' });
  if (withWarning) vm.runInContext(warningSource, context, { filename: 'recording-warning.js' });
  vm.runInContext(contentSource, context, { filename: 'content.js' });
  return {
    state, observer, attempts, delivered, timers, listeners, location, warningNodes, mountedWarnings,
    mutate: () => observer.callback(),
    pagehide: () => listeners.get('pagehide')?.(),
    tick() {
      for (const entry of [...timers.values()]) if (entry.kind === 'interval') entry.callback();
    },
    async flush() { for (let i = 0; i < 8; i++) await Promise.resolve(); },
  };
}

function assertRetired(h) {
  assert.equal(h.observer.active, false);
  assert.equal(h.observer.disconnects, 1);
  assert.equal(h.timers.size, 0);
  assert.equal(h.listeners.has('pagehide'), false);
}

test('actual content scan recognizes the observed toolbar, heartbeats, and sends a real pagehide leave', async () => {
  const h = harness();
  h.tick();
  h.pagehide();
  await h.flush();
  assert.deepEqual(h.delivered.map(message => message.state), ['joined', 'joined', 'left']);
  assert.equal(new Set(h.delivered.map(message => message.meeting)).size, 1);
  assert.deepEqual(Object.keys(h.delivered[0]).sort(), ['documentID', 'meeting', 'provider', 'state', 'type']);
  assert.equal(h.delivered[0].provider, 'google-meet');
  assert.equal(JSON.stringify(h.delivered).includes('meet.google.com'), false);
});

test('a prefixed Meet page sends joined heartbeats and a real leave without transmitting its URL', async () => {
  const h = harness(null, 'https://meet.google.com/meet/abc-defg-hij?authuser=1');
  h.tick();
  h.pagehide();
  await h.flush();
  assert.deepEqual(h.delivered.map(message => message.state), ['joined', 'joined', 'left']);
  assert.equal(new Set(h.delivered.map(message => message.meeting)).size, 1);
  assert.deepEqual(Object.keys(h.delivered[0]).sort(), ['documentID', 'meeting', 'provider', 'state', 'type']);
  assert.equal(h.delivered[0].provider, 'google-meet');
  assert.equal(JSON.stringify(h.delivered).includes('meet.google.com'), false);
  assert.equal(JSON.stringify(h.delivered).includes('authuser'), false);
});

test('a same-document Meet route change preserves the joined session instead of reporting a false leave', async () => {
  const h = harness();
  const originalMeeting = h.delivered[0].meeting;
  h.location.href = 'https://meet.google.com/meet/abc-defg-hij?authuser=1';
  h.tick();
  h.location.href = 'https://meet.google.com/abc-defg-hij';
  h.tick();
  await h.flush();
  assert.deepEqual(h.delivered.map(message => message.state), ['joined', 'joined', 'joined']);
  assert.ok(h.delivered.every(message => message.meeting === originalMeeting));
  h.pagehide();
  await h.flush();
  assert.equal(h.delivered.at(-1).state, 'left');
  assert.equal(h.delivered.at(-1).meeting, originalMeeting);
});

test('synchronous invalidation on initial send is caught and retires every listener without a leave', () => {
  const h = harness({ kind: 'sync', message: 'Extension context invalidated.' });
  assertRetired(h);
  h.mutate();
  h.tick();
  h.pagehide();
  assertRetired(h);
  assert.deepEqual(h.attempts.map(message => message.state), ['joined']);
  assert.deepEqual(h.delivered, []);
});

test('invalidation cancels a pending mutation scan and already-queued callbacks remain inert', () => {
  const h = harness();
  h.mutate();
  const queuedCallbacks = [...h.timers.values()].map(timer => timer.callback);
  h.state.failure = { kind: 'sync', message: 'Extension context invalidated.' };
  h.tick();
  assertRetired(h);
  h.state.failure = null;
  for (const callback of queuedCallbacks) callback();
  h.mutate();
  h.pagehide();
  assert.deepEqual(h.delivered.map(message => message.state), ['joined']);
  assert.deepEqual(h.attempts.map(message => message.state), ['joined', 'joined']);
  assertRetired(h);
});

test('asynchronous invalidation tears down the pending scan, heartbeat, and pagehide listener', async () => {
  const h = harness({ kind: 'async', message: 'Extension context invalidated.' });
  h.mutate();
  await h.flush();
  assertRetired(h);
  h.state.failure = null;
  h.mutate();
  h.tick();
  h.pagehide();
  assert.deepEqual(h.attempts.map(message => message.state), ['joined']);
  assert.deepEqual(h.delivered, []);
});

test('observed pagehide invalidation is caught without adding any synthetic leave attempt', () => {
  const h = harness();
  h.state.failure = { kind: 'sync', message: 'Extension context invalidated.' };
  assert.doesNotThrow(() => h.pagehide());
  assertRetired(h);
  h.pagehide();
  h.tick();
  assert.deepEqual(h.attempts.map(message => message.state), ['joined', 'left']);
  assert.deepEqual(h.delivered.map(message => message.state), ['joined']);
});

for (const kind of ['sync', 'async']) {
  test(`${kind} transient messaging failure keeps scanning and retries fresh state`, async () => {
    const h = harness({ kind, message: 'Could not establish connection. Receiving end does not exist.' });
    await h.flush();
    assert.equal(h.observer.active, true);
    assert.equal(h.observer.disconnects, 0);
    assert.equal(h.listeners.has('pagehide'), true);
    assert.equal(h.timers.size, 1);
    h.state.failure = null;
    h.state.toolbar = false;
    h.tick();
    h.state.toolbar = true;
    h.tick();
    await h.flush();
    assert.deepEqual(h.delivered.map(message => message.state), ['unknown', 'joined']);
    assert.equal(new Set(h.attempts.map(message => message.meeting)).size, 1);
    assert.equal(h.attempts.some(message => message.state === 'left'), false);
  });
}

test('the shipped content and warning scripts warn on an unsupported route without emitting joined', async () => {
  const h = harness(null, 'https://meet.google.com/future/abc-defg-hij', true);
  await h.flush();
  for (let i = 0; i < 3; i++) { h.state.now += 5000; h.tick(); await h.flush(); }
  assert.ok(h.delivered.length > 0);
  assert.ok(h.delivered.every(message => message.type === 'quill-recording-health'));
  assert.equal(h.mountedWarnings.length, 1);
  assert.equal(h.warningNodes.find(node => node.tag === 'strong').textContent, 'Quill is not recording');
  h.warningNodes.find(node => node.tag === 'button').click();
  assert.equal(h.mountedWarnings[0].removed, true);
  for (let i = 0; i < 3; i++) { h.state.now += 5000; h.tick(); await h.flush(); }
  assert.equal(h.mountedWarnings.length, 1);
});

test('extension invalidation retires signal delivery but keeps a local reminder until the page leaves', async () => {
  const h = harness({kind: 'sync', message: 'Extension context invalidated.'}, 'https://meet.google.com/meet/abc-defg-hij', true);
  await h.flush();
  assert.equal(h.observer.active, false);
  assert.equal(h.timers.size, 1);
  const attempts = h.attempts.length;
  for (let i = 0; i < 3; i++) { h.state.now += 5000; h.tick(); await h.flush(); }
  assert.equal(h.attempts.length, attempts);
  assert.equal(h.mountedWarnings.length, 1);
  assert.equal(h.warningNodes.find(node => node.tag === 'strong').textContent, 'Quill cannot confirm recording');
  h.pagehide();
  assert.equal(h.timers.size, 0);
  assert.equal(h.listeners.has('pagehide'), false);
  assert.equal(h.mountedWarnings[0].removed, true);
});

test('confirmed capture removes the warning and stopped toolbar evidence does not create new capture signals', async () => {
  const h = harness(null, 'https://meet.google.com/future/abc-defg-hij', true);
  await h.flush();
  h.state.now = 15000; h.tick(); await h.flush();
  assert.equal(h.mountedWarnings.length, 1);
  h.state.healthState = 'recording';
  h.state.now += 5000; h.tick(); await h.flush();
  assert.equal(h.mountedWarnings[0].removed, true);
  h.state.toolbar = false;
  h.state.now += 20000; h.tick(); await h.flush();
  assert.ok(h.delivered.every(message => message.type === 'quill-recording-health'));
  assert.equal(h.mountedWarnings.length, 1);
});

test('an unrecognized new call never inherits the prior meeting suppression identity', async () => {
  const h = harness(null, 'https://meet.google.com/abc-defg-hij', true);
  await h.flush();
  const oldMeeting = h.delivered.find(message => message.type === 'quill-meeting').meeting;
  assert.equal(h.delivered.find(message => message.type === 'quill-recording-health').meeting, oldMeeting);
  h.location.href = 'https://example.invalid/';
  h.tick(); await h.flush();
  h.location.href = 'https://meet.google.com/future/klm-nopq-rst';
  h.state.now += 5000; h.tick(); await h.flush();
  const newHealth = h.delivered.filter(message => message.type === 'quill-recording-health').at(-1);
  assert.notEqual(newHealth.meeting, oldMeeting);
  assert.ok(!h.delivered.some(message => message.type === 'quill-meeting' && message.meeting === newHealth.meeting));
});
