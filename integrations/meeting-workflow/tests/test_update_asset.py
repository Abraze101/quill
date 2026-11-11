import hashlib
import io
import json
from pathlib import Path
import plistlib
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
import update_asset as assets


class ReleaseAssetTests(unittest.TestCase):
    version = "0.1.18"
    build = "21"
    sha = "bc" * 20
    body = struct.pack("<4I", 0xFEEDFACF, assets.ARCHITECTURES["arm64"], 0, 2) + b"fixture"
    digest = hashlib.sha256(body).hexdigest()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.target = Path(temp.name) / "quill"
        host = patch.object(assets.platform, "machine", return_value="arm64")
        host.start()
        self.addCleanup(host.stop)
        self.tag = "v" + self.version
        self.url = f"{assets.SITE}/releases/download/{self.tag}/quill-macos-arm64"
        self.asset = dict(name="quill-macos-arm64", state="uploaded", digest="sha256:" + self.digest,
                          size=len(self.body), browser_download_url=self.url)
        self.release = dict(tag_name=self.tag, draft=False, prerelease=False, assets=[self.asset])
        self.plist = {"CFBundleIdentifier": "com.digimata.quill", "CFBundleShortVersionString": self.version,
                      "CFBundleVersion": self.build}

    def download(self, *, commit=None, data=None):
        content = {f"{assets.API}/commits/{self.tag}": (commit or self.sha).encode(),
                   f"{assets.API}/releases/tags/{self.tag}": json.dumps(self.release).encode(),
                   self.url: self.body if data is None else data}
        def fetch(url, accept):
            return io.BytesIO(content[url])
        native = subprocess.CompletedProcess([], 0, b"fixture:\n(__TEXT,__info_plist) section\n" +
                                             plistlib.dumps(self.plist))
        with patch.object(assets, "_open", side_effect=fetch), patch.object(assets.subprocess, "run", return_value=native):
            return assets.download_binary(self.sha, self.version, self.build, self.target)

    def test_verified_executable_and_private_staging_file(self):
        self.assertEqual(self.download(), self.digest)
        self.assertEqual(self.target.read_bytes(), self.body)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)

    def test_unpublished_or_stale_tag(self):
        for value in ("not-a-sha", "ee" * 20):
            with self.subTest(commit=value), self.assertRaises(assets.UpdateAssetError):
                self.download(commit=value)
        for field in ("draft", "prerelease"):
            self.release[field] = True
            with self.subTest(field=field), self.assertRaises(assets.UpdateAssetError):
                self.download()
            self.release[field] = False
        self.assertFalse(self.target.exists())

    def test_unverified_duplicate_or_missing_architecture(self):
        self.release["assets"] = [self.asset, dict(self.asset)]
        with self.assertRaises(assets.UpdateAssetError):
            self.download()
        self.release["assets"] = [self.asset]
        for key, bad in (("name", "quill-macos-x86_64"), ("browser_download_url", "https://example.org/file"),
                         ("size", assets.MAX_BINARY + 1), ("size", True), ("state", "new"), ("digest", "")):
            old = self.asset[key]
            self.asset[key] = bad
            with self.subTest(key=key), self.assertRaises(assets.UpdateAssetError):
                self.download()
            self.asset[key] = old
        self.assertFalse(self.target.exists())

    def test_corrupt_incomplete_oversized_or_wrong_architecture(self):
        for body in (self.body[:-1], self.body + b"1", b"a" * len(self.body)):
            with self.subTest(size=len(body)), self.assertRaises(assets.UpdateAssetError):
                self.download(data=body)
            self.assertFalse(self.target.exists())
        wrong = struct.pack("<4I", 0xFEEDFACF, assets.ARCHITECTURES["x86_64"], 0, 2)
        self.asset.update(digest="sha256:" + hashlib.sha256(wrong).hexdigest(), size=len(wrong))
        with self.assertRaisesRegex(assets.UpdateAssetError, "architecture"):
            self.download(data=wrong)
        self.assertFalse(self.target.exists())

    def test_embedded_build_mismatch_or_other_app(self):
        for key in self.plist:
            old = self.plist[key]
            self.plist[key] = "other"
            with self.subTest(key=key), self.assertRaises(assets.UpdateAssetError):
                self.download()
            self.assertFalse(self.target.exists())
            self.plist[key] = old

    def test_existing_file_survives(self):
        self.target.write_bytes(b"other file")
        with self.assertRaises(assets.UpdateAssetError):
            self.download()
        self.assertEqual(self.target.read_bytes(), b"other file")

    def test_redirects_restricted_to_github_asset_hosts(self):
        redirect = assets._ReleaseRedirect()
        source = assets.urllib.request.Request(self.url)
        for url in ("http://release-assets.githubusercontent.com/file", "https://example.com/file",
                    "https://release-assets.githubusercontent.com@evil.example/file"):
            with self.subTest(url=url), self.assertRaises(assets.UpdateAssetError):
                redirect.redirect_request(source, None, 302, "", {}, url)
        result = redirect.redirect_request(source, None, 302, "", {}, "https://release-assets.githubusercontent.com/file")
        self.assertEqual(result.host, "release-assets.githubusercontent.com")
        metadata = assets.urllib.request.Request(assets.API + "/releases")
        with self.assertRaises(assets.UpdateAssetError):
            redirect.redirect_request(metadata, None, 302, "", {}, "https://release-assets.githubusercontent.com/file")


if __name__ == "__main__":
    unittest.main()
