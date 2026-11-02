"""Apply reviewed DeepFaceLive source files with hash checks and local backups."""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import shutil


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply(root, source=None):
    source = Path(source or Path(__file__).resolve().parent)
    root = Path(root).resolve()
    if not (root / 'DeepFaceLive.bat').is_file() and (root / 'DeepFaceLive_NVIDIA').is_dir():
        root = root / 'DeepFaceLive_NVIDIA'
    target = root / '_internal/DeepFaceLive'
    if not (root / 'DeepFaceLive.bat').is_file() or not (target / 'main.py').is_file():
        raise ValueError('Choose the DeepFaceLive_NVIDIA folder containing DeepFaceLive.bat')
    manifest = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
    state_path = root / '.facelive-result-camera-patch.json'
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
    changed = []
    for name, expected in manifest['files'].items():
        src, dst = source / 'payload' / name, (target / name).resolve()
        if target.resolve() not in dst.parents:
            raise ValueError('Invalid patch target')
        if sha(src) != expected['sha256']:
            raise ValueError('Patch checksum mismatch: ' + name)
        current = sha(dst) if dst.exists() else None
        if current == expected['sha256']:
            continue
        allowed = [expected.get('original_sha256'), state.get('files', {}).get(name)]
        if current not in allowed:
            raise ValueError('Local changes/version mismatch; no files modified: ' + name)
        changed.append((name, src, dst))
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    backup = root / 'FaceLivePatchBackups' / stamp
    for name, src, dst in changed:
        if dst.exists():
            saved = backup / name
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(dst), str(saved))
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dst))
    result = {'version': manifest['version'], 'files': {name: value['sha256'] for name, value in manifest['files'].items()},
              'backup': str(backup), 'changed_files': len(changed)}
    state_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Patch applied. Restart DeepFaceLive. Backup: ' + str(backup))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    apply(args.root)
