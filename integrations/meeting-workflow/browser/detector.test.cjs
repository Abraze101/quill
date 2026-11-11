const test = require('node:test');
const assert = require('node:assert/strict');
const { classify, controlLabel, warningEvidence, visibleParticipants, participantName } = require('./detector.js');
const control = (label, visible = true) => ({label, visible, enabled: true});
const meet = { url: 'https://meet.google.com/abc-defg-hij', controls: [control('Leave call'), control('People'), control('Turn off microphone (⌘ + d)')] };

const TILE = '[data-participant-id][data-tile-media-id]';
const ROW = "[role='listitem'][data-participant-id]";
const OWNER = '[data-participant-id]';
const LEGACY = '[data-participant-id] [data-self-name],[data-participant-id][data-self-name]';
function meetDocument(legacy = [], tiles = []) {
  return { querySelectorAll: selector => selector === LEGACY ? legacy : selector === TILE ? tiles : [] };
}

function participant(value, { self = false, visible = true, excluded = false, children = 0 } = {}) {
  const owner = { getAttribute: key => key === 'data-participant-id' ? 'id:' + value : null };
  return { visible, childElementCount: children,
    getAttribute(name) { return name === 'data-self-name' ? value : name === 'data-is-self' && self ? 'true' : null; },
    closest(selector) {
      if (selector === OWNER) return owner;
      if (selector === "[data-is-self='true']") return self ? this : null;
      return excluded ? {} : null;
    },
    get textContent() { if (excluded) throw new Error('Must not read excluded text'); return value; } };
}

test('visible Meet names are bounded and normalized without reading chat or captions', () => {
  const rows = [participant('  Casey  Rivera  '), participant('Casey Rivera'),
    participant('Owner Name (You)'), participant('Hidden', { visible: false }),
    participant('Private chat', { excluded: true }), participant('person@example.test'),
    participant('https://example.test'), participant('You'), participant('A'.repeat(101))];
  const result = visibleParticipants(meetDocument(rows), 'google-meet', item => item.visible);
  assert.deepEqual(result, [{ name: 'Casey Rivera', self: false }, { name: 'Owner Name', self: true }]);
});

function mediaTile(entries) {
  const tile = { visible: true, closest: () => null, getAttribute: () => 'tile-key', querySelectorAll(selector) {
    assert.equal(selector, '.notranslate');
    return nodes;
  }, get textContent() { throw new Error('Must not read aggregate tile text'); } };
  const nodes = entries.map(({ text = '', tag = 'SPAN', visible = true, excluded = false,
    children = 0, inTile = true, forbidText = false } = {}) => ({
    tagName: tag, visible, childElementCount: children,
    getAttribute() { return null; },
    matches(selector) { return selector === '.notranslate'; },
    closest(selector) {
      return selector === OWNER ? (inTile ? tile : null) : excluded ? {} : null;
    },
    get textContent() {
      if (forbidText) throw new Error('Must not read excluded or non-leaf text');
      return text;
    },
  }));
  return { tile, nodes };
}

test('Meet media tiles read the visible name SPAN without icons or container text', () => {
  const { tile } = mediaTile([
    ...['frame_person', 'visual_effects', 'more_vert', 'devices'].map(text => (
      { text, tag: 'I', visible: false, forbidText: true })),
    { text: 'Jamie Rivera' },
    ...Array.from({ length: 4 }, () => ({ visible: false, forbidText: true })),
    { text: 'mute', tag: 'I', forbidText: true },
    { children: 1, forbidText: true },
    { text: 'Private chat', excluded: true, forbidText: true },
  ]);
  const result = visibleParticipants(meetDocument([], [tile]), 'google-meet', item => item.visible);
  assert.deepEqual(result, [{ name: 'Jamie Rivera', self: false }]);
});

test('Meet media tiles omit ambiguous names and names outside the tile', () => {
  const ambiguous = mediaTile([{ text: 'Jamie Rivera' }, { text: 'Casey Lane' }]).tile;
  const unscoped = mediaTile([{ text: 'Unscoped person', inTile: false }]).tile;
  const result = visibleParticipants(meetDocument([], [ambiguous, unscoped]),
    'google-meet', item => item.visible);
  assert.deepEqual(result, []);
});

