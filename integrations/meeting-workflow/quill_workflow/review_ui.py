"""Presentation for the local review page; no scripts or external resources."""
import html
import re
from urllib.parse import urlencode


def escape(value):
    return html.escape(str(value), quote=True)


def notes_html(text, heading_offset=2):
    """Render the small Markdown subset used by notes, with all text escaped.

    Links, images, embedded HTML and arbitrary Markdown extensions are deliberately
    treated as text. Notes cannot introduce navigation or executable content.
    """
    def inline(value):
        value = escape(value)
        return re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", value)

    output, paragraph, listing = [], [], False

    def flush():
        if paragraph:
            output.append("<p>" + "<br>".join(paragraph) + "</p>")
            paragraph.clear()

    for line in text.splitlines():
        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        bullet = re.match(r"^\s*(?:[-*]|\d+\.)\s+(.+)$", line)
        if not bullet and listing:
            output.append("</ul>")
            listing = False
        if heading:
            flush()
            # A meeting heading is already present outside this document.
            level = max(2, min(6, len(heading[1]) + heading_offset))
            output.append(f"<h{level}>" + inline(heading[2]) + f"</h{level}>")
        elif bullet:
            flush()
            if not listing:
                output.append("<ul>")
                listing = True
            output.append("<li>" + inline(bullet[1]) + "</li>")
        elif not line.strip():
            flush()
        else:
            paragraph.append(inline(line))
    flush()
    if listing:
        output.append("</ul>")
    return '<div class="notes-document">' + "".join(output) + "</div>"


FEATHER = """<svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
<path d="M20 4c-5-2-11 1-13 6-1 2-1 4-1 6l4 1c5-1 9-7 10-13Z"
stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>
<path d="m4 21 10-12M7 15h6" stroke="currentColor" stroke-width="1.6"
stroke-linecap="round"/></svg>"""

