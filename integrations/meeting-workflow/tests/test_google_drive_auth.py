"""Private OAuth setup and refresh behavior; fake Google HTTP, no browser or account."""
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quill_workflow.google_drive_auth import (
    ABOUT_URI, AUTH_URI, DRIVE_SCOPE, TOKEN_URI, DriveAuthNeedsConfiguration,
    DriveAuthRetryable, GoogleDriveAuth, _NoRedirect, _callback_code, _json_request,
    authorize,
)


class FakeGoogle:
    def __init__(self):
        self.calls = []
        self.token = "fake-access-token"
        self.account = "owner@example.test"
        self.response = None

    def __call__(self, method, url, body, headers):
        self.calls.append((method, url, body, headers))
        if self.response is not None:
            return self.response
        if (method, url) == ("POST", TOKEN_URI):
            return {"access_token": self.token, "expires_in": 3600,
                    "refresh_token": "fake-refresh-token", "token_type": "Bearer", "scope": DRIVE_SCOPE}
        if (method, url) == ("GET", ABOUT_URI):
            return {"user": {"emailAddress": self.account}}
        raise AssertionError("Unexpected request")


class FakeReceiver:
    redirect_uri = "http://127.0.0.1:12345/oauth/callback"

    def __init__(self, state, timeout):
        self.state = state
        self.timeout = timeout
        self.closed = False

    def wait(self):
        return "fake-auth-code"

    def close(self):
        self.closed = True


class DriveAuthChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.credentials = self.root / "credentials.json"
        self.client = self.root / "client.json"
        self.client_data = {"installed": {"client_id": "test.apps.googleusercontent.com",
                            "client_secret": "fake-client-secret", "auth_uri": AUTH_URI,
                            "token_uri": TOKEN_URI}}
        self.client.write_text(json.dumps(self.client_data))
        self.data = {"type": "authorized_user", "client_id": "test.apps.googleusercontent.com",
                     "client_secret": "fake-client-secret", "refresh_token": "fake-refresh-token",
                     "token_uri": TOKEN_URI, "account_email": "owner@example.test", "scopes": [DRIVE_SCOPE]}
        self.google = FakeGoogle()
        self.messages = []
        self.receivers = []

    def write_credentials(self, data=None):
        self.credentials.write_text(json.dumps(data or self.data))
        self.credentials.chmod(0o600)

    def receiver(self, state, timeout):
        result = FakeReceiver(state, timeout)
        self.receivers.append(result)
        return result

    def setup_auth(self, **options):
        return authorize(self.client, self.credentials, request=self.google,
                         receiver_factory=self.receiver, announce=self.messages.append, **options)

    def test_preflight_is_local_only_and_omits_secrets(self):
        self.write_credentials()
        auth = GoogleDriveAuth(self.credentials, request=self.google)
        self.assertEqual(auth.preflight(), {"email": "owner@example.test", "scopes": [DRIVE_SCOPE],
                                          "client_id": "test.apps.googleusercontent.com"})
        self.assertEqual(self.google.calls, [])

    def test_missing_credentials_do_not_discover_others(self):
        with self.assertRaises(DriveAuthNeedsConfiguration):
            GoogleDriveAuth(self.credentials, request=self.google).preflight()
        self.assertEqual(self.google.calls, [])

    def test_nonprivate_symlink_and_hardlink_credentials_rejected(self):
        self.write_credentials()
        self.credentials.chmod(0o644)
        auth = GoogleDriveAuth(self.credentials, request=self.google)
        with self.assertRaises(DriveAuthNeedsConfiguration):
            auth.access_token()
        self.credentials.chmod(0o600)
        linked = self.root / "linked.json"
        linked.symlink_to(self.credentials)
        with self.assertRaises(DriveAuthNeedsConfiguration):
            GoogleDriveAuth(linked, request=self.google).preflight()
        linked.unlink()
        os.link(self.credentials, linked)
        with self.assertRaises(DriveAuthNeedsConfiguration):
            auth.preflight()
        self.assertEqual(self.google.calls, [])

    def test_bounded_regular_json_and_owned_credentials_required(self):
        for content in ("[]", "{bad", "x" * 65537):
            self.credentials.write_text(content)
            self.credentials.chmod(0o600)
            with self.assertRaises(DriveAuthNeedsConfiguration):
                GoogleDriveAuth(self.credentials).preflight()
        self.write_credentials()
        with patch("quill_workflow.google_drive_auth.os.getuid", return_value=os.getuid() + 1):
            with self.assertRaises(DriveAuthNeedsConfiguration):
                GoogleDriveAuth(self.credentials).preflight()

    def test_credential_fields_rejected_without_network(self):
        cases = [{"type": "service_account"}, {"scopes": []}, {"scopes": ["drive"]},
                 {"token_uri": "https://attacker.invalid/token"}, {"refresh_token": "bad\nvalue"},
                 {"account_email": "invalid"}, {"client_id": None}]
        for changes in cases:
            with self.subTest(changes=changes):
                self.write_credentials(dict(self.data, **changes))
                with self.assertRaises(DriveAuthNeedsConfiguration):
                    GoogleDriveAuth(self.credentials, request=self.google).access_token()
        self.assertEqual(self.google.calls, [])

    def test_cache_expiry_and_refresh_uses_explicit_credentials(self):
        self.write_credentials()
        now = [1000]
        auth = GoogleDriveAuth(self.credentials, request=self.google, clock=lambda: now[0])
        self.assertEqual(auth.access_token(), "fake-access-token")
        self.assertEqual(auth.access_token(), "fake-access-token")
        self.assertEqual(len(self.google.calls), 1)
        values = urllib.parse.parse_qs(self.google.calls[0][2].decode())
        self.assertEqual(values["grant_type"], ["refresh_token"])
        self.assertEqual(values["refresh_token"], [self.data["refresh_token"]])
        self.assertEqual(values["client_id"], [self.data["client_id"]])
        now[0] += 3540
        self.google.token = "next-access-token"
        self.assertEqual(auth.access_token(), "next-access-token")
        self.assertEqual(len(self.google.calls), 2)

    def test_cache_invalidated_by_account_change(self):
        self.write_credentials()
        auth = GoogleDriveAuth(self.credentials, request=self.google)
        auth.access_token()
        self.write_credentials(dict(self.data, account_email="different@example.test"))
        auth.access_token()
        self.assertEqual(len(self.google.calls), 2)

    def test_credentials_changed_during_refresh_rejected(self):
        self.write_credentials()
        def request(*args):
            self.write_credentials(dict(self.data, account_email="other@example.test"))
            return self.google(*args)
        with self.assertRaisesRegex(DriveAuthNeedsConfiguration, "changed"):
            GoogleDriveAuth(self.credentials, request=request).access_token()

    def test_bad_token_responses_rejected(self):
        self.write_credentials()
        for response in ({}, {"access_token": "secret", "expires_in": True},
                         {"access_token": "secret", "expires_in": float("inf")},
                         {"access_token": "secret", "expires_in": 3600, "token_type": 123},
                         {"access_token": "secret\n", "expires_in": 3600},
                         {"access_token": "secret", "expires_in": 3600, "scope": "email"}):
            with self.subTest(response=response):
                self.google.response = response
                with self.assertRaises(DriveAuthNeedsConfiguration) as caught:
                    GoogleDriveAuth(self.credentials, request=self.google).access_token()
                self.assertNotIn("secret", str(caught.exception))

    def test_setup_pkce_account_and_private_atomic_file(self):
        result = self.setup_auth(expected_email="owner@example.test")
        self.assertEqual(result["account_email"], "owner@example.test")
        saved = json.loads(self.credentials.read_text())
        self.assertEqual(saved, self.data)
        self.assertEqual(self.credentials.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("access_token", saved)
        self.assertTrue(self.receivers[0].closed)
        self.assertIn("delete all Drive files", self.messages[0])
        url = urllib.parse.urlsplit(self.messages[-1])
        self.assertEqual(url.netloc, "accounts.google.com")
        query = urllib.parse.parse_qs(url.query)
        self.assertEqual(query["state"], [self.receivers[0].state])
        self.assertEqual(query["scope"], [DRIVE_SCOPE])
        self.assertEqual(query["access_type"], ["offline"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        sent = urllib.parse.parse_qs(self.google.calls[0][2].decode())
        challenge = base64.urlsafe_b64encode(hashlib.sha256(sent["code_verifier"][0].encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(query["code_challenge"], [challenge])
        self.assertEqual(self.google.calls[1][1], ABOUT_URI)
        self.assertEqual(self.google.calls[1][3]["Authorization"], "Bearer fake-access-token")
        for secret in ("fake-access-token", "fake-refresh-token", "fake-client-secret", "fake-auth-code"):
            self.assertNotIn(secret, "\n".join(self.messages))
        self.assertEqual(list(self.root.glob(".quill-oauth-*")), [])

    def test_setup_never_overwrites_different_account_without_choice(self):
        self.write_credentials()
        before = self.credentials.read_bytes()
        self.google.account = "another@example.test"
        with self.assertRaisesRegex(DriveAuthNeedsConfiguration, "replace-account"):
            self.setup_auth()
        self.assertEqual(self.credentials.read_bytes(), before)
        self.setup_auth(replace_account=True)
        self.assertEqual(json.loads(self.credentials.read_text())["account_email"], "another@example.test")

    def test_setup_expected_account_mismatch_saves_nothing(self):
        with self.assertRaisesRegex(DriveAuthNeedsConfiguration, "does not match"):
            self.setup_auth(expected_email="wrong@example.test")
        self.assertFalse(self.credentials.exists())

    def test_bad_desktop_client_does_not_start_setup(self):
        for data in ({"web": self.client_data["installed"]},
                     {"installed": dict(self.client_data["installed"], token_uri="https://attacker.invalid/")},
                     {"installed": dict(self.client_data["installed"], auth_uri="https://attacker.invalid/")}):
            self.client.write_text(json.dumps(data))
            with self.assertRaises(DriveAuthNeedsConfiguration):
                self.setup_auth()
        self.assertEqual(self.receivers, [])
        self.assertEqual(self.google.calls, [])

    def test_setup_rechecks_credentials_before_replacing(self):
        self.write_credentials()
        def receiver(state, timeout):
            result = self.receiver(state, timeout)
            original_wait = result.wait
            def changed():
                self.write_credentials(dict(self.data, refresh_token="another-refresh-token"))
                return original_wait()
            result.wait = changed
            return result
        with self.assertRaisesRegex(DriveAuthNeedsConfiguration, "changed during setup"):
            authorize(self.client, self.credentials, request=self.google, receiver_factory=receiver,
                      announce=self.messages.append)
        self.assertEqual(json.loads(self.credentials.read_text())["refresh_token"], "another-refresh-token")

    def test_setup_missing_offline_token_saves_nothing(self):
        self.google.response = {"access_token": "fake-access-token", "expires_in": 3600}
        with self.assertRaisesRegex(DriveAuthNeedsConfiguration, "refresh token"):
            self.setup_auth()
        self.assertFalse(self.credentials.exists())
        self.assertTrue(self.receivers[0].closed)

    def test_callback_accepts_only_exact_path_state_and_single_code(self):
        self.assertEqual(_callback_code("/oauth/callback?state=expected&code=abc", "expected"), "abc")
        cases = ["/wrong?state=expected&code=abc", "/oauth/callback?state=wrong&code=abc",
                 "/oauth/callback?state=expected&state=expected&code=abc", "/oauth/callback?state=expected",
                 "/oauth/callback?state=expected&code=a&code=b", "/oauth/callback?state=expected&code=",
                 "https://attacker.invalid/oauth/callback?state=expected&code=abc",
                 "/oauth/callback?state=%E2%82%AC&code=abc", "/oauth/callback?state=expected&error=access_denied"]
        for path in cases:
            with self.subTest(path=path), self.assertRaises(DriveAuthNeedsConfiguration):
                _callback_code(path, "expected")

    def test_redirects_and_unexpected_endpoints_rejected(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, None, None, "https://attacker.invalid"))
        with self.assertRaisesRegex(DriveAuthNeedsConfiguration, "endpoint"):
            _json_request("POST", "https://attacker.invalid", b"secret", {})

    def test_http_failures_are_sanitized_and_retry_classified(self):
        for code in (301, 400, 401, 429, 503):
            failure = urllib.error.HTTPError(TOKEN_URI, code, "private-secret", {}, None)
            with self.subTest(code=code), patch("quill_workflow.google_drive_auth.urllib.request.build_opener") as builder:
                builder.return_value.open.side_effect = failure
                expected = DriveAuthRetryable if code in (429, 503) else DriveAuthNeedsConfiguration
                with self.assertRaises(expected) as caught:
                    _json_request("POST", TOKEN_URI, b"private-secret", {})
                self.assertNotIn("private-secret", str(caught.exception))

    def test_setup_timeout_closes_receiver_and_writes_nothing(self):
        def receiver(state, timeout):
            result = self.receiver(state, timeout)
            result.wait = lambda: (_ for _ in ()).throw(DriveAuthNeedsConfiguration("timed out"))
            return result
        with self.assertRaisesRegex(DriveAuthNeedsConfiguration, "timed out"):
            authorize(self.client, self.credentials, request=self.google, receiver_factory=receiver,
                      announce=self.messages.append)
        self.assertTrue(self.receivers[0].closed)
        self.assertFalse(self.credentials.exists())

    def test_concurrent_setup_cannot_replace_credentials(self):
        self.write_credentials()
        before = self.credentials.read_bytes()
        descriptor = os.open(str(self.credentials) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(DriveAuthNeedsConfiguration, "Another Quill authorization"):
                self.setup_auth()
        finally:
            os.close(descriptor)
        self.assertEqual(self.credentials.read_bytes(), before)

    def test_unsafe_parent_cannot_receive_new_credentials(self):
        self.root.chmod(0o777)
        try:
            with self.assertRaisesRegex(DriveAuthNeedsConfiguration, "only its owner"):
                self.setup_auth()
        finally:
            self.root.chmod(0o700)
        self.assertFalse(self.credentials.exists())


if __name__ == "__main__":
    unittest.main()
