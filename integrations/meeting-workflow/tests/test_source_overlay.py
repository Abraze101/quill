"""Real Git operations in disposable repositories only; no real checkout writes."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[3] / 'scripts/source-overlay.py'
SPEC = importlib.util.spec_from_file_location('quill_source_overlay', SCRIPT)
overlay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(overlay)


class SourceOverlayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.repo = self.base / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        self.write('.gitignore', b'.build/\n')
        self.write('README.md', b'Base source\n')
        self.write('Sources/main.swift', b'let first = 1\nlet second = 2\n')
        self.write('Tests/sample.bin', b'\x00old\xff')
        self.write('docs/obsolete.md', b'Removed locally\n')
        self.git('add', '.')
        self.commit()
        self.commit_id = self.git('rev-parse', 'HEAD').decode().strip()
        self.bundle = self.base / 'overlay'
        self.target = self.base / 'candidate'

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.DEVNULL)

    def commit(self):
        self.git('-c', 'user.name=Synthetic Test', '-c', 'user.email=test@example.invalid',
                 '-c', 'commit.gpgsign=false', 'commit', '-qm', 'Synthetic fixture')

    def write(self, name, data, mode=0o644):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
        return path

    def mutate(self):
        self.write('Sources/main.swift', b'let first = 3\nlet second = 2\n')
        self.write('Tests/sample.bin', b'\x00new\x80')
        (self.repo / 'docs/obsolete.md').unlink()
        self.write('scripts/new helper.sh', b'#!/bin/sh\nexit 0\n', 0o755)
        self.write('Sources/staged.swift', b'let staged = true\n')
        self.git('add', 'Sources/staged.swift')

    def test_exact_replay_preserves_binary_deletion_staged_and_untracked_modes(self):
        self.mutate()
        before = self.git('status', '--porcelain=v1', '-z')
        index = (self.repo / '.git/index').read_bytes()
        manifest = overlay.export(self.repo, self.bundle)
        result = overlay.replay(self.bundle, self.target)
        self.assertEqual(result['files'], manifest['working_files'])
        self.assertEqual((self.target / 'Tests/sample.bin').read_bytes(), b'\x00new\x80')
        self.assertFalse((self.target / 'docs/obsolete.md').exists())
        self.assertEqual(stat.S_IMODE((self.target / 'scripts/new helper.sh').stat().st_mode), 0o755)
        self.assertIn(b'GIT binary patch', (self.bundle / 'tracked.patch').read_bytes())
        self.assertFalse((self.target / '.git').exists())
        self.assertEqual(before, self.git('status', '--porcelain=v1', '-z'))
        self.assertEqual(index, (self.repo / '.git/index').read_bytes())
        self.assertEqual(self.commit_id, self.git('rev-parse', 'HEAD').decode().strip())

    def test_private_build_recording_and_runtime_files_are_excluded(self):
        for name in ['.build/output', 'Recordings/session/mic.caf', 'scripts/.env.local',
                     'integrations/config.json', 'Sources/private.key', 'work/state.db']:
            self.write(name, b'DO NOT EXPORT THIS PRIVATE FIXTURE')
        manifest = overlay.export(self.repo, self.bundle)
        self.assertNotIn('integrations/config.json', manifest['working_files'])
        self.assertIn('scripts/.env.local', manifest['excluded_untracked_paths'])
        self.assertFalse(any(b'DO NOT EXPORT THIS PRIVATE FIXTURE' in path.read_bytes()
                             for path in (self.bundle / 'objects').iterdir()))

    def test_export_rejects_tracked_private_path_without_publishing(self):
        self.write('scripts/.env.local', b'PRIVATE FIXTURE')
        self.git('add', 'scripts/.env.local')
        with self.assertRaisesRegex(overlay.OverlayError, 'outside the source inventory'):
            overlay.export(self.repo, self.bundle)
        self.assertFalse(self.bundle.exists())

    def test_symlink_source_is_not_followed(self):
        external = self.base / 'external'
        external.write_text('PRIVATE FIXTURE')
        (self.repo / 'Sources/link.swift').symlink_to(external)
        with self.assertRaisesRegex(overlay.OverlayError, 'symlinks'):
            overlay.export(self.repo, self.bundle)
        self.assertFalse(self.bundle.exists())

    def test_existing_outputs_and_checkout_targets_are_preserved(self):
        overlay.export(self.repo, self.bundle)
        sentinel = self.bundle / 'manifest.json'
        original = sentinel.read_bytes()
        with self.assertRaisesRegex(overlay.OverlayError, 'already exists'):
            overlay.export(self.repo, self.bundle)
        self.target.mkdir()
        (self.target / 'sentinel').write_text('Preserve')
        with self.assertRaisesRegex(overlay.OverlayError, 'already exists'):
            overlay.replay(self.bundle, self.target)
        with self.assertRaisesRegex(overlay.OverlayError, 'outside the checkout'):
            overlay.replay(self.bundle, self.repo / 'candidate', self.repo, 'HEAD')
        self.assertEqual(sentinel.read_bytes(), original)
        self.assertEqual((self.target / 'sentinel').read_text(), 'Preserve')

    def test_corrupt_object_stops_before_output(self):
        manifest = overlay.export(self.repo, self.bundle)
        digest = manifest['base_files']['README.md']['sha256']
        (self.bundle / 'objects' / digest).write_bytes(b'corrupt')
        with self.assertRaisesRegex(overlay.OverlayError, 'hash or size mismatch'):
            overlay.replay(self.bundle, self.target)
        self.assertFalse(self.target.exists())

    def test_corrupt_patch_stops_before_output(self):
        self.mutate()
        overlay.export(self.repo, self.bundle)
        (self.bundle / 'tracked.patch').write_bytes(b'Corrupted patch')
        with self.assertRaisesRegex(overlay.OverlayError, 'patch hash mismatch'):
            overlay.replay(self.bundle, self.target)
        self.assertFalse(self.target.exists())

    def test_output_parent_symlink_cannot_hide_checkout_destination(self):
        link = self.base / 'checkout-link'
        link.symlink_to(self.repo, target_is_directory=True)
        with self.assertRaisesRegex(overlay.OverlayError, 'outside the checkout'):
            overlay.export(self.repo, link / 'bundle')
        self.assertFalse((self.repo / 'bundle').exists())

    def test_newer_base_keeps_unrelated_upstream_edits(self):
        self.write('Sources/main.swift', b'let first = 3\nlet second = 2\n')
        overlay.export(self.repo, self.bundle)
        self.git('restore', 'Sources/main.swift')
        self.write('README.md', b'Newer upstream documentation\n')
        self.git('add', 'README.md')
        self.commit()
        result = overlay.replay(self.bundle, self.target, self.repo, 'HEAD')
        self.assertTrue(result['requires_review'])
        self.assertFalse(result['same_base'])
        self.assertEqual((self.target / 'README.md').read_bytes(), b'Newer upstream documentation\n')
        self.assertEqual((self.target / 'Sources/main.swift').read_bytes(), b'let first = 3\nlet second = 2\n')

    def test_conflicting_newer_base_fails_without_candidate(self):
        self.write('Sources/main.swift', b'let first = 3\nlet second = 2\n')
        overlay.export(self.repo, self.bundle)
        self.write('Sources/main.swift', b'let unrelated = 42\n')
        self.git('add', 'Sources/main.swift')
        self.commit()
        with self.assertRaises(overlay.OverlayError):
            overlay.replay(self.bundle, self.target, self.repo, 'HEAD')
        self.assertFalse(self.target.exists())

    def test_newer_base_cannot_take_over_untracked_added_path(self):
        self.write('Sources/local.swift', b'local\n')
        overlay.export(self.repo, self.bundle)
        self.write('Sources/local.swift', b'upstream\n')
        self.git('add', 'Sources/local.swift')
        self.commit()
        with self.assertRaisesRegex(overlay.OverlayError, 'collides'):
            overlay.replay(self.bundle, self.target, self.repo, 'HEAD')
        self.assertFalse(self.target.exists())

    def test_manifest_path_traversal_is_rejected(self):
        manifest = overlay.export(self.repo, self.bundle)
        manifest['working_files']['../outside'] = manifest['working_files']['README.md']
        (self.bundle / 'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(overlay.OverlayError, 'Invalid source manifest'):
            overlay.replay(self.bundle, self.target)
        self.assertFalse((self.base / 'outside').exists())
        self.assertFalse(self.target.exists())


if __name__ == '__main__':
    unittest.main()
