"""Match separated speech to bounded observations from the meeting interface.

The platform identifies a participant/device, not a biometric identity. Shared
microphones and conflicting voices therefore remain suggestions. This module is
pure: it neither reads files nor changes the original transcript.
"""
from bisect import bisect_right
import copy
import json
import re

from .meeting_context import participant_name

MAX_TIMELINE_BYTES = 5_000_000
MAX_RECORDING_MS = 86_400_000


def validate_timeline(value):
    if (not isinstance(value, dict)
            or set(value) != {"schema_version", "provider", "key", "participants", "intervals", "truncated"}
            or type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or type(value.get("truncated")) is not bool):
        raise ValueError("Invalid meeting speaker timeline")
    if not isinstance(value.get("provider"), str):
        raise ValueError("Invalid speaker timeline meeting")
    pattern = {"google-meet": r"[a-z]{3}-[a-z]{4}-[a-z]{3}", "zoom-web": r"[0-9]{9,12}"}.get(value["provider"])
    if pattern is None or not isinstance(value.get("key"), str) or not re.fullmatch(pattern, value["key"]):
        raise ValueError("Invalid speaker timeline meeting")
    participants, intervals = value.get("participants"), value.get("intervals")
    if (not isinstance(participants, list) or len(participants) > 100
            or not isinstance(intervals, list) or len(intervals) > 20_000):
        raise ValueError("Meeting speaker timeline is too large")
    identities = set()
    for person in participants:
        if (not isinstance(person, dict) or set(person) != {"id", "name", "self"}
                or not isinstance(person.get("id"), str)
                or not re.fullmatch(r"[A-Za-z0-9:_-]{1,160}", person["id"])
                or person["id"] in identities or type(person.get("self")) is not bool):
            raise ValueError("Invalid speaker timeline participant")
        participant_name(person.get("name"))
        identities.add(person["id"])
    previous_end = 0
    for interval in intervals:
        if (not isinstance(interval, dict) or set(interval) != {"start_ms", "end_ms", "active"}
                or type(interval.get("start_ms")) is not int or type(interval.get("end_ms")) is not int
                or not previous_end <= interval["start_ms"] < interval["end_ms"] <= MAX_RECORDING_MS
                or not isinstance(interval.get("active"), list) or len(interval["active"]) > 4
                or any(not isinstance(identity, str) or identity not in identities for identity in interval["active"])
                or len(set(interval["active"])) != len(interval["active"])):
            raise ValueError("Invalid or overlapping speaker timeline intervals")
        previous_end = interval["end_ms"]
    if len(json.dumps(value, ensure_ascii=False).encode()) > MAX_TIMELINE_BYTES:
        raise ValueError("Meeting speaker timeline is too large")
    return copy.deepcopy(value)


def _duration(ranges):
    """Union duration prevents overlapping transcript fragments multiplying proof."""
    result, end = 0, -1
    for left, right in sorted(ranges):
        result += max(0, right - max(left, end))
        end = max(end, right)
    return result


def _name_key(value):
    return " ".join(value.casefold().split())


