"""Build the small GPL source patch and existing MIT DirectShow drivers."""
import hashlib
import json
from pathlib import Path
import zipfile


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    root = Path(__file__).resolve().parent
    project = root.parents[1]
    reference = Path('D:/DeepFaceLive_Official/DeepFaceLive_NVIDIA/_internal/DeepFaceLive')
    manifest_path = root / 'manifest.json'
    old = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else {'files': {}}
    files = {}
    for source in sorted((root / 'payload').rglob('*.py')):
        name = source.relative_to(root / 'payload').as_posix()
        original = reference / name
        original_hash = old['files'].get(name, {}).get('original_sha256')
        if name not in old['files'] and original.exists():
            original_hash = sha(original)
        files[name] = {'sha256': sha(source), 'original_sha256': original_hash}
    manifest = {'version': '1.0.0', 'upstream': 'https://github.com/iperov/DeepFaceLive',
                'license': 'GPL-3.0', 'files': files}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    entries = {'FaceLivePatch/' + p.relative_to(root).as_posix(): p for p in root.rglob('*')
               if p.is_file() and (p.suffix in ('.py', '.json', '.txt', '.md'))
               and '__pycache__' not in p.parts and 'tests' not in p.parts}
    entries['FaceLivePatch/deploy/install-driver.ps1'] = project / 'deploy/install-driver.ps1'
    for name in ('UnityCaptureFilter64.dll', 'UnityCaptureFilter32.dll', 'LICENSE-UnityCaptureFilter.txt'):
        entries['FaceLivePatch/VirtualCam/' + name] = project.parent / 'app/VirtualCam' / name
    destination = project / 'dist/DeepFaceLive-FaceLiveCamera3-patch-1.0.0.zip'
    checksums = {name: sha(path) for name, path in entries.items()}
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, path in entries.items():
            archive.write(path, 'DeepFaceLive_NVIDIA/' + name)
        archive.writestr('DeepFaceLive_NVIDIA/apply-result-camera.cmd',
            '@echo off\r\n"%~dp0_internal\\python\\python.exe" "%~dp0FaceLivePatch\\apply_patch.py" --root "%~dp0."\r\npause\r\n')
        archive.writestr('DeepFaceLive_NVIDIA/install-result-camera.cmd',
            '@echo off\r\n"%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0FaceLivePatch\\deploy\\install-driver.ps1" -DeviceCount 3\r\npause\r\n')
        archive.writestr('DeepFaceLive_NVIDIA/FaceLivePatch/SHA256SUMS',
            ''.join('{}  {}\n'.format(checksums[name], name) for name in sorted(entries)))
    with zipfile.ZipFile(destination) as archive:
        assert archive.testzip() is None
        for name, expected in checksums.items():
            assert hashlib.sha256(archive.read('DeepFaceLive_NVIDIA/' + name)).hexdigest() == expected
    result = {'path': str(destination), 'size_kb': round(destination.stat().st_size / 1024, 1),
              'sha256': sha(destination), 'verified': True}
    (project / 'reports/deepfacelive-result-patch.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
