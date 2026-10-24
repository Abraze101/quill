# Review and share meeting notes

Open Quill's menu and choose **Review meetings…**. The library has **All meetings**,
**Needs attention**, folder filters and **Search meetings**. Select a meeting to
open its **Notes**, then choose **Share notes…** when available. Completed meetings
filed as native Google Docs have a separate **Share meeting notes** review.
Sharing is optional; recording, transcription and private filing do not wait for it.

**Transcript** and **Recording** remain separate tabs in the meeting detail.
**Meetings** returns to the library. Completed or dismissed sharing reviews use
**Sharing details** instead of **Share notes…**. A prepared copy awaiting approval
appears in **Needs attention**. Folder filters only change the list; they do not
file or share a meeting.

1. Read the notes preview and the calendar suggestions. When automatic lookup is
   enabled, Quill finds invitees after new meeting notes finish filing in Drive.
   Choose **Find attendees from calendar** for older meetings, or refresh a lookup
   when needed. You can also enter email addresses yourself.
   Each suggestion shows its calendar, event, time and match reason. **Use these
   suggestions** fills the editable addresses field; remove anyone who should not
   receive the notes. Invitees are not confirmed attendees, and names in the
   transcript never become guessed email addresses.
2. Choose **Prepare private sharing copy**. This creates a separate notes-only
   Google Doc in the meeting's Notes folder. Choose **Refresh status** when
   preparation finishes; the same meeting's sharing review stays selected.
3. Choose **Open notes-only copy** and check its notes and listed recipients.
   **Change recipients** opens the editable addresses; **Save recipients for
   review** saves the new list without sharing. Choose **Share view-only** only
   when the copy and addresses are correct. Google may email access invitations.

The shared copy includes the meeting title, date, participants, summary,
decisions, next steps, topics and open questions. It omits the transcript,
recording links and internal sales-analysis sections. Review the text: a summary
can still contain information that should remain private. The existing combined
notes/transcript document and recordings retain their private permissions.

**Keep private** dismisses an unsent review. A prepared private copy remains in
Drive; dismissing a review does not delete it. After sharing starts, the review
shows confirmed recipient access and any unfinished checks. Manage existing
access in Google Drive if you need to revoke it.

**More meeting actions → View activity** shows saved filing and sharing status.
Library labels such as **Saved in Drive** reflect persisted filing evidence, and
sharing details reflect saved permission results. **Refresh status** reloads
those local records; it does not perform a fresh Drive permission check.
**Delete local meeting…**, also under **More meeting actions**, requires a separate
confirmation before moving the entire local meeting and queued copies to macOS
Trash. It leaves existing Drive documents and shared notes in place.

## Calendar suggestions

The lookup uses the connected Google Calendar accounts through local Codex. It
reads visible calendars and events within 90 minutes of the recording's start.
It checks up to 20 calendars, 25 search results per calendar and 10 full event
records, with a 90-second overall deadline. Limits, incomplete attendee lists and
unavailable calendars are shown in the review. The page identifies which calendars
were checked. Lookup runs for completed notes; calendar events never start a
recording.

Automatic lookup is configured in `~/.config/quill/workflow.json` with
`sharing.automatic_calendar_suggestions` and an explicit positive Unix timestamp
in `sharing.automatic_calendar_suggestions_since`. Only meetings created on or
after that timestamp qualify, and only after their native notes publication
succeeds. Turning it on does not search historical meetings. Existing reviews,
edited recipients, manual lookups and dismissed reviews are preserved.

The completion worker saves each request before starting its calendar process.
If that process cannot start, dispatch retries at most three times with a delay.
Worker or app startup can recover an interrupted dispatch. Once a calendar lookup
starts, an interruption requires a manual retry; it is never replayed automatically.
A stranded request becomes retryable in the review page. Disabling automatic
lookup or changing its activation timestamp blocks subsequent automatic reads.
These failures do not retry the completed Drive upload or interrupt recording.

Matching uses the saved recording time, validated note title and identified
participant names. A recording with a saved meeting identifier can also be matched
to its Meet or Zoom link. Current recording metadata does not save that identifier,
so current suggestions rely on time, title and names and remain tentative.
Cancelled events, declined invitees, resources, your document-owner address and
verified own calendar-account addresses are excluded. Each event offers at most
20 addresses; a longer list is marked partial.

Only addresses from a fully read event's attendee list are offered. The lookup
does not infer emails from names, domains or usernames, and sends no transcript
or notes text to Google Calendar. It cannot create, edit, respond to or delete
events. It has no Drive-sharing capability.

Choosing suggestions saves an editable draft with its source event. It does not
prepare a document, grant access or send an invitation. Changing suggestions for
an already prepared copy requires **Save recipients for review** before the final
sharing action becomes available. A source change invalidates the saved lookup.

## Approval and recovery

Sharing has a separate local database, `sharing.sqlite3`, and a separate restricted
Drive transport. The private filing worker cannot grant access. Sharing allows
only individual reader permissions on the saved notes-only copy. There are no
public, company-wide or editing permissions in this workflow.

Each approval binds the source meeting, notes, company review, copy revision and
recipient addresses. Changed sources or an edited copy stop the operation for
review. The copy comes from the pinned blank template, so the original transcript
is never placed in the copy or its revision history. Document content, date chip,
paragraph styles, location, owner and permissions are checked before sharing.

The folder check follows private ancestors to the authenticated My Drive root.
Connectors may return a null parent list for that root. Quill accepts it only
after a separate read-only `root` lookup confirms the same concrete folder ID,
owner and private permissions. Missing ancestry on other folders still stops
sharing. Root aliases remain unavailable to write operations.

Intent and returned IDs are saved before advancing. An interrupted operation
keeps its journal and offers a manual retry, with at most three attempts per
operation. A retry checks known results first. An uncertain invitation is never
sent again unless the connector established that it was not dispatched. If an
uncertain document copy has no saved ID, the workflow holds for manual review
instead of making a duplicate. A stopped review server does not discard decisions;
the separately started operation continues, and its result appears on reopening.

## Validation

Automated tests cover the native template, filtered notes, source changes,
recipient changes, exact confirmation, duplicate submissions, interrupted copies,
partial sharing, permission readback and calendar matching. Calendar tests include
own-account exclusion, rescheduled events, long recordings, partial results and
the separation between suggestions and approval. Automatic-lookup tests cover
completion dispatch, activation boundaries, interrupted starts, duplicate requests,
manual choices and settings changes during lookup. Controlled live checks have
verified notes-only copy preparation and recipient permission readback. Invitation
email delivery and a positive automatic calendar match still need separate live
verification.

[Meeting folders and review](company-routing.md) · [Google Drive filing](codex-drive.md)
