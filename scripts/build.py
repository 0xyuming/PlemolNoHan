#!/usr/bin/env python3
"""Build PlemolNoHan fonts by subsetting upstream PlemolJP releases."""

from __future__ import annotations

import argparse
import io
import logging
import os
import shutil
import unicodedata
import urllib.request
import zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from fontTools import subset
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

DEFAULT_VERSION = "3.1.0"
UPSTREAM_REPO = "yuru7/PlemolJP"
VARIANT_PREFIXES = {
    "standard": "PlemolJP",
    "nf": "PlemolJP_NF",
    "hs": "PlemolJP_HS",
}

DEFAULT_PAPER_MONO_VERSION = "1.000"
PAPER_MONO_REPO = "paper-design/paper-mono"
KEYCAP_CODEPOINTS: tuple[int, ...] = (
    0x21E7,  # ⇧ UPWARDS WHITE ARROW
    0x2303,  # ⌃ UP ARROWHEAD
    0x2318,  # ⌘ PLACE OF INTEREST SIGN
    0x2325,  # ⌥ OPTION KEY
    0x232B,  # ⌫ ERASE TO THE LEFT
    0x238B,  # ⎋ BROKEN CIRCLE WITH NORTHWEST ARROW
    0x23CE,  # ⏎ RETURN SYMBOL
)

# Weight compensation mapping for Paper Mono variable font scaled down to 1 cell.
NARROW_CELL_WGHT_MAP: dict[int, int] = {
    100: 220,
    200: 320,
    300: 480,
    400: 700,
    450: 740,
    500: 770,
    600: 800,
    700: 800,
}
WIDE_CELL_WGHT_MAP: dict[int, int] = {
    100: 200,
    200: 280,
    300: 420,
    400: 620,
    450: 680,
    500: 730,
    600: 780,
    700: 800,
}

# Static Unicode ranges removed from PlemolJP:
# Intentionally keeps U+3000 (Ideographic Space) and Halfwidth Katakana (U+FF61-U+FF9F).
STATIC_REMOVED_RANGES: tuple[tuple[int, int], ...] = (
    (0x2E80, 0x2EFF),   # CJK Radicals Supplement
    (0x2F00, 0x2FDF),   # Kangxi Radicals
    (0x3001, 0x303F),   # CJK Symbols and Punctuation
    (0x3400, 0x4DBF),   # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),   # CJK Unified Ideographs
    (0xF900, 0xFAFF),   # CJK Compatibility Ideographs
    (0xFF01, 0xFF5E),   # Fullwidth ASCII & Punctuation
    (0x20000, 0x3FFFF), # CJK Unified Ideographs Extensions B-I & Compat Supplement
)

# Optional Kana ranges removed when --strip-kana is passed.
KANA_RANGES: tuple[tuple[int, int], ...] = (
    (0x3040, 0x309F),   # Hiragana
    (0x30A0, 0x30FF),   # Katakana
    (0x31F0, 0x31FF),   # Katakana Phonetic Extensions
    (0xFF61, 0xFF9F),   # Halfwidth Katakana & Punctuation
)

PAPER_MONO_COPYRIGHT_MARKER = "[Paper Mono]"
PAPER_MONO_COPYRIGHT_BLOCK = (
    "\n\n[Paper Mono]\n"
    "Copyright 2025 The Paper-Mono.Git Project Authors "
    "(https://github.com/paper-design/paper-mono)"
)
DERIVED_COPYRIGHT_MARKER = "[PlemolNoHan]"
DERIVED_COPYRIGHT_BLOCK = (
    "\n\n[PlemolNoHan]\n"
    "Copyright (c) 2026 Yeziii1210 (derived from PlemolJP)"
)
NAME_IDS_TO_RENAME = frozenset({1, 3, 4, 6, 16, 18, 21})

_PAPER_MONO_CACHE: dict[tuple[int, int], TTFont] = {}


@dataclass(frozen=True)
class BuildStats:
    """Summary of one subsetted font file."""

    family_dir: str
    filename: str
    out_path: str
    original_cps: int
    kept_cps: int
    removed_static: int
    removed_eaw_n_wide: int
    removed_eaw_a_wide: int
    removed_kana: int
    patched_keycaps: int


def is_pua(cp: int) -> bool:
    """Return True if cp belongs to Unicode Private Use Areas."""
    return (0xE000 <= cp <= 0xF8FF) or (0xF0000 <= cp <= 0x10FFFF)


