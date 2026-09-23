#!/usr/bin/env python3
"""Publish lossless artwork originals and responsive WebP/AVIF variants.

The checked-in asset map keeps Arkpedia's stable, human-readable filenames
separate from the upstream game's internal filenames.  This script never
upscales: each requested width is capped at the original PNG width.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from PIL import Image

try:
    import pillow_avif  # noqa: F401 - registers AVIF with Pillow
except ImportError:
    pillow_avif = None


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WIDTHS = (320, 768, 1280)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def rendition_path(asset: str, format_name: str, width: int) -> Path:
    suffix = ".avif" if format_name == "avif" else ".webp"
    return ROOT / "variants" / format_name / f"w{width}" / Path(asset).with_suffix(suffix)


def build_one(task: tuple[str, str, tuple[int, ...]]) -> tuple[str, dict]:
    asset, original_text, widths = task
    original = Path(original_text)
    with Image.open(original) as source:
        source.load()
        original_width, original_height = source.size
        rows: dict[str, dict[str, dict]] = {"webp": {}, "avif": {}}
        for width in widths:
            target_width = min(width, original_width)
            target_height = max(1, round(original_height * target_width / original_width))
            resized = source if (target_width, target_height) == source.size else source.resize(
                (target_width, target_height), Image.Resampling.LANCZOS
            )
            for format_name in ("webp", "avif"):
                target = rendition_path(asset, format_name, width)
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(f".{target.name}.tmp")
                if format_name == "webp":
                    resized.save(temporary, format="WEBP", quality=84, method=4)
                else:
                    resized.save(temporary, format="AVIF", quality=62, speed=8)
                os.replace(temporary, target)
                rows[format_name][str(width)] = {
                    "path": target.relative_to(ROOT).as_posix(),
                    "width": target_width,
                    "height": target_height,
                    "bytes": target.stat().st_size,
                    "sha256": sha256(target),
                }
            if resized is not source:
                resized.close()
    return asset, {
        "original": {
            "path": original.relative_to(ROOT).as_posix(),
            "width": original_width,
            "height": original_height,
            "bytes": original.stat().st_size,
            "sha256": sha256(original),
        },
        "variants": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--jobs", type=int, default=max(1, min(8, os.cpu_count() or 1)))
    parser.add_argument("--widths", type=int, nargs="+", default=DEFAULT_WIDTHS)
    args = parser.parse_args()

    mapping = json.loads((ROOT / "asset-source-map.json").read_text())
    manifest_path = ROOT / "asset-manifest.json"
    previous_manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    previous_files = previous_manifest.get("files", {})
    widths = tuple(sorted(set(args.widths)))
    tasks: list[tuple[str, str, tuple[int, ...]]] = []
    detail: dict[str, dict] = {}
    for asset, source in sorted(mapping["files"].items()):
        source_path = args.source_root / source
        if not source_path.is_file():
            raise FileNotFoundError(f"Missing upstream artwork: {source}")
        original = ROOT / "originals" / Path(asset).with_suffix(".png")
        original.parent.mkdir(parents=True, exist_ok=True)
        if not original.exists() or sha256(original) != sha256(source_path):
            shutil.copyfile(source_path, original)
        source_hash = sha256(original)
        previous = previous_files.get(asset, {})
        previous_detail = {
            "original": previous.get("original"),
            "variants": previous.get("variants"),
        }
        expected = [
            rendition_path(asset, format_name, width)
            for format_name in ("webp", "avif")
            for width in widths
        ]
        if (
            previous_detail["original"]
            and previous_detail["variants"]
            and previous_detail["original"].get("sha256") == source_hash
            and all(path.is_file() for path in expected)
        ):
            detail[asset] = previous_detail
        else:
            tasks.append((asset, str(original), widths))

        # Repair a missing or invalid legacy fallback without recompressing every
        # existing root asset. The root URL remains backwards-compatible.
        fallback = ROOT / asset
        try:
            with Image.open(fallback) as image:
                image.verify()
        except (FileNotFoundError, OSError):
            with Image.open(original) as image:
                image.save(fallback, format="WEBP", quality=90, method=6)

    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        for index, (asset, row) in enumerate(pool.map(build_one, tasks), 1):
            detail[asset] = row
            if index % 50 == 0 or index == len(tasks):
                print(f"Rendered {index}/{len(tasks)} artworks", flush=True)

    # Keep non-artwork files visible in the manifest as well.  Existing callers
    # use the root-level files collection as the availability index.
    files: dict[str, dict] = {}
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or ".git" in path.parts or path.is_relative_to(args.source_root.resolve()) or ".cache" in path.parts:
            continue
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith(("originals/", "variants/", "scripts/", ".github/")):
            continue
        if path.suffix.lower() not in {".webp", ".png", ".jpg", ".jpeg", ".avif"}:
            continue
        with Image.open(path) as image:
            width, height = image.size
        row = {
            "bytes": path.stat().st_size,
            "sha256": sha256(path),
            "width": width,
            "height": height,
        }
        if relative in detail:
            row.update(detail[relative])
        elif relative not in mapping["files"]:
            # Hand-added artwork (sources/planner-outfits.md) stays out of the
            # source map on purpose: mapping it would replace its reviewed bytes
            # with the upstream render.  Nothing regenerates its original or
            # variants, so carry their committed rows forward; dropping them
            # leaves those files unlisted and fails validation.
            previous = previous_files.get(relative, {})
            row.update({key: previous[key] for key in ("original", "variants") if previous.get(key)})
        files[relative] = row

    manifest = {
        "version": 2,
        "sourceRepository": "https://github.com/yuanyan3060/ArknightsGameResource",
        "sourceCommit": args.source_commit,
        "widths": list(widths),
        "files": files,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")) + "\n")
    (ROOT / "source.json").write_text(json.dumps({
        "sourceRepository": manifest["sourceRepository"],
        "sourceCommit": args.source_commit,
        "paths": ["skin/"],
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
