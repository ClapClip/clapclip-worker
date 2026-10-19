"""Check source/history or a distribution without printing matched secrets."""
from __future__ import annotations

import argparse
import io
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tomllib
import zipfile

PRIVATE_DIRS = {
    'build', 'dist', 'logs', 'work', 'models', 'uploads', 'inputs', 'outputs',
    'results', '.mediaworker', '.ssh', '.aws', '__pycache__',
}
PRIVATE_SUFFIXES = {
    '.pem', '.key', '.pfx', '.p12', '.onnx', '.safetensors', '.pth', '.pt',
    '.ckpt', '.db', '.sqlite', '.sqlite3', '.pyc', '.bundle', '.zip', '.whl',
    '.gz', '.tgz', '.tar', '.7z',
}
PATTERNS = {
    'private key': re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----'),
    'GitHub token': re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b'),
    'AWS access key': re.compile(rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'Google API key': re.compile(rb'\bAIza[A-Za-z0-9_-]{30,}\b'),
    'JWT': re.compile(rb'\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b'),
}
MAX_SOURCE_BYTES = 2 * 1024 * 1024


def private_path(name: str) -> bool:
    path = PurePosixPath(name.replace('\\', '/'))
    return (
        path.is_absolute() or '..' in path.parts
        or any(part in PRIVATE_DIRS or part.startswith('.venv') for part in path.parts)
        or path.suffix.lower() in PRIVATE_SUFFIXES
        or (path.name.startswith('.env') and path.name not in {'.env.example', '.env.local.example'})
        or path.name.startswith('node-token')
        or path.name in {'id_rsa', 'id_ed25519'}
        or path.name.endswith('.log')
    )


def scan(name: str, data: bytes) -> list[str]:
    findings = []
    if len(data) > MAX_SOURCE_BYTES:
        return [f'{name}: oversized public source file']
    for label, pattern in PATTERNS.items():
        for match in pattern.finditer(data):
            line = data[:match.start()].count(b'\n') + 1
            findings.append(f'{name}:{line}: possible {label} (value hidden)')
    return findings


def check_archive(path: Path) -> list[str]:
    findings = []
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for item in archive.infolist():
                if item.is_dir():
                    continue
                if private_path(item.filename) or item.file_size > MAX_SOURCE_BYTES:
                    findings.append(f'{item.filename}: private/oversized archive entry')
                else:
                    findings.extend(scan(item.filename, archive.read(item)))
    else:
        with tarfile.open(path) as archive:
            for item in archive:
                if item.isdir():
                    continue
                if not item.isfile() or private_path(item.name) or item.size > MAX_SOURCE_BYTES:
                    findings.append(f'{item.name}: private/unsupported archive entry')
                else:
                    with archive.extractfile(item) as stream:
                        findings.extend(scan(item.name, stream.read()))
    return findings


def check_source(root: Path) -> list[str]:
    def git(*args: str) -> bytes:
        return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL)

    findings = []
    files = set(git('ls-files', '--cached', '--others', '--exclude-standard', '-z').decode().split('\0')) - {''}
    for name in sorted(files):
        path = root / name
        if private_path(name) or path.is_symlink():
            findings.append(f'{name}: private path or symlink in public source')
        elif path.is_file():
            if path.stat().st_size > MAX_SOURCE_BYTES:
                findings.append(f'{name}: oversized public source file')
            else:
                findings.extend(scan(name, path.read_bytes()))
    for required in ('LICENSE', 'THIRD_PARTY.md', 'SECURITY.md', 'CONTRIBUTING.md'):
        if required not in files:
            findings.append(f'{required}: missing release document')
    metadata = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))
    if metadata['project'].get('license') != 'MIT':
        findings.append('pyproject.toml: expected approved MIT license metadata')
    # Inspect deleted historical files as well; ignored working files are never read.
    objects = {}
    for row in git('rev-list', '--objects', '--all').decode().splitlines():
        oid, _, name = row.partition(' ')
        if name and private_path(name):
            findings.append(f'history/{name}: private path in published history')
        objects[oid] = name
    object_ids = '\n'.join(objects).encode() + b'\n'
    info = subprocess.check_output(
        ['git', '-C', str(root), 'cat-file', '--batch-check=%(objectname) %(objecttype) %(objectsize)'],
        input=object_ids, stderr=subprocess.DEVNULL,
    )
    selected = []
    blobs = 0
    for row in info.decode().splitlines():
        oid, kind, size = row.split()
        if kind not in {'blob', 'commit', 'tag'}:
            continue
        blobs += kind == 'blob'
        if int(size) > MAX_SOURCE_BYTES:
            findings.append(f'history/{oid[:12]}: oversized source object')
        else:
            selected.append(oid)
    raw = subprocess.check_output(
        ['git', '-C', str(root), 'cat-file', '--batch'],
        input=('\n'.join(selected) + '\n').encode(), stderr=subprocess.DEVNULL,
    )
    stream = io.BytesIO(raw)
    for oid in selected:
        header = stream.readline().decode().split()
        size = int(header[2])
        findings.extend(scan(f'history/{oid[:12]}', stream.read(size)))
        stream.read(1)
    print(f'Checked {len(files)} source files and {blobs} historical blobs.')
    return findings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, help='Check a source ZIP, sdist or wheel instead of Git.')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    findings = check_archive(args.archive) if args.archive else check_source(root)
    for finding in sorted(set(findings)):
        print(finding)
    if findings:
        raise SystemExit(1)
    print('Release content checks passed; this does not replace manual review.')


if __name__ == '__main__':
    main()