def in_ranges(cp: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    """Return True if cp falls inside any inclusive (start, end) range."""
    return any(start <= cp <= end for start, end in ranges)


def get_unicode_cmap(font: TTFont) -> dict[int, str]:
    """Collect the merged Unicode cmap (preferring Format 12 over Format 4)."""
    merged: dict[int, str] = {}
    cmap_table = font["cmap"]
    for table in sorted(cmap_table.tables, key=lambda t: (t.format, t.platformID)):
        if table.isUnicode():
            merged.update(table.cmap)
    best = cmap_table.getBestCmap()
    if best:
        merged.update(best)
    return merged


def rewrite_name_table(font: TTFont, *, include_paper_mono: bool) -> None:
    """Update font name records while preserving upstream copyright notices verbatim."""
    for rec in font["name"].names:
        text = rec.toUnicode()
        if rec.nameID == 0:
            extra_blocks = ""
            if include_paper_mono and PAPER_MONO_COPYRIGHT_MARKER not in text:
                extra_blocks += PAPER_MONO_COPYRIGHT_BLOCK
            if DERIVED_COPYRIGHT_MARKER not in text:
                extra_blocks += DERIVED_COPYRIGHT_BLOCK
            new_text = text.rstrip() + extra_blocks if extra_blocks else text
        elif rec.nameID in NAME_IDS_TO_RENAME:
            new_text = text.replace("PlemolJP", "PlemolNoHan")
        else:
            continue

        try:
            rec.string = new_text.encode(rec.getEncoding())
        except UnicodeEncodeError:
            rec.string = new_text.encode("utf-8", errors="replace")


def compute_drop_codepoints(
    font: TTFont,
    *,
    strip_ambiguous_wide: bool,
    strip_kana: bool,
) -> tuple[set[int], set[int], int, dict[str, int]]:
    """Determine which codepoints to keep and drop for a single font."""
    cmap = get_unicode_cmap(font)
    hmtx = font["hmtx"]
    if 0x0044 not in cmap:
        raise ValueError("Font is missing ASCII 'D' (U+0044) reference glyph")
    base_adv = hmtx[cmap[0x0044]][0]
    wide_threshold = base_adv * 1.1

    drop_cps: set[int] = set()
    counts = {
        "original_cps": len(cmap),
        "removed_static": 0,
        "removed_eaw_n_wide": 0,
        "removed_eaw_a_wide": 0,
        "removed_kana": 0,
    }

    for cp, glyph_name in cmap.items():
        if in_ranges(cp, STATIC_REMOVED_RANGES):
            drop_cps.add(cp)
            counts["removed_static"] += 1
            continue

        if strip_kana and in_ranges(cp, KANA_RANGES):
            drop_cps.add(cp)
            counts["removed_kana"] += 1
            continue

        if is_pua(cp):
            continue

        adv = hmtx[glyph_name][0]
        if adv > wide_threshold:
            eaw = unicodedata.east_asian_width(chr(cp))
            if eaw == "N":
                drop_cps.add(cp)
                counts["removed_eaw_n_wide"] += 1
            elif strip_ambiguous_wide and eaw == "A" and 0x2000 <= cp <= 0x2BFF:
                drop_cps.add(cp)
                counts["removed_eaw_a_wide"] += 1

    keep_cps = set(cmap.keys()) - drop_cps
    return keep_cps, drop_cps, base_adv, counts


def build_subset_options(font: TTFont) -> subset.Options:
    """Create fontTools Subsetter options preserving layout features and UI names."""
    extra_name_ids = sorted(
        {rec.nameID for rec in font["name"].names if rec.nameID >= 256}
    )
    return subset.Options(
        layout_features=["*"],
        name_IDs=["*", *extra_name_ids],
        name_legacy=True,
        name_languages=["*"],
        glyph_names=True,
        hinting=True,
        legacy_kern=True,
        notdef_glyph=True,
        notdef_outline=True,
        recommended_glyphs=True,
        symbol_cmap=True,
    )


def prune_dropped_from_cmap(font: TTFont, drop_cps: set[int]) -> None:
    """Ensure dropped codepoints do not survive in cmap via bidi mirroring or closure."""
    cmap_table = font["cmap"]
    kept_tables = []
    for table in cmap_table.tables:
        if table.format in (14, 15):
            table.uvsDict = {
                uvs: [(u, g) for u, g in pairs if u not in drop_cps]
                for uvs, pairs in table.uvsDict.items()
                if uvs not in drop_cps
            }
            table.uvsDict = {k: v for k, v in table.uvsDict.items() if v}
            if table.uvsDict:
                kept_tables.append(table)
        elif hasattr(table, "cmap"):
            for cp in drop_cps:
                table.cmap.pop(cp, None)
            if table.cmap:
                kept_tables.append(table)
        else:
            kept_tables.append(table)
    cmap_table.tables = kept_tables


def resolve_target_wght(font: TTFont, base_adv: int) -> int:
    """Map target font weight class and cell width to compensated Paper Mono wght."""
    weight_class = int(getattr(font["OS/2"], "usWeightClass", 400) or 400)
    wght_map = NARROW_CELL_WGHT_MAP if base_adv <= 560 else WIDE_CELL_WGHT_MAP
    closest = min(wght_map.keys(), key=lambda key: abs(key - weight_class))
    return wght_map[closest]


def get_paper_mono_instance(paper_mono_bytes: bytes, target_wght: int) -> TTFont:
    """Return a cached Paper Mono variable font instance for target_wght."""
    cache_key = (len(paper_mono_bytes), target_wght)
    inst = _PAPER_MONO_CACHE.get(cache_key)
    if inst is None:
        base_font = TTFont(io.BytesIO(paper_mono_bytes))
        inst = instantiateVariableFont(base_font, {"wght": target_wght}, inplace=True)
        _PAPER_MONO_CACHE[cache_key] = inst
    return inst


def _measure_glyph_bounds(font: TTFont, codepoint: int) -> tuple[float, float, float, float]:
    """Measure exact outline bounding box (x0, y0, x1, y1) for a codepoint."""
    cmap = font.getBestCmap()
    if not cmap or codepoint not in cmap:
        raise ValueError(f"Source font missing keycap codepoint U+{codepoint:04X}")
    glyph_set = font.getGlyphSet()
    pen = BoundsPen(glyph_set)
    glyph_set[cmap[codepoint]].draw(pen)
    if pen.bounds is None:
        raise ValueError(f"Empty outline for U+{codepoint:04X}")
    return pen.bounds


def inject_keycap_glyphs(
    font: TTFont,
    paper_inst_font: TTFont,
    base_adv: int,
) -> int:
    """Embed 1-cell, weight-matched keycap glyphs from Paper Mono into font."""
    cap_height = int(getattr(font["OS/2"], "sCapHeight", 677) or 677)
    target_yc = cap_height / 2.0

    paper_cmap = paper_inst_font.getBestCmap()
    paper_glyph_set = paper_inst_font.getGlyphSet()
    raw_bounds = {cp: _measure_glyph_bounds(paper_inst_font, cp) for cp in KEYCAP_CODEPOINTS}

    ref_w = max(
        raw_bounds[cp][2] - raw_bounds[cp][0]
        for cp in (0x2318, 0x2325, 0x21E7)
    )
    cmd_y0, cmd_y1 = raw_bounds[0x2318][1], raw_bounds[0x2318][3]
    ref_yc = (cmd_y0 + cmd_y1) / 2.0
    scale = (0.84 * base_adv) / ref_w

    cmap = get_unicode_cmap(font)
    glyf_table = font["glyf"]
    hmtx_table = font["hmtx"]
    glyph_order = list(font.getGlyphOrder())
    order_changed = False

    gdef_class_defs = None
    if "GDEF" in font and getattr(font["GDEF"].table, "GlyphClassDef", None) is not None:
        gdef_class_defs = font["GDEF"].table.GlyphClassDef.classDefs

    for cp in KEYCAP_CODEPOINTS:
        x0, _, x1, _ = raw_bounds[cp]
        g_scale = min(scale, (0.86 * base_adv) / (x1 - x0))
        dx = (base_adv / 2.0) - g_scale * ((x0 + x1) / 2.0)
        dy = target_yc - g_scale * ref_yc

        tt_pen = TTGlyphPen(None)
        transform_pen = TransformPen(tt_pen, (g_scale, 0.0, 0.0, g_scale, dx, dy))
        paper_glyph_set[paper_cmap[cp]].draw(transform_pen)
        new_glyph = tt_pen.glyph()

        existing_gname = cmap.get(cp)
        is_shared = existing_gname is not None and any(
            u != cp and g == existing_gname for u, g in cmap.items()
        )
        if existing_gname is not None and not is_shared:
            gname = existing_gname
        else:
            gname = f"uni{cp:04X}"
            while gname in glyf_table or gname in glyph_order:
                gname = f"{gname}_keycap"
            glyph_order.append(gname)
            order_changed = True

        glyf_table[gname] = new_glyph
        new_glyph.recalcBounds(glyf_table)
        hmtx_table[gname] = (base_adv, int(new_glyph.xMin))

        for table in font["cmap"].tables:
            if table.isUnicode() and table.format in (4, 12):
                table.cmap[cp] = gname
        cmap[cp] = gname

        if gdef_class_defs is not None:
            gdef_class_defs[gname] = 1

    if order_changed:
        font.setGlyphOrder(glyph_order)

    return len(KEYCAP_CODEPOINTS)


def subset_font_bytes(
    font_bytes: bytes,
    rel_out_path: Path,
    output_dir: Path,
    *,
    strip_ambiguous_wide: bool,
    strip_kana: bool,
    paper_mono_bytes: bytes | None,
) -> BuildStats:
    """Subset a single TTF byte buffer, patch keycaps, and write the output font."""
    logging.getLogger("fontTools.subset").setLevel(logging.WARNING)
    font = TTFont(io.BytesIO(font_bytes))
    keep_cps, drop_cps, base_adv, counts = compute_drop_codepoints(
        font,
        strip_ambiguous_wide=strip_ambiguous_wide,
        strip_kana=strip_kana,
    )

    subsetter = subset.Subsetter(build_subset_options(font))
    subsetter.populate(unicodes=sorted(keep_cps))
    subsetter.subset(font)
    prune_dropped_from_cmap(font, drop_cps)

    patched_keycaps = 0
    if paper_mono_bytes is not None:
        target_wght = resolve_target_wght(font, base_adv)
        paper_inst = get_paper_mono_instance(paper_mono_bytes, target_wght)
        patched_keycaps = inject_keycap_glyphs(font, paper_inst, base_adv)

    rewrite_name_table(font, include_paper_mono=paper_mono_bytes is not None)

    dest_path = output_dir / rel_out_path
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    font.save(dest_path)
    kept_final = len(get_unicode_cmap(font))
    font.close()

    family_dir = rel_out_path.parent.as_posix() or "."
    return BuildStats(
        family_dir=family_dir,
        filename=rel_out_path.name,
        out_path=str(dest_path),
        original_cps=counts["original_cps"],
        kept_cps=kept_final,
        removed_static=counts["removed_static"],
        removed_eaw_n_wide=counts["removed_eaw_n_wide"],
        removed_eaw_a_wide=counts["removed_eaw_a_wide"],
        removed_kana=counts["removed_kana"],
        patched_keycaps=patched_keycaps,
    )


def _worker_from_zip(
    zip_path: str,
    member: str,
    rel_out_path: Path,
    output_dir: Path,
    strip_ambiguous_wide: bool,
    strip_kana: bool,
    paper_mono_bytes: bytes | None,
) -> BuildStats:
    with zipfile.ZipFile(zip_path) as zf:
        data = zf.read(member)
    return subset_font_bytes(
        data,
        rel_out_path,
        output_dir,
        strip_ambiguous_wide=strip_ambiguous_wide,
        strip_kana=strip_kana,
        paper_mono_bytes=paper_mono_bytes,
    )


def _worker_from_file(
    src_path: Path,
    rel_out_path: Path,
    output_dir: Path,
    strip_ambiguous_wide: bool,
    strip_kana: bool,
    paper_mono_bytes: bytes | None,
) -> BuildStats:
    return subset_font_bytes(
        src_path.read_bytes(),
        rel_out_path,
        output_dir,
        strip_ambiguous_wide=strip_ambiguous_wide,
        strip_kana=strip_kana,
        paper_mono_bytes=paper_mono_bytes,
    )


def rewrite_relative_path(rel_path: Path) -> Path:
    """Replace PlemolJP with PlemolNoHan across every path segment."""
    parts = [part.replace("PlemolJP", "PlemolNoHan") for part in rel_path.parts]
    return Path(*parts)


def strip_common_root(paths: list[Path]) -> list[Path]:
    """Strip a shared top-level wrapper directory if present (e.g. PlemolJP_v3.1.0/)."""
    if not paths:
        return paths
    first_parts = {p.parts[0] for p in paths if len(p.parts) > 1}
    if len(first_parts) == 1 and all(len(p.parts) > 1 for p in paths):
        return [Path(*p.parts[1:]) for p in paths]
    return paths


def download_archive(url: str, dest: Path) -> Path:
    """Download an archive to dest if not already cached."""
    if dest.exists() and dest.stat().st_size > 0:
        print(f"Using cached archive: {dest}")
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"Downloading {url} -> {dest}")
    req = urllib.request.Request(url, headers={"User-Agent": "PlemolNoHan-Builder/0.1"})
    with urllib.request.urlopen(req) as resp, tmp.open("wb") as out:
        shutil.copyfileobj(resp, out)
    tmp.replace(dest)
    return dest


