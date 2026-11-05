"""Local, reversible speaker corrections with source and stale-form checks."""
import copy
import hashlib
import json
import os
import tempfile
from pathlib import Path
import re
import time

from .speaker_identity import attach, empty_bundle, validate_bundle, validate_corrections
from .transcript_sources import effective_attributions, render_reviewed_transcript, transcript_digest


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def load_bundle(sources, job, transcript, notes=None):
    bundle = copy.deepcopy((notes or {}).get("_speaker_identity") or empty_bundle(transcript))
    saved = sources.json(f"results/{job}/speakers/review.json", 500_000)
    if saved is not None:
        validate_bundle(saved, transcript)
        bundle = saved
    corrections = sources.json(f"results/{job}/speakers/corrections.json", 100_000)
    if corrections is not None:
        bundle["corrections"] = validate_corrections(corrections, transcript)
    return validate_bundle(bundle, transcript)


class SpeakerCorrections:
    def __init__(self, state):
        self.state = Path(state).expanduser().absolute()

    def learned(self):
        from .review_library import _Sources
        from .speaker_review import validate_context
        sources = _Sources(self.state)
        try:
            value = sources.json("speaker-identities.json", 200_000)
        finally:
            sources.close()
        if value is None:
            return []
        # Reuse the bounded identity grammar. No voiceprints or transcript text
        # enter this store, and only explicit remember actions write it.
        checked = validate_context(value)
        if any(p["source"] != "confirmed_correction" or not p["email"] for p in checked["candidates"]):
            raise ValueError("Saved speaker identities need review")
        return checked["candidates"]

    def _snapshot(self, job):
        from .review_library import _Sources, _transcript, _notes
        from .local_deletion import stopped_jobs
        if not isinstance(job, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", job):
            raise ValueError("Choose a saved meeting")
        if job in stopped_jobs(self.state, job):
            raise ValueError("Processing is stopped for this meeting")
        sources = _Sources(self.state)
        try:
            jobs, tasks, removed = sources.rows()
            if job in removed or not any(row["id"] == job for row in jobs):
                raise ValueError("This meeting is no longer available")
            transcript, raw_hash, _, _ = _transcript(sources, job)
            if transcript is None:
                raise ValueError("A completed transcript is needed to review speakers")
            notes, _ = _notes(sources, job, tasks.get(job, {}), transcript, raw_hash)
            bundle = load_bundle(sources, job, transcript, notes)
            revision = _digest({"job": job, "source": transcript_digest(transcript), "bundle": bundle,
                                "notes": notes, "learned": self.learned()})
            return transcript, notes, bundle, revision
        finally:
            sources.close()

    def options(self, job):
        try:
            transcript, notes, bundle, revision = self._snapshot(job)
            people = effective_attributions(transcript, attach(notes, transcript, bundle)) or {}
            learned = self.learned()
            result = []
            for identity, person in people.items():
                samples = [s["text"] for s in transcript["segments"] if s["speaker"] == identity]
                # Longer excerpts make a voice easier to recognize than an isolated 'yes'.
                samples = sorted(samples, key=len, reverse=True)[:3]
                result.append({"id": identity, "label": person["label"],
                    "name": person.get("name") or person.get("suggested_name") or "",
                    "email": person.get("email") or "",
                    "status": person.get("review_status") or ("confirmed" if person.get("name") else
                        "suggested" if person.get("suggested_name") else "unresolved"),
                    "samples": [s[:350] for s in samples], "reason": person.get("review_reason", ""),
                    "remembered": bool(person.get("email") and any(
                        p["email"].casefold() == person["email"].casefold() for p in learned))})
            from .speaker_notes import read_status
            status = read_status(self.state, job) or {}
            if status.get("state") == "succeeded":
                from .speaker_notes import load_reviewed_notes
                from .review_library import _Sources
                sources = _Sources(self.state)
                try:
                    if load_reviewed_notes(sources, job, transcript, notes, bundle) is None:
                        status = {"state": "stale", "error": None}
                finally:
                    sources.close()
            return {"job": job, "revision": revision, "speakers": result,
                    "notes_status": status.get("state"), "notes_error": status.get("error"),
                    "candidates": bundle["context"]["candidates"],
                    "unassigned_segments": sum(s["speaker"] == "unknown" for s in transcript["segments"])}
        except (OSError, ValueError):
            return None

    def _directory(self, job):
        parent = self.state
        for part in ("results", job, "speakers"):
            path = parent / part
            path.mkdir(mode=0o700, exist_ok=True)
            if path.is_symlink() or not path.is_dir():
                raise ValueError("Speaker output must be a local directory")
            parent = path
        return parent

    def _write(self, job, transcript, notes, bundle):
        from .pipeline import atomic_json, render_notes
        rendered = attach(notes, transcript, bundle)
        outputs = [("transcript-reviewed.md", render_reviewed_transcript(transcript, rendered)),
                   ("notes-reviewed.md", render_notes(rendered, transcript) if notes else "")]
        directory = self._directory(job)
        # Append-only snapshots allow a mistaken correction to be reconstructed.
        history = directory / "history"
        history.mkdir(mode=0o700, exist_ok=True)
        if history.is_symlink():
            raise ValueError("Speaker history must be a local directory")
        atomic_json(history / (_digest(bundle) + ".json"), bundle)
        atomic_json(directory / "corrections.json", bundle["corrections"])
        # Separate filenames leave original source and filed artifacts intact.
        from .pipeline import safe_member
        for name, text in outputs:
            if (directory / name).is_symlink():
                raise ValueError("Speaker output must be a regular local file")
            fd, temporary = tempfile.mkstemp(prefix=".speaker-", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(text)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, directory / name)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def _learn(self, name, email, *, forget=False):
        from .local_deletion import _lock
        from .pipeline import atomic_json
        from .recipient_suggestions import _email
        if _email(email) is None:
            raise ValueError("Add an email to remember this identity")
        with _lock(self.state / "speaker-identities.lock"):
            people = self.learned()
            old = next((p for p in people if p["email"].casefold() == email.casefold()), None)
            people = [p for p in people if p["email"].casefold() != email.casefold()]
            if not forget:
                aliases = sorted(set((old or {}).get("aliases", []) + ([old["name"]] if old else [])) - {name})
                people.append({"name": name, "email": email, "source": "confirmed_correction", "aliases": aliases[:10]})
            if len(people) > 100:
                raise ValueError("The saved identity list is full; forget an unused identity first")
            atomic_json(self.state / "speaker-identities.json", {"schema_version": 1, "candidates": people})

    def decision(self, action, job, revision, speaker_id, name="", email="", remember=False):
        from .local_deletion import job_lock
        with job_lock(self.state, job):
            transcript, notes, bundle, current = self._snapshot(job)
            if revision != current:
                raise ValueError("This speaker review changed; reload before saving")
            people = effective_attributions(transcript, attach(notes, transcript, bundle)) or {}
            if speaker_id not in people:
                raise ValueError("Choose a separated voice from this transcript")
            if action == "speaker_save":
                person = {"name": name.strip(), "email": email.strip() or None, "status": "user_confirmed"}
                bundle["corrections"]["assignments"][speaker_id] = person
                validate_corrections(bundle["corrections"], transcript)
                if remember and not person["email"]:
                    raise ValueError("Add an email to remember this identity")
                self._write(job, transcript, notes, bundle)
                if remember:
                    self._learn(person["name"], person["email"])
            elif action == "speaker_clear":
                bundle["corrections"]["assignments"].pop(speaker_id, None)
                self._write(job, transcript, notes, bundle)
            elif action == "speaker_forget":
                person = people[speaker_id]
                self._learn(person.get("name") or person.get("suggested_name"), person.get("email"), forget=True)
            else:
                raise ValueError("Unknown speaker action")
        return self.options(job)

    def save_review(self, job, bundle, revision):
        from .local_deletion import job_lock
        from .pipeline import atomic_json
        with job_lock(self.state, job):
            transcript, notes, current, current_revision = self._snapshot(job)
            if revision != current_revision:
                raise ValueError("This meeting changed during speaker review")
            validate_bundle(bundle, transcript)
            bundle = copy.deepcopy(bundle)
            bundle["corrections"] = current["corrections"]
            directory = self._directory(job)
            self._write(job, transcript, notes, bundle)
            atomic_json(directory / "review.json", bundle)
        return self.options(job)
