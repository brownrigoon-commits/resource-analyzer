"""Create a versioned E-drive copy of this project's PC install artifacts.

No deletion or mirroring. Every copy is checked against its source SHA-256.
Measurement databases, screenshots, account data and environment secrets are
outside this allowlisted artifact backup. Run at installation/build milestones.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[1]
BACKUP_ROOT = Path('E:/RIGOON_STUDIO/PC_INSTALL_BACKUPS/resource-analyzer')
EXTENSIONS = {'.exe', '.dll', '.config', '.txt', '.zip', '.msi', '.msix', '.whl', '.7z'}


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(4 * 1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--outputs-root', type=Path, required=True)
    parser.add_argument('--sensor-archive', type=Path)
    args = parser.parse_args()
    outputs = args.outputs_root.resolve(strict=True)
    drive_root = Path('E:/RIGOON_STUDIO').resolve(strict=True)
    destination_root = BACKUP_ROOT.resolve()
    if not destination_root.is_relative_to(drive_root):
        raise RuntimeError('Backup destination must remain under E:/RIGOON_STUDIO')
    candidates = []
    for directory in sorted(outputs.iterdir()):
        if not directory.is_dir() or not directory.name.startswith(('ResourceAnalyzer-v', 'NVIDIA-Studio-')):
            continue
        for source in sorted(directory.rglob('*')):
            if source.is_file() and source.suffix.lower() in EXTENSIONS:
                if source.is_symlink() or not source.resolve().is_relative_to(outputs):
                    raise RuntimeError('Artifact path points outside the selected output directory')
                candidates.append((source, Path('installables') / source.relative_to(outputs)))
    sensor_root = ROOT / 'vendor/sensors'
    if sensor_root.is_dir():
        for source in sorted(sensor_root.rglob('*')):
            if source.is_file() and source.suffix.lower() in EXTENSIONS:
                if source.is_symlink() or not source.resolve().is_relative_to(sensor_root.resolve()):
                    raise RuntimeError('Sensor artifact points outside its folder')
                candidates.append((source, Path('sensor-engine') / source.relative_to(sensor_root)))
    if args.sensor_archive:
        source = args.sensor_archive.resolve(strict=True)
        candidates.append((source, Path('upstream') / source.name))
    instructions = Path.home() / '.codex/AGENTS.md'
    if instructions.is_file():
        candidates.append((instructions, Path('work-instructions/AGENTS.md')))
    if not candidates:
        raise RuntimeError('No PC installation artifacts were found')
    size = sum(source.stat().st_size for source, _ in candidates)
    if shutil.disk_usage(drive_root).free < size + 100 * 1024**2:
        raise RuntimeError('Not enough E-drive space for a new versioned backup')
    stamp = datetime.now(timezone(timedelta(hours=9))).strftime('%Y-%m-%d_%H-%M-%S')
    snapshot = destination_root / (stamp + '_' + uuid.uuid4().hex[:6])
    snapshot.mkdir(parents=True, exist_ok=False)
    manifest = {'created_at': datetime.now(timezone(timedelta(hours=9))).isoformat(),
                'status': 'copying', 'files': []}
    manifest_path = snapshot / 'manifest.json'
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        objects = destination_root / '.objects/sha256'
        for source, relative in candidates:
            destination = snapshot / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            print(f'Copying {relative}', flush=True)
            before = source.stat()
            source_hash = digest(source)
            blob = objects / source_hash[:2] / source_hash
            reused = blob.exists()
            if reused:
                if blob.stat().st_size != before.st_size or digest(blob) != source_hash:
                    raise RuntimeError(f'An existing backup object failed verification: {source.name}')
            else:
                blob.parent.mkdir(parents=True, exist_ok=True)
                partial = blob.with_name(blob.name + '.copying-' + uuid.uuid4().hex)
                shutil.copy2(source, partial)
                if partial.stat().st_size != before.st_size or digest(partial) != source_hash:
                    raise RuntimeError(f'Backup verification failed: {source.name}')
                partial.rename(blob)
            try:
                # Only E-to-E links: C originals remain independent and untouched.
                os.link(blob, destination)
                storage = 'reused-hardlink' if reused else 'new-hardlink'
            except OSError:
                shutil.copy2(blob, destination)
                storage = 'verified-copy'
            after = source.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise RuntimeError(f'Source changed while copying: {source.name}')
            if destination.stat().st_size != before.st_size or digest(destination) != source_hash:
                raise RuntimeError(f'Backup verification failed: {source.name}')
            manifest['files'].append({'path': relative.as_posix(), 'size': before.st_size,
                                      'sha256': source_hash, 'storage': storage})
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        source_bundle = snapshot / 'resource-analyzer.git.bundle'
        commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        previous_bundle = None
        pointer = destination_root / 'LATEST.txt'
        if pointer.is_file():
            previous = Path(pointer.read_text(encoding='utf-8').strip()).resolve()
            if previous.is_relative_to(destination_root) and (previous / 'manifest.json').is_file():
                previous_manifest = json.loads((previous / 'manifest.json').read_text(encoding='utf-8'))
                if previous_manifest.get('status') == 'verified' and previous_manifest.get('source_commit') == commit:
                    for row in previous_manifest.get('files', []):
                        candidate = previous / 'resource-analyzer.git.bundle'
                        if row['path'] == candidate.name and candidate.is_file() and digest(candidate) == row['sha256']:
                            previous_bundle = candidate
                            break
        if previous_bundle:
            try:
                os.link(previous_bundle, source_bundle)
            except OSError:
                shutil.copy2(previous_bundle, source_bundle)
        else:
            subprocess.run(['git', 'bundle', 'create', str(source_bundle), '--all'], cwd=ROOT, check=True, capture_output=True)
        subprocess.run(['git', 'bundle', 'verify', str(source_bundle)], cwd=ROOT, check=True, capture_output=True)
        manifest['files'].append({'path': source_bundle.name, 'size': source_bundle.stat().st_size,
                                  'sha256': digest(source_bundle)})
        manifest['source_commit'] = commit
        (snapshot / 'RESTORE.txt').write_text(
            'Resource Analyzer PC installation backup\n\n'
            '1. Run installables/ResourceAnalyzer-v*/ResourceAnalyzer.exe (prefer the newest version).\n'
            '2. Open http://127.0.0.1:7442/dashboard in the same PC browser.\n'
            '3. If CPU/RAM sensors are unavailable on a restored PC, run the bundled official PawnIO installer\n'
            '   or ResourceAnalyzer.exe --install-temperature-support, then restart the app.\n'
            '4. NVIDIA-Studio-* contains the original NVIDIA driver installer; hardware compatibility still applies.\n'
            '5. Source can be restored with: git clone resource-analyzer.git.bundle resource-analyzer\n\n'
            'manifest.json records SHA-256 and byte size for every copied artifact and the source bundle.\n'
            'Identical artifacts reuse verified E-drive objects through hardlinks where supported.\n'
            'Copy backup files to a working folder before editing them. The C-drive originals are independent.\n'
            'This snapshot does not contain live measurements, account credentials, installed Windows settings,\n'
            'or the entire C drive. Existing snapshots are retained. No background backup service is installed.\n', encoding='utf-8')
        manifest['status'] = 'verified'
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        (destination_root / 'LATEST.txt').write_text(str(snapshot) + '\n', encoding='utf-8')
        print(json.dumps({'status': 'verified', 'folder': str(snapshot), 'files': len(manifest['files']),
                          'bytes': sum(row['size'] for row in manifest['files'])}), flush=True)
    except Exception:
        manifest['status'] = 'incomplete'
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        raise


if __name__ == '__main__':
    main()
