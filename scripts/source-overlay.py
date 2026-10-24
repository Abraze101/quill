#!/usr/bin/env python3
"""Export local Quill source changes and replay them in a separate directory.

Reads Git objects and working files; never commits, fetches, checks out, or installs.
The bundle contains source, not recordings, runtime settings or signing material.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import tempfile


SCHEMA = 'quill-source-overlay-v1'
ROOT_FILES = {'.gitignore', 'LICENSE', 'Package.swift', 'Package.resolved', 'README.md'}
SOURCE_DIRS = {'Sources', 'Tests', 'docs', 'integrations', 'scripts', '.issues', '.github'}
PRIVATE_NAMES = {'auth.json', 'credentials.json', 'secrets.json', 'config.json',
                 'workflow.json', 'signing.json', 'drive-folders.json'}
EXCLUDED_DIRS = {'.git', '.build', '.config', '.local', '__pycache__', 'node_modules',
                 'recordings', 'state', 'work', 'task-work', 'outputs'}
EXCLUDED_SUFFIXES = {'.caf', '.m4a', '.mp4', '.mkv', '.wav', '.mp3', '.mov', '.sqlite',
                     '.sqlite3', '.db', '.pem', '.key', '.p12', '.pfx', '.pyc'}
FILE_LIMIT = 50 * 1024 * 1024


class OverlayError(Exception):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def source_path(value):
    if not isinstance(value, str) or not value or '\\' in value or any(ord(c) < 32 for c in value):
        return False
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(p in ('.', '..') for p in path.parts):
        return False
    if value not in ROOT_FILES and (len(path.parts) < 2 or path.parts[0] not in SOURCE_DIRS):
        return False
    return (not any(part.lower() in EXCLUDED_DIRS for part in path.parts)
            and path.name.lower() not in PRIVATE_NAMES
            and not path.name.lower().startswith('.env')
            and path.suffix.lower() not in EXCLUDED_SUFFIXES)


def git(root, *args):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS='0')
    result = subprocess.run(['git', '--no-optional-locks', '-C', str(root), *args],
                            capture_output=True, env=env)
    if result.returncode:
        raise OverlayError('Git source operation failed: ' + result.stderr.decode(errors='replace').strip())
    return result.stdout


def commit_id(root, ref):
    return git(root, 'rev-parse', '--verify', '--end-of-options', ref + '^{commit}').decode().strip()


def metadata(data, mode):
    return {'sha256': sha(data), 'size': len(data), 'mode': mode}


def tree(root, commit):
    files, blobs = {}, {}
    for item in git(root, 'ls-tree', '-r', '--full-tree', '-z', commit).split(b'\0'):
        if not item:
            continue
        prefix, raw_path = item.split(b'\t', 1)
        mode, kind, object_id = prefix.decode().split()
        path = raw_path.decode()
        if kind != 'blob' or mode not in ('100644', '100755') or not source_path(path):
            raise OverlayError('Tracked path is outside the ordinary source inventory: ' + path)
        data = git(root, 'cat-file', 'blob', object_id)
        if len(data) > FILE_LIMIT:
            raise OverlayError('Source file exceeds the export size limit: ' + path)
        record = metadata(data, int(mode, 8) & 0o777)
        files[path] = record
        blobs[record['sha256']] = data
    return files, blobs


def read_source(root, name):
    path = root / name
    if any(parent.is_symlink() for parent in [path, *path.parents] if parent != root.parent):
        raise OverlayError('Source symlinks are not exported: ' + name)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'rb') as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > FILE_LIMIT:
            raise OverlayError('Expected a bounded ordinary source file: ' + name)
        data = handle.read(FILE_LIMIT + 1)
        after = os.fstat(handle.fileno())
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_mode)
    if identity(before) != identity(after) or len(data) != before.st_size:
        raise OverlayError('Source changed during export: ' + name)
    return metadata(data, stat.S_IMODE(before.st_mode)), data


def inventory(root, base):
    indexed = {p.decode() for p in git(root, 'ls-files', '--cached', '-z').split(b'\0') if p}
    others = {p.decode() for p in git(root, 'ls-files', '--others', '--exclude-standard', '-z').split(b'\0') if p}
    if others & set(base):
        raise OverlayError('An untracked path replaces a base file; resolve that index state before export')
    if any(not source_path(p) for p in indexed):
        raise OverlayError('The index contains paths outside the source inventory')
    untracked = sorted(p for p in others if source_path(p))
    files, blobs = {}, {}
    for name in sorted(set(base) | indexed | set(untracked)):
        result = read_source(root, name)
        if result is None:
            continue
        record, data = result
        files[name], blobs[record['sha256']] = record, data
    return files, blobs, untracked, sorted(others - set(untracked))


def patch_bytes(root, base):
    return git(root, 'diff', '--binary', '--full-index', '--no-ext-diff', '--no-textconv',
               '--no-renames', base, '--')


def publish(staging, destination):
    # mkdir is exclusive: never replace an existing file, directory or symlink.
    destination.mkdir(mode=0o700)
    for child in staging.iterdir():
        child.rename(destination / child.name)


def output_path(value):
    path = Path(value).absolute()
    return path.parent.resolve() / path.name


def export(root, destination):
    root, destination = Path(root).resolve(), output_path(destination)
    if root in destination.parents or destination == root:
        raise OverlayError('Write the source bundle outside the checkout')
    if destination.exists() or destination.is_symlink():
        raise OverlayError('Export destination already exists')
    base_commit = commit_id(root, 'HEAD')
    base, blobs = tree(root, base_commit)
    current, working_blobs, untracked, excluded = inventory(root, base)
    patch = patch_bytes(root, base_commit)
    again, _, again_untracked, again_excluded = inventory(root, base)
    if (current != again or untracked != again_untracked or excluded != again_excluded
            or commit_id(root, 'HEAD') != base_commit or patch_bytes(root, base_commit) != patch):
        raise OverlayError('Checkout changed during export; finish edits and retry')
    blobs.update(working_blobs)
    manifest = {'schema': SCHEMA, 'created_at': datetime.now(timezone.utc).isoformat(),
                'base_commit': base_commit, 'base_files': base, 'working_files': current,
                'untracked_paths': untracked, 'excluded_untracked_paths': excluded,
                'deleted_paths': sorted(set(base) - set(current)), 'patch_sha256': sha(patch)}
    with tempfile.TemporaryDirectory(prefix='.quill-export-', dir=destination.parent) as temp:
        staging = Path(temp)
        (staging / 'objects').mkdir(mode=0o700)
        for digest, data in blobs.items():
            (staging / 'objects' / digest).write_bytes(data)
        (staging / 'tracked.patch').write_bytes(patch)
        (staging / 'manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
        # Prove that the patch and additions reconstruct this exact working tree.
        with tempfile.TemporaryDirectory(prefix='.quill-verify-', dir=destination.parent) as check:
            reconstruct(staging, Path(check), manifest, base, blobs, same_base=True)
        for file in staging.rglob('*'):
            if file.is_file():
                file.chmod(0o600)
        publish(staging, destination)
    return manifest


def load_bundle(bundle):
    bundle = Path(bundle).resolve()
    if (bundle / 'manifest.json').is_symlink() or (bundle / 'objects').is_symlink():
        raise OverlayError('Source bundle metadata and objects must be ordinary paths')
    manifest = json.loads((bundle / 'manifest.json').read_text())
    if manifest.get('schema') != SCHEMA:
        raise OverlayError('Unsupported source overlay format')
    blobs = {}
    for mapping in ('base_files', 'working_files'):
        for name, record in manifest[mapping].items():
            if (not source_path(name) or not isinstance(record, dict)
                    or type(record.get('mode')) is not int or not 0 <= record['mode'] <= 0o777
                    or type(record.get('size')) is not int or not 0 <= record['size'] <= FILE_LIMIT):
                raise OverlayError('Invalid source manifest entry: ' + str(name))
            digest = record.get('sha256', '')
            if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                raise OverlayError('Invalid source digest')
            blob = bundle / 'objects' / digest
            if blob.is_symlink() or not blob.is_file():
                raise OverlayError('Missing ordinary source object')
            data = blob.read_bytes()
            if sha(data) != digest or len(data) != record['size']:
                raise OverlayError('Source object hash or size mismatch: ' + name)
            blobs[digest] = data
    for name in manifest['untracked_paths']:
        if name not in manifest['working_files'] or name in manifest['base_files']:
            raise OverlayError('Invalid untracked inventory')
    if manifest['deleted_paths'] != sorted(set(manifest['base_files']) - set(manifest['working_files'])):
        raise OverlayError('Invalid deletion inventory')
    patch = bundle / 'tracked.patch'
    if patch.is_symlink() or sha(patch.read_bytes()) != manifest['patch_sha256']:
        raise OverlayError('Tracked patch hash mismatch')
    return manifest, blobs


def write_file(root, name, record, blobs):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as handle:
        handle.write(blobs[record['sha256']])
    path.chmod(record['mode'])


def reconstruct(bundle, target, manifest, base, blobs, *, same_base):
    for name in manifest['untracked_paths']:
        if any(name == existing or name.startswith(existing + '/') or existing.startswith(name + '/') for existing in base):
            raise OverlayError('Upstream collides with a local added path: ' + name)
    for name, record in base.items():
        write_file(target, name, record, blobs)
    patch = (bundle / 'tracked.patch').read_bytes()
    if patch:
        if any(line.startswith((b'new file mode ', b'new mode ')) and line.split()[-1] not in (b'100644', b'100755')
               for line in patch.splitlines()):
            raise OverlayError('Symlink patches are not supported')
        stats = git(target, 'apply', '--numstat', '-z', str(bundle / 'tracked.patch'))
        changed = [item.split(b'\t', 2)[2].decode() for item in stats.split(b'\0') if item]
        if any(not source_path(name) for name in changed):
            raise OverlayError('Patch contains a path outside the source inventory')
        git(target, 'apply', '--check', '--binary', str(bundle / 'tracked.patch'))
        git(target, 'apply', '--binary', '--whitespace=nowarn', str(bundle / 'tracked.patch'))
    for name in manifest['untracked_paths']:
        write_file(target, name, manifest['working_files'][name], blobs)
    for name, record in manifest['working_files'].items():
        path = target / name
        if path.is_symlink():
            raise OverlayError('Replayed source contains a symlink')
        original = manifest['base_files'].get(name)
        if path.is_file() and (same_base or original is None or record['mode'] != original['mode']):
            path.chmod(record['mode'])
    actual = {}
    for path in target.rglob('*'):
        if path.is_symlink():
            raise OverlayError('Replayed source contains a symlink')
        if path.is_file():
            name = str(path.relative_to(target))
            if not source_path(name):
                raise OverlayError('Replayed path is outside the source inventory')
            actual[name] = metadata(path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
    if same_base and actual != manifest['working_files']:
        raise OverlayError('Same-base replay does not match exported files and modes')
    if any(name in actual for name in manifest['deleted_paths']):
        raise OverlayError('A deleted source file remains after replay')
    if any(actual.get(name) != manifest['working_files'][name] for name in manifest['untracked_paths']):
        raise OverlayError('Local added files changed during replay')
    return actual


def replay(bundle, destination, onto_repo=None, onto_ref=None):
    bundle, destination = Path(bundle).resolve(), output_path(destination)
    if destination == bundle or bundle in destination.parents:
        raise OverlayError('Replay into a separate directory outside the source bundle')
    if onto_repo is not None:
        repository = Path(onto_repo).resolve()
        if destination == repository or repository in destination.parents:
            raise OverlayError('Replay into a separate directory outside the checkout')
    if destination.exists() or destination.is_symlink():
        raise OverlayError('Replay destination already exists')
    manifest, blobs = load_bundle(bundle)
    if (onto_repo is None) != (onto_ref is None):
        raise OverlayError('Specify both --onto-repo and --onto-ref')
    commit = manifest['base_commit']
    base = manifest['base_files']
    if onto_repo is not None:
        commit = commit_id(onto_repo, onto_ref)
        base, new_blobs = tree(onto_repo, commit)
        blobs.update(new_blobs)
    with tempfile.TemporaryDirectory(prefix='.quill-replay-', dir=destination.parent) as temp:
        staging = Path(temp)
        actual = reconstruct(bundle, staging, manifest, base, blobs,
                             same_base=commit == manifest['base_commit'])
        publish(staging, destination)
    return {'base_commit': manifest['base_commit'], 'replayed_commit': commit,
            'same_base': commit == manifest['base_commit'], 'files': actual,
            'requires_review': commit != manifest['base_commit']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    exp = sub.add_parser('export')
    exp.add_argument('--repo', default=str(Path(__file__).resolve().parents[1]))
    exp.add_argument('--output', required=True)
    rep = sub.add_parser('replay')
    rep.add_argument('--bundle', required=True)
    rep.add_argument('--output', required=True)
    rep.add_argument('--onto-repo')
    rep.add_argument('--onto-ref')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.operation == 'export':
            result = export(args.repo, args.output)
            print(json.dumps({'base_commit': result['base_commit'], 'files': len(result['working_files']),
                              'untracked': len(result['untracked_paths']),
                              'excluded_untracked_paths': result['excluded_untracked_paths'],
                              'same_base_replay_verified': True}, indent=2))
        else:
            result = replay(args.bundle, args.output, args.onto_repo, args.onto_ref)
            print(json.dumps({key: value for key, value in result.items() if key != 'files'}, indent=2))
    except (OverlayError, OSError, ValueError, KeyError, TypeError) as error:
        raise SystemExit(str(error))


if __name__ == '__main__':
    main()
