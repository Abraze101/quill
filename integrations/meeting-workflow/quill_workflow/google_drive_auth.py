"""Explicit, private Google OAuth credentials for Quill's large-file uploader.

This module never discovers or borrows credentials from another application.
Tokens stay out of stdout, errors, workflow receipts, and model prompts.
"""
import base64
import fcntl
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import os
from pathlib import Path
import secrets
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from .pipeline import NeedsConfiguration, WorkflowError

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
ABOUT_URI = "https://www.googleapis.com/drive/v3/about?fields=user(emailAddress)"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
MAX_JSON_BYTES = 64 * 1024


class DriveAuthNeedsConfiguration(NeedsConfiguration):
    pass


class DriveAuthRetryable(WorkflowError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _json_request(method, url, body, headers):
    """Only Google's fixed OAuth and account-read endpoints, with no redirects."""
    if (method, url) not in {("POST", TOKEN_URI), ("GET", ABOUT_URI)}:
        raise DriveAuthNeedsConfiguration("Unexpected Google authorization endpoint")
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(urllib.request.Request(url, data=body, headers=headers, method=method),
                         timeout=30) as response:
            payload = response.read(MAX_JSON_BYTES + 1)
        if len(payload) > MAX_JSON_BYTES:
            raise ValueError("oversized response")
        result = json.loads(payload)
        if not isinstance(result, dict):
            raise ValueError("invalid response")
        return result
    except urllib.error.HTTPError as error:
        if error.code == 429 or error.code >= 500:
            raise DriveAuthRetryable("Google authorization is temporarily unavailable; retry later") from None
        raise DriveAuthNeedsConfiguration("Google authorization was rejected; reconnect Quill's large-file access") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise DriveAuthRetryable("Could not reach Google authorization; retry when online") from None
    except (ValueError, UnicodeError):
        raise DriveAuthNeedsConfiguration("Google returned an invalid authorization response") from None


def _read_json(path, *, private):
    """Bounded, owned regular files only; do not follow a credential symlink."""
    path = Path(path).expanduser()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            before = os.fstat(handle.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or before.st_size > MAX_JSON_BYTES or before.st_nlink != 1
                    or private and before.st_mode & 0o077):
                raise ValueError("unsafe file")
            raw = handle.read(MAX_JSON_BYTES + 1)
            after = os.fstat(handle.fileno())
        current = path.lstat()
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
        if (len(raw) > MAX_JSON_BYTES or identity(before) != identity(after)
                or identity(after) != identity(current)):
            raise ValueError("changed file")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("invalid JSON")
        return result, hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError, UnicodeError):
        kind = "credentials" if private else "Desktop client JSON"
        detail = " with owner-only permissions (0600)" if private else ""
        raise DriveAuthNeedsConfiguration("Quill needs a readable, owned, regular " + kind + detail) from None


