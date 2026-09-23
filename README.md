# Arkpedia skin assets

Skin and outfit media used by [Arkpedia](https://github.com/Arkpedia/arkpedia).

The root rarity folders contain the full-size WebP artwork used as the stable
fallback URLs. Lossless upstream PNGs live under `originals/` and are requested
only for artwork zoom. Routine cards and operator pages use the generated
`variants/webp/` and `variants/avif/` renditions at 320, 768, and 1280 pixels.
The manifest records dimensions and hashes for every rendition.

Artwork is synced from the public
[ArknightsGameResource](https://github.com/yuanyan3060/ArknightsGameResource)
mirror. The daily workflow runs after the Global server reset window and skips
work when the recorded upstream revision has not changed. It commits only when
an original, variant or root artwork file changes, because Arkpedia pins skin
URLs to this repository's latest commit; an upstream revision that leaves every
mapped skin untouched publishes nothing. `source.json` therefore records the
upstream revision of the last sync that changed artwork.

These game assets remain the property of Hypergryph, Yostar, and their respective rights holders. This repository does not grant a license to reuse or redistribute them. Corrections and takedown requests may be submitted through the repository issue tracker.
