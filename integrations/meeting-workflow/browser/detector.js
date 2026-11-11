/* Pure evidence classifier, shared by the content script and Node tests. */
(function (root) {
  "use strict";
  const EXCLUDED_LABEL_CONTEXT = "[role='log'],[role='complementary'],[data-message-id]";
  const MAX_LABEL_LENGTH = 160;

  function shortLabel(value) {
    if (typeof value !== "string" || value.length > MAX_LABEL_LENGTH) return null;
    return value.replace(/\s+/g, " ").trim();
  }

  function boundedLabelText(element) {
    // Do not aggregate textContent from a referenced container. Inspect only a
    // small label-shaped tree, rejecting chat/log content before reading text.
    const pending = [{ node: element, depth: 0 }];
    const parts = [];
    let visited = 0;
    let length = 0;
    while (pending.length) {
      const { node, depth } = pending.pop();
      if (++visited > 16 || depth > 3) return null;
      if (node.nodeType === 3) {
        const value = node.nodeValue;
        if (typeof value !== "string" || value.length > MAX_LABEL_LENGTH) return null;
        length += value.length;
        if (length > MAX_LABEL_LENGTH) return null;
        parts.push(value);
      } else if (node.nodeType === 1) {
        if (node.closest(EXCLUDED_LABEL_CONTEXT) || /^(SCRIPT|STYLE|TEMPLATE|NOSCRIPT|IFRAME)$/.test(node.tagName)) return null;
        const children = node.childNodes;
        if (children.length > 8) return null;
        for (let index = children.length - 1; index >= 0; index--) {
          pending.push({ node: children[index], depth: depth + 1 });
        }
      }
    }
    return shortLabel(parts.join(" "));
  }

  function controlLabel(element) {
    if (element.closest(EXCLUDED_LABEL_CONTEXT)) return "";
    const labelledBy = element.getAttribute("aria-labelledby");
    if (labelledBy?.trim()) {
      if (labelledBy.length > 512) return "";
      const references = labelledBy.trim().split(/\s+/);
      if (references.length > 4 || references.some(id => id.length > 128)) return "";
      const labels = [];
      let resolved = 0;
      for (const id of references) {
        const reference = element.ownerDocument.getElementById(id);
        if (!reference) continue;
        resolved++;
        const label = boundedLabelText(reference);
        if (label === null) return "";
        labels.push(label);
      }
      if (resolved) return shortLabel(labels.join(" ")) ?? "";
    }
    for (const attribute of ["aria-label", "data-tooltip", "title"]) {
      const raw = element.getAttribute(attribute);
      if (!raw?.trim()) continue;
      return shortLabel(raw) ?? "";
    }
    return "";
  }

  function participantName(value) {
    if (typeof value !== "string" || value.length > 400) return null;
    const name = value.normalize("NFC").replace(/\s+/g, " ").trim();
    if (!name || [...name].length > 100 || /\p{C}|@|:\/\//u.test(name)
        || /^(you|me|host|participant|participants|unknown|guest)$/i.test(name)) return null;
    return name;
  }

  const meetTile = "[data-participant-id][data-tile-media-id]";
  const meetRow = "[role='listitem'][data-participant-id]";
  const participantOwner = "[data-participant-id]";
  const excludedParticipant = "[role='log'],[data-message-id],[data-caption-id],[aria-hidden='true'],[hidden],.chat-container,.chat-message,.chat-box";

  function participantLabel(value, explicitSelf) {
    if (typeof value !== "string" || value.length > 400) return null;
    const suffix = /\s*\(you\)\s*$/i;
    const name = participantName(value.replace(suffix, ""));
    return name ? { name, self: explicitSelf || suffix.test(value) } : null;
  }

  function hasSelfMarker(element, owner) {
    // A wrapper inside this participant may carry the marker. An enclosing
    // roster or a nested participant cannot assert identity for this tile.
    const marker = element.closest("[data-is-self='true']");
    return Boolean(marker && marker.closest(participantOwner) === owner);
  }

  function meetParticipantLabel(container, ownerSelector, visible) {
    if (!visible(container) || container.closest(excludedParticipant)) return null;
    const row = ownerSelector === meetRow;
    const nodes = container.querySelectorAll(row ? ".zWGUib" : ".notranslate");
    if (nodes.length > 100) return null;
    const ownedLeaf = node => node.tagName === "SPAN" && node.childElementCount === 0
      && visible(node) && !node.closest(excludedParticipant) && node.closest(participantOwner) === container;
    let label = null;
    for (const node of nodes) {
      if (!ownedLeaf(node)) continue;
      const candidate = participantLabel(node.textContent, hasSelfMarker(node, container));
      if (!candidate) continue;
      if (label) return null;
      label = candidate;
    }
    if (label && row) {
      const badges = container.querySelectorAll(".NnTWjc");
      if (badges.length <= 10) label.self ||= [...badges].some(node => ownedLeaf(node)
        && (node.textContent || "").trim() === "(You)");
    }
    return label;
  }

  function visibleParticipants(document, provider, visible) {
    const byID = new Map();
    function add(owner, label) {
      const key = owner?.getAttribute("data-participant-id");
      if (!label || !key || key.length > 512) return;
      if (!byID.has(key)) byID.set(key, label);
      else {
        const known = byID.get(key);
        if (known && known.name === label.name) known.self ||= label.self;
        else byID.set(key, null);
      }
    }
    let candidates;
    if (provider === "google-meet") {
      const legacy = document.querySelectorAll("[data-participant-id] [data-self-name],[data-participant-id][data-self-name]");
      for (const element of [...legacy].slice(0, 100)) {
        if (!visible(element) || element.closest(excludedParticipant)) continue;
        const owner = element.closest(participantOwner);
        if (owner) add(owner, participantLabel(element.getAttribute("data-self-name"), hasSelfMarker(element, owner)));
      }
      for (const selector of [meetRow, meetTile]) {
        for (const container of [...document.querySelectorAll(selector)].slice(0, 100)) {
          add(container, meetParticipantLabel(container, selector, visible));
        }
      }
      candidates = [...byID.values()].filter(Boolean);
      if (candidates.filter(person => person.self).length > 1) candidates = candidates.filter(person => !person.self);
    } else if (provider === "zoom-web") {
      candidates = [];
      for (const element of [...document.querySelectorAll(".video-avatar__avatar-name")].slice(0, 100)) {
        if (!visible(element) || element.closest(excludedParticipant) || element.childElementCount > 2) continue;
        const label = participantLabel(element.textContent, element.getAttribute("data-is-self") === "true");
        if (label) candidates.push(label);
      }
    } else return [];

    const people = [], seen = new Set();
    for (const person of candidates) {
      const key = person.name.toLowerCase() + "|" + person.self;
      if (!seen.has(key)) people.push(person);
      seen.add(key);
      if (people.length === 20) break;
    }
    return people;
  }

  function classify(snapshot) {
    let url;
    try { url = new URL(snapshot.url); } catch { return { state: "unknown", reason: "url" }; }
    let provider;
    // Meet can redirect its calendar link to /meet/<code> after navigation.
    // Both forms still require the complete visible in-call toolbar below.
    if (url.protocol === "https:" && url.hostname === "meet.google.com" && /^\/(?:meet\/)?[a-z]{3}-[a-z]{4}-[a-z]{3}\/?$/.test(url.pathname)) {
      provider = "google-meet";
    } else if (url.protocol === "https:" && /(^|\.)zoom\.us$/.test(url.hostname) && /^\/wc\//.test(url.pathname)) {
      provider = "zoom-web";
    } else {
      return { state: "outside", reason: "not-meeting-page" };
    }
    return callEvidence(snapshot, provider);
  }

  // Warning evidence deliberately has no route allowlist. A new URL shape
  // must not disable both automatic capture and its independent reminder.
  // This result is never sent as an automatic recording signal.
  function warningEvidence(snapshot) {
    let url;
    try { url = new URL(snapshot.url); } catch { return { state: "unknown" }; }
    if (url.protocol !== "https:") return { state: "outside" };
    const provider = url.hostname === "meet.google.com" ? "google-meet"
      : /(^|\.)zoom\.us$/.test(url.hostname) ? "zoom-web" : null;
    return provider ? callEvidence(snapshot, provider) : { state: "outside" };
  }

  function callEvidence(snapshot, provider) {
    const labels = (snapshot.controls || []).filter(x => x.visible && x.enabled !== false)
      .map(x => String(x.label || "").trim().toLowerCase());
    const notices = (snapshot.notices || []).filter(x => x.visible)
      .map(x => String(x.text || "").trim().toLowerCase());
    // Read only structural status/heading nodes, never chat or captions.
    const ended = notices.some(x => /^(you (have )?left the (meeting|call)|the (meeting|call) (has )?ended|this meeting has been ended by (the )?host)[.!]?$/.test(x));
    if (ended) return { provider, state: "left", reason: "ended-screen" };
    const lobby = notices.some(x => /^(ready to join|asking to join|waiting for (the )?host|please wait.*(host|let you in)|the host will let you in|you.ll join.*(host|lets you in))/i.test(x));
    if (lobby) return { provider, state: "idle", reason: "lobby" };
    const matches = pattern => labels.some(x => pattern.test(x));
    const leave = matches(provider === "google-meet"
      ? /^(leave call|leave the call|leave meeting)(\b|\s|\()/
      : /^(leave|leave meeting)(\b|\s|\()/);
    const people = matches(/^(people|participants|show everyone|show all participants|open people)(\b|\s|\()/)
      || (provider === "zoom-web" && matches(/^open the participants list pane(?:,\s*\[[0-9]{1,6}\]\s*partic(?:i)?pants)?$/));
    const audio = matches(/^(turn (on|off) microphone|microphone|mute|unmute|join audio|audio)(\b|\s|\()/);
    if (leave && people && audio) return { provider, state: "joined", reason: "in-call-controls" };
    return { provider, state: "unknown", reason: "no-confirmed-call-state" };
  }
  const api = { classify, controlLabel, warningEvidence, visibleParticipants, participantName,
    meetParticipantLabel, meetTile, meetRow };
  root.QuillMeetingDetector = api;
  if (typeof module !== "undefined") module.exports = api;
})(globalThis);