def _text(value, field, limit=8192):
    if (not isinstance(value, str) or not value or len(value) > limit
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise DriveAuthNeedsConfiguration("Google authorization has an invalid " + field)
    return value


def _email(value):
    value = _text(value, "account email", 320)
    if value.count("@") != 1 or any(char.isspace() for char in value):
        raise DriveAuthNeedsConfiguration("Google did not identify the selected account")
    local, domain = value.split("@")
    if not local or not domain:
        raise DriveAuthNeedsConfiguration("Google did not identify the selected account")
    return value


def _validate_credentials(data):
    if data.get("type") != "authorized_user" or data.get("token_uri", TOKEN_URI) != TOKEN_URI:
        raise DriveAuthNeedsConfiguration("Expected Quill's Google authorized-user credentials")
    scopes = data.get("scopes")
    if scopes != [DRIVE_SCOPE]:
        raise DriveAuthNeedsConfiguration("Quill large-file access needs the approved Google Drive scope")
    return {"type": "authorized_user", "token_uri": TOKEN_URI,
            "client_id": _text(data.get("client_id"), "client ID", 1024),
            "client_secret": _text(data.get("client_secret"), "client secret"),
            "refresh_token": _text(data.get("refresh_token"), "refresh token"),
            "account_email": _email(data.get("account_email")), "scopes": [DRIVE_SCOPE]}


def _validate_client(data):
    client = data.get("installed")
    if not isinstance(client, dict) or "web" in data:
        raise DriveAuthNeedsConfiguration("Choose a Google OAuth Desktop app client JSON")
    # Google downloads have used both of these official authorization URIs.
    if (client.get("auth_uri") not in {AUTH_URI, "https://accounts.google.com/o/oauth2/auth"}
            or client.get("token_uri") != TOKEN_URI):
        raise DriveAuthNeedsConfiguration("Desktop client JSON contains unexpected Google endpoints")
    return {"client_id": _text(client.get("client_id"), "client ID", 1024),
            "client_secret": _text(client.get("client_secret"), "client secret")}


def _token(result):
    if (not isinstance(result, dict) or not isinstance(result.get("token_type", "Bearer"), str)
            or result.get("token_type", "Bearer").lower() != "bearer"):
        raise DriveAuthNeedsConfiguration("Google returned an invalid access token")
    token = _text(result.get("access_token"), "access token")
    expires = result.get("expires_in")
    if (isinstance(expires, bool) or not isinstance(expires, (int, float))
            or not math.isfinite(expires) or not 0 < expires <= 7 * 24 * 3600):
        raise DriveAuthNeedsConfiguration("Google returned an invalid token expiry")
    scope = result.get("scope")
    if scope is not None and (not isinstance(scope, str) or DRIVE_SCOPE not in scope.split()):
        raise DriveAuthNeedsConfiguration("Google Drive access was not granted")
    return token, expires


def _post_token(request, values):
    return request("POST", TOKEN_URI, urllib.parse.urlencode(values).encode("ascii"),
                   {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})


class GoogleDriveAuth:
    def __init__(self, credentials_path, *, request=None, clock=time.time):
        self.credentials_path = Path(credentials_path).expanduser()
        self.request = request or _json_request
        self.clock = clock
        self._cached = None
        self._expires = 0
        self._identity = None

    def preflight(self):
        """Validate only local setup before folder writes; never authenticate."""
        data, _ = _read_json(self.credentials_path, private=True)
        credentials = _validate_credentials(data)
        return {"email": credentials["account_email"],
                **{key: credentials[key] for key in ("scopes", "client_id")}}

    def access_token(self):
        data, identity = _read_json(self.credentials_path, private=True)
        credentials = _validate_credentials(data)
        if self._cached is not None and self._identity == identity and self.clock() < self._expires:
            return self._cached
        result = _post_token(self.request, {"grant_type": "refresh_token",
                            **{key: credentials[key] for key in ("client_id", "client_secret", "refresh_token")}})
        token, expires = _token(result)
        # Reject a refresh whose credential file was replaced while in flight.
        _, current_identity = _read_json(self.credentials_path, private=True)
        if current_identity != identity:
            raise DriveAuthNeedsConfiguration("Quill's Google account changed during authorization; retry")
        self._identity = identity
        self._cached = token
        self._expires = self.clock() + max(0, expires - 60)
        return token


def _callback_code(path, expected_state):
    parsed = urllib.parse.urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.path != "/oauth/callback" or parsed.fragment or len(path) > 16_384:
        raise DriveAuthNeedsConfiguration("Unexpected authorization callback")
    try:
        values = urllib.parse.parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True,
                                      max_num_fields=20)
    except ValueError:
        raise DriveAuthNeedsConfiguration("Invalid authorization callback") from None
    state = values.get("state", [])
    if len(state) != 1 or not hmac.compare_digest(state[0].encode(), expected_state.encode()):
        raise DriveAuthNeedsConfiguration("Authorization callback state did not match")
    if "error" in values:
        raise DriveAuthNeedsConfiguration("Google Drive access was not approved")
    code = values.get("code", [])
    if len(code) != 1:
        raise DriveAuthNeedsConfiguration("Google returned no authorization code")
    return _text(code[0], "authorization code", 4096)


class _LoopbackReceiver:
    def __init__(self, state, timeout=300):
        self.state = state
        self.timeout = timeout
        self.code = None
        self.error = None
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                valid = False
                try:
                    if self.headers.get("Host") != receiver.host:
                        raise DriveAuthNeedsConfiguration("Unexpected callback host")
                    receiver.code = _callback_code(self.path, receiver.state)
                    valid = True
                except DriveAuthNeedsConfiguration as error:
                    # Bad requests cannot cancel somebody else's live OAuth flow.
                    if str(error) == "Google Drive access was not approved":
                        receiver.error = error
                body = ("Google approval received. Return to Quill setup to confirm the connection." if valid else
                        "Quill could not accept this authorization response. Return to setup.").encode()
                self.send_response(200 if valid else 400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)

        class BoundedHTTPServer(HTTPServer):
            def get_request(self):
                connection, address = super().get_request()
                connection.settimeout(5)
                return connection, address

            def handle_error(self, request, client_address):
                # A disconnected callback client must not emit request traces.
                pass

        self.server = BoundedHTTPServer(("127.0.0.1", 0), Handler)
        self.server.timeout = 1
        self.host = "127.0.0.1:" + str(self.server.server_port)
        self.redirect_uri = "http://" + self.host + "/oauth/callback"

    def wait(self):
        deadline = time.monotonic() + self.timeout
        while self.code is None and self.error is None and time.monotonic() < deadline:
            self.server.handle_request()
        if self.error:
            raise self.error
        if self.code is None:
            raise DriveAuthNeedsConfiguration("Google authorization timed out; run setup again when ready")
        return self.code

    def close(self):
        self.server.server_close()


