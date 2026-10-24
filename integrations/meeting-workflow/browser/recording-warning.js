/* Local reminder only. No recording commands, media, or meeting content. */
(function (root) {
  "use strict";
  const STATES = new Set(["not-recording", "starting", "recording", "paused", "skipped", "unavailable"]);
  const FRESHNESS = 8000;
  const JOIN_FRESHNESS = 15000;
  const GRACE = 15000;
  const START_GRACE = 30000;

  function createMonitor({ now = () => Date.now(), requestStatus, render, randomID = () => crypto.randomUUID() }) {
    let call = null;
    let sequence = 0;
    let invalidated = false;
    let rendered = null;
    function display(value) {
      if (rendered === value) return;
      rendered = value;
      render(value);
    }
    function refresh() {
      const time = now();
      if (!call || call.dismissed || call.suppressed || time - call.joinedAt > JOIN_FRESHNESS || time - call.since < GRACE) {
        display(null); return;
      }
      const status = !invalidated && time - call.confirmedAt <= FRESHNESS ? call.state : "unavailable";
      if (["recording", "paused", "skipped"].includes(status) ||
          (status === "starting" && call.startingAt !== null && time - call.startingAt < START_GRACE)) {
        display(null); return;
      }
      display(status === "not-recording" ? "not-recording" : "unavailable");
    }
    function clear() { call = null; sequence++; display(null); }
    function observe(evidence, meeting) {
      const time = now();
      if (["left", "idle", "outside"].includes(evidence.state)) { clear(); return; }
      if (evidence.state !== "joined") { refresh(); return; }
      if (!call || call.provider !== evidence.provider || (meeting && call.meeting && call.meeting !== meeting)) {
        sequence++;
        call = { id: randomID(), provider: evidence.provider, since: time, joinedAt: time,
          requestedAt: -Infinity, confirmedAt: -Infinity, state: "unavailable",
          startingAt: null, dismissed: false, suppressed: false };
      }
      call.joinedAt = time;
      // A warning-only call uses its own opaque ID until strict detection has
      // supplied a recording identity. Neither ID contains a URL or title.
      if (meeting && call.meeting !== meeting) {
        call.meeting = meeting;
        // A fallback-only probe cannot establish suppression for a newly
        // available strict identity, even if its reply arrives last.
        sequence++;
        call.requestedAt = -Infinity;
      }
      if (!invalidated && time - call.requestedAt >= 5000) {
        call.requestedAt = time;
        const target = call;
        const request = ++sequence;
        Promise.resolve().then(() => requestStatus({ meeting: target.meeting || target.id, provider: target.provider }))
          .then(answer => {
            if (call !== target || request !== sequence || invalidated) return;
            const state = answer?.recordingStatus?.state;
            target.state = STATES.has(state) ? state : "unavailable";
            target.confirmedAt = now();
            if (state === "starting" && target.startingAt === null) target.startingAt = now();
            if (["recording", "paused", "not-recording"].includes(state)) target.startingAt = null;
            if (state === "skipped") target.suppressed = true;
            refresh();
          }).catch(() => {
            if (call !== target || request !== sequence) return;
            target.state = "unavailable";
            target.confirmedAt = now();
            refresh();
          });
      }
      refresh();
    }
    return { observe, refresh, clear,
      dismiss() { if (call) call.dismissed = true; refresh(); },
      connectionLost() { invalidated = true; sequence++; refresh(); },
    };
  }

  function createBanner(document, dismiss) {
    let host = null;
    let title = null;
    let current = null;
    return state => {
      if (state === current) return;
      current = state;
      if (!state) { host?.remove(); host = null; title = null; return; }
      if (!host) {
        host = document.createElement("div");
        host.setAttribute("data-quill-recording-warning", "");
        host.style.cssText = "all:initial;position:fixed;top:20px;right:20px;z-index:2147483647;max-width:calc(100vw - 40px);";
        // The isolated tree keeps its controls out of meeting-control scans.
        const shadow = host.attachShadow({ mode: "closed" });
        const style = document.createElement("style");
        style.textContent = ":host{color-scheme:light}section{box-sizing:border-box;width:360px;max-width:calc(100vw - 40px);padding:18px;background:#fff8e7;color:#29251d;border:1px solid #b88836;border-radius:12px;box-shadow:0 4px 24px #0004;font:14px/1.45 system-ui,sans-serif}strong{display:block;font-size:16px}p{margin:8px 0 14px}button{font:inherit;background:#29251d;color:white;border:0;border-radius:6px;padding:8px 12px;cursor:pointer}button:focus-visible{outline:3px solid #245fce;outline-offset:3px}";
        const section = document.createElement("section");
        section.setAttribute("role", "alert");
        section.setAttribute("aria-atomic", "true");
        title = document.createElement("strong");
        const detail = document.createElement("p");
        detail.textContent = "Open Quill from Applications or the menu bar to start recording or check its status.";
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = "Dismiss reminder";
        button.addEventListener("click", dismiss);
        section.append(title, detail, button);
        shadow.append(style, section);
        document.documentElement.append(host);
      }
      title.textContent = state === "not-recording" ? "Quill is not recording" : "Quill cannot confirm recording";
    };
  }
  root.QuillRecordingWarning = { createMonitor, createBanner };
  if (typeof module !== "undefined") module.exports = { createMonitor, createBanner };
})(globalThis);