test('Meet media tiles ignore blank leaves and deduplicate legacy name evidence', () => {
  const { tile } = mediaTile([{ text: 'Jamie Rivera' }, { text: '  ' }]);
  const result = visibleParticipants(meetDocument([participant('Jamie Rivera')], [tile]),
    'google-meet', item => item.visible);
  assert.deepEqual(result, [{ name: 'Jamie Rivera', self: false }]);
});

test('Zoom reads only dedicated tile names and omits expanded containers and unscoped roster rows', () => {
  const rows = [participant('Casey Rivera'), participant('Owner Name (You)'), participant('Container', { children: 3 })];
  let selector;
  const result = visibleParticipants({ querySelectorAll(value) { selector = value; return rows; } }, 'zoom-web', item => item.visible);
  assert.equal(selector, '.video-avatar__avatar-name');
  assert.deepEqual(result, [{ name: 'Casey Rivera', self: false }, { name: 'Owner Name', self: true }]);
});

test('visible participant extraction stops at twenty and rejects unsupported providers', () => {
  const document = meetDocument(Array.from({ length: 100 }, (_, index) => participant('Person ' + index)));
  assert.equal(visibleParticipants(document, 'google-meet', () => true).length, 20);
  assert.deepEqual(visibleParticipants(document, 'discord', () => true), []);
  for (const value of [null, '', 'A\u202eB', '\u0000Name', 'x'.repeat(101), 'guest', 'me']) assert.equal(participantName(value), null);
  assert.equal(participantName('Jose\u0301'), 'José');
});
test('requires actual in-call controls, including while muted', () => {
  assert.equal(classify(meet).state, 'joined');
  assert.equal(classify({...meet, controls: [control('Leave call'), control('People'), control('Turn on microphone')]}).state, 'joined');
});
test('app home, calendar links, prejoin and waiting rooms cannot start', () => {
  assert.equal(classify({...meet, url: 'https://meet.google.com/'}).state, 'outside');
  assert.equal(classify({...meet, controls: [control('Join now'), control('Turn off microphone')]}).state, 'unknown');
  assert.equal(classify({...meet, notices: [{text: 'Waiting for the host to let you in', visible: true}]}).state, 'idle');
});
test('hidden leave controls and reconnecting are unknown, not left', () => {
  assert.equal(classify({...meet, controls: [control('Leave call', false), control('People'), control('Mute')]}).state, 'unknown');
});
test('recognizes explicit left/end screens', () => {
  assert.equal(classify({...meet, controls: [], notices: [{text: 'You left the meeting', visible: true}]}).state, 'left');
  assert.equal(classify({...meet, notices: [{text: 'You left the meeting', visible: false}]}).state, 'joined');
});
for (const pathname of ['/abc-defg-hij', '/meet/abc-defg-hij']) {
  test(`Meet ${pathname} preserves join, lobby, leave, and uncertain state evidence`, () => {
    for (const suffix of ['', '/', '?authuser=1', '/?authuser=1']) {
      const snapshot = {...meet, url: `https://meet.google.com${pathname}${suffix}`};
      assert.equal(classify(snapshot).state, 'joined');
      assert.equal(classify({...snapshot, controls: [], notices: [
        {text: 'Ready to join?', visible: true},
      ]}).state, 'idle');
      assert.equal(classify({...snapshot, controls: [control('Join now'), control('Turn on microphone')]}).state, 'unknown');
      assert.equal(classify({...snapshot, notices: [
        {text: 'Waiting for the host to let you in', visible: true},
      ]}).state, 'idle');
      assert.equal(classify({...snapshot, controls: [], notices: [
        {text: 'You left the meeting', visible: true},
      ]}).state, 'left');
      assert.equal(classify({...snapshot, controls: [control('Leave call', false), control('People'), control('Mute')]}).state, 'unknown');
      assert.equal(classify({...snapshot, controls: []}).state, 'unknown');
    }
  });
}
test('Meet route recognition rejects malformed, deeper, and untrusted URLs even with matching controls', () => {
  const paths = [
    '/meet', '/meet/', '/meet/abc-defg-hi', '/meet/abc-defg-hijk', '/meet/ABC-defg-hij',
    '/meet/abc-defg-hij/details', '/meet/meet/abc-defg-hij', '/abc-defg-hij/details',
    '/other/abc-defg-hij', '/meeting/abc-defg-hij', '/meet//abc-defg-hij',
  ];
  for (const pathname of paths) {
    assert.equal(classify({...meet, url: `https://meet.google.com${pathname}`}).state, 'outside', pathname);
  }
  for (const pathname of ['/abc-defg-hij', '/meet/abc-defg-hij']) {
    for (const origin of ['http://meet.google.com', 'https://meet.google.com.attacker.test', 'https://attacker.test']) {
      assert.equal(classify({...meet, url: `${origin}${pathname}`}).state, 'outside', origin + pathname);
    }
  }
});
test('Zoom requires its meeting toolbar and excludes the lobby', () => {
  const zoom = {url: 'https://us02web.zoom.us/wc/123/start', controls: [control('Leave'), control('Participants'), control('Unmute')]};
  assert.equal(classify(zoom).state, 'joined');
  assert.equal(classify({...zoom, notices: [{text: 'Please wait, the host will let you in soon', visible: true}]}).state, 'idle');
});
test('observed Zoom embedded client toolbar confirms a joined call without device access', () => {
  const zoom = {url: 'https://app.zoom.us/wc/12345678901/join', controls: [
    control('Leave'), control('open the participants list pane,[1] particpants'), control('audio')
  ]};
  assert.equal(classify(zoom).state, 'joined');
  assert.equal(classify({...zoom, controls: zoom.controls.slice(1)}).state, 'unknown');
  assert.equal(classify({...zoom, controls: [control('Leave'), control('open the chat panel'), control('audio')]}).state, 'unknown');
  assert.equal(classify({...zoom, controls: [control('Leave'), control('open the participants list pane,[1] particpants', false), control('audio')]}).state, 'unknown');
  assert.equal(classify({...zoom, url: meet.url}).state, 'unknown');
});
test('Zoom frame injection stays limited to web-client pages while Meet remains top-level', () => {
  const manifest = require('./manifest.json');
  const meetScript = manifest.content_scripts.find(x => x.matches.includes('https://meet.google.com/*'));
  const zoomScript = manifest.content_scripts.find(x => x.matches.includes('https://*.zoom.us/wc/*'));
  assert.equal(meetScript.all_frames, false);
  assert.deepEqual(zoomScript.matches, ['https://*.zoom.us/wc/*']);
  assert.equal(zoomScript.all_frames, true);
  assert.deepEqual(zoomScript.js, ['detector.js', 'recording-warning.js', 'content.js']);
});
test('warning evidence survives unknown meeting routes without authorizing automatic capture', () => {
  for (const url of ['https://meet.google.com/future/abc-defg-hij', 'https://app.zoom.us/future/123']) {
    const snapshot = {...meet, url, controls: [control('Leave meeting'), control('Participants'), control('Mute')]};
    assert.equal(classify(snapshot).state, 'outside');
    assert.equal(warningEvidence(snapshot).state, 'joined');
    assert.equal(warningEvidence({...snapshot, controls: []}).state, 'unknown');
    assert.equal(warningEvidence({...snapshot, notices: [{text: 'Ready to join?', visible: true}]}).state, 'idle');
    assert.equal(warningEvidence({...snapshot, notices: [{text: 'You left the meeting', visible: true}]}).state, 'left');
  }
  assert.equal(warningEvidence({...meet, url: 'https://meet.google.com.attacker.test/abc-defg-hij'}).state, 'outside');
  assert.equal(warningEvidence({...meet, url: 'http://meet.google.com/abc-defg-hij'}).state, 'outside');
});
test('lookalike hosts and unsupported locales fail closed', () => {
  assert.equal(classify({...meet, url: 'https://meet.google.com.attacker.test/abc-defg-hij'}).state, 'outside');
  assert.equal(classify({...meet, controls: [control('Quitter'), control('Participants'), control('Microphone')]}).state, 'unknown');
});

