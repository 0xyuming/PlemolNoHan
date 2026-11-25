#!/usr/bin/env python3
"""Verify generated PlemolNoHan font files for coverage, keycap metrics, and OFL names."""

from __future__ import annotations

import argparse
import sys
import unicodedata
from pathlib import Path

from fontTools.ttLib import TTFont

REMOVED_RANGES: tuple[tuple[int, int], ...] = (
    (0x2E80, 0x2EFF),
    (0x2F00, 0x2FDF),
    (0x3001, 0x303F),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0xFF01, 0xFF5E),
    (0x20000, 0x3FFFF),
)

REQUIRED_CODEPOINTS: tuple[tuple[int, str], ...] = (
    (0x0044, "LATIN CAPITAL LETTER D"),
    (0x0057, "LATIN CAPITAL LETTER W"),
    (0x21E5, "RIGHTWARDS ARROW TO BAR"),
    (0x21E7, "UPWARDS WHITE ARROW"),
    (0x3000, "IDEOGRAPHIC SPACE"),
    (0x3042, "HIRAGANA LETTER A"),
    (0x30A2, "KATAKANA LETTER A"),
)

KEYCAP_CODEPOINTS: tuple[tuple[int, str], ...] = (
    (0x21E7, "UPWARDS WHITE ARROW (Shift)"),
    (0x2303, "UP ARROWHEAD (Control)"),
    (0x2318, "PLACE OF INTEREST SIGN (Command)"),
    (0x2325, "OPTION KEY (Option)"),
    (0x232B, "ERASE TO THE LEFT (Delete)"),
    (0x238B, "BROKEN CIRCLE WITH NORTHWEST ARROW (Escape)"),
    (0x23CE, "RETURN SYMBOL (Return)"),
)

FORBIDDEN_CODEPOINTS: tuple[tuple[int, str], ...] = (
    (0x2600, "BLACK SUN WITH RAYS"),
    (0x26A0, "WARNING SIGN"),
    (0x2702, "BLACK SCISSORS"),
    (0x025B, "LATIN SMALL LETTER OPEN E"),
)

RENAMED_NAME_IDS = frozenset({1, 3, 4, 6, 16, 18, 21})


def is_pua(cp: int) -> bool:
    return (0xE000 <= cp <= 0xF8FF) or (0xF0000 <= cp <= 0x10FFFF)


def merged_unicode_cmap(font: TTFont) -> dict[int, str]:
    merged: dict[int, str] = {}
    cmap_table = font["cmap"]
    for table in sorted(cmap_table.tables, key=lambda t: (t.format, t.platformID)):
        if table.isUnicode():
            merged.update(table.cmap)
    best = cmap_table.getBestCmap()
    if best:
        merged.update(best)
    return merged


