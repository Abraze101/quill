const test = require('node:test');
const assert = require('node:assert/strict');
const { visibleParticipants } = require('./detector.js');
const { createCollector, read } = require('./speaker-activity.js');

function fixture() {
  let now = 1700000000000;
  const collector = createCollector({ clock: () => now });
  const observation = { source: 'google-meet-dom-v1', participants: [
    { key: 'private-platform-key', name: 'Casey Rivera', self: false },
    { key: 'private-self-key', name: 'Jamie Riley', self: true },
  ], active: ['private-platform-key'] };
  return { collector, observation, advance: ms => now += ms,
    sample: (session = 'meeting-A', next = observation) => collector.observe(session, next) };
}

test('collector records bounded timestamped activity with ephemeral participant IDs', () => {
  const h = fixture();
  assert.equal(h.sample().participants[0].id, 'p1');
  for (let i = 0; i < 50; i++) { h.advance(500); h.sample(); }
  h.advance(500);
  const result = h.sample();
  assert.equal(result.samples.length, 32);
  assert.equal(result.samples.at(-1).at_ms - result.samples[0].at_ms, 15500);
  assert.deepEqual(result.samples.at(-1).active, ['p1']);
  assert.deepEqual(result.participants[1], { id: 'p2', name: 'Jamie Riley', self: true });
  assert.ok(!JSON.stringify(result).includes('private-'));
});

test('silence and simultaneous speakers remain explicit rather than selecting a winner', () => {
  const h = fixture(); h.sample(); h.advance(500);
  h.observation.active = []; h.sample(); h.advance(500);
  h.observation.active = h.observation.participants.map(p => p.key);
  const result = h.sample();
  assert.deepEqual(result.samples.map(s => s.active), [['p1'], [], ['p1', 'p2']]);
});

test('suspension, reversed clocks, and meeting changes do not carry an old speaking interval', () => {
  const h = fixture(); h.sample(); h.advance(500); h.sample();
  h.advance(20000); assert.equal(h.sample().samples.length, 1);
  h.advance(-1000); assert.equal(h.sample().samples.length, 1);
  h.advance(500); assert.equal(h.sample('meeting-B').samples.length, 1);
  h.collector.reset(); h.advance(500); assert.equal(h.sample().samples.length, 1);
});

test('a renamed tile gets a fresh identity and cannot rewrite prior active samples', () => {
  const h = fixture(); h.sample(); h.advance(500);
  h.observation.participants[0].name = 'Alex Rivera';
  const result = h.sample();
  assert.equal(result.participants[0].id, 'p3');
  assert.equal(result.samples.length, 1);
  assert.deepEqual(result.samples[0].active, ['p3']);
});

test('unsupported providers and malformed observations clear continuity', () => {
  const h = fixture(); h.sample(); h.advance(500);
  assert.equal(read({}, 'zoom-web', () => true), null);
  const invalid = [null, {}, { ...h.observation, source: 'unverified' },
    { ...h.observation, active: ['missing'] }, { ...h.observation, active: ['private-platform-key', 'private-platform-key'] },
    { ...h.observation, participants: Array.from({ length: 21 }, (_, i) => ({ key: '' + i, name: 'Casey', self: false })) },
    { ...h.observation, participants: [{ key: 'x', name: 'private@example.test', self: false }] },
    { ...h.observation, participants: [{ key: 'x', name: 'Casey', self: 'false' }] }];
  for (const next of invalid) {
    assert.equal(h.sample('meeting-A', next), null);
    h.advance(500); assert.equal(h.sample().samples.length, 1); h.advance(500);
  }
});

test('returned snapshots cannot mutate subsequent history', () => {
  const h = fixture(); const first = h.sample();
  first.samples[0].active.push('spoofed'); first.participants[0].name = 'Spoofed';
  h.advance(500); const second = h.sample();
  assert.equal(second.participants[0].name, 'Casey Rivera');
  assert.deepEqual(second.samples[0].active, ['p1']);
});

const TILE = '[data-participant-id][data-tile-media-id]';
const ROW = "[role='listitem'][data-participant-id]";
function domFixture() {
  const tiles = [], rows = [];
  const document = { querySelectorAll: selector => selector === TILE ? tiles : selector === ROW ? rows : [] };
  const visible = element => element.visible !== false;
  function person({ key = 'private-key', display = 'Casey Rivera', self = false, marked = false, state = 'idle', row = false } = {}) {
    const owner = row ? ROW : TILE;
    const container = { visible: true, excluded: false, key, names: [], labels: [], indicators: [],
      getAttribute: attribute => attribute === 'data-participant-id' ? container.key : null,
      closest: () => container.excluded ? {} : null,
      querySelectorAll(selector) {
        if (selector === '.notranslate' || selector === '.zWGUib') return container.names;
        if (selector === '.NnTWjc') return container.labels;
        if (selector === "[jsname='QgSmzd']") return container.indicators;
        return [];
      } };
    function leaf(text) { return { tagName: 'SPAN', childElementCount: 0, textContent: text, visible: true,
      closest(selector) {
        if (selector === owner || selector === '[data-participant-id]') return container;
        if (selector === "[data-is-self='true']" && marked) return { closest: () => container };
        return null;
      } }; }
    container.names = [leaf(display)];
    if (self) container.labels = [leaf('(You)')];
    const indicator = { visible: true, classes: new Set(state === 'active' ? ['HX2H7'] : state === 'idle' ? ['gjg47c'] : []),
      closest: selector => selector === owner ? container : null };
    indicator.classList = { contains: value => indicator.classes.has(value) };
    container.indicators = [indicator];
    (row ? rows : tiles).push(container);
    return container;
  }
  return { document, visible, tiles, rows, person, read: session => read(document, 'google-meet', visible, session) };
}

