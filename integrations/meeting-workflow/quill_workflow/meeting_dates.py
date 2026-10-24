"""Use the timezone saved at recording start, never the upload machine's location."""
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def meeting_zone(name):
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Meeting timezone must be a saved IANA timezone")
    try:
        return ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise ValueError("Meeting timezone is unknown; review the recording metadata") from error


def recording_timezone(metadata):
    """Return a validated snapshot, or None for recordings made by older builds.

    Missing historical information is not inferred from today's location. A
    present but invalid value must be reviewed rather than silently replaced.
    """
    if "recording_timezone" not in metadata:
        return None
    name = metadata["recording_timezone"]
    zone = meeting_zone(name)
    try:
        started = datetime.fromisoformat(metadata["started"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, ValueError) as error:
        raise ValueError("Recording needs a valid start timestamp") from error
    if started.tzinfo is None:
        raise ValueError("Recording start time needs an explicit timezone")
    if "recording_utc_offset_seconds" in metadata:
        offset = metadata["recording_utc_offset_seconds"]
        if type(offset) is not int or offset != int(started.astimezone(zone).utcoffset().total_seconds()):
            raise ValueError("Recording timezone disagrees with its saved UTC offset")
    return name