def download_upstream_zip(version: str, variant: str, cache_dir: Path) -> Path:
    """Download the requested PlemolJP release zip into cache_dir if absent."""
    prefix = VARIANT_PREFIXES[variant]
    zip_name = f"{prefix}_v{version}.zip"
    url = f"https://github.com/{UPSTREAM_REPO}/releases/download/v{version}/{zip_name}"
    return download_archive(url, cache_dir / zip_name)


def download_paper_mono_zip(version: str, cache_dir: Path) -> Path:
    """Download the Paper Mono release zip into cache_dir if absent."""
    zip_name = f"paper-mono-v{version}.zip"
    url = f"https://github.com/{PAPER_MONO_REPO}/releases/download/v{version}/{zip_name}"
    return download_archive(url, cache_dir / zip_name)


def load_paper_mono_var_bytes(zip_path: Path) -> bytes:
    """Load PaperMono[wght].ttf bytes from the Paper Mono release archive."""
    if not zip_path.is_file():
        raise FileNotFoundError(f"Paper Mono zip not found: {zip_path}")
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            if Path(member).name.lower() == "papermono[wght].ttf":
                return zf.read(member)
    raise FileNotFoundError(f"PaperMono[wght].ttf not found inside {zip_path}")


def collect_jobs(args: argparse.Namespace) -> tuple[str, list[tuple[str | Path, Path]]]:
    """Collect (source_ref, rewritten_relative_output_path) jobs."""
    if args.input_dir is not None:
        root = args.input_dir.resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"Input directory not found: {root}")
        src_files = sorted(root.rglob("*.ttf"))
        if not src_files:
            raise FileNotFoundError(f"No .ttf files found under {root}")
        rel_paths = strip_common_root([p.relative_to(root) for p in src_files])
        return "dir", [
            (src, rewrite_relative_path(rel)) for src, rel in zip(src_files, rel_paths)
        ]

    zip_path = (
        args.input_zip.resolve()
        if args.input_zip is not None
        else download_upstream_zip(args.version, args.variant, args.cache_dir)
    )
    if not zip_path.is_file():
        raise FileNotFoundError(f"Input zip not found: {zip_path}")

    with zipfile.ZipFile(zip_path) as zf:
        members = sorted(
            m for m in zf.namelist() if m.lower().endswith(".ttf") and not m.endswith("/")
        )
    if not members:
        raise FileNotFoundError(f"No .ttf files found inside {zip_path}")

    rel_paths = strip_common_root([Path(m) for m in members])
    jobs: list[tuple[str | Path, Path]] = [
        (f"{zip_path}::{member}", rewrite_relative_path(rel))
        for member, rel in zip(members, rel_paths)
    ]
    return "zip", jobs


