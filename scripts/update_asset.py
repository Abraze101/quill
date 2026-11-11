"""Verify the prebuilt Quill executable for a pinned public release."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import struct
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from xml.parsers.expat import ExpatError


REPO = "Abraze101/quill"
API = "https://api.github.com/repos/" + REPO
SITE = "https://github.com/" + REPO
MAX_BINARY = 128 * 1024 * 1024
ARCHITECTURES = {"arm64": 0x0100000C, "x86_64": 0x01000007}
VERSION = r"(?:0|[1-9][0-9]{0,3})(?:\.(?:0|[1-9][0-9]{0,3})){2}"


class UpdateAssetError(Exception):
    """The release could not be verified or downloaded."""


class _ReleaseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        previous = urllib.parse.urlsplit(req.full_url)
        if (previous.hostname == "api.github.com" or target.scheme != "https"
                or target.netloc not in ("release-assets.githubusercontent.com", "objects.githubusercontent.com")):
            raise UpdateAssetError("GitHub returned an unexpected release download address.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url, accept):
    request = urllib.request.Request(url, headers={"User-Agent": "Quill-Update-Checker", "Accept": accept})
    return urllib.request.build_opener(_ReleaseRedirect()).open(request, timeout=20)


def _chunks(response, limit, deadline):
    size = 0
    while True:
        if time.monotonic() > deadline:
            raise UpdateAssetError("The update download timed out. Try again.")
        block = response.read1(64 * 1024)
        if not block:
            return
        size += len(block)
        if size > limit:
            raise UpdateAssetError("The update exceeded its expected download size.")
        yield block


def _read_metadata(url, accept, limit, deadline):
    with _open(url, accept) as response:
        return b"".join(_chunks(response, limit, deadline))


def tag_revision(version):
    """Resolve the official lightweight or annotated version tag to its commit."""
    if not isinstance(version, str) or not re.fullmatch(VERSION, version):
        raise UpdateAssetError("The selected Quill version is invalid.")
    try:
        data = _read_metadata(API + "/commits/v" + version, "application/vnd.github.sha", 256,
                              time.monotonic() + 25)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateAssetError("This Quill version has no published release yet.") from exc
        raise UpdateAssetError("GitHub could not verify the release tag. Try again later.") from exc
    except (OSError, http.client.HTTPException) as exc:
        raise UpdateAssetError("Couldn’t reach GitHub to verify the release tag.") from exc
    commit = data.strip()
    if not re.fullmatch(b"[0-9a-f]{40}", commit):
        raise UpdateAssetError("GitHub returned an invalid release revision.")
    return commit.decode("ascii")


def _matching_asset(release, tag, architecture):
    name = "quill-macos-" + architecture
    asset_url = SITE + "/releases/download/" + tag + "/" + name
    if (not isinstance(release, dict) or release.get("tag_name") != tag
            or release.get("draft") is not False or release.get("prerelease") is not False
            or not isinstance(release.get("assets"), list)):
        raise UpdateAssetError("GitHub did not return the requested stable Quill release.")
    matches = [a for a in release["assets"] if isinstance(a, dict) and a.get("name") == name]
    if len(matches) != 1:
        raise UpdateAssetError("This Quill release has no installable update for this Mac yet.")
    asset = matches[0]
    digest, size = asset.get("digest"), asset.get("size")
    if (asset.get("browser_download_url") != asset_url or asset.get("state") != "uploaded"
            or type(size) is not int or not 0 < size <= MAX_BINARY
            or not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)):
        raise UpdateAssetError("GitHub could not verify the update file for this Mac.")
    return asset_url, size, digest[7:]


def verify_binary(path, architecture, version, build):
    """Check the publisher-built executable without running it."""
    with Path(path).open("rb") as executable:
        header = executable.read(16)
    # The workflow builds one thin, 64-bit little-endian MH_EXECUTE per asset.
    if len(header) != 16:
        raise UpdateAssetError("The release executable is incomplete.")
    magic, cpu, _, file_type = struct.unpack("<4I", header)
    if magic != 0xFEEDFACF or file_type != 2 or cpu != ARCHITECTURES.get(architecture):
        raise UpdateAssetError("The release executable does not match this Mac’s architecture.")
    # Quill's linker embeds its Info.plist in the __TEXT segment. macOS otool
    # prints the file and section labels on two lines before the plist itself.
    try:
        result = subprocess.run(["/usr/bin/otool", "-P", str(path)], check=True, capture_output=True, timeout=20)
        info = plistlib.loads(result.stdout.split(b"\n", 2)[2].strip())
    except (OSError, ValueError, IndexError, ExpatError, plistlib.InvalidFileException, subprocess.SubprocessError) as exc:
        raise UpdateAssetError("Couldn’t read the version inside the release executable.") from exc
    if (not isinstance(info, dict) or info.get("CFBundleIdentifier") != "com.digimata.quill"
            or info.get("CFBundleShortVersionString") != version or info.get("CFBundleVersion") != build):
        raise UpdateAssetError("The release executable’s version does not match the selected update.")


def download_binary(revision, version, build, destination):
    """Write a verified executable to a new file in the caller's private staging directory."""
    architecture = platform.machine()
    if architecture not in ARCHITECTURES:
        raise UpdateAssetError("No Quill release executable is available for this Mac’s architecture.")
    if not (isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{40}", revision)
            and isinstance(version, str) and re.fullmatch(VERSION, version)
            and isinstance(build, str) and re.fullmatch(r"[1-9][0-9]{0,3}", build)):
        raise UpdateAssetError("The selected update has an invalid version or revision.")
    destination = Path(destination)
    tag = "v" + version
    deadline = time.monotonic() + 180
    try:
        if tag_revision(version) != revision:
            raise UpdateAssetError("The release tag does not match the selected source revision. Check for updates again.")
        raw = _read_metadata(API + "/releases/tags/" + tag, "application/vnd.github+json", 256 * 1024, deadline)
        release = json.loads(raw)
        url, size, expected = _matching_asset(release, tag, architecture)
        with _open(url, "application/octet-stream") as response:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                digest = hashlib.sha256()
                with os.fdopen(descriptor, "wb") as output:
                    for block in _chunks(response, size, deadline):
                        output.write(block)
                        digest.update(block)
                    if output.tell() != size or digest.hexdigest() != expected:
                        raise UpdateAssetError("The downloaded executable differs from the published release.")
                verify_binary(destination, architecture, version, build)
            except BaseException:
                destination.unlink()
                raise
        return expected
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateAssetError("The prebuilt update is not available for this release yet.") from exc
        raise UpdateAssetError("GitHub could not provide the update. Try again later.") from exc
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise UpdateAssetError("Couldn’t download or save the update. Check your connection and available space.") from exc
