"""Prepare a versioned Quill update while running, then install it after a normal quit."""

import argparse
from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import runpy
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid


APP = Path('/Applications/Quill.app')
RESOURCES = Path(__file__).resolve().parent
REPOSITORY = 'https://github.com/Abraze101/quill.git'
MARKER = Path('Contents/Resources/quill-local-preview.json')
GIT = '/usr/bin/git'


class UpdateError(Exception):
    pass


def git(repo, *args, timeout=90):
    # Use the exact requested repository and revision. Replacements, hooks and
    # terminal credential prompts do not belong in an unattended app update.
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env['GIT_TERMINAL_PROMPT'] = '0'
    cmd = [GIT, '--no-replace-objects', '-c', 'core.hooksPath=/dev/null', '-C', str(repo), *args]
    try:
        return subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=env, timeout=timeout).stdout
    except subprocess.CalledProcessError as e:
        raise UpdateError('Git could not ' + args[0] + ' the update. Open the installation guide to review your checkout.') from e


def checkout_state(root, expected=None):
    if (git(root, 'rev-parse', '--show-toplevel').decode().strip() != str(root)
            or git(root, 'symbolic-ref', '--quiet', 'HEAD').strip() != b'refs/heads/main'):
        raise UpdateError('Switch your original Quill checkout to main before updating.')
    if git(root, 'status', '--porcelain=v1', '-z', '--untracked-files=normal', '--ignore-submodules=none'):
        raise UpdateError('Your Quill checkout has local changes. Save them before updating.')
    head = git(root, 'rev-parse', 'HEAD').decode().strip()
    if expected and head != expected:
        raise UpdateError('The Quill checkout changed during the update. Check for updates again.')
    for name in ('index.lock', 'MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply', 'BISECT_START', 'sequencer'):
        if (root / os.fsdecode(git(root, 'rev-parse', '--git-path', name).strip())).exists():
            raise UpdateError('Finish the current Git operation in your Quill checkout before updating.')
    flags = git(root, 'ls-files', '-v', '-z')
    if any(path[:1] == b'S' or path[:1].islower() for path in flags.split(b'\x00') if path):
        raise UpdateError('The checkout hides changes to tracked files. Use a full checkout before updating.')
    return head


def ignored_conflicts(root, old, new):
    # --keep cannot be relied upon to preserve ignored, untracked files.
    # Limit the scan to paths introduced by this transition (including rollback).
    added = git(root, 'diff', '--no-renames', '--diff-filter=AT', '--name-only', '-z', old, new, '--')
    for raw in filter(None, added.split(b'\x00')):
        path = Path(os.fsdecode(raw))
        for relative in (path, *path.parents):
            if relative == Path('.'):
                break
            local = root / relative
            if not local.exists() and not local.is_symlink():
                continue
            if relative != path and local.is_dir() and not local.is_symlink():
                continue
            # A tracked file or directory can legitimately change shape. Git's
            # merge guards tracked content; explicitly check for ignored children.
            if git(root, 'ls-files', '-z', '--others', '--ignored', '--exclude-standard', '--', str(relative)):
                raise UpdateError('The update would overwrite an ignored local file. Move it aside before updating.')


def version_pair(version, build):
    if not re.fullmatch(r'(?:0|[1-9][0-9]{0,3})(?:\.(?:0|[1-9][0-9]{0,3})){2}', str(version)) or not re.fullmatch(r'[1-9][0-9]{0,3}', str(build)):
        raise UpdateError('Quill could not verify the update version.')
    return tuple(map(int, version.split('.'))) + (int(build),)


def app_info(app):
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    return version_pair(info['CFBundleShortVersionString'], info['CFBundleVersion'])


def private_directory(path):
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or path.resolve() != path:
        raise UpdateError('Quill needs a private update staging directory.')
    return path


def token_directory(updates, token):
    if str(uuid.UUID(token)) != token:
        raise UpdateError('Invalid update transaction.')
    return updates / token