CSS = """
:root{color-scheme:light dark;--bg:#f4f6f3;--surface:#fff;--subtle:#f6f7f4;
--ink:#25312b;--muted:#627067;--line:#e2e8e1;--accent:#285b40;--tint:#e8f0e8;
--amber:#82551b;--amber-bg:#faf4e8;--red:#a73838;--red-bg:#faefed;
font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink)}
a{color:var(--accent);text-underline-offset:3px}button,input,select,textarea{font:inherit;color:inherit}
button,a,summary,label{-webkit-tap-highlight-color:transparent}
button,.button{display:inline-flex;align-items:center;justify-content:center;gap:7px;min-height:38px;padding:8px 13px;border:1px solid var(--line);border-radius:7px;background:var(--surface);text-decoration:none;color:var(--ink);font-weight:500;line-height:1.4;cursor:pointer}
button[value=approve],button[value=sharing_confirm],button[value=sharing_prepare],button[value=resume],button[value=retry]{background:var(--accent);border-color:var(--accent);color:#fff}
button[value=skip],button[value=sharing_private],.quiet{background:transparent;color:var(--muted)}
button:disabled{opacity:.5;cursor:default}:focus-visible{outline:3px solid #6a8fd1;outline-offset:3px}
input,select,textarea{border:1px solid var(--line);border-radius:7px;background:var(--surface);padding:10px 12px;max-width:100%}
select,textarea{width:100%}textarea{resize:vertical;min-height:105px;line-height:1.6}
label{display:block;font-weight:500;margin:16px 0 7px}form{margin:14px 0}form>button{margin:0 7px 7px 0}
form>div{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}
p{margin:0 0 14px;overflow-wrap:anywhere}ul{padding-left:21px}
h1,h2,h3,h4,h5,h6{line-height:1.3;letter-spacing:-.025em;overflow-wrap:anywhere;font-weight:600}
h1{font-size:29px;margin:0 0 12px}h2{font-size:23px;margin:0 0 14px}h3{font-size:17px;margin:24px 0 10px}h4,h5,h6{font-size:15px;margin:22px 0 10px}
.muted{color:var(--muted)}.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
.app{max-width:1440px;margin:24px auto;padding:0 24px}
.workspace{display:grid;grid-template-columns:190px minmax(0,1fr);min-height:calc(100vh - 48px);background:var(--surface);border:1px solid var(--line);border-radius:14px;overflow:clip}
.sidebar{padding:25px 13px;background:var(--subtle);border-right:1px solid var(--line);min-width:0}
.brand{display:flex;gap:9px;align-items:center;font-size:21px;font-weight:600;letter-spacing:-.6px;margin:0 10px 27px;text-decoration:none;color:var(--ink)}
.brand svg{width:26px;height:26px;color:var(--accent)}
.nav-link{display:flex;align-items:center;justify-content:space-between;gap:8px;min-height:40px;padding:8px 11px;margin:3px 0;border-radius:7px;color:var(--ink);font-size:13px;text-decoration:none;overflow-wrap:anywhere}
.nav-link[aria-current=page]{background:var(--tint);color:var(--accent);font-weight:600}
.nav-label{font-size:12px;color:var(--muted);margin:29px 11px 8px}.count{font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums;flex:none}
.sidebar-foot{margin:34px 11px 0;color:var(--muted);font-size:12px}
.meeting-main{min-width:0;padding:26px 34px 38px}
.toolbar{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;margin-bottom:28px}
.toolbar h1{font-size:25px;margin:0}.toolbar-actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.back,.refresh{font-size:13px;text-decoration:none;color:var(--muted);padding:7px 0}.back:before{content:"←";margin-right:7px}
.search{display:flex;gap:8px;align-items:center;margin:0;max-width:380px}.search input[type=search]{width:220px;min-width:0;font-size:13px}.search button{font-size:13px}
.table-head,.meeting-row{display:grid;grid-template-columns:minmax(0,1fr) 125px 145px;gap:18px}
.table-head{font-size:12px;color:var(--muted);padding:0 4px 12px;border-bottom:1px solid var(--line)}
.meeting-row{align-items:center;padding:18px 4px;border-bottom:1px solid var(--line);text-decoration:none;color:var(--ink)}
.meeting-row strong{display:block;font-size:14px;font-weight:500;line-height:1.5;overflow-wrap:anywhere}
.row-meta{display:block;margin-top:4px;color:var(--muted);font-size:12px}.row-folder,.row-status{font-size:12px;color:var(--muted);overflow-wrap:anywhere}
.row-status.amber{color:var(--amber)}.row-status.red{color:var(--red)}
.meeting-document{max-width:720px;margin:0 auto}.panel-header{margin-bottom:23px}.panel-header h1{max-width:680px}
.meeting-meta{display:flex;gap:6px 17px;flex-wrap:wrap;font-size:13px;color:var(--muted);margin-bottom:13px}
.meeting-state{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;font-size:12px;color:var(--muted)}
.folder-label{border:1px solid var(--line);border-radius:6px;padding:3px 8px}.badge{font-size:12px;color:var(--muted)}
.badge.amber{color:var(--amber)}.badge.red{color:var(--red)}
.workflow-action{padding:17px 19px;margin:20px 0;border-radius:8px;background:var(--amber-bg)}
.workflow-action.neutral{background:var(--subtle)}.workflow-action.red{background:var(--red-bg)}
.workflow-action>p{font-size:13px;margin-bottom:0}.workflow-action .panel-body{padding:0}.workflow-action h3{margin:0 0 10px;font-size:15px}
.workflow-action form:last-child{margin-bottom:0}.workflow-action form:last-child button{margin-bottom:0}
.workflow-action details{margin:13px 0 0;background:transparent;border:0}.workflow-action summary{padding:5px 0;background:transparent;color:var(--accent)}
.tabs{display:flex;gap:24px;flex-wrap:wrap;border-bottom:1px solid var(--line);margin:26px 0}
.tab{padding:7px 0 11px;margin-bottom:-1px;border-bottom:2px solid transparent;text-decoration:none;color:var(--muted)}
.tab[aria-current=page]{color:var(--ink);border-color:var(--accent);font-weight:500}
.notes-document{font-size:14px;line-height:1.8;overflow-wrap:anywhere}.notes-document h3{font-size:18px;margin-top:24px}.notes-document h4{font-size:17px;margin-top:24px}
.notes-document h5,.notes-document h6{font-size:15px}.notes-document li{padding:3px 0}.notes-document p{color:var(--ink);margin:9px 0 14px}.notes-document ul{margin:0 0 14px}.notes-document strong{font-weight:600}
.transcript{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.9 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.recording-path{display:block;padding:14px;background:var(--subtle);border-radius:7px;overflow-wrap:anywhere;white-space:pre-wrap;font-size:12px}
details{border:1px solid var(--line);border-radius:8px;margin:16px 0;overflow:hidden}
summary{cursor:pointer;font-size:13px;font-weight:500;padding:11px 14px;background:var(--subtle)}
details>p,details>ul{margin:14px 16px}details>form{margin:14px 16px}details>.notes-document{padding:4px 20px 20px}
.meeting-actions{border:0;border-radius:0;margin:22px 0 0}.meeting-actions>summary{display:inline-block;background:transparent;color:var(--muted);padding:6px 0}
.local-removal{padding:16px 0;border-top:1px solid var(--line);margin-top:10px}.local-removal h3{font-size:14px;margin-top:0}.local-removal p{font-size:13px}.danger{color:var(--red);border-color:var(--red)}
.activity dl{display:grid;grid-template-columns:140px minmax(0,1fr);gap:14px 22px}.activity dt{color:var(--muted)}.activity dd{margin:0;overflow-wrap:anywhere}
.sharing-review h3{margin-top:0}.sharing-review .review-layout{display:flex;flex-direction:column;gap:15px}.sharing-review .action-pane{border:0;padding:0}.sharing-review .reading{min-width:0}
.sharing-heading{margin-top:0}.calendar-event{padding:14px 17px;border:1px solid var(--line);border-radius:8px;margin:14px 0;background:var(--subtle)}.calendar-event h5{margin:0 0 10px}.calendar-event ul,.calendar-event p{font-size:13px}
.suggestion{font-size:13px}.suggestion-title{margin:0 0 7px}.notes-caption{font-size:12px;color:var(--muted)}
.notice{background:var(--tint);border-radius:8px;padding:12px 16px;margin-bottom:22px;font-size:13px}
[role=alert]{color:var(--red)}.empty{padding:55px 0;max-width:500px;color:var(--muted)}.empty h2{font-size:20px;color:var(--ink)}
.review-footer{font-size:12px;color:var(--muted);margin-top:34px;display:flex;gap:15px;flex-wrap:wrap}
.demo-banner{padding:10px 16px;margin-bottom:18px;background:var(--amber-bg);color:var(--amber);border-radius:8px;font-size:13px}
.error-page{max-width:680px;padding:34px;background:var(--surface);border:1px solid var(--line);border-radius:12px;margin:40px auto}.error-page h1{font-size:25px}.error-page h2{font-size:19px}
@media(hover:hover){.nav-link:hover,.meeting-row:hover{background:var(--subtle)}.nav-link[aria-current=page]:hover{background:var(--tint)}button:hover,.button:hover{filter:brightness(.97)}}
@media(prefers-color-scheme:dark){:root{--bg:#141715;--surface:#191c1a;--subtle:#141715;--ink:#e4eae5;--muted:#a2afa6;--line:#343c36;--accent:#b8d9be;--tint:#293b2e;--amber:#e7c18b;--amber-bg:#342b20;--red:#f1aaa4;--red-bg:#3c2928}
button[value=approve],button[value=sharing_confirm],button[value=sharing_prepare],button[value=resume],button[value=retry]{color:#18291e}}
@media(max-width:1000px){.app{padding:0 14px;margin:14px auto}.workspace{grid-template-columns:170px minmax(0,1fr)}.meeting-main{padding:24px}.table-head,.meeting-row{grid-template-columns:minmax(0,1fr) 135px}.folder-column{display:none}.panel-header h1{font-size:26px}}
@media(max-width:620px){.app{margin:0;padding:0}.workspace{display:block;border:0;border-radius:0;min-height:100vh}.sidebar{padding:16px;border-right:0;border-bottom:1px solid var(--line)}.brand{margin:0 0 12px;font-size:19px}.brand svg{width:23px;height:23px}.sidebar nav{display:flex;gap:5px;flex-wrap:wrap}.nav-link{margin:0;min-height:38px;font-size:12px;padding:6px 9px}.nav-label{width:100%;margin:8px 9px 0}.sidebar-foot{display:none}.meeting-main{padding:22px 18px}.toolbar{gap:15px;margin-bottom:24px}.search{width:100%;max-width:none}.search input[type=search]{flex:1;width:100%}.table-head{display:none}.meeting-row{display:flex;flex-direction:column;align-items:flex-start;gap:8px;padding:16px 2px}.row-meta{font-size:12px}.panel-header h1{font-size:25px}.tabs{gap:22px}.activity dl{grid-template-columns:1fr;gap:5px}.activity dd{margin-bottom:14px}.error-page{margin:15px;padding:24px}.toolbar-actions{gap:12px}.workflow-action{padding:15px}.review-footer{margin-top:28px}}
@media(pointer:coarse){button,.button,.nav-link,.tab,.back,.refresh,summary{min-height:44px}input,textarea,select{font-size:16px}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{scroll-behavior:auto!important;transition:none!important;animation:none!important}}
"""