def _save_credentials(path, data, *, replace_account=False, expected_identity=None):
    path = Path(path).expanduser()
    if not path.is_absolute():
        raise DriveAuthNeedsConfiguration("Use an absolute path for Quill's credentials")
    validated = _validate_credentials(data)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise DriveAuthNeedsConfiguration("Credentials need a real local directory")
    parent = path.parent.stat()
    if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
        raise DriveAuthNeedsConfiguration("Credentials need a directory only its owner can modify")
    # A stable advisory lock serializes concurrent setup without unlink/inode races.
    descriptor = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        lock = os.fstat(descriptor)
        if (not stat.S_ISREG(lock.st_mode) or lock.st_uid != os.getuid()
                or lock.st_nlink != 1 or lock.st_mode & 0o077):
            raise DriveAuthNeedsConfiguration("Quill's authorization lock is not private")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DriveAuthNeedsConfiguration("Another Quill authorization is saving credentials; retry setup") from None
        # Recheck after locking and just before replacement.
        if path.exists() or path.is_symlink():
            old, identity = _read_json(path, private=True)
            previous = _validate_credentials(old)
            if identity != expected_identity:
                raise DriveAuthNeedsConfiguration("Credentials changed during setup; nothing was replaced")
            if previous["account_email"].casefold() != validated["account_email"].casefold() and not replace_account:
                raise DriveAuthNeedsConfiguration("A different Google account is already connected; use --replace-account explicitly")
        elif expected_identity is not None:
            raise DriveAuthNeedsConfiguration("Credentials changed during setup; nothing was replaced")
        temporary_fd, temporary = tempfile.mkstemp(prefix=".quill-oauth-", dir=path.parent)
        try:
            with os.fdopen(temporary_fd, "w") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(validated, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    finally:
        os.close(descriptor)


def authorize(client_path, credentials_path, *, replace_account=False, expected_email=None,
              timeout=300, request=None, announce=print, receiver_factory=None):
    """Explicit setup only. Prints an approval URL, never opens a browser."""
    client, _ = _read_json(client_path, private=False)
    client = _validate_client(client)
    path = Path(credentials_path).expanduser()
    if not path.is_absolute():
        raise DriveAuthNeedsConfiguration("Use an absolute path for Quill's credentials")
    previous_identity = None
    if path.exists() or path.is_symlink():
        previous, previous_identity = _read_json(path, private=True)
        _validate_credentials(previous)
    if expected_email is not None:
        expected_email = _email(expected_email)
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 1 <= timeout <= 1800:
        raise DriveAuthNeedsConfiguration("Authorization timeout must be between 1 and 1800 seconds")
    request = request or _json_request
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    receiver = (receiver_factory or _LoopbackReceiver)(state, timeout)
    try:
        parameters = {"client_id": client["client_id"], "redirect_uri": receiver.redirect_uri,
                      "response_type": "code", "scope": DRIVE_SCOPE, "state": state,
                      "code_challenge": challenge, "code_challenge_method": "S256",
                      "access_type": "offline", "prompt": "consent select_account"}
        if expected_email:
            parameters["login_hint"] = expected_email
        announce("This setup requests Google Drive access to read, create, modify and delete all Drive files. "
                 "The broad scope lets Quill use your existing meeting folders; its uploader only files approved recordings "
                 "and does not share or delete files. No model API is involved.")
        announce("Open this Google approval URL in your browser and select the same account as your connected Drive:")
        announce(AUTH_URI + "?" + urllib.parse.urlencode(parameters))
        code = receiver.wait()
        result = _post_token(request, {"grant_type": "authorization_code", "code": code,
                             "redirect_uri": receiver.redirect_uri, "code_verifier": verifier, **client})
        token, _ = _token(result)
        refresh = _text(result.get("refresh_token"), "refresh token")
        about = request("GET", ABOUT_URI, None, {"Authorization": "Bearer " + token, "Accept": "application/json"})
        user = about.get("user") if isinstance(about, dict) else None
        account = _email(user.get("emailAddress") if isinstance(user, dict) else None)
        if expected_email and account.casefold() != expected_email.casefold():
            raise DriveAuthNeedsConfiguration("The selected Google account does not match --account; nothing was saved")
        data = {"type": "authorized_user", "token_uri": TOKEN_URI, **client, "refresh_token": refresh,
                "account_email": account, "scopes": [DRIVE_SCOPE]}
        _save_credentials(path, data, replace_account=replace_account, expected_identity=previous_identity)
        return {"account_email": account, "credentials_path": str(path), "scopes": [DRIVE_SCOPE]}
    finally:
        receiver.close()
