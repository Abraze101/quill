"""Local company routing decisions; this module never reads recordings or uploads.

Pass the exact transcript.json bytes to ``propose`` and ``route``. A caller must
use ``route`` against its current transcript/config before consuming a decision.
Approval only chooses from the company map frozen in the displayed review.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid


class RoutingError(ValueError):
    pass


class StaleReviewError(RoutingError):
    pass


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _job(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        raise RoutingError("Expected a fixed job ID")
    return value


def validate_companies(companies):
    """Return a canonical allowlist. Folder names and paths are never accepted."""
    if not isinstance(companies, list) or not 1 <= len(companies) <= 100:
        raise RoutingError("Configure between one and 100 company folders")
    normalized, ids, folders = [], set(), set()
    for company in companies:
        if not isinstance(company, dict) or set(company) - {"id", "name", "drive_folder_id", "description", "aliases"}:
            raise RoutingError("Company entries contain unsupported fields")
        company_id, name, folder = (company.get(key) for key in ("id", "name", "drive_folder_id"))
        description = company.get("description", "")
        if not isinstance(company_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", company_id):
            raise RoutingError("Company IDs must be fixed lowercase identifiers")
        if not isinstance(name, str) or not name.strip() or len(name) > 200 or name != name.strip():
            raise RoutingError("Company names must be nonempty and bounded")
        if not isinstance(folder, str) or not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", folder):
            raise RoutingError("Use a Google Drive folder ID, not a URL or path")
        if not isinstance(description, str) or len(description) > 8000:
            raise RoutingError("Company descriptions must be bounded text")
        aliases = company.get("aliases", [])
        if not isinstance(aliases, list) or len(aliases) > 20:
            raise RoutingError("Company aliases must be a list of at most 20 names")
        if any(not isinstance(alias, str) or not alias or alias != alias.strip()
               or len(alias) > 200 or len(alias.casefold()) > 200
               or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in alias)
               for alias in aliases):
            raise RoutingError("Company aliases must be nonempty bounded names without control characters")
        if company_id in ids or folder in folders:
            raise RoutingError("Company IDs and Drive folder IDs must be unique")
        ids.add(company_id)
        folders.add(folder)
        entry = {"id": company_id, "name": name, "drive_folder_id": folder,
                 "description": description}
        aliases = sorted({alias.casefold() for alias in aliases} - {name.casefold()})
        # Omit empty aliases to preserve the exact canonical shape and hashes of
        # existing maps. Reordering or repeating aliases does not revise a map.
        if aliases:
            entry["aliases"] = aliases
        normalized.append(entry)
    return sorted(normalized, key=lambda company: company["id"])


def preserves_company_map(current, frozen):
    """Allow new destinations without changing an existing job's allowlist.

    Only compare live configuration with a frozen job here. Saved review maps,
    hashes and receipts still require exact equality. Every original entry,
    including its description and aliases, must remain available. New aliases
    only affect new jobs; they never revise a frozen map or grant approval.
    """
    current_by_id = {company["id"]: company for company in validate_companies(current)}
    for company in validate_companies(frozen):
        live = current_by_id.get(company["id"])
        if live is None or any(live[key] != company[key]
                               for key in ("id", "name", "drive_folder_id", "description")):
            return False
        if not set(company.get("aliases", [])).issubset(live.get("aliases", [])):
            return False
    return True


def transcript_identity(transcript):
    """Hash exact source bytes, never a reformatted or model-produced transcript."""
    if not isinstance(transcript, bytes) or not 0 < len(transcript) <= 20_000_000:
        raise RoutingError("Pass bounded transcript.json bytes")
    try:
        decoded = json.loads(transcript.decode("utf-8"))
    except (ValueError, UnicodeError) as error:
        raise RoutingError("Transcript must be UTF-8 JSON") from error
    segments = decoded.get("segments") if isinstance(decoded, dict) else None
    if not isinstance(segments, list) or not segments:
        raise RoutingError("Transcript must contain segments")
    if any(not isinstance(segment, dict) or not isinstance(segment.get("text"), str)
           for segment in segments):
        raise RoutingError("Transcript segments must contain text")
    return hashlib.sha256(transcript).hexdigest(), len(segments)


def validate_suggestion(suggestion, segment_count):
    keys = {"schema_version", "company_id", "confidence", "reason", "evidence", "ambiguous"}
    if not isinstance(suggestion, dict):
        raise RoutingError("Routing suggestion must be an object")
    version = suggestion.get("schema_version")
    if type(version) is not int or version not in (1, 2):
        raise RoutingError("Routing suggestion must use schema_version 1 or 2")
    if version == 2:
        keys.add("explicit_basis")
    if set(suggestion) != keys:
        raise RoutingError("Routing suggestion must contain only the defined schema fields")
    company_id = suggestion["company_id"]
    if company_id is not None and (not isinstance(company_id, str)
                                  or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", company_id)):
        raise RoutingError("Suggested company must be a fixed ID or null")
    if suggestion["confidence"] not in ("high", "medium", "low"):
        raise RoutingError("Confidence must be high, medium, or low")
    reason = suggestion["reason"]
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 8000:
        raise RoutingError("A bounded routing reason is required")
    if type(suggestion["ambiguous"]) is not bool:
        raise RoutingError("Ambiguous must be a boolean")
    evidence = suggestion["evidence"]
    if not isinstance(evidence, list) or len(evidence) > segment_count:
        raise RoutingError("Evidence must be a list of transcript segment indices")
    if any(type(index) is not int or not 0 <= index < segment_count for index in evidence):
        raise RoutingError("Evidence contains an invalid transcript segment index")
    if len(set(evidence)) != len(evidence):
        raise RoutingError("Evidence indices must be unique")
    basis = suggestion.get("explicit_basis")
    if basis is not None:
        if not isinstance(basis, dict) or set(basis) != {"kind", "segment_index", "quote"}:
            raise RoutingError("Explicit routing evidence must use the defined schema")
        if basis["kind"] not in ("ownership", "filing_request"):
            raise RoutingError("Unknown explicit routing evidence kind")
        if type(basis["segment_index"]) is not int or basis["segment_index"] not in evidence:
            raise RoutingError("Explicit routing evidence must cite a supporting segment")
        if not isinstance(basis["quote"], str) or not basis["quote"].strip() or len(basis["quote"]) > 10000:
            raise RoutingError("Explicit routing evidence must include bounded verbatim text")
    return dict(suggestion, evidence=sorted(evidence))


def validate_explicit_source(suggestion, transcript):
    """Reject invented or shortened quotes, including omitted leading negations."""
    basis = suggestion.get("explicit_basis")
    if basis is not None:
        if transcript["segments"][basis["segment_index"]]["text"] != basis["quote"]:
            raise RoutingError("Explicit routing quote must match its entire transcript segment exactly")


_CAUTION = re.compile(
    r"\b(?:not|never|no|don't|doesn't|isn't|wasn't|won't|wouldn't|cannot|can't|"
    r"if|unless|maybe|perhaps|might|could|would|hypothetical|example|imagine|"
    r"customer|client|vendor|competitor|quote|quoted|said|says|say|told|"
    r"pretend|test|testing|demo|joke|joking|actually|instead|correction|"
    r"repeat|reading|script|roleplay)\b", re.I)
_INJECTION = re.compile(
    r"\b(?:ignore|override|bypass|disregard)\b.{0,100}\b(?:instructions?|rules?|approval|review|system|policy)\b"
    r"|\b(?:system prompt|developer message|schema_version|company_id|explicit_basis)\b", re.I | re.S)


def _names(company):
    # Only names in this job's validated, frozen configuration can match. Alias
    # collisions across companies remain ambiguous in _mentions.
    return (company["name"].casefold(), *company.get("aliases", []))


def _mentions(text, companies):
    return {company["id"] for company in companies
            if any(re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, re.I)
                   for name in _names(company))}


def _explicit_statement(text, company, kind):
    """Conservative affirmative grammar, in addition to the model's assessment.

    No arbitrary keyword/industry match grants permission. Unsupported phrasing
    remains a normal suggestion that the user can review.
    """
    text = " ".join(text.replace("’", "'").split()).strip()
    if _CAUTION.search(text) or _INJECTION.search(text) or any(mark in text for mark in ('"', "“", "”", "?", "`")):
        return False
    name = "(?:" + "|".join(re.escape(value) for value in _names(company)) + ")"
    subject = r"(?:this|today's|our) (?:meeting|call|discussion|session)"
    if kind == "filing_request":
        pattern = (r"(?:please |let's |i want to )?(?:file|save|store) "
                   r"(?:this|today's|our) (?:meeting|recording|notes|transcript)"
                   r"(?: and (?:notes|transcript|recording))? (?:under|in|to) (?:the )?"
                   + name + r"(?: folder)?")
    else:
        own_work = r"(?:own (?:[a-z0-9-]+\s+){0,10}(?:work|plan|planning|operations|business|project|projects|team|strategy|launch|recruitment))"
        pattern = (r"(?:" + subject + r" (?:is for|belongs to|is owned by) " + name
                   + r"|this is (?:a |an )?" + name + r" (?:meeting|call|discussion|session)"
                   + r"|" + subject + r" (?:is about|concerns|covers) " + name + r"'s " + own_work + r")")
    # Whisper may put a declaration and its agenda in the same segment. Preserve
    # and vet that full segment above, then accept a complete affirmative sentence
    # with a small discourse prefix, never a clause extracted from a qualification.
    for sentence in re.split(r"[.!]\s*", text):
        sentence = re.sub(r"^(?:okay|ok|so|all right|alright|to confirm),?\s+", "", sentence.strip(), flags=re.I)
        if re.fullmatch(pattern, sentence, re.I):
            return True
    return False


def explicit_filing_basis(suggestion, transcript, companies):
    """Return source-bound permission only for an unambiguous company statement."""
    basis = suggestion.get("explicit_basis")
    selected = suggestion["company_id"]
    if (basis is None or selected in (None, "general") or suggestion["confidence"] != "high"
            or suggestion["ambiguous"] or not suggestion["evidence"]):
        return None
    company = next((company for company in companies if company["id"] == selected), None)
    if company is None:
        return None
    validate_explicit_source(suggestion, transcript)
    segments = [segment["text"] for segment in transcript["segments"]]
    # Context can span transcription segments. A quoted instruction, conditional,
    # denial, or competing company beside the quote always returns to review.
    index = basis["segment_index"]
    context = " ".join(segments[max(0, index - 1):index + 2]).replace("’", "'")
    evidence = " ".join(segments[index] for index in suggestion["evidence"])
    if (_CAUTION.search(context) or any(mark in context for mark in ('"', "“", "”", "?", "`"))
            or _mentions(evidence, companies) != {selected}
            or len(_mentions(context, companies)) > 1
            or _INJECTION.search(" ".join(segments))
            or not _explicit_statement(basis["quote"], company, basis["kind"])):
        return None
    for text in segments:
        mentioned = _mentions(text, companies)
        if mentioned and _CAUTION.search(text.replace("’", "'")) and re.search(
                r"\b(?:meeting|call|discussion|session|file|save|store|folder)\b", text, re.I):
            return None
        # Conflicting declarations elsewhere cannot be hidden by choosing one quote.
        for other in companies:
            if other["id"] != selected:
                if any(_explicit_statement(text, other, kind) for kind in ("ownership", "filing_request")):
                    return None
                if other["id"] in mentioned and re.search(
                        r"\b(?:owns?|owned|belongs?|work|projects?|operations|strategy|plans?|planning|team)\b", text, re.I):
                    return None
    return dict(basis)


def _route(companies, company_id):
    for company in companies:
        if company["id"] == company_id:
            return {"company_id": company_id, "company_name": company["name"],
                    "drive_folder_id": company["drive_folder_id"]}
    raise RoutingError("Choose a company from the frozen folder allowlist")


class RoutingReviews:
    """Durable reviews with atomic decisions and revision-checked retries."""

    def __init__(self, state):
        self.state = Path(state).expanduser()
        if self.state.is_symlink():
            raise RoutingError("Review state must not be a symlink")
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state.chmod(0o700)
        path = self.state / "reviews.sqlite3"
        # The file is private from creation, including before SQLite connects.
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS routing_reviews (job TEXT PRIMARY KEY, record TEXT NOT NULL)")
        self.db.commit()

    def close(self):
        self.db.close()

    def get(self, job):
        row = self.db.execute("SELECT record FROM routing_reviews WHERE job=?", (_job(job),)).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, status=None):
        if status is not None and status not in ("pending", "approved", "skipped"):
            raise RoutingError("Unknown review status")
        from .local_deletion import deleted_jobs
        removed = deleted_jobs(self.state.absolute())
        records = [json.loads(row[0]) for row in self.db.execute("SELECT record FROM routing_reviews")]
        records = [record for record in records if record["job"] not in removed]
        return sorted((record for record in records if status is None or record["status"] == status),
                      key=lambda record: (record["created_at"], record["job"]))

    def pending(self):
        return self.list("pending")

    def _save(self, record):
        self.db.execute("INSERT OR REPLACE INTO routing_reviews VALUES (?, ?)",
                        (record["job"], _json(record)))
        return record

    def propose(self, job, transcript, companies, suggestion, review_required=True,
                auto_file_explicit_companies=False):
        job = _job(job)
        transcript_hash, count = transcript_identity(transcript)
        companies = validate_companies(companies)
        company_hash = _digest(companies)
        suggestion = validate_suggestion(suggestion, count)
        decoded = json.loads(transcript)
        validate_explicit_source(suggestion, decoded)
        if type(review_required) is not bool or type(auto_file_explicit_companies) is not bool:
            raise RoutingError("Review policy must be a boolean")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            previous = self.get(job)
            same_provenance = previous and (previous["transcript_sha256"], previous["company_map_sha256"]) == (
                transcript_hash, company_hash)
            # Model retries cannot overrule a human decision or release a held
            # review just because the automatic-approval setting has changed.
            if same_provenance and (previous["status"] != "pending" or previous["suggestion"] == suggestion):
                self.db.commit()
                return previous
            now = time.time()
            explicit = (explicit_filing_basis(suggestion, decoded, companies)
                        if auto_file_explicit_companies else None)
            automatic = (previous is None and (bool(explicit) if auto_file_explicit_companies else not review_required)
                         and suggestion["confidence"] == "high"
                         and not suggestion["ambiguous"] and bool(suggestion["evidence"])
                         and suggestion["company_id"] in {company["id"] for company in companies})
            record = {"schema_version": 1, "job": job, "status": "approved" if automatic else "pending",
                      "transcript_sha256": transcript_hash, "company_map_sha256": company_hash,
                      "companies": companies, "suggestion": suggestion, "revision": uuid.uuid4().hex,
                      "approved_route": _route(companies, suggestion["company_id"]) if automatic else None,
                      "review_required": review_required, "decision_from_revision": None,
                      "auto_file_explicit_companies": auto_file_explicit_companies,
                      "automatic_basis": explicit if automatic else None,
                      "decision_source": ("automatic_explicit" if explicit else "automatic") if automatic else None,
                      "created_at": previous["created_at"] if previous else now, "updated_at": now}
            self._save(record)
            self.db.commit()
            return record
        except Exception:
            self.db.rollback()
            raise

    def _decide(self, job, expected_revision, company_id=None, skip=False):
        from .local_deletion import deleted_jobs
        if job in deleted_jobs(self.state.absolute()):
            raise RoutingError("This local meeting was deleted; its review is closed")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            record = self.get(job)
            if record is None:
                raise RoutingError("Review does not exist")
            status = "skipped" if skip else "approved"
            duplicate = (record["status"] == status and
                         (skip or record["approved_route"]["company_id"] == company_id))
            accepted_revisions = [record["revision"]]
            if duplicate and record["decision_from_revision"] is not None:
                accepted_revisions.append(record["decision_from_revision"])
            if not isinstance(expected_revision, str) or expected_revision not in accepted_revisions:
                raise StaleReviewError("This review has changed; reload it before deciding")
            if duplicate:
                self.db.commit()
                return record
            if record["status"] != "pending":
                raise RoutingError("This review already has a decision")
            companies = validate_companies(record["companies"])
            if _digest(companies) != record["company_map_sha256"]:
                raise StaleReviewError("The frozen company mapping has changed")
            record["approved_route"] = None if skip else _route(companies, company_id)
            record.update(status=status, decision_source="user", decision_from_revision=record["revision"],
                          revision=uuid.uuid4().hex, updated_at=time.time())
            self._save(record)
            self.db.commit()
            return record
        except Exception:
            self.db.rollback()
            raise

    def approve(self, job, company_id, expected_revision):
        return self._decide(job, expected_revision, company_id=company_id)

    def skip(self, job, expected_revision):
        return self._decide(job, expected_revision, skip=True)

    def route(self, job, transcript, companies):
        """Return a permitted destination only when its source and map still match."""
        transcript_hash, _ = transcript_identity(transcript)
        companies = validate_companies(companies)
        record = self.get(job)
        if record is None:
            return None
        if (record["transcript_sha256"], record["company_map_sha256"]) != (transcript_hash, _digest(companies)):
            raise StaleReviewError("Transcript or company folders changed; a new review is required")
        if record["status"] != "approved":
            return None
        # Resolve from the caller's validated allowlist; never accept a model's
        # destination or return an unverified folder copied out of a receipt.
        return _route(companies, record["approved_route"]["company_id"])
