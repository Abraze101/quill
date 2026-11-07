/* No media capture. Visible meeting controls, notices and participant names. */
(() => {
  "use strict";
  const documentID = crypto.randomUUID();
  let meeting = null;
  let provider = null;
  let joined = false;
  let last = null;
  let stopped = false;
  let pendingScan = null;
  let heartbeat = null;
  let warningTimer = null;
  let warning = null;
  let activityTimer = null;
  const activity = globalThis.QuillSpeakerActivity?.createCollector();
  let lastActivityAt = -Infinity;
  let lastActivity = null;
  let activitySession = null;
  if (globalThis.QuillRecordingWarning) {
    const render = QuillRecordingWarning.createBanner(document, () => warning.dismiss());
    warning = QuillRecordingWarning.createMonitor({ render,
      requestStatus: details => chrome.runtime.sendMessage({ type: "quill-recording-health", ...details }) });
    warningTimer = setInterval(() => stopped ? scan() : warning.refresh(), 1000);
  }

  function teardown() {
    if (stopped) return;
    stopped = true;
    observer.disconnect();
    if (heartbeat !== null) clearInterval(heartbeat);
    if (activityTimer !== null) clearInterval(activityTimer);
    if (pendingScan !== null) clearTimeout(pendingScan);
    heartbeat = null;
    activityTimer = null;
    activity?.reset();
    lastActivity = null;
    activitySession = null;
    pendingScan = null;
    if (!warning) window.removeEventListener("pagehide", onPageHide);
    warning?.connectionLost();
    // Reloading the extension retires this script; it is not evidence that
    // the meeting ended. A fresh page load supplies the replacement script.
  }
  function sendFailed(error) {
    const message = typeof error === "string" ? error : error?.message;
    if (typeof message === "string" && /extension context invalidated/i.test(message)) teardown();
    // A temporarily unavailable worker or message channel can recover on
    // the next scan. Keep collecting fresh state instead of replaying it.
  }

  function visible(element) {
    const style = getComputedStyle(element);
    return style.display !== "none" && style.visibility !== "hidden" && element.getClientRects().length > 0;
  }
  function send(state, participants, speakerActivity) {
    if (stopped || !meeting || !provider) return;
    const message = { type: "quill-meeting", documentID, meeting, provider, state,
      ...(state === "joined" ? { participants: participants || [] } : {}),
      ...(state === "joined" && speakerActivity ? { speakerActivity } : {}) };
    try {
      Promise.resolve(chrome.runtime.sendMessage(message)).catch(sendFailed);
    } catch (error) {
      // Chrome can throw synchronously after an unpacked extension reload.
      sendFailed(error);
    }
  }
  function scan() {
    if (stopped && !warning) return;
    const controls = [...document.querySelectorAll("button,[role='button']")].map(element => ({
      label: QuillMeetingDetector.controlLabel(element),
      enabled: !element.disabled && element.getAttribute("aria-disabled") !== "true",
      visible: visible(element)
    }));
    const notices = [...document.querySelectorAll("h1,h2,[role='heading'][aria-level='1'],[role='heading'][aria-level='2']")]
      .filter(element => !element.closest("[role='log'],[role='complementary'],[data-message-id]"))
      // Large containers can include captions/chat; ignore them entirely.
      .filter(element => element.childElementCount < 5 && (element.textContent || "").length < 180)
      .map(element => ({ text: element.textContent, visible: visible(element) }));
    const snapshot = { url: location.href, controls, notices };
    if (stopped) {
      warning?.observe(QuillMeetingDetector.warningEvidence(snapshot));
      return;
    }
    const result = QuillMeetingDetector.classify(snapshot);
    if (result.provider) provider = result.provider;
    if (result.state === "joined") {
      if (!joined) meeting = crypto.randomUUID();
      joined = true;
      last = "joined";
    } else if (result.state === "left" || result.state === "outside") {
      joined = false;
      last = "left";
    } else if (joined) {
      // Hidden controls, reconnecting, or an unreadable state cannot prove leave.
      last = "unknown";
    }
    let speakerActivity;
    if (result.state === "joined" && activity) speakerActivity = sampleActivity(true);
    else { activity?.reset(); lastActivity = null; activitySession = null; lastActivityAt = -Infinity; }
    if (last) send(last, result.state === "joined"
      ? QuillMeetingDetector.visibleParticipants(document, provider, visible) : undefined, speakerActivity);
    warning?.observe(QuillMeetingDetector.warningEvidence(snapshot), result.state === "joined" ? meeting : null);
  }
  function sampleActivity(allowSessionChange = false) {
    if (!activity || stopped || last !== "joined") return;
    try {
      const now = Date.now();
      const route = new URL(location.href).pathname.replace(/^\/meet\//, "/").replace(/\/$/, "");
      const activityKey = meeting + "|" + provider + "|" + route;
      if (activityKey !== activitySession) {
        activity.reset(); lastActivity = null; lastActivityAt = -Infinity;
        // A timer must not carry evidence into a new meeting before the normal
        // detector has confirmed that route is joined.
        if (!allowSessionChange) { activitySession = null; return; }
        activitySession = activityKey;
      }
      if (now - lastActivityAt >= 400 || now < lastActivityAt) {
        lastActivityAt = now;
        lastActivity = activity.observe(activityKey,
          QuillSpeakerActivity.read(document, provider, visible, activityKey));
      }
      return lastActivity;
    } catch { activity.reset(); lastActivity = null; }
  }
  function onPageHide() {
    activity?.reset();
    lastActivity = null;
    activitySession = null;
    if (activityTimer !== null) clearInterval(activityTimer);
    activityTimer = null;
    if (!stopped && meeting) send("left");
    warning?.clear();
    if (warningTimer !== null) clearInterval(warningTimer);
    warningTimer = null;
    window.removeEventListener("pagehide", onPageHide);
  }
  const observer = new MutationObserver(() => {
    if (stopped || pendingScan !== null) return;
    pendingScan = setTimeout(() => {
      pendingScan = null;
      scan();
    }, 150);
  });
  observer.observe(document.documentElement, { subtree: true, childList: true,
    attributes: true, attributeFilter: ["aria-label", "aria-labelledby", "id", "aria-hidden", "hidden", "style", "class"] });
  // WebRTC pages ordinarily continue receiving timers in background tabs.
  // If a browser suspends delivery, Quill shows signal loss and requires a
  // manual stop instead of treating a missing heartbeat as a leave event.
  heartbeat = setInterval(scan, 5000);
  // Sample activity independently; retain the existing detection and health
  // cadence. Batches travel with the next normal joined heartbeat/DOM scan.
  if (activity) activityTimer = setInterval(() => sampleActivity(), 500);
  window.addEventListener("pagehide", onPageHide);
  scan();
})();
