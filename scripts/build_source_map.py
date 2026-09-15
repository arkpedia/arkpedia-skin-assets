#!/usr/bin/env python3
"""Match Arkpedia artwork names to public upstream PNGs by image content."""

from __future__ import annotations

import argparse
import json
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
ART_DIRS = tuple(f"{word}-star-skins" for word in ("one", "two", "three", "four", "five", "six"))
SOURCE_OVERRIDES = {
    # The inherited WebP is an empty file, so it cannot be content-matched.
    # The public skin table identifies Chestnut's second built-in portrait.
    "four-star-skins/Chestnut - Elite 2.webp": "skin/char_4041_chnut_2b.png",
}


def signature(path_text: str) -> tuple[str, int, int, list[float]]:
    path = Path(path_text)
    with Image.open(path) as image:
        image.load()
        width, height = image.size
        rgba = image.convert("RGBA")
        alpha = np.asarray(rgba.getchannel("A")) > 12
        # Ignore isolated transparent-canvas specks present in a few upstream
        # exports. They otherwise expand the crop and defeat perceptual matching.
        useful_rows = np.flatnonzero(alpha.sum(axis=1) >= max(2, round(width * 0.006)))
        useful_cols = np.flatnonzero(alpha.sum(axis=0) >= max(2, round(height * 0.006)))
        if useful_rows.size and useful_cols.size:
            rgba = rgba.crop((
                int(useful_cols[0]), int(useful_rows[0]),
                int(useful_cols[-1] + 1), int(useful_rows[-1] + 1),
            ))
        rgba.thumbnail((40, 40), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
        x = (40 - rgba.width) // 2
        y = (40 - rgba.height) // 2
        canvas.alpha_composite(rgba, (x, y))
        array = np.asarray(canvas, dtype=np.float32) / 255.0
        rgb, alpha = array[..., :3], array[..., 3:4]
        # Comparing both composites keeps transparent artwork pixels meaningful
        # without depending on hidden RGB values in fully transparent PNG pixels.
        black = rgb * alpha
        white = rgb * alpha + (1.0 - alpha)
        vector = np.concatenate((black.ravel(), white.ravel(), alpha.ravel()))
        return path_text, width, height, vector.tolist()


def collect(paths: list[Path], jobs: int, label: str) -> dict[str, tuple[int, int, np.ndarray]]:
    result: dict[str, tuple[int, int, np.ndarray]] = {}
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        for index, (path, width, height, vector) in enumerate(pool.map(signature, map(str, paths)), 1):
            result[path] = (width, height, np.asarray(vector, dtype=np.float32))
            if index % 100 == 0 or index == len(paths):
                print(f"Read {index}/{len(paths)} {label}", flush=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--jobs", type=int, default=max(1, min(8, os.cpu_count() or 1)))
    parser.add_argument("--max-error", type=float, default=0.015)
    parser.add_argument("--skin-table", type=Path)
    args = parser.parse_args()

    assets = sorted(path for folder in ART_DIRS for path in (ROOT / folder).glob("*.webp"))
    sources = sorted((args.source_root / "skin").glob("*.png"))
    matchable_assets = [
        path for path in assets if path.relative_to(ROOT).as_posix() not in SOURCE_OVERRIDES
    ]
    asset_signatures = collect(matchable_assets, args.jobs, "Arkpedia artworks")
    source_signatures = collect(sources, args.jobs, "upstream artworks")

    metadata: list[dict] = []
    if args.skin_table:
        table = json.loads(args.skin_table.read_text())
        metadata = list(table["charSkins"].values())

    def metadata_source(asset: Path) -> str | None:
        if not metadata:
            return None
        operator, label = asset.stem.rsplit(" - ", 1)
        if label in {"Base", "Elite 1", "Elite 2"}:
            candidates = [
                row for row in metadata
                if row["displaySkin"]["modelName"] == operator
                and row["displaySkin"]["skinName"] is None
            ]
            if label == "Base":
                candidates = [row for row in candidates if row["skinId"].endswith("#1")]
            elif label == "Elite 1":
                preferred = [row for row in candidates if row["skinId"].endswith("#1+")]
                candidates = preferred or [row for row in candidates if row["skinId"].endswith("#2")]
            else:
                candidates = [row for row in candidates if row["skinId"].endswith("#2")]
        else:
            candidates = [
                row for row in metadata
                if row["displaySkin"]["modelName"] == operator
                and row["displaySkin"]["skinName"] == label
            ]
        existing = []
        for row in candidates:
            relative = f"skin/{row['portraitId']}b.png"
            if (args.source_root / relative).is_file():
                existing.append(relative)
        return existing[0] if len(existing) == 1 else None

    by_dimensions: dict[tuple[int, int], list[tuple[str, np.ndarray]]] = {}
    for source, (width, height, vector) in source_signatures.items():
        by_dimensions.setdefault((width, height), []).append((source, vector))

    mapping: dict[str, str] = {}
    diagnostics: list[dict] = []
    used: set[str] = set()
    for asset in assets:
        relative_asset = asset.relative_to(ROOT).as_posix()
        if relative_asset in SOURCE_OVERRIDES:
            relative_source = SOURCE_OVERRIDES[relative_asset]
            source = str(args.source_root / relative_source)
            if not Path(source).is_file():
                raise FileNotFoundError(f"Missing override source: {relative_source}")
            mapping[relative_asset] = relative_source
            used.add(source)
            diagnostics.append({
                "asset": relative_asset,
                "source": relative_source,
                "error": None,
                "nextError": None,
                "reason": "metadata override for empty inherited WebP",
            })
            continue
        direct_source = metadata_source(asset)
        if direct_source:
            source = str(args.source_root / direct_source)
            mapping[relative_asset] = direct_source
            used.add(source)
            diagnostics.append({
                "asset": relative_asset,
                "source": direct_source,
                "error": None,
                "nextError": None,
                "reason": "exact English skin metadata",
            })
            continue
        width, height, vector = asset_signatures[str(asset)]
        # Some inherited WebPs were normalized to a larger canvas (commonly
        # 1024 -> 2048) while preserving the artwork. Match by aspect ratio even
        # when another, unrelated upstream file happens to share exact pixels.
        candidates = [
            (source, candidate)
            for rows in by_dimensions.values()
            for source, candidate in rows
        ]
        if not candidates:
            raise RuntimeError(f"No upstream artwork has aspect {width}x{height}: {asset.relative_to(ROOT)}")
        matrix = np.stack([candidate for _, candidate in candidates])
        errors = np.mean(np.square(matrix - vector), axis=1)
        order = np.argsort(errors)
        ranked = [(float(errors[index]), candidates[index][0]) for index in order[:2]]
        error, source = ranked[0]
        next_error = ranked[1][0] if len(ranked) > 1 else None
        if error > args.max_error:
            raise RuntimeError(
                f"Weak match ({error:.6f}) for {asset.relative_to(ROOT)} -> {Path(source).name}"
            )
        relative_source = Path(source).relative_to(args.source_root).as_posix()
        mapping[relative_asset] = relative_source
        used.add(source)
        diagnostics.append({
            "asset": relative_asset,
            "source": relative_source,
            "error": round(error, 8),
            "nextError": round(next_error, 8) if next_error is not None else None,
        })

    output = {
        "version": 1,
        "sourceRepository": "https://github.com/yuanyan3060/ArknightsGameResource",
        "files": dict(sorted(mapping.items())),
    }
    (ROOT / "asset-source-map.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    (ROOT / "source-map-diagnostics.json").write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2) + "\n")
    print(f"Mapped {len(mapping)} artworks to {len(used)} upstream PNGs")


if __name__ == "__main__":
    main()
