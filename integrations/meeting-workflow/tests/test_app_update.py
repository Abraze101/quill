"""Pinned, two-component restart transactions in disposable git repos and Applications folders."""
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import uuid

SCRIPTS = Path(__file__).resolve().parents[3] / 'scripts'


def module(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


u = module('app-update')
installer = module('install-app')


class AppUpdateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.home = self.base / 'home'
        self.updates = u.private_directory(self.home / '.local/state/quill/app-updates')
        self.stage = self.updates / str(uuid.uuid4())
        self.app = self.base / 'Applications/Quill.app'
        self.repo = self.base / 'source'
        self.repo.mkdir()
        u.git(self.repo, 'init', '-b', 'main')
        u.git(self.repo, 'config', 'user.name', 'Update Tests')
        u.git(self.repo, 'config', 'user.email', 'user@example.org')
        self.certificate = b'synthetic release signing certificate'
        self.identity = hashlib.sha1(self.certificate).hexdigest().upper()
        self.info = {'CFBundleIdentifier': 'com.digimata.quill', 'CFBundleExecutable': 'quill',
                     'CFBundlePackageType': 'APPL', 'CFBundleShortVersionString': '0.1.1', 'CFBundleVersion': '1'}
        self.create_bundle(self.app, self.repo, self.info, b'old')
        source_plist = self.repo / 'Sources/quill/Info.plist'
        source_plist.parent.mkdir(parents=True)
        source_plist.write_bytes(plistlib.dumps(self.info))
        (self.repo / '.gitignore').write_text('private.log\n*.secret\n')
        self.old = self.commit('First version')
        self.next_info = dict(self.info, CFBundleShortVersionString='0.1.2', CFBundleVersion='2')
        source_plist.write_bytes(plistlib.dumps(self.next_info))
        (self.repo / 'workflow').write_text('next')
        self.new = self.commit('Second version')
        self.remote = self.base / 'upstream'
        u.git(self.base, 'clone', '--bare', str(self.repo), str(self.remote))
        u.git(self.repo, 'reset', '--hard', self.old)
        patcher = patch.object(u, 'REPOSITORY', str(self.remote))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.install_api = {'validate_app': installer.validate_app, 'install': self.install,
                            'run': self.runner, 'require_idle': lambda _: None}

    def commit(self, subject):
        u.git(self.repo, 'add', '-A')
        u.git(self.repo, 'commit', '-m', subject)
        return u.git(self.repo, 'rev-parse', 'HEAD').decode().strip()

    def runner(self, cmd, **kwargs):
        if cmd[0] == '/usr/bin/codesign':
            for arg in cmd:
                if arg.startswith('--extract-certificates='):
                    Path(arg.split('=', 1)[1] + '0').write_bytes(self.certificate)
        else:
            return subprocess.run(cmd, check=True, text=True, **kwargs)
        return subprocess.CompletedProcess(cmd, 0)

    def install(self, root, destination, **kw):
        return installer.install(root, destination, **kw, runner=self.runner, idle_check=lambda _: None)

    def create_bundle(self, app, root, info, binary):
        resources = app / 'Contents/Resources'
        resources.mkdir(parents=True)
        (resources / 'quill-local-preview.json').write_text(json.dumps(
            {'schema': installer.SCHEMA, 'checkout': str(root), 'signing_identity': self.identity}))
        (app / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
        signature = app / 'Contents/_CodeSignature/CodeResources'
        signature.parent.mkdir()
        signature.write_bytes(hashlib.sha256(binary).digest())
        exe = app / 'Contents/MacOS/quill'
        exe.parent.mkdir()
        exe.write_bytes(binary)
        exe.chmod(0o755)

    def prepare(self, revision=None):
        def asset(commit, version, build, file):
            file.write_bytes(b'prebuilt')
            return hashlib.sha256(file.read_bytes()).hexdigest()

        def package(source_root, root, binary, digest, dest, identity):
            self.assertEqual(identity, self.identity)
            self.assertEqual(u.git(source_root, 'rev-parse', 'HEAD').decode().strip(), revision or self.new)
            self.assertEqual(digest, hashlib.sha256(binary.read_bytes()).hexdigest())
            self.create_bundle(dest, root, self.next_info, binary.read_bytes())

        u.prepare(self.stage, revision or self.new, '0.1.2', '2', self.app, self.install_api,
                  {'package_prebuilt': package}, {'download_binary': asset})

    def head(self):
        return u.git(self.repo, 'rev-parse', 'HEAD').decode().strip()

    def finish(self, recover=False):
        return u.finish_update(self.updates, self.stage, self.app, self.install_api, self.home, recover)

    def test_prepare_preserves_install_and_worktree_then_apply_updates_both(self):
        self.prepare()
        self.assertEqual(self.head(), self.old)
        self.assertEqual(u.app_info(self.app)[-1], 1)
        self.assertTrue(self.finish()['success'])
        self.assertEqual(self.head(), self.new)
        self.assertEqual(u.app_info(self.app)[-1], 2)
        self.assertFalse((self.updates / 'pending.json').exists())
        self.assertFalse(self.stage.exists())
        self.assertEqual(len(list(self.app.parent.glob('.quill-app-backups/install-*/Quill.app'))), 1)

    def test_source_already_updated_and_app_older(self):
        u.git(self.repo, 'merge', '--ff-only', self.new)
        self.prepare()
        self.assertTrue(self.finish()['success'])
        self.assertEqual(self.head(), self.new)

    def test_dirty_and_diverged_checkouts_are_unchanged(self):
        (self.repo / 'untracked').write_text('mine')
        with self.assertRaisesRegex(u.UpdateError, 'local changes'):
            self.prepare()
        self.assertFalse(self.stage.exists())
        local = self.commit('Local work')
        with self.assertRaises(u.UpdateError):
            self.prepare()
        self.assertEqual(self.head(), local)
        self.assertEqual((self.repo / 'untracked').read_text(), 'mine')

    def test_ignored_file_colliding_with_new_tracked_path(self):
        u.git(self.repo, 'merge', '--ff-only', self.new)
        (self.repo / 'private.log').write_text('upstream')
        u.git(self.repo, 'add', '-f', 'private.log')
        target = self.commit('A formerly ignored path')
        u.git(self.repo, 'push', str(self.remote), 'main')
        u.git(self.repo, 'reset', '--hard', self.old)
        (self.repo / 'private.log').write_text('personal')
        with self.assertRaisesRegex(u.UpdateError, 'ignored local'):
            self.prepare(target)
        self.assertEqual(self.head(), self.old)
        self.assertEqual((self.repo / 'private.log').read_text(), 'personal')

    def test_local_edit_after_prepare(self):
        self.prepare()
        filepath = self.repo / 'Sources/quill/Info.plist'
        filepath.write_text('mine')
        self.assertFalse(self.finish()['success'])
        self.assertEqual(filepath.read_text(), 'mine')
        self.assertEqual(u.app_info(self.app)[-1], 1)

    def interrupted(self, callback):
        self.prepare()
        def interrupt(root, destination, **kw):
            original = kw['consistency_check']
            def check():
                if u.app_info(self.app)[-1] == 2:
                    callback()
                original()
            kw['consistency_check'] = check
            self.install(root, destination, **kw)

        # Use a child process and os._exit to bypass normal rollback/finally
        # exactly after the atomic bundle swap and before the checkout advance.
        child = os.fork()
        if child == 0:
            self.install_api['install'] = interrupt
            u.install_prepared(self.stage, self.app, self.install_api)
            os._exit(1)
        _, status = os.waitpid(child, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 42)
        self.assertTrue((self.updates / 'pending.json').exists())
        self.assertEqual(u.app_info(self.app)[-1], 2)
        self.assertEqual(self.head(), self.old)

    def test_process_death_after_swap_recovers_at_next_launch(self):
        self.interrupted(lambda: os._exit(42))
        self.assertTrue(self.finish(recover=True)['success'])
        self.assertEqual(self.head(), self.new)
        self.assertEqual(u.app_info(self.app)[-1], 2)

    def test_changed_source_after_crash_restores_old_app_without_overwriting_edits(self):
        self.interrupted(lambda: os._exit(42))
        file = self.repo / 'my-notes'
        file.write_text('personal edits')
        self.assertFalse(self.finish(recover=True)['success'])
        self.assertEqual(u.app_info(self.app)[-1], 1)
        self.assertEqual(self.head(), self.old)
        self.assertEqual(file.read_text(), 'personal edits')

    def test_partial_git_write_prevents_relaunch_until_resolved(self):
        self.interrupted(lambda: os._exit(42))
        file = self.repo / 'Sources/quill/Info.plist'
        file.write_bytes(plistlib.dumps(self.next_info))
        with self.assertRaisesRegex(u.UpdateError, 'Files touched by this update'):
            self.finish(recover=True)
        self.assertTrue((self.updates / 'pending.json').exists())
        self.assertTrue((self.updates / 'manual-repair.json').exists())
        self.assertEqual(u.app_info(self.app)[-1], 2)
        file.write_bytes(plistlib.dumps(self.info))
        self.assertTrue(self.finish(recover=True)['success'])
        self.assertFalse((self.updates / 'pending.json').exists())
        self.assertFalse((self.updates / 'manual-repair.json').exists())

    def test_repair_never_overwrites_an_ignored_file_on_reverse_transition(self):
        # The old release tracks this path; the new release removes it.
        (self.repo / 'private.log').write_text('old tracked')
        u.git(self.repo, 'add', '-f', 'private.log')
        source = self.commit('Old release tracks the log')
        (self.repo / 'private.log').unlink()
        target = self.commit('New release removes the log')
        (self.repo / 'private.log').write_text('created after update')
        sig = u.signature_digest(self.app)
        u.save_json(self.updates / 'pending.json', {'checkout':str(self.repo), 'identity':self.identity,
            'token':self.stage.name, 'from':source, 'to':target, 'previous_signature':sig, 'staged_signature':'different'})
        with self.assertRaisesRegex(u.UpdateError, 'ignored local'):
            u.reconcile(self.updates, self.app, self.install_api)
        self.assertTrue((self.updates / 'pending.json').exists())
        self.assertEqual(self.head(), target)
        self.assertEqual((self.repo / 'private.log').read_text(), 'created after update')

    def test_failed_final_backup_rolls_back_source_and_app(self):
        self.prepare()
        original = installer.save_backup_manifest
        def refuse_success(directory, manifest):
            if manifest['status'] == 'installed':
                raise OSError('receipt unavailable')
            return original(directory, manifest)
        with patch.object(installer, 'save_backup_manifest', side_effect=refuse_success):
            self.assertFalse(self.finish()['success'])
        self.assertEqual(self.head(), self.old)
        self.assertEqual(u.app_info(self.app)[-1], 1)
        self.assertFalse((self.updates / 'pending.json').exists())

    def test_processing_and_reopened_app_locks_block_apply(self):
        for name in ('run.lock', 'workflow/worker.lock', 'workflow/supervisor.lock', 'workflow/sharing-work.lock', 'workflow/sharing-earlier.lock'):
            path = self.home / '.local/state/quill' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('a') as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                with self.assertRaisesRegex(u.UpdateError, 'still running'):
                    with u.activity_locks(self.home, wait=0):
                        self.fail('Concurrent ownership')