def verify_font(path: Path) -> list[str]:
    """Return a list of verification error messages for a single TTF file."""
    errors: list[str] = []
    font = TTFont(path)
    try:
        cmap = merged_unicode_cmap(font)
        hmtx = font["hmtx"]
        glyf = font["glyf"]

        # 1. Removed Han / CJK punctuation / Fullwidth ASCII ranges must be empty.
        leaked = [
            cp
            for cp in cmap
            if any(start <= cp <= end for start, end in REMOVED_RANGES)
        ]
        if leaked:
            sample = ", ".join(f"U+{cp:04X}" for cp in sorted(leaked)[:5])
            errors.append(f"Found {len(leaked)} codepoint(s) in removed ranges: {sample}")

        # 2. Required codepoints must still be present.
        for cp, label in REQUIRED_CODEPOINTS:
            if cp not in cmap:
                errors.append(f"Missing required codepoint U+{cp:04X} ({label})")

        # 3. Forbidden double-width EAW=N symbols must be removed, and zero EAW=N wide glyphs may remain.
        for cp, label in FORBIDDEN_CODEPOINTS:
            if cp in cmap:
                errors.append(f"Forbidden EAW=N wide codepoint still present: U+{cp:04X} ({label})")

        if 0x0044 not in cmap:
            errors.append("Missing reference glyph U+0044 ('D')")
            return errors

        base_adv = hmtx[cmap[0x0044]][0]
        wide_eaw_n = [
            cp
            for cp, gname in cmap.items()
            if not is_pua(cp)
            and hmtx[gname][0] > base_adv * 1.1
            and unicodedata.east_asian_width(chr(cp)) == "N"
        ]
        if wide_eaw_n:
            sample = ", ".join(f"U+{cp:04X}" for cp in sorted(wide_eaw_n)[:5])
            errors.append(
                f"Found {len(wide_eaw_n)} non-PUA EAW=N wide glyph(s): {sample}"
            )

        # 4. Embedded 1-cell macOS keycap icons: presence, 1-cell advance, breathing margins, and alignment.
        keycap_boxes: dict[int, tuple[int, int, int, int]] = {}
        for cp, label in KEYCAP_CODEPOINTS:
            if cp not in cmap:
                errors.append(f"Missing embedded keycap U+{cp:04X} ({label})")
                continue
            gname = cmap[cp]
            adv = hmtx[gname][0]
            if adv != base_adv:
                errors.append(
                    f"Keycap U+{cp:04X} advance {adv} != base_adv {base_adv}"
                )
            glyph = glyf[gname]
            if glyph.numberOfContours <= 0:
                errors.append(f"Keycap U+{cp:04X} ({gname}) has empty/composite outline")
                continue
            if glyph.xMin < 25 or glyph.xMax > base_adv - 25:
                errors.append(
                    f"Keycap U+{cp:04X} tight/overflowing margins [{glyph.xMin}..{glyph.xMax}] in {base_adv} cell"
                )
            keycap_boxes[cp] = (glyph.xMin, glyph.yMin, glyph.xMax, glyph.yMax)

        align_cps = (0x2318, 0x2325, 0x21E7)
        if all(cp in keycap_boxes for cp in align_cps):
            heights = [keycap_boxes[cp][3] - keycap_boxes[cp][1] for cp in align_cps]
            ycs = [(keycap_boxes[cp][1] + keycap_boxes[cp][3]) / 2.0 for cp in align_cps]
            if max(heights) - min(heights) > 25:
                errors.append(
                    f"Keycap heights (⌘/⌥/⇧) diverge by {max(heights) - min(heights)} > 25: {heights}"
                )
            if max(ycs) - min(ycs) > 10:
                errors.append(
                    f"Keycap vertical centers (⌘/⌥/⇧) diverge by {max(ycs) - min(ycs):.1f} > 10: {ycs}"
                )

        # 5. OFL name table compliance.
        names = font["name"].names
        copyright_records = [rec for rec in names if rec.nameID == 0]
        if not copyright_records:
            errors.append("Missing copyright record (nameID 0)")
        for rec in copyright_records:
            text = rec.toUnicode()
            for required in ("Paper-Mono", "Yuko Otawara", "IBM Corp.", "[PlemolNoHan]"):
                if required not in text:
                    errors.append(
                        f"Copyright record (plat={rec.platformID}) missing {required!r}"
                    )

        for rec in names:
            if rec.nameID in RENAMED_NAME_IDS:
                text = rec.toUnicode()
                if "PlemolNoHan" not in text:
                    errors.append(
                        f"nameID {rec.nameID} (plat={rec.platformID}) missing 'PlemolNoHan': {text!r}"
                    )
                if "PlemolJP" in text or "Plex" in text:
                    errors.append(
                        f"nameID {rec.nameID} (plat={rec.platformID}) contains reserved name: {text!r}"
                    )

        # 6. OpenType stylistic set UI names (nameID >= 256) preserved.
        ss_names = [rec.toUnicode() for rec in names if rec.nameID >= 256]
        if not ss_names:
            errors.append("Missing OpenType stylistic set UI names (nameID >= 256)")
        elif not any("slashed number zero" in s for s in ss_names):
            errors.append("Missing stylistic set UI name 'slashed number zero' (nameID >= 256)")
    finally:
        font.close()

    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify generated PlemolNoHan .ttf files."
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=Path("dist"),
        help="Directory containing .ttf files to verify (default: dist)",
    )
    parser.add_argument(
        "--glob",
        default="*.ttf",
        help="Recursive filename glob pattern under --dir (default: *.ttf)",
    )
    parser.add_argument(
        "--expected-count",
        type=int,
        default=None,
        help="Optional exact count of .ttf files expected",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    target_dir = args.dir.resolve()
    if not target_dir.is_dir():
        print(f"ERROR: Directory does not exist: {target_dir}", file=sys.stderr)
        return 1

    ttf_files = sorted(target_dir.rglob(args.glob))
    if not ttf_files:
        print(f"ERROR: No files matching {args.glob!r} under {target_dir}", file=sys.stderr)
        return 1

    if args.expected_count is not None and len(ttf_files) != args.expected_count:
        print(
            f"ERROR: Expected {args.expected_count} font(s), found {len(ttf_files)} under {target_dir}",
            file=sys.stderr,
        )
        return 1

    failed = 0
    for path in ttf_files:
        errors = verify_font(path)
        if errors:
            failed += 1
            print(f"FAIL: {path}")
            for err in errors:
                print(f"  - {err}")

    if failed:
        print(f"\nVerification FAILED: {failed}/{len(ttf_files)} font(s) had errors.", file=sys.stderr)
        return 1

    print(f"PASS: Verified {len(ttf_files)}/{len(ttf_files)} font(s) in {target_dir} with 0 errors.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