def document(body, extra_css=""):
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Quill · Meetings</title><style>' + CSS + extra_css +
            '</style></head><body><main class="app">' + body + '</main></body></html>')


def panel_header(title, status, detail, tone="neutral", step=None):
    # Compatibility for non-library callers. Sharing is optional, never a
    # mandatory last step in a completion strip.
    return ('<header class="panel-header"><h1>' + escape(title) + '</h1>'
            '<span class="badge ' + escape(tone) + '">' + escape(status) + '</span>'
            '<p>' + escape(detail) + '</p></header>')


def meeting_url(path, selection=None, *, view="all", folder=None, query="", tab="notes"):
    fields = {}
    if selection:
        fields["meeting"] = selection
    if view != "all":
        fields["view"] = view
    if folder:
        fields["folder"] = folder
    if query:
        fields["q"] = query
    if selection and tab != "notes":
        fields["tab"] = tab
    return path + ("?" + urlencode(fields) if fields else "")


def _link(label, url, klass="", current=False):
    return ('<a href="' + escape(url) + '"' +
            (' class="' + escape(klass) + '"' if klass else '') +
            (' aria-current="page"' if current else '') + '>' + label + '</a>')


def _reading_notes(text):
    # The meeting title already has a heading above the document. Remove only
    # the first Markdown H1, never arbitrary lines or subsequent headings.
    return notes_html(re.sub(r"\A\s*# [^\n]*(?:\n|$)", "", text or "", count=1), heading_offset=0)


