const test = require('node:test');
const assert = require('node:assert/strict');
const { createMonitor, createBanner } = require('./recording-warning.js');
const joined = { state: 'joined', provider: 'google-meet' };
const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };

function harness() {
  let now = 0;
  let sequence = 0;
  const requests = [];
  const rendered = [];
  const monitor = createMonitor({ now: () => now, randomID: () => `warning-${++sequence}`,
    requestStatus: details => new Promise((resolve, reject) => requests.push({ details, resolve, reject })),
    render: value => rendered.push(value),
  });
  return { monitor, requests, rendered,
    async step(milliseconds = 0, evidence = joined, meeting = 'call-A') {
      now += milliseconds;
      monitor.observe(evidence, meeting);
      await flush();
    },
    async answer(state, index = requests.length - 1) {
      requests[index].resolve({ recordingStatus: { state } });
      await flush();
    },
    async idle(milliseconds) { now += milliseconds; monitor.refresh(); await flush(); },
  };
}

test('the reminder waits 15 seconds then distinguishes known idle from missing confirmation', async () => {
  const h = harness();
  await h.step(); await h.answer('not-recording');
  await h.step(10000); await h.answer('not-recording');
  assert.deepEqual(h.rendered, []);
  await h.step(5000); await h.answer('not-recording');
  assert.equal(h.rendered.at(-1), 'not-recording');
  await h.step(5000); await h.answer('unavailable');
  assert.equal(h.rendered.at(-1), 'unavailable');
  await h.step(5000); await h.answer('recording');
  assert.equal(h.rendered.at(-1), null);
});

test('missing, malformed, and rejected health replies never imply a confirmed recording', async () => {
  for (const outcome of ['never', 'malformed', 'rejected']) {
    const h = harness();
    await h.step();
    await h.step(15000);
    if (outcome === 'malformed') await h.answer('anything-else');
    if (outcome === 'rejected') { h.requests.at(-1).reject(new Error('unavailable')); await flush(); }
    assert.equal(h.rendered.at(-1), 'unavailable', outcome);
  }
});

test('starting grace is bounded even when the app keeps reporting starting', async () => {
  const h = harness();
  await h.step(); await h.answer('starting');
  for (let i = 0; i < 5; i++) { await h.step(5000); await h.answer('starting'); }
  await h.step(4000);
  assert.deepEqual(h.rendered, []);
  await h.step(1000);
  assert.equal(h.rendered.at(-1), 'unavailable');
});

test('pause and explicit skip are respected; a stale recording confirmation cannot hide the reminder', async () => {
  for (const state of ['recording', 'paused', 'skipped']) {
    const h = harness();
    await h.step(); await h.answer(state);
    await h.step(15000); await h.answer(state);
    assert.equal(h.rendered.at(-1) ?? null, null, state);
    await h.step(9000);
    assert.equal(h.rendered.at(-1), state === 'skipped' ? undefined : 'unavailable', state);
  }
});

test('a new start or resume receives fresh bounded grace after a confirmed recording or pause', async () => {
  const h = harness();
  await h.step(); await h.answer('starting');
  await h.step(5000); await h.answer('recording');
  await h.step(5000); await h.answer('paused');
  for (let i = 0; i < 5; i++) { await h.step(5000); await h.answer('paused'); }
  await h.step(5000); await h.answer('starting');
  for (let i = 0; i < 5; i++) { await h.step(5000); await h.answer('starting'); }
  assert.deepEqual(h.rendered, []);
  await h.step(5000); await h.answer('starting');
  assert.equal(h.rendered.at(-1), 'unavailable');
});

test('late and out-of-order confirmations cannot silence a newer failure or another call', async () => {
  const h = harness();
  await h.step();
  await h.step(15000);
  await h.answer('not-recording', 1);
  await h.answer('recording', 0);
  assert.equal(h.rendered.at(-1), 'not-recording');
  await h.step(5000);
  await h.step(0, { state: 'left' });
  await h.step(0, joined, 'call-B');
  await h.answer('recording', 2);
  await h.step(15000, joined, 'call-B');
  assert.equal(h.rendered.at(-1), 'unavailable');
});