def install_to_macos_fonts(stats_list: list[BuildStats]) -> Path:
    """Copy generated .ttf files flat into ~/Library/Fonts/."""
    fonts_dir = Path.home() / "Library" / "Fonts"
    fonts_dir.mkdir(parents=True, exist_ok=True)
    for item in stats_list:
        shutil.copy2(item.out_path, fonts_dir / item.filename)
    return fonts_dir


def print_summary(stats_list: list[BuildStats]) -> None:
    """Print grouped per-family build statistics."""
    by_family: dict[str, list[BuildStats]] = {}
    for item in stats_list:
        by_family.setdefault(item.family_dir, []).append(item)

    print("\nBuild summary:")
    for family, items in sorted(by_family.items()):
        rep = items[0]
        print(
            f"  {family:22s}: {len(items):2d} fonts | "
            f"kept={rep.kept_cps}/{rep.original_cps} cps | "
            f"removed han/cjk={rep.removed_static}, "
            f"eaw_n_wide={rep.removed_eaw_n_wide}, "
            f"eaw_a_wide={rep.removed_eaw_a_wide}, "
            f"kana={rep.removed_kana}, "
            f"keycaps={rep.patched_keycaps}"
        )
    print(f"Total fonts built: {len(stats_list)}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build PlemolNoHan fonts from upstream PlemolJP releases."
    )
    parser.add_argument(
        "--version",
        default=DEFAULT_VERSION,
        help=f"Upstream PlemolJP release version (default: {DEFAULT_VERSION})",
    )
    parser.add_argument(
        "--variant",
        choices=sorted(VARIANT_PREFIXES.keys()),
        default="standard",
        help="Upstream PlemolJP release variant (default: standard)",
    )
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument(
        "--input-zip",
        type=Path,
        default=None,
        help="Path to an existing PlemolJP release zip (skips download)",
    )
    source_group.add_argument(
        "--input-dir",
        type=Path,
        default=None,
        help="Path to an extracted directory of PlemolJP .ttf files (skips download)",
    )
    parser.add_argument(
        "--paper-mono-version",
        default=DEFAULT_PAPER_MONO_VERSION,
        help=f"Paper Mono release version for macOS keycap icons (default: {DEFAULT_PAPER_MONO_VERSION})",
    )
    parser.add_argument(
        "--paper-mono-zip",
        type=Path,
        default=None,
        help="Path to an existing Paper Mono release zip (skips download)",
    )
    parser.add_argument(
        "--no-keycap-patch",
        action="store_true",
        help="Disable embedding 1-cell macOS keycap icons from Paper Mono",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(".cache"),
        help="Cache directory for downloaded archives (default: .cache)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("dist"),
        help="Output directory for generated fonts (default: dist)",
    )
    parser.add_argument(
        "--strip-ambiguous-wide",
        action="store_true",
        help="Also strip double-width East Asian Ambiguous (EAW=A) symbols in U+2000-U+2BFF",
    )
    parser.add_argument(
        "--strip-kana",
        action="store_true",
        help="Also strip Hiragana, Katakana, and Halfwidth Katakana",
    )
    parser.add_argument(
        "--install-macos",
        action="store_true",
        help="Copy generated .ttf files flat into ~/Library/Fonts/",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=min(os.cpu_count() or 1, 8),
        help="Worker processes for font subsetting (default: min(cpus, 8))",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    mode, jobs = collect_jobs(args)

    paper_mono_bytes: bytes | None = None
    if not args.no_keycap_patch:
        paper_zip = (
            args.paper_mono_zip.resolve()
            if args.paper_mono_zip is not None
            else download_paper_mono_zip(args.paper_mono_version, args.cache_dir)
        )
        paper_mono_bytes = load_paper_mono_var_bytes(paper_zip)

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Subsetting {len(jobs)} font(s) into {output_dir} using {args.jobs} worker(s)...")
    stats_list: list[BuildStats] = []

    with ProcessPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = []
        for src_ref, rel_out in jobs:
            if mode == "zip":
                zip_path_str, member = str(src_ref).split("::", 1)
                futures.append(
                    pool.submit(
                        _worker_from_zip,
                        zip_path_str,
                        member,
                        rel_out,
                        output_dir,
                        args.strip_ambiguous_wide,
                        args.strip_kana,
                        paper_mono_bytes,
                    )
                )
            else:
                futures.append(
                    pool.submit(
                        _worker_from_file,
                        Path(src_ref),
                        rel_out,
                        output_dir,
                        args.strip_ambiguous_wide,
                        args.strip_kana,
                        paper_mono_bytes,
                    )
                )

        for fut in as_completed(futures):
            stats_list.append(fut.result())

    stats_list.sort(key=lambda s: (s.family_dir, s.filename))
    print_summary(stats_list)

    if args.install_macos:
        fonts_dir = install_to_macos_fonts(stats_list)
        print(f"Installed {len(stats_list)} font(s) to {fonts_dir}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