def _metadata(entry):
    values = entry.get("metadata") or []
    if not values:
        values = ([", ".join(entry["participants"])] if entry.get("participants") else [])
        if entry.get("display_date"):
            values.append(entry["display_date"])
    return values


def _storage_label(entry):
    return {"saved_in_drive": "Saved in Drive", "partly_saved": "Partly saved in Drive",
            "upload_uncertain": "Drive save unconfirmed", "not_confirmed": "Drive save not confirmed",
            "not_requested": "Kept on this Mac"}.get(entry.get("storage_status"), "")


def _meeting_content(entry, path, navigation, tab):
    title, status = entry["title"], entry["status"]
    selection = entry.get("selection")
    url = lambda part: meeting_url(path, selection, tab=part, **navigation)
    body = '<section class="meeting-panel" aria-label="' + escape(title) + '">'
    body += '<div class="toolbar">' + _link('Meetings', meeting_url(path, **navigation), 'back')
    body += '<div class="toolbar-actions">' + _link('Refresh status', url(tab), 'refresh')
    if entry.get("sharing_body") and tab != "sharing":
        label = 'Sharing details' if entry.get("sharing_status") in ("shared", "private") else 'Share notes…'
        body += _link(label, url('sharing'), 'button')
    body += '</div></div><div class="meeting-document">'
    body += '<header class="panel-header"><h1>' + escape(title) + '</h1>'
    values = _metadata(entry)
    if values:
        body += '<div class="meeting-meta">' + ''.join('<span>' + escape(v) + '</span>' for v in values) + '</div>'
    body += '<div class="meeting-state">'
    if entry.get("folder_name"):
        body += '<span class="folder-label">' + escape(entry["folder_name"]) + '</span>'
    body += '<span class="badge ' + escape(entry.get("tone", "neutral")) + '">' + escape(status) + '</span>'
    storage = _storage_label(entry)
    if storage and storage.casefold() != status.casefold():
        body += '<span>' + escape(storage) + '</span>'
    body += '</div></header>'
    if tab == "notes" and (entry.get("body") or entry.get("detail") and entry.get("attention")):
        body += '<section class="workflow-action ' + escape(entry.get("tone", "neutral")) + '" aria-label="Next action">'
        if entry.get("detail"):
            body += '<p>' + escape(entry["detail"]) + '</p>'
        body += entry.get("body", "") + '</section>'
    elif tab == "notes" and entry.get("detail") and not entry.get("notes"):
        body += '<p class="muted">' + escape(entry["detail"]) + '</p>'
    body += '<nav class="tabs" aria-label="Meeting content">'
    for key, label in (("notes", "Notes"), ("transcript", "Transcript"), ("recording", "Recording")):
        body += _link(label, url(key), 'tab', tab == key)
    body += '</nav>'
    if tab == "sharing":
        body += (entry.get("sharing_body") or '<p class="muted">Sharing is unavailable until this meeting’s private notes document is ready.</p>')
    elif tab == "transcript":
        transcript = entry.get("transcript") or entry.get("transcript_text")
        body += ('<div class="transcript">' + escape(transcript) + '</div>' if transcript else
                 '<p class="muted">A transcript preview is not available for this meeting yet. Its local files are unchanged.</p>')
    elif tab == "recording":
        body += '<h2>Recording files</h2>'
        if entry.get("recording_files"):
            body += '<p class="muted">Tracks included in this recording</p><ul>'
            for recording in entry["recording_files"]:
                body += '<li>' + escape(recording["label"]) + ' · ' + escape(recording["name"]) + '</li>'
            body += '</ul>'
        else:
            body += entry.get("recording_body") or '<p class="muted">Open Quill’s recordings folder from the menu bar to find the local recording.</p>'
        if entry.get("local_folder"):
            body += '<p class="muted">Saved local location</p><code class="recording-path">' + escape(entry["local_folder"]) + '</code>'
    elif tab == "activity":
        body += '<section class="activity"><h2>Meeting activity</h2><dl>'
        for key, value in (("Processing", entry.get("detail") or status),
                           ("Folder", entry.get("folder_name") or "No confirmed destination"),
                           ("Drive", storage or "No verified filing status available"),
                           ("Sharing", entry.get("sharing_label") or "No sharing confirmation available")):
            body += '<dt>' + key + '</dt><dd>' + escape(value) + '</dd>'
        storage_states = {"confirmed": "Save confirmed", "uncertain": "Save unconfirmed", "pending": "Waiting",
                          "failed": "Needs attention", "unknown": "No verified save information", "not_requested": "Not requested"}
        for key, label in (("document", "Notes & transcript"), ("media", "Recording files")):
            if entry.get("storage", {}).get(key):
                body += '<dt>' + label + '</dt><dd>' + storage_states.get(entry["storage"][key]["status"], "No verified save information") + '</dd>'
        body += '</dl>' + entry.get("activity_body", "") + '</section>'
    elif entry.get("notes"):
        body += _reading_notes(entry["notes"])
    else:
        body += '<p class="muted">Notes will appear here when they’re ready.</p>'
    body += '<details class="meeting-actions"><summary>More meeting actions</summary>'
    body += '<p>' + _link('View activity', url('activity')) + '</p>'
    body += entry.get("deletion_body", "") + '</details></div></section>'
    return body