test('observed Meet indicator distinguishes muted/silent from speaking without reading caption or chat text', () => {
  const h = domFixture(); const tile = h.person();
  assert.deepEqual(h.read('call').active, []);
  tile.indicators[0].classes = new Set(['DYfzY', 'cYKTje', 'HX2H7', 'sxlEM']);
  assert.deepEqual(h.read('call').active, ['private-key']);
  tile.indicators[0].classes = new Set(['IisKdb', 'GF8M7d', 'gjg47c', 'KUNJSe']);
  assert.deepEqual(h.read('call').active, []);
  tile.indicators[0].visible = false;
  assert.equal(h.read('call'), null);
});

test('explicit People self badge is retained for matching tile in this meeting but never from host role', () => {
  const h = domFixture(); h.person({ display: 'Jamie Riley' });
  h.person({ display: 'Jamie Riley', self: true, row: true });
  assert.equal(h.read('call-A').participants[0].self, true);
  h.rows.splice(0);
  assert.equal(h.read('call-A').participants[0].self, true);
  assert.equal(h.read('call-B').participants[0].self, false);
});

test('visible Meet roster captures the explicit People badge even without a speaking indicator', () => {
  const h = domFixture();
  const local = h.person({ key: 'local', display: 'Morgan Kim', row: true, self: true });
  const remote = h.person({ key: 'other', display: 'Casey Rivera', row: true, self: true });
  remote.labels[0].textContent = '(Host)';
  local.indicators = []; remote.indicators = [];
  assert.deepEqual(visibleParticipants(h.document, 'google-meet', h.visible), [
    { name: 'Morgan Kim', self: true }, { name: 'Casey Rivera', self: false },
  ]);
  assert.equal(h.read('call'), null);
});

test('both readers bind a tile ancestor self attribute only inside its own participant', () => {
  const h = domFixture();
  h.person({ key: 'local', display: 'Morgan Kim', marked: true });
  const other = h.person({ key: 'remote', display: 'Casey Rivera' });
  const closest = other.names[0].closest;
  other.names[0].closest = selector => selector === "[data-is-self='true']"
    ? { closest: () => h.tiles[0] } : closest(selector);
  assert.deepEqual(visibleParticipants(h.document, 'google-meet', h.visible), [
    { name: 'Morgan Kim', self: true }, { name: 'Casey Rivera', self: false },
  ]);
  assert.deepEqual(h.read('call').participants.map(p => [p.key, p.self]), [['local', true], ['remote', false]]);
});

test('self badges on hidden leaves and nested participants cannot label the enclosing People row', () => {
  const h = domFixture();
  const row = h.person({ row: true, self: true });
  const badge = row.labels[0];
  badge.visible = false;
  assert.equal(visibleParticipants(h.document, 'google-meet', h.visible)[0].self, false);
  badge.visible = true;
  badge.closest = selector => selector === '[data-participant-id]' ? {} : null;
  assert.equal(visibleParticipants(h.document, 'google-meet', h.visible)[0].self, false);
  assert.equal(h.read('call').participants[0].self, false);
});

test('conflicting self IDs or conflicting names on one ID never produce a local identity', () => {
  const h = domFixture();
  h.person({ key: 'one', display: 'First Person', row: true, self: true });
  h.person({ key: 'two', display: 'Second Person', marked: true });
  assert.deepEqual(visibleParticipants(h.document, 'google-meet', h.visible), []);
  assert.equal(h.read('call'), null);
  h.tiles[0].key = 'one';
  assert.deepEqual(visibleParticipants(h.document, 'google-meet', h.visible), []);
  assert.equal(h.read('call'), null);
  h.rows.length = 0; h.tiles.length = 0;
  h.person({ key: 'one', display: 'First Person' });
  assert.equal(h.read('call').participants[0].self, false);
});

test('tile and People row deduplicate active identities and reject conflicting names or states', () => {
  const h = domFixture(); h.person({ state: 'active' });
  const row = h.person({ row: true, state: 'active' });
  assert.equal(h.read('call').participants.length, 1);
  assert.deepEqual(h.read('call').active, ['private-key']);
  row.indicators[0].classes = new Set(['gjg47c']);
  assert.equal(h.read('call'), null);
  row.indicators[0].classes = new Set(['HX2H7']);
  row.names[0].textContent = 'Different Person';
  assert.equal(h.read('call'), null);
});

test('an unrecognized speaking state, multiple names, or excluded container supplies no attribution', () => {
  const h = domFixture(); const tile = h.person({ state: 'unknown' });
  assert.equal(h.read('call'), null);
  tile.indicators[0].classes = new Set(['HX2H7', 'gjg47c']);
  assert.equal(h.read('call'), null);
  tile.indicators[0].classes = new Set(['HX2H7']);
  tile.names.push({ ...tile.names[0], textContent: 'Second Name' });
  assert.equal(h.read('call'), null);
  tile.names.pop(); tile.excluded = true;
  assert.equal(h.read('call'), null);
});
