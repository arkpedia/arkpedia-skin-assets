#!/usr/bin/env python3
"""Validate manifest inventory plus decoded image bytes (full or staged changes)."""
import argparse
import hashlib
import io
import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
MEDIA = {'.png', '.webp', '.jpg', '.jpeg', '.avif', '.gif', '.ico', '.svg'}

def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--staged', action='store_true')
    args = parser.parse_args()
    manifest = json.loads((ROOT / 'asset-manifest.json').read_text())
    records = {}
    for path, row in manifest['files'].items():
        records[path] = row
        if row.get('original'):
            records[row['original']['path']] = row['original']
        for widths in row.get('variants', {}).values():
            for variant in widths.values():
                records[variant['path']] = variant
    tracked = {p for p in git('ls-files', '-z').decode().split('\0') if p}
    media = {p for p in tracked if Path(p).suffix.lower() in MEDIA and not p.startswith('scripts/')}
    missing = set(records) - tracked
    unlisted = media - records.keys()
    if missing or unlisted:
        raise ValueError(f'Inventory mismatch: missing={sorted(missing)[:12]}, unlisted={sorted(unlisted)[:12]}')
    if args.staged:
        deleted = git('diff', '--cached', '--name-only', '--diff-filter=D', '-z').decode().split('\0')
        if any(Path(p).suffix.lower() in MEDIA for p in deleted if p):
            raise ValueError('Automated media deletion is forbidden; review it in a separate PR')
        paths = set(git('diff', '--cached', '--name-only', '-z').decode().split('\0')) & records.keys()
    else:
        paths = records.keys()
    for path in sorted(paths):
        if Path(path).is_absolute() or '..' in Path(path).parts:
            raise ValueError(f'Unsafe path: {path}')
        local = ROOT / path
        data = local.read_bytes() if local.exists() else git('show', f':{path}')
        row = records[path]
        if not 0 < len(data) < 100 * 1024 * 1024:
            raise ValueError(f'Empty or oversized image: {path}')
        if len(data) != row['bytes'] or hashlib.sha256(data).hexdigest() != row['sha256']:
            raise ValueError(f'Hash/size mismatch: {path}')
        if Path(path).suffix.lower() == '.svg':
            ET.fromstring(data)
            continue
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            if image.width <= 0 or image.height <= 0:
                raise ValueError(f'Invalid image dimensions: {path}')
            if row.get('width') and (image.width, image.height) != (row['width'], row['height']):
                raise ValueError(f'Dimensions differ from manifest: {path}')
    print(f'Validated complete inventory ({len(records)} paths), decoded and hashed {len(paths)} images.')

if __name__ == '__main__':
    main()
