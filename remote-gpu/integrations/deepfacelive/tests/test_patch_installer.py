"""No user installation touched: test patch preflight, backup and idempotence."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('dfl_patch_apply', ROOT / 'apply_patch.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class Tests(unittest.TestCase):
    def fixture(self, temporary):
        directory = Path(temporary)
        root, source = directory / 'DFL', directory / 'patch'
        (root / '_internal/DeepFaceLive').mkdir(parents=True)
        (root / 'DeepFaceLive.bat').write_text('launcher')
        (root / '_internal/DeepFaceLive/main.py').write_text('main')
        (source / 'payload').mkdir(parents=True)
        before, after = root / '_internal/DeepFaceLive/example.py', source / 'payload/example.py'
        before.write_text('original')
        after.write_text('patched')
        (source / 'manifest.json').write_text(json.dumps({'version': 'test', 'files': {
            'example.py': {'sha256': installer.sha(after), 'original_sha256': installer.sha(before)}}}))
        return root, source, before

    def test_apply_backups_and_reapply_without_overwriting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, source, target = self.fixture(temporary)
            result = installer.apply(root, source)
            self.assertEqual(target.read_text(), 'patched')
            self.assertEqual((Path(result['backup']) / 'example.py').read_text(), 'original')
            self.assertEqual(installer.apply(root, source)['changed_files'], 0)

    def test_unknown_local_changes_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, source, target = self.fixture(temporary)
            target.write_text('custom changes')
            with self.assertRaises(ValueError): installer.apply(root, source)
            self.assertEqual(target.read_text(), 'custom changes')


if __name__ == '__main__':
    unittest.main()
