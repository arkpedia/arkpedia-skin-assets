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
import subprocess
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from PIL import Image

try:
    import pillow_avif  # noqa: F401 - registers AVIF with Pillow
except ImportError:
    pillow_avif = None


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WIDTHS = (320, 768, 1280)
ROOT_SUFFIXES = {".webp", ".png", ".jpg", ".jpeg", ".avif"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def rendition_path(asset: str, format_name: str, width: int) -> Path:
    suffix = ".avif" if format_name == "avif" else ".webp"
    return ROOT / "variants" / format_name / f"w{width}" / Path(asset).with_suffix(suffix)


def is_root_asset(relative: str) -> bool:
    """Whether the rebuild lists a repository path as a root manifest row."""
    if relative.startswith(("originals/", "variants/", "scripts/", ".github/")):
        return False
    return Path(relative).suffix.lower() in ROOT_SUFFIXES


def manifest_files(
    root_rows: dict[str, dict],
    detail: dict[str, dict],
    mapping: dict,
    previous_files: dict[str, dict],
) -> dict[str, dict]:
    """Attach the nested original/variants rows to each published root row."""
    files: dict[str, dict] = {}
    for relative, row in sorted(root_rows.items()):
        if relative in detail:
            row = {**row, **detail[relative]}
        elif relative not in mapping["files"]:
            # Hand-added artwork (sources/planner-outfits.md) stays out of the
            # source map on purpose: mapping it would replace its reviewed bytes
            # with the upstream render.  Nothing regenerates its original or
            # variants, so carry their committed rows forward; dropping them
            # leaves those files unlisted and fails validation.
            previous = previous_files.get(relative, {})
            row = {**row, **{key: previous[key] for key in ("original", "variants") if previous.get(key)}}
        files[relative] = row
    return files


def listed_paths(files: dict[str, dict]) -> set[str]:
    """Every path a manifest lists, expanded the way validate_images.py does."""
    paths = set(files)
    for row in files.values():
        if row.get("original"):
            paths.add(row["original"]["path"])
        for widths in row.get("variants", {}).values():
            paths.update(variant["path"] for variant in widths.values())
    return paths


def verify_rebuild(mapping: dict, previous_files: dict[str, dict]) -> None:
    """Fail when a rebuild of the committed state would stop listing tracked media.

    The daily sync regenerates the manifest from scratch and then runs
    validate_images.py --staged, so a row the rebuild drops turns that job red
    even though the committed manifest still validates.  This replays the same
    listing at push time without upstream artwork or media bytes: git supplies
    the inventory and, as when upstream is unchanged, mapped artwork keeps its
    previous original/variants rows.
    """
    # Imported here, not at module level: arkpedia's publish-catalogue-assets.py
    # loads this file by path for build_one, without scripts/ on sys.path.
    from validate_images import MEDIA

    tracked = {
        path
        for path in subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
        if path
    }
    root_rows: dict[str, dict] = {relative: {} for relative in tracked if is_root_asset(relative)}
    detail: dict[str, dict] = {}
    for asset in mapping["files"]:
        previous = previous_files.get(asset, {})
        if previous.get("original") and previous.get("variants"):
            detail[asset] = {"original": previous["original"], "variants": previous["variants"]}
    listed = listed_paths(manifest_files(root_rows, detail, mapping, previous_files))
    media = {path for path in tracked if Path(path).suffix.lower() in MEDIA and not path.startswith("scripts/")}
    missing = listed - tracked
    unlisted = media - listed
    dropped = listed_paths(previous_files) - listed
    if missing or unlisted or dropped:
        raise ValueError(
            f"Rebuilt manifest would not match the repository: missing={sorted(missing)[:12]}, "
            f"unlisted={sorted(unlisted)[:12]}, dropped={sorted(dropped)[:12]}"
        )
    print(f"Verified that a rebuild lists all {len(listed)} tracked media paths.")


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
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--source-commit")
    parser.add_argument("--jobs", type=int, default=max(1, min(8, os.cpu_count() or 1)))
    parser.add_argument("--widths", type=int, nargs="+", default=DEFAULT_WIDTHS)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="check that rebuilding the committed state keeps every tracked media path listed",
    )
    args = parser.parse_args()
    if not args.verify and (args.source_root is None or args.source_commit is None):
        parser.error("--source-root and --source-commit are required unless --verify is given")

    mapping = json.loads((ROOT / "asset-source-map.json").read_text())
    manifest_path = ROOT / "asset-manifest.json"
    previous_manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    previous_files = previous_manifest.get("files", {})
    if args.verify:
        verify_rebuild(mapping, previous_files)
        return
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
    root_rows: dict[str, dict] = {}
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or ".git" in path.parts or path.is_relative_to(args.source_root.resolve()) or ".cache" in path.parts:
            continue
        relative = path.relative_to(ROOT).as_posix()
        if not is_root_asset(relative):
            continue
        with Image.open(path) as image:
            width, height = image.size
        root_rows[relative] = {
            "bytes": path.stat().st_size,
            "sha256": sha256(path),
            "width": width,
            "height": height,
        }
    files = manifest_files(root_rows, detail, mapping, previous_files)

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