// A minimal DOM-shaped fixture supplies the APIs used by label extraction.
// Reading aggregate textContent throws so a regression cannot scrape a tree.
function labelDOM() {
  const ids = new Map();
  const document = { getElementById: id => ids.get(id) ?? null };
  function element(tag, attributes = {}, ...children) {
    const node = {
      nodeType: 1, tagName: tag.toUpperCase(), ownerDocument: document,
      parentElement: null, childNodes: [],
      getAttribute: name => attributes[name] ?? null,
      closest(selector) {
        const terms = selector.split(',').map(term => /^\[([^=\]]+)(?:='([^']+)')?\]$/.exec(term));
        for (let current = this; current; current = current.parentElement) {
          if (terms.some(term => term && current.getAttribute(term[1]) !== null &&
              (term[2] === undefined || current.getAttribute(term[1]) === term[2]))) return current;
        }
        return null;
      },
      get textContent() { throw new Error('Unbounded textContent read'); },
    };
    for (const child of children) {
      const value = typeof child === 'string' ? { nodeType: 3, nodeValue: child } : child;
      value.parentElement = node;
      node.childNodes.push(value);
    }
    if (attributes.id) ids.set(attributes.id, node);
    return node;
  }
  return element;
}

test('observed Meet People button with only aria-labelledby confirms a muted joined call', () => {
  const element = labelDOM();
  element('span', { id: 'DPUxh-nav9Xe' }, 'People');
  const people = element('button', { 'aria-labelledby': 'DPUxh-nav9Xe' }, element('span', {}, '1'));
  assert.equal(people.getAttribute('aria-label'), null);
  assert.equal(people.getAttribute('title'), null);
  assert.equal(people.getAttribute('data-tooltip'), null);
  const buttons = [element('button', { 'aria-label': 'Leave call' }), people,
    element('button', { 'aria-label': 'Turn on microphone' })];
  assert.equal(controlLabel(people), 'People');
  assert.equal(classify({ url: meet.url, controls: buttons.map(button => control(controlLabel(button))) }).state, 'joined');
});