def save_json(path, value):
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as f:
        name = Path(f.name)
        try:
            json.dump(value, f)
            f.flush()
            os.fsync(f.fileno())
            os.replace(name, path)
            sync_dir(path.parent)
        finally:
            name.unlink(missing_ok=True)


def sync_dir(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def signature_digest(app):
    return hashlib.sha256((app / 'Contents/_CodeSignature/CodeResources').read_bytes()).hexdigest()


def installed(app, installer):
    root = Path(json.loads((app / MARKER).read_bytes())['checkout']).resolve(strict=True)
    identity = installer['validate_app'](app, root, installer['run'])
    # The signed bundle is also the previous version's recovery reference.
    return root, identity, signature_digest(app)


def prepare(directory, revision, version, build, app, installer, packager, asset):
    if not re.fullmatch('[0-9a-f]{40}', revision):
        raise UpdateError('Invalid update revision.')
    root, identity, signature = installed(app, installer)
    requested = version_pair(version, build)
    if requested <= app_info(app):
        raise UpdateError('The selected version is already installed or older. Check for updates again.')
    original = checkout_state(root)
    directory.mkdir(mode=0o700)
    try:
        binary = directory / 'quill'
        digest = asset['download_binary'](revision, version, build, binary)
        # Fetch into the object database only. No branch or working tree is
        # changed until the installed app has quit and all activity locks are held.
        git(root, 'fetch', '--no-tags', '--no-write-fetch-head', '--no-recurse-submodules',
            REPOSITORY, revision, timeout=180)
        git(root, 'merge-base', '--is-ancestor', original, revision)
        ignored_conflicts(root, original, revision)
        # An independent checkout shares objects, not files or index, with the
        # live checkout. Removing staging leaves no worktree registrations.
        source = directory / 'source'
        git(directory, 'clone', '--shared', '--no-checkout', '--no-tags', str(root), str(source))
        git(source, 'checkout', '--detach', revision)
        candidate = plistlib.loads((source / 'Sources/quill/Info.plist').read_bytes())
        if (candidate.get('CFBundleIdentifier') != 'com.digimata.quill'
                or version_pair(candidate.get('CFBundleShortVersionString'), candidate.get('CFBundleVersion')) != requested):
            raise UpdateError('The source revision does not match the requested Quill version.')
        staged = directory / 'Quill.app'
        packager['package_prebuilt'](source, root, binary, digest, staged, identity)
        if installer['validate_app'](staged, root, installer['run']) != identity or app_info(staged) != requested:
            raise UpdateError('The prepared app failed signature or version verification.')
        shutil.rmtree(source)
        binary.unlink()
        checkout_state(root, original)
        save_json(directory / 'prepared.json', {'checkout': str(root), 'from': original, 'to': revision,
                  'identity': identity, 'previous_signature': signature, 'version': version, 'build': build,
                  'staged_signature': signature_digest(staged)})
    except BaseException:
        shutil.rmtree(directory)
        raise


def workflow_state(home):
    native = home / '.config/quill/config.json'
    config = json.loads(native.read_bytes()) if native.exists() else {}
    if 'completion_status_file' in config:
        file = Path(config['completion_status_file']).expanduser()
        if not file.is_absolute():
            raise UpdateError('Your completion status location must be absolute for an in-app update.')
        return file.parent
    return home / '.local/state/quill/workflow'


@contextmanager
def activity_locks(home, *, wait=12):
    state = workflow_state(home)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths = [home / '.local/state/quill/run.lock', state / 'supervisor.lock', state / 'worker.lock',
             state / 'deletion-worker.lock', state / 'local-deletion-control.lock', state / 'sharing-work.lock']
    deadline = time.monotonic() + wait
    with ExitStack() as stack:
        # Generate the legacy list only after the common sharing lock is held.
        def lock_paths():
            yield from paths
            yield from sorted(p for p in state.glob('sharing-*.lock') if p != paths[-1])

        for path in lock_paths():
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            stack.callback(os.close, fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise UpdateError('Quill could not verify the recording or processing activity locks.')
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise UpdateError('Quill or meeting processing is still running. Finish that work and retry the update.')
                    time.sleep(0.2)
        yield


def wait_for_exit(pid, timeout=30):
    if pid <= 1:
        raise UpdateError('Invalid Quill process.')
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.15)
    raise UpdateError('Quill is still running. The update was postponed.')


def install_prepared(directory, app, installer):
    receipt = json.loads((directory / 'prepared.json').read_bytes())
    root, identity, signature = installed(app, installer)
    source, target = receipt['from'], receipt['to']
    if (not all(re.fullmatch('[0-9a-f]{40}', value) for value in (source, target))
            or str(root) != receipt['checkout'] or identity != receipt['identity'] or signature != receipt['previous_signature']):
        raise UpdateError('Your installation changed during the update. Check for updates again.')
    staged = directory / 'Quill.app'
    if (installer['validate_app'](staged, root, installer['run']) != identity
            or app_info(staged) <= app_info(app)
            or app_info(staged) != version_pair(receipt['version'], receipt['build'])
            or signature_digest(staged) != receipt['staged_signature']):
        raise UpdateError('The staged update changed. Check for updates again.')
    checkout_state(root, source)
    git(root, 'merge-base', '--is-ancestor', source, target)
    ignored_conflicts(root, source, target)
    updates = directory.parent
    # Persist before either live component changes. On reboot the app's early
    # startup gate selects recover instead of opening the recorder and workers.
    journal = dict(receipt, token=directory.name)
    save_json(updates / 'pending.json', journal)
    validated = False

    def verify_then_advance():
        nonlocal validated
        checkout_state(root, source)
        if validated and source != target:
            ignored_conflicts(root, source, target)
            git(root, 'merge', '--ff-only', '--no-autostash', '--no-overwrite-ignore', target)
            checkout_state(root, target)
        validated = True

    installer['install'](root, app, source_app=staged, consistency_check=verify_then_advance,
                         report=lambda _: None, atomic_swap=True)


def restore_backup(app, root, directory, previous_signature, installer):
    # The normal installer persists and verifies this backup before swapping.
    # Identify this transaction's backup by source, destination AND signed bytes.
    for receipt_file in (app.parent / '.quill-app-backups').glob('install-*/manifest.json'):
        receipt = json.loads(receipt_file.read_bytes())
        if receipt.get('source_app') != str(directory / 'Quill.app') or receipt.get('destination_app') != str(app):
            continue
        backup = receipt_file.parent / 'Quill.app'
        installer['validate_app'](backup, root, installer['run'])
        if signature_digest(backup) == previous_signature:
            installer['install'](root, app, source_app=backup, report=lambda _: None, atomic_swap=True)
            return
    raise UpdateError('The matching signed backup could not be found in Applications. Open the recovery guide.')


def require_aligned_checkout(root, source, target, expected):
    # SIGKILL during Git's two-tree checkout can leave HEAD unchanged and
    # some files already written. HEAD alone is not a completed update.
    for guard in ('index.lock', 'HEAD.lock', 'refs/heads/main.lock'):
        if (root / os.fsdecode(git(root, 'rev-parse', '--git-path', guard).strip())).exists():
            raise UpdateError('Git has an unfinished checkout operation. Review it before retrying repair.')
    changed = [os.fsdecode(p) for p in git(root, 'diff', '--no-renames', '--name-only', '-z', source, target).split(b'\x00') if p]
    for offset in range(0, len(changed), 128):
        paths = changed[offset:offset + 128]
        tracked = git(root, 'diff', '--name-only', '-z', expected, '--', *paths)
        indexed = git(root, 'diff', '--cached', '--name-only', '-z', expected, '--', *paths)
        untracked = git(root, 'ls-files', '--others', '-z', '--', *paths)
        if tracked or indexed or untracked:
            raise UpdateError('Files touched by this update differ from the checked-out version. Review your local changes and retry repair.')


def reconcile(updates, app, installer):
    """Recover an interrupted helper without discarding another process's edits."""
    journal = json.loads((updates / 'pending.json').read_bytes())
    directory = token_directory(updates, journal['token'])
    root, identity, signature = installed(app, installer)
    if str(root) != journal['checkout'] or identity != journal['identity']:
        raise UpdateError('The installed Quill or its source folder changed. Use the recovery guide before restarting.')
    source, target = journal['from'], journal['to']
    if not all(re.fullmatch('[0-9a-f]{40}', value) for value in (source, target)):
        raise UpdateError('Quill could not verify the saved update transaction.')
    head = git(root, 'rev-parse', 'HEAD').decode().strip()
    if git(root, 'symbolic-ref', '--quiet', 'HEAD').strip() != b'refs/heads/main' or head not in (source, target):
        raise UpdateError('Your checkout changed during the update. Restore its original main branch and retry repair.')
    if signature == journal['previous_signature']:
        if head != source:
            checkout_state(root, target)
            ignored_conflicts(root, target, source)
            git(root, 'reset', '--keep', source)
        updated = False
    elif signature == journal['staged_signature']:
        if head != target:
            try:
                checkout_state(root, source)
                ignored_conflicts(root, source, target)
                git(root, 'merge', '--ff-only', '--no-autostash', '--no-overwrite-ignore', target)
            except UpdateError:
                # A concurrent local change prevents rolling forward. Prefer
                # restoring the corresponding old app over changing that file.
                if git(root, 'rev-parse', 'HEAD').decode().strip() != source:
                    raise
                require_aligned_checkout(root, source, target, source)
                restore_backup(app, root, directory, journal['previous_signature'], installer)
                return complete_repair(updates, False)
        updated = True
    else:
        raise UpdateError('A different Quill build is installed. Review the retained app backup before restarting.')
    require_aligned_checkout(root, source, target, target if updated else source)
    return complete_repair(updates, updated)


def complete_repair(updates, updated):
    (updates / 'manual-repair.json').unlink(missing_ok=True)
    (updates / 'pending.json').unlink()
    sync_dir(updates)
    return updated


@contextmanager
def updater_lock(updates):
    fd = os.open(updates / 'update.lock', os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise UpdateBusy('Another Quill update is running.') from e
        yield
    finally:
        os.close(fd)


class UpdateBusy(UpdateError):
    pass


def finish_update(updates, directory, app, installer, home, recover=False):
    token = directory.name
    result = {'token': token, 'success': False, 'message': 'Quill could not complete the update.'}
    with activity_locks(home):
        installer['require_idle'](app)
        if not recover:
            try:
                install_prepared(directory, app, installer)
            except Exception as error:
                result['message'] = user_message(error)
        if (updates / 'pending.json').exists():
            try:
                if reconcile(updates, app, installer):
                    result.update(success=True, message='Quill updated successfully.')
                elif recover:
                    result['message'] = 'The previous version of Quill was restored.'
            except Exception as error:
                result['message'] = user_message(error)
                save_json(updates / 'manual-repair.json', {'token': token, 'message': result['message']})
                raise
        if not (updates / 'pending.json').exists():
            shutil.rmtree(directory, ignore_errors=True)
    return result


def main():
    os.umask(0o077)
    sys.dont_write_bytecode = True
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('prepare', 'apply', 'discard', 'bootstrap', 'recover'):
        cmd = sub.add_parser(action)
        if action not in ('bootstrap', 'recover'):
            cmd.add_argument('--token', required=True)
        if action == 'prepare':
            cmd.add_argument('--commit', required=True)
            cmd.add_argument('--version', required=True)
            cmd.add_argument('--build', required=True)
        if action in ('apply', 'recover'):
            cmd.add_argument('--pid', type=int, required=(action == 'apply'))
    args = parser.parse_args()
    if args.action != 'bootstrap' and RESOURCES != APP / 'Contents/Resources':
        raise UpdateError('Use the signed Quill app in Applications to run this update.')
    home = Path.home()
    updates = private_directory(home / '.local/state/quill/app-updates')
    try:
        with updater_lock(updates):
            execute(args, home, updates)
    except UpdateBusy:
        # An app opened mid-swap may launch a second recover helper. The
        # original helper owns the journal and will reopen the application.
        if args.action != 'recover':
            raise


def execute(args, home, updates):
    pending = updates / 'pending.json'
    if pending.exists() and args.action not in ('bootstrap', 'recover'):
        raise UpdateError('A previous Quill update needs recovery first.')
    recovering = pending.exists() and args.action in ('bootstrap', 'recover')
    if args.action == 'recover' and not recovering:
        return
    if recovering:
        if getattr(args, 'pid', None) is not None:
            wait_for_exit(args.pid)
        try:
            token = json.loads(pending.read_bytes())['token']
            token_directory(updates, token)
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            message = 'The saved update receipt is unreadable. Review the retained backup and checkout before starting Quill.'
            save_json(updates / 'manual-repair.json', {'message': message})
            if (APP / 'Contents/Resources/app-update.py').is_file():
                subprocess.run(['/usr/bin/open', str(APP)])
            raise UpdateError(message) from error
    else:
        token = str(uuid.uuid4()) if args.action == 'bootstrap' else args.token
    directory = token_directory(updates, token)
    # Load trusted helpers before replacing the installed app's resources.
    installer = runpy.run_path(str(RESOURCES / 'install-app.py'))
    if args.action in ('prepare', 'bootstrap') and not recovering:
        packager = runpy.run_path(str(RESOURCES / 'build-preview-app.py'))
        asset = runpy.run_path(str(RESOURCES / 'update_asset.py'))
        if args.action == 'bootstrap':
            installer['require_idle'](APP)
            info, _ = packager['source_info'](RESOURCES.parent)
            version, build = info['CFBundleShortVersionString'], info['CFBundleVersion']
            revision = asset['tag_revision'](version)
        else:
            revision, version, build = args.commit, args.version, args.build
        prepare(directory, revision, version, build, APP, installer, packager, asset)
        if args.action == 'prepare':
            return
    elif args.action == 'discard':
        if directory.exists():
            shutil.rmtree(directory)
        return
    result = {'token': token, 'success': False, 'message': 'The update was interrupted. Check for updates again.'}
    save_json(updates / 'result.json', result)
    try:
        if args.action == 'apply':
            wait_for_exit(args.pid)
        result = finish_update(updates, directory, APP, installer, home, recover=recovering)
    except Exception as error:
        result['message'] = user_message(error)
        if pending.exists():
            save_json(updates / 'manual-repair.json', {'token': token, 'message': result['message']})
        else:
            shutil.rmtree(directory, ignore_errors=True)
    finally:
        save_json(updates / 'result.json', result)
        # Opening a pending + manual-repair journal only shows the repair UI.
        # A launch failure does not change either verified software component.
        if pending.exists() and not (APP / 'Contents/Resources/app-update.py').is_file():
            raise UpdateError(result['message'] + ' Keep Quill closed and rerun the bootstrap command after repair.')
        if subprocess.run(['/usr/bin/open', str(APP)]).returncode:
            result['message'] += ' Open Quill from Applications to continue.'
            save_json(updates / 'result.json', result)
            raise UpdateError(result['message'])
    if not result['success']:
        raise UpdateError(result['message'])


def user_message(error):
    if isinstance(error, (UpdateError, OSError, ValueError)) or error.__class__.__name__ in ('PackagingError', 'InstallError', 'UpdateAssetError'):
        return str(error)
    if isinstance(error, subprocess.TimeoutExpired):
        return 'The Quill update timed out. Try again.'
    return 'Quill could not complete the update. See the installation guide.'


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        sys.exit(user_message(error))