def workspace(entries, path, notice=None, sharing=False, preview=False, selected=None,
              *, view="all", folder=None, query="", tab="notes", companies=None):
    """Render a read-only library or one detail; actions remain exact native POSTs.

    Search, folders and tabs navigate with GET. No script or external resource
    is needed, so saved transcript text cannot become executable UI content.
    """
    navigation = {"view": view, "folder": folder, "query": query}
    folders = {c["id"]: c["name"] for c in companies or []}
    for entry in entries:
        if entry.get("folder_id") and entry.get("folder_name"):
            folders[entry["folder_id"]] = entry["folder_name"]
    body = '<div class="workspace"><aside class="sidebar" aria-label="Quill navigation">'
    body += _link(FEATHER + 'Quill', path, 'brand') + '<nav aria-label="Meeting views">'
    body += _link('All meetings <span class="count">' + str(len(entries)) + '</span>', path, 'nav-link', view == 'all' and not folder)
    count = sum(bool(e.get("attention")) for e in entries)
    body += _link('Needs attention <span class="count">' + str(count) + '</span>', meeting_url(path, view='attention'), 'nav-link', view == 'attention' and not folder)
    if folders:
        body += '<p class="nav-label">Folders</p>'
        for key, name in folders.items():
            body += _link(escape(name), meeting_url(path, folder=key), 'nav-link', folder == key)
    body += '</nav><p class="sidebar-foot">On this Mac</p></aside><div class="meeting-main">'
    if preview:
        body += '<p class="demo-banner">Design preview · Sample meetings. Actions stay in this preview.</p>'
    if notice:
        body += '<p class="notice" role="status">' + escape(notice) + '</p>'
    chosen = next((e for e in entries if selected and e.get("selection") == selected), None)
    if selected and not chosen:
        body += '<p class="notice" role="status">That meeting is no longer available in this review.</p>'
    if chosen:
        body += _meeting_content(chosen, path, navigation, tab)
    else:
        title = folders.get(folder, "Meetings") if folder else "Needs attention" if view == 'attention' else 'Meetings'
        body += '<div class="toolbar"><h1>' + escape(title) + '</h1>'
        body += '<div class="toolbar-actions">' + _link('Refresh status', meeting_url(path, **navigation), 'refresh')
        body += '<form method="get" action="' + escape(path) + '" class="search" role="search">'
        if view != 'all':
            body += '<input type="hidden" name="view" value="' + escape(view) + '">'
        if folder:
            body += '<input type="hidden" name="folder" value="' + escape(folder) + '">'
        body += '<label class="sr-only" for="meeting-search">Search meetings</label><input type="search" name="q" id="meeting-search" maxlength="160" value="' + escape(query) + '" placeholder="Search meetings">'
        body += '<button type="submit">Search</button></form></div></div>'
        shown = [e for e in entries if (view != 'attention' or e.get('attention'))
                 and (not folder or e.get('folder_id') == folder)
                 and (not query or query.casefold() in ' '.join([e['title'], e.get('folder_name') or '', *_metadata(e)]).casefold())]
        shown.sort(key=lambda e: -(e.get('recorded_at') or 0))
        if shown:
            body += '<div class="table-head" aria-hidden="true"><span>Meeting</span><span class="folder-column">Folder</span><span>Status</span></div><div class="meeting-list" aria-label="Choose a meeting">'
            for entry in shown:
                row = '<span><strong>' + escape(entry['title']) + '</strong>'
                values = _metadata(entry)
                if values:
                    row += '<span class="row-meta">' + escape(' · '.join(values)) + '</span>'
                row += '</span><span class="row-folder folder-column">' + escape(entry.get('folder_name') or '—') + '</span>'
                row += '<span class="row-status ' + escape(entry.get('tone', 'neutral')) + '">' + escape(entry['status']) + '</span>'
                body += _link(row, meeting_url(path, entry.get('selection'), **navigation), 'meeting-row')
            body += '</div>'
        else:
            heading = 'No meetings need your attention' if view == 'attention' and not query else 'No matching meetings' if query or folder else 'No meetings yet'
            body += '<div class="empty"><h2>' + heading + '</h2><p>' + ('Try another search or folder.' if query or folder else 'New recordings and their notes will appear here.') + '</p></div>'
    body += '<footer class="review-footer"><span>Filing keeps meetings private.</span><span>'
    body += ('Sharing requires a separate notes-only review.' if sharing else 'Nothing is shared with participants here.')
    return document(body + '</span></footer></div></div>')