test('labelledby takes precedence and combines multiple references in order while ignoring dangling references', () => {
  const element = labelDOM();
  element('span', { id: 'people-label' }, 'People');
  element('span', { id: 'people-count' }, '1');
  const button = element('button', { 'aria-labelledby': 'missing people-label people-count', 'aria-label': 'Obsolete label', title: 'Tooltip' });
  assert.equal(controlLabel(button), 'People 1');
});

test('all dangling references use the existing bounded aria-label, tooltip, then title fallback', () => {
  const element = labelDOM();
  assert.equal(controlLabel(element('button', { 'aria-labelledby': 'missing', 'aria-label': 'People', 'data-tooltip': 'Unused' })), 'People');
  assert.equal(controlLabel(element('button', { 'aria-labelledby': 'missing', 'data-tooltip': 'Participants', title: 'Unused' })), 'Participants');
  assert.equal(controlLabel(element('button', { 'aria-labelledby': 'missing', title: 'People' })), 'People');
  assert.equal(controlLabel(element('button', { 'aria-labelledby': 'missing' }, 'Arbitrary button content')), '');
});

test('oversized, wide, deep, and excessive referenced labels fail closed without partial evidence', () => {
  const element = labelDOM();
  element('span', { id: 'large' }, 'People ' + 'x'.repeat(160));
  element('div', { id: 'wide' }, ...Array.from({ length: 9 }, () => element('span', {}, 'People')));
  element('div', { id: 'deep' }, element('div', {}, element('div', {}, element('span', {}, 'People'))));
  for (const id of ['large', 'wide', 'deep']) {
    assert.equal(controlLabel(element('button', { 'aria-labelledby': id, 'aria-label': 'People' })), '');
  }
  assert.equal(controlLabel(element('button', { 'aria-labelledby': 'one two three four five', 'aria-label': 'People' })), '');
  assert.equal(controlLabel(element('button', { 'aria-label': 'People ' + 'x'.repeat(160) })), '');
});

test('referenced chat/log labels and controls within excluded containers are never meeting evidence', () => {
  for (const attributes of [{ role: 'log' }, { role: 'complementary' }, { 'data-message-id': 'message-1' }]) {
    const element = labelDOM();
    const reference = element('span', { id: 'chat-text' }, 'People');
    const button = element('button', { 'aria-label': 'Leave call' });
    element('div', attributes, reference, button);
    assert.equal(controlLabel(element('button', { 'aria-labelledby': 'chat-text', title: 'People' })), '');
    assert.equal(controlLabel(button), '');
  }
});

test('genuine label references can live inside a call-feature notification region', () => {
  const element = labelDOM();
  const reference = element('span', { id: 'people' }, 'People');
  const button = element('button', { 'aria-labelledby': 'people' });
  element('section', { role: 'region', 'aria-label': 'Call feature notifications and actions', 'aria-live': 'polite' }, reference, button);
  assert.equal(controlLabel(button), 'People');
});
