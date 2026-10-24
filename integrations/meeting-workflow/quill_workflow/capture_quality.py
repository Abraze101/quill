"""Bounded capture evidence and a fixed, deterministic reader notice.

Device errors and caller-supplied warning prose never become export content.
An absent field is the legacy contract, not proof of a complete recording.
"""

NOTICE = "Some audio was interrupted or could not be captured. The transcript and notes may be incomplete."
_SOURCES = ("mic", "system")


def validated_quality(transcript):
    """Return canonical capture evidence, or None for unchanged legacy input."""
    if not isinstance(transcript, dict):
        raise ValueError("Transcript must be an object")
    if "capture_quality" not in transcript:
        return None
    quality = transcript["capture_quality"]
    if (not isinstance(quality, dict) or set(quality) != {"status", "affected_sources"}
            or quality.get("status") != "incomplete"):
        raise ValueError("Capture quality must contain only incomplete status and affected sources")
    sources = quality.get("affected_sources")
    if (not isinstance(sources, list) or not 1 <= len(sources) <= 2
            or any(not isinstance(source, str) or source not in _SOURCES for source in sources)):
        raise ValueError("Capture quality needs one or two microphone or system sources")
    return {"status": "incomplete", "affected_sources": [source for source in _SOURCES if source in sources]}


def notice(transcript):
    return NOTICE if validated_quality(transcript) is not None else None


def validate_note_notice(notes, transcript):
    """Keep the fixed notice outside model-authored findings and their evidence."""
    if validated_quality(transcript) is None:
        return

    def contains(value):
        if isinstance(value, str):
            return NOTICE in value
        if isinstance(value, dict):
            return any(contains(item) for item in value.values())
        if isinstance(value, list):
            return any(contains(item) for item in value)
        return False

    if contains(notes):
        raise ValueError("Omit the capture-quality notice from note fields; the exporter adds it once")


def with_summary_notice(sections, transcript):
    """Keep native notes-only sharing and combined documents equally explicit."""
    warning = notice(transcript)
    return dict(sections, SUMMARY=warning + "\n\n" + sections["SUMMARY"]) if warning else sections