def _representative_evidence(indices, segment_turns, segments, limit=100):
    """Bound report citations without reducing the speech checked for a match."""
    ordered = sorted(indices, key=lambda index: (segments[index]["start_ms"], index))
    if len(ordered) <= limit:
        return sorted(ordered)

    def spaced(items, count):
        if count == 1:
            return [items[len(items) // 2]]
        return [items[index * (len(items) - 1) // (count - 1)] for index in range(count)]

    turns = {}
    for index in ordered:
        turns.setdefault(segment_turns[index], index)
    representatives = list(turns.values())
    if len(representatives) >= limit:
        return sorted(spaced(representatives, limit))
    selected = set(representatives) | {ordered[0], ordered[-1]}
    remaining = [index for index in ordered if index not in selected]
    count = limit - len(selected)
    if count:
        selected.update(spaced(remaining, count))
    return sorted(selected)


def match_timeline(transcript, timeline):
    """Return deterministic matches and separately marked device suggestions.

    A match requires three separate turns, at least three seconds of clean
    observations, 80% coverage of that voice's speech, and 95% support among
    observed speech. Any conflicting active person or shared-device voice blocks
    automatic naming. Self-device evidence alone never identifies a shared mic.
    """
    from .transcript_sources import introduction_candidates, speaker_attributions
    timeline = validate_timeline(timeline)
    people = speaker_attributions(transcript)
    result = {"matches": {}, "suggestions": {},
              "local_participants": [dict(person) for person in timeline["participants"] if person["self"]]}
    if people is None or not timeline["intervals"]:
        return result
    participants = {person["id"]: person for person in timeline["participants"]}
    intervals = timeline["intervals"]
    starts = [item["start_ms"] for item in intervals]
    segments = transcript["segments"]
    introductions = introduction_candidates(segments, policy="explicit_intro_v3")
    timed = []
    for index, segment in enumerate(segments):
        start, end = segment.get("start_ms"), segment.get("end_ms")
        if (type(start) is not int or type(end) is not int
                or not 0 <= start < end <= MAX_RECORDING_MS):
            # Missing timing prevents a complete voice-level coverage check.
            return result
        if segment["text"].strip():
            timed.append((start, end, index, segment["speaker"]))
    timed.sort()
    stats = {identity: {"ranges": [], "support": {}, "conflict": False, "refs": {}, "turns": {}}
             for identity in people}
    # Remove all overlapping speech, including unidentified speech and the
    # other audio track. A meeting's active-speaker signal cannot untangle it.
    competing = {index: [] for _, _, index, _ in timed}
    open_segments = []
    for start, end, index, identity in timed:
        open_segments = [item for item in open_segments if item[1] > start]
        if len(open_segments) > 200:
            return result
        for other_start, other_end, other_index, other_identity in open_segments:
            if other_identity != identity or identity == "unknown":
                overlap = (start, min(end, other_end))
                competing[index].append(overlap)
                competing[other_index].append(overlap)
        open_segments.append((start, end, index, identity))
    previous_identity, previous_end, turn = None, -1, -1
    segment_turns = {}
    compared = 0
    for start, end, index, identity in timed:
        if identity != previous_identity or start - previous_end > 1000:
            turn += 1
        previous_identity, previous_end = identity, max(end, previous_end)
        segment_turns[index] = turn
        if identity not in stats:
            continue
        stat = stats[identity]
        stat["ranges"].append((start, end))
        at = max(0, bisect_right(starts, start) - 1)
        while at < len(intervals) and intervals[at]["start_ms"] < end:
            compared += 1
            if compared > 2_000_000:
                return result
            interval = intervals[at]
            at += 1
            left, right = max(start, interval["start_ms"]), min(end, interval["end_ms"])
            if right <= left:
                continue
            active = interval["active"]
            if len(active) > 1:
                stat["conflict"] = True
                continue
            if not active:
                continue
            participant = participants[active[0]]
            if participant["self"] != (people[identity]["source"] == "mic"):
                stat["conflict"] = True
                continue
            # Count only portions that are not overlapped by another voice.
            blocked = [(max(left, a), min(right, b)) for a, b in competing[index] if a < right and b > left]
            if blocked:
                stat["conflict"] = True
                continue
            stat["support"].setdefault(active[0], []).append((left, right))
            stat["refs"].setdefault(active[0], set()).add(index)
            stat["turns"].setdefault(active[0], set()).add(turn)
    proposals = {}
    claims = {}
    for identity, stat in stats.items():
        weights = {participant: _duration(ranges) for participant, ranges in stat["support"].items()}
        # Even a short second voice on this participant/device is evidence of
        # a shared stream or fragmented voice labels, not a second confirmation.
        for participant, weight in weights.items():
            if weight >= 500:
                claims.setdefault(participant, set()).add(identity)
        if not weights:
            continue
        winner = max(weights, key=weights.get)
        person = participants[winner]
        total, supported = _duration(stat["ranges"]), weights[winner]
        if (supported < 3000 or supported < total * .8 or supported < sum(weights.values()) * .95
                or len(stat["turns"][winner]) < 3):
            continue
        # UI names longer than the name contract remain available as candidates,
        # but cannot silently alter the established identity format.
        from .speaker_review import _name
        try:
            _name(person["name"])
        except ValueError:
            continue
        proposals[identity] = {"name": person["name"], "participant_id": winner,
                               "evidence": _representative_evidence(stat["refs"][winner], segment_turns, segments),
                               "self": person["self"]}
    name_counts = {}
    for person in participants.values():
        name_counts.setdefault(_name_key(person["name"]), set()).add(person["id"])
    for identity, proposal in proposals.items():
        stat, person = stats[identity], participants[proposal["participant_id"]]
        automatic = (not timeline["truncated"] and not person["self"] and not stat["conflict"]
                     and len(stat["support"]) == 1 and len(claims.get(person["id"], set())) == 1
                     and len(name_counts[_name_key(person["name"])]) == 1
                     and not any(_name_key(name) == _name_key(person["name"])
                                 for other, names in introductions.items() if other != identity for name in names))
        # A platform name cannot replace a different reliable introduction.
        existing = people[identity].get("name")
        if existing is not None or identity in introductions:
            continue
        if automatic:
            proposal["reason"] = "Matched to repeated speaking activity from this meeting participant."
            result["matches"][identity] = proposal
        else:
            proposal["reason"] = ("This is your meeting participant; a shared microphone still needs voice-specific evidence."
                                  if person["self"] else "Meeting activity suggests this participant, but the device or speech is ambiguous.")
            result["suggestions"][identity] = proposal
    return result