test('dismissal lasts for this call only and does not send a recording or skip command', async () => {
  const h = harness();
  await h.step(0, joined, null);
  assert.equal(h.requests[0].details.meeting, 'warning-1');
  await h.step(15000, joined, null);
  h.monitor.dismiss();
  await h.step(15000, joined, null);
  assert.equal(h.rendered.at(-1), null);
  await h.step(0, { state: 'idle' });
  await h.step(0, joined, null);
  await h.step(15000, joined, null);
  assert.equal(h.rendered.at(-1), 'unavailable');
  assert.equal(h.requests.at(-1).details.meeting, 'warning-2');
  assert.ok(h.requests.every(request => Object.keys(request.details).sort().join() === 'meeting,provider'));
});

test('a changed strict session cannot inherit dismissal, skip, or an old in-flight acknowledgment', async () => {
  for (const prior of ['skipped', 'dismissed', 'pending']) {
    const h = harness();
    await h.step(0, joined, 'call-A');
    if (prior === 'skipped') await h.answer('skipped');
    if (prior === 'dismissed') h.monitor.dismiss();
    await h.step(1000, joined, 'call-B');
    assert.equal(h.requests.at(-1).details.meeting, 'call-B');
    if (prior === 'pending') await h.answer('skipped', 0);
    await h.answer('not-recording');
    await h.step(15000, joined, 'call-B'); await h.answer('not-recording');
    assert.equal(h.rendered.at(-1), 'not-recording', prior);
  }
});

test('the first strict identity invalidates a pending fallback-only probe without delaying its own check', async () => {
  const h = harness();
  await h.step(0, joined, null);
  await h.step(1000, joined, 'strict-call');
  assert.equal(h.requests.length, 2);
  assert.equal(h.requests[1].details.meeting, 'strict-call');
  await h.answer('not-recording', 1);
  await h.answer('skipped', 0);
  await h.step(14000, joined, 'strict-call'); await h.answer('not-recording');
  assert.equal(h.rendered.at(-1), 'not-recording');
});

test('host failure or extension invalidation can warn locally; no stale call evidence leaves a banner behind', async () => {
  const h = harness();
  await h.step();
  h.monitor.connectionLost();
  await h.step(15000);
  assert.equal(h.rendered.at(-1), 'unavailable');
  assert.equal(h.requests.length, 1);
  await h.idle(16000);
  assert.equal(h.rendered.at(-1), null);
  await h.step(0, { state: 'left' });
  await h.answer('recording', 0);
  assert.equal(h.rendered.at(-1), null);
});

test('health requests are rate limited and use a strict session ID once one becomes available', async () => {
  const h = harness();
  await h.step(0, joined, null);
  for (let i = 0; i < 20; i++) await h.step(100, joined, null);
  assert.equal(h.requests.length, 1);
  await h.step(3000, joined, 'strict-session');
  assert.equal(h.requests.length, 2);
  assert.equal(h.requests[1].details.meeting, 'strict-session');
});

test('banner has an accessible dismiss button, isolates its controls, and avoids repeated DOM changes', () => {
  let dismissed = 0;
  let appends = 0;
  const nodes = [];
  const createElement = tag => {
    const node = { tag, attrs: {}, children: [], style: {},
      setAttribute(name, value) { this.attrs[name] = value; },
      append(...values) { this.children.push(...values); },
      attachShadow(options) { this.shadowOptions = options; return this.shadow = createElement('shadow'); },
      addEventListener(name, callback) { this[name] = callback; },
      remove() { this.removed = true; },
    };
    nodes.push(node); return node;
  };
  const render = createBanner({ createElement, documentElement: { append() { appends++; } } }, () => dismissed++);
  render('not-recording');
  render('not-recording');
  render('unavailable');
  assert.equal(appends, 1);
  assert.equal(nodes.find(node => node.tag === 'div').shadowOptions.mode, 'closed');
  assert.equal(nodes.find(node => node.tag === 'section').attrs.role, 'alert');
  assert.equal(nodes.find(node => node.tag === 'strong').textContent, 'Quill cannot confirm recording');
  nodes.find(node => node.tag === 'button').click();
  assert.equal(dismissed, 1);
  render(null);
  assert.equal(nodes.find(node => node.tag === 'div').removed, true);
});
