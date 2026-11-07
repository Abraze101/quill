/* Bounded meeting-interface observations. No media, chat, captions or profiles. */
(function (root) {
  "use strict";
  const MAX_PEOPLE = 20;
  const MAX_SAMPLES = 32;
  const MAX_AGE_MS = 20000;
  const name = value => {
    if (typeof value !== "string" || !value || [...value].length > 100
        || /\p{C}|@|:\/\//u.test(value)) return null;
    const normalized = value.normalize("NFC").replace(/\s+/g, " ").trim();
    return normalized === value && !/^(you|me|host|participant|participants|unknown|guest)$/i.test(value)
      ? value : null;
  };

  function createCollector({ clock = () => Date.now() } = {}) {
    let session = null;
    let people = new Map();
    let samples = [];
    let sequence = 0;
    function reset() { session = null; people = new Map(); samples = []; sequence = 0; }
    function observe(meeting, observation) {
      if (meeting !== session) { reset(); session = meeting; }
      const at = clock();
      if (typeof meeting !== "string" || !meeting || !Number.isSafeInteger(at) || at <= 0) {
        reset(); return null;
      }
      // A reversed clock or suspended page breaks continuity. Never turn the
      // most recently observed speaker into a long interval through a gap.
      if (samples.length && (at <= samples.at(-1).at_ms || at - samples.at(-1).at_ms > 1500)) samples = [];
      if (!observation || observation.source !== "google-meet-dom-v1"
          || !Array.isArray(observation.participants) || observation.participants.length > MAX_PEOPLE
          || !Array.isArray(observation.active) || observation.active.length > 4) {
        samples = []; return null;
      }
      const current = new Map();
      for (const person of observation.participants) {
        if (!person || typeof person.key !== "string" || !person.key || person.key.length > 512
            || !name(person.name) || typeof person.self !== "boolean" || current.has(person.key)) {
          samples = []; return null;
        }
        let known = people.get(person.key);
        // A renamed/reused tile cannot rewrite older observations.
        if (!known || known.name !== person.name || known.self !== person.self) {
          if (people.size >= MAX_PEOPLE && !known) { samples = []; return null; }
          known = { id: "p" + (++sequence), name: person.name, self: person.self };
          people.set(person.key, known);
        }
        current.set(person.key, known);
      }
      if (observation.active.some(key => !current.has(key))
          || new Set(observation.active).size !== observation.active.length) { samples = []; return null; }
      const active = observation.active.map(key => current.get(key).id).sort();
      samples.push({ at_ms: at, active });
      samples = samples.filter(sample => at - sample.at_ms <= MAX_AGE_MS).slice(-MAX_SAMPLES);
      // Keep names only while their current ID is referenced or currently on
      // screen. Changed identities invalidate old samples instead of relabeling.
      const roster = [...people.values()];
      const ids = new Set(roster.map(person => person.id));
      samples = samples.filter(sample => sample.active.every(id => ids.has(id)));
      return { schema_version: 1, source: observation.source,
        participants: roster.map(person => ({ ...person })),
        samples: samples.map(sample => ({ at_ms: sample.at_ms, active: [...sample.active] })) };
    }
    return { observe, reset };
  }

  const readers = new WeakMap();
  // Verified against a muted, silent, and speaking Meet call. These are
  // interface observations, not a biometric identity or an account lookup.
  function read(document, provider, visible, session = null) {
    if (provider !== "google-meet") return null;
    const tileSelector = "[data-participant-id][data-tile-media-id]";
    const rowSelector = "[role='listitem'][data-participant-id]";
    const excluded = "[role='log'],[data-message-id],[data-caption-id],[aria-hidden='true'],[hidden],.chat-container,.chat-message,.chat-box";
    let reader = readers.get(document);
    if (!reader || reader.session !== session) {
      reader = { session, self: new Map() };
      readers.set(document, reader);
    }
    const people = new Map();
    function leafNames(container, selector, owner) {
      const nodes = [...container.querySelectorAll(selector)];
      if (nodes.length > 100) return [];
      return nodes.filter(node => node.tagName === "SPAN" && node.childElementCount === 0
        && visible(node) && !node.closest(excluded) && node.closest(owner) === container)
        .map(node => node.textContent).filter(value => name(value));
    }
    function collect(container, owner, names, self = false) {
      const key = container.getAttribute("data-participant-id");
      if (!key || key.length > 512 || !visible(container) || container.closest(excluded) || names.length !== 1) return;
      const display = names[0];
      if (reader.self.has(key) && reader.self.get(key) !== display) reader.self.delete(key);
      if (self && reader.self.size < MAX_PEOPLE) reader.self.set(key, display);
      if (!people.has(key)) people.set(key, { key, name: display, self: false, states: new Set() });
      const person = people.get(key);
      // A stale row or a renamed tile must not associate two names with one
      // activity signal. Drop this scan rather than selecting one arbitrarily.
      if (person.name !== display) { person.conflict = true; return; }
      person.self ||= self || reader.self.get(key) === display;
      const indicators = [...container.querySelectorAll("[jsname='QgSmzd']")];
      if (indicators.length > 10) { person.conflict = true; return; }
      for (const indicator of indicators) {
        if (!visible(indicator) || indicator.closest(excluded) || indicator.closest(owner) !== container) continue;
        const active = indicator.classList.contains("HX2H7");
        const idle = indicator.classList.contains("gjg47c");
        if (active === idle) { person.conflict = true; continue; }
        person.states.add(active ? "active" : "idle");
      }
    }
    const tiles = [...document.querySelectorAll(tileSelector)];
    const rows = [...document.querySelectorAll(rowSelector)];
    if (tiles.length > 100 || rows.length > 100) return null;
    // Read the explicitly labelled local participant before the tiles so a
    // closed People drawer can subsequently retain this same-document hint.
    for (const row of rows) {
      const labels = [...row.querySelectorAll(".NnTWjc")];
      const self = labels.length <= 10 && labels.some(node => node.tagName === "SPAN"
        && node.childElementCount === 0 && visible(node) && !node.closest(excluded)
        && node.closest(rowSelector) === row && (node.textContent || "").trim() === "(You)");
      collect(row, rowSelector, leafNames(row, ".zWGUib", rowSelector), self);
    }
    for (const tile of tiles) collect(tile, tileSelector, leafNames(tile, ".notranslate", tileSelector));
    if (!people.size || people.size > MAX_PEOPLE) return null;
    const participants = [...people.values()];
    if (participants.some(person => person.conflict || person.states.size > 1)
        || !participants.some(person => person.states.size === 1)) return null;
    const active = participants.filter(person => person.states.has("active")).map(person => person.key);
    if (active.length > 4) return null;
    return { source: "google-meet-dom-v1", participants: participants.map(({ key, name, self }) => ({ key, name, self })), active };
  }

  const api = { createCollector, read };
  root.QuillSpeakerActivity = api;
  if (typeof module !== "undefined") module.exports = api;
})(globalThis);
