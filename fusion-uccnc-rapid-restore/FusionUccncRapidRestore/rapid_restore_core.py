"""UCCNC-safe conservative G-code rapid-move restorer for Fusion Personal output.

Converts safe G1 moves back to G0 rapids:
  1. Pure upward Z retracts (reaching at least Safe Z by default)
  2. XY positioning moves at or above Safe Z (with no downward Z motion)

Preserves UCCNC modal G0/G1 state and modal feedrate (F) so that cutting,
plunge, arc, and canned-cycle moves never inherit G0 or lose their feedrate.
"""

from dataclasses import dataclass, field
import argparse
import math
import os
import re
import sys
from typing import List, Optional, Tuple


# Matches a single G-code word like G0, G91.1, X-12.345, F1000., N120
_WORD_RE = re.compile(r"([A-Za-z])\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))")

# Non-modal or special G-codes that must never be converted to G0
_BLOCKING_GCODES = frozenset(
    {
        4,   # Dwell
        10,  # Data setting
        28,  # Return to home
        30,  # Secondary home
        41,  # Cutter comp left
        42,  # Cutter comp right
        43,  # Tool length offset
        49,  # Cancel tool length offset
        53,  # Machine coordinates
        92,  # Coordinate system offset
    }
)

# Canned cycle G-codes (modal motion group 1)
_CANNED_CYCLE_GCODES = frozenset({73, 74, 76, 81, 82, 83, 84, 85, 86, 87, 88, 89})

# Work coordinate system G-codes
_WCS_GCODES = frozenset({54, 55, 56, 57, 58, 59, 59.1, 59.2, 59.3})

# Rotary axes skipped in conservative mode
_ROTARY_AXES = frozenset({"A", "B", "C"})


@dataclass
class GCodeWord:
    letter: str
    raw_value: str
    value: float
    start: int
    end: int


@dataclass
class RestoreOptions:
    safe_z: float = 5.0
    retract_must_reach_safe_z: bool = True
    tolerance: float = 1e-4
    add_header_comment: bool = True
    strip_feedrate_on_rapid: bool = True


@dataclass
class InspectionSummary:
    units: str = "mm"
    line_count: int = 0
    min_z: Optional[float] = None
    max_z: Optional[float] = None
    g43_z_values: List[float] = field(default_factory=list)
    suggested_safe_z: float = 5.0


@dataclass
class RestoreStats:
    units: str = "mm"
    total_lines: int = 0
    converted_total: int = 0
    converted_z_retracts: int = 0
    converted_xy_rapids: int = 0
    g1_restored_count: int = 0
    feedrate_reinjected_count: int = 0
    rapid_distance: float = 0.0
    sample_changes: List[Tuple[int, str, str]] = field(default_factory=list)


def _split_code_and_comments(line: str) -> List[Tuple[int, int]]:
    """Return list of (start, end) spans of non-comment code segments in a line."""
    spans: List[Tuple[int, int]] = []
    n = len(line)
    i = 0
    in_paren = 0
    seg_start: Optional[int] = None

    while i < n:
        ch = line[i]
        if ch == ";" and in_paren == 0:
            if seg_start is not None:
                spans.append((seg_start, i))
                seg_start = None
            break
        elif ch == "(":
            if in_paren == 0 and seg_start is not None:
                spans.append((seg_start, i))
                seg_start = None
            in_paren += 1
        elif ch == ")" and in_paren > 0:
            in_paren -= 1
        else:
            if in_paren == 0 and seg_start is None:
                seg_start = i
        i += 1

    if seg_start is not None:
        spans.append((seg_start, n))
    return spans


def parse_gcode_words(line: str) -> List[GCodeWord]:
    """Extract G-code words from non-comment portions of a line."""
    words: List[GCodeWord] = []
    for seg_start, seg_end in _split_code_and_comments(line):
        segment = line[seg_start:seg_end]
        for match in _WORD_RE.finditer(segment):
            letter = match.group(1).upper()
            raw_val = match.group(2)
            words.append(
                GCodeWord(
                    letter=letter,
                    raw_value=raw_val,
                    value=float(raw_val),
                    start=seg_start + match.start(),
                    end=seg_start + match.end(),
                )
            )
    return words


def _is_g_code(word: GCodeWord, code: float) -> bool:
    return word.letter == "G" and abs(word.value - code) < 1e-6


def _apply_span_edits(line: str, edits: List[Tuple[int, int, str]]) -> str:
    """Apply non-overlapping (start, end, replacement) edits from right to left."""
    out = line
    for start, end, repl in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        out = out[:start] + repl + out[end:]
    # Clean up any double spaces created by removing a word (preserve leading indent)
    leading_len = len(out) - len(out.lstrip(" \t"))
    leading = out[:leading_len]
    body = out[leading_len:]
    body = re.sub(r" {2,}", " ", body).rstrip(" ")
    return leading + body


def _code_end_offset(line: str, words: List[GCodeWord]) -> int:
    """Return index immediately after the last parsed G-code word (before comments)."""
    if words:
        return words[-1].end
    spans = _split_code_and_comments(line)
    if spans:
        return spans[-1][1]
    return len(line)


def _motion_insert_offset(line: str, words: List[GCodeWord]) -> Tuple[int, str]:
    """Return (offset, prefix) where a new G0/G1 word should be inserted."""
    if words and words[0].letter == "N":
        return words[0].end, " "
    if words:
        return words[0].start, ""
    leading_len = len(line) - len(line.lstrip(" \t"))
    return leading_len, ""


def inspect_gcode_text(gcode_text: str) -> InspectionSummary:
    """Inspect G-code text to detect units, Z bounds, and G43 safe Z heights."""
    summary = InspectionSummary()
    lines = gcode_text.splitlines()
    summary.line_count = len(lines)

    distance_mode = 90  # G90 absolute
    g43_zs: List[float] = []

    for line in lines:
        words = parse_gcode_words(line)
        if not words:
            continue

        g_vals = [w.value for w in words if w.letter == "G"]
        if any(abs(g - 20.0) < 1e-6 for g in g_vals):
            summary.units = "in"
        if any(abs(g - 21.0) < 1e-6 for g in g_vals):
            summary.units = "mm"
        if any(abs(g - 90.0) < 1e-6 for g in g_vals):
            distance_mode = 90
        if any(abs(g - 91.0) < 1e-6 for g in g_vals):
            distance_mode = 91

        has_non_wcs_z = any(
            any(abs(g - code) < 1e-6 for code in (28, 30, 53, 92)) for g in g_vals
        )
        has_g43 = any(abs(g - 43.0) < 1e-6 for g in g_vals)

        if distance_mode == 90 and not has_non_wcs_z:
            for w in words:
                if w.letter == "Z":
                    if summary.min_z is None or w.value < summary.min_z:
                        summary.min_z = w.value
                    if summary.max_z is None or w.value > summary.max_z:
                        summary.max_z = w.value
                    if has_g43 and w.value not in g43_zs:
                        g43_zs.append(w.value)

    summary.g43_z_values = sorted(g43_zs)
    if summary.g43_z_values:
        summary.suggested_safe_z = min(summary.g43_z_values)
    else:
        summary.suggested_safe_z = 0.2 if summary.units == "in" else 5.0
    return summary


def restore_rapids_text(
    gcode_text: str,
    options: Optional[RestoreOptions] = None,
) -> Tuple[str, RestoreStats]:
    """Convert conservative rapid candidates in UCCNC G-code from G1 to G0.

    Returns (converted_gcode_text, stats).
    """
    if options is None:
        options = RestoreOptions()

    line_ending = "\r\n" if "\r\n" in gcode_text else "\n"
    raw_lines = gcode_text.splitlines()
    stats = RestoreStats(total_lines=len(raw_lines))

    # Modal state of the ORIGINAL program
    orig_motion: Optional[int] = None  # 0, 1, 2, 3, or canned cycle int
    distance_mode: int = 90            # 90 (absolute) or 91 (incremental)
    program_f: Optional[str] = None    # Raw F value string from original program

    # Modal state of the EMITTED (UCCNC machine) program
    machine_motion: Optional[int] = None
    machine_f: Optional[str] = None

    # Current known WCS tool position
    cur_x: Optional[float] = None
    cur_y: Optional[float] = None
    cur_z: Optional[float] = None

    out_lines: List[str] = []

    for line_idx, line in enumerate(raw_lines, start=1):
        words = parse_gcode_words(line)
        if not words:
            out_lines.append(line)
            continue

        g_words = [w for w in words if w.letter == "G"]
        m_words = [w for w in words if w.letter == "M"]
        f_words = [w for w in words if w.letter == "F"]

        # Update units
        for gw in g_words:
            if abs(gw.value - 20.0) < 1e-6:
                stats.units = "in"
            elif abs(gw.value - 21.0) < 1e-6:
                stats.units = "mm"

        # Determine distance mode effective on this line
        line_distance_mode = distance_mode
        for gw in g_words:
            if abs(gw.value - 90.0) < 1e-6:
                line_distance_mode = 90
            elif abs(gw.value - 91.0) < 1e-6:
                line_distance_mode = 91

        # Determine explicit motion G-code on this line
        explicit_motion_word: Optional[GCodeWord] = None
        explicit_motion_code: Optional[int] = None
        cancels_cycle = False
        for gw in g_words:
            if abs(gw.value - 0.0) < 1e-6:
                explicit_motion_word = gw
                explicit_motion_code = 0
            elif abs(gw.value - 1.0) < 1e-6:
                explicit_motion_word = gw
                explicit_motion_code = 1
            elif abs(gw.value - 2.0) < 1e-6:
                explicit_motion_word = gw
                explicit_motion_code = 2
            elif abs(gw.value - 3.0) < 1e-6:
                explicit_motion_word = gw
                explicit_motion_code = 3
            elif abs(gw.value - 80.0) < 1e-6:
                cancels_cycle = True
                explicit_motion_word = None
                explicit_motion_code = None
            elif any(abs(gw.value - c) < 1e-6 for c in _CANNED_CYCLE_GCODES):
                explicit_motion_word = gw
                explicit_motion_code = int(round(gw.value))

        # Check blocking / non-convertible conditions
        has_blocking_g = any(
            any(abs(gw.value - b) < 1e-6 for b in _BLOCKING_GCODES) for gw in g_words
        )
        has_wcs_change = any(
            any(abs(gw.value - wcs) < 1e-6 for wcs in _WCS_GCODES) for gw in g_words
        )
        has_tool_change = any(abs(mw.value - 6.0) < 1e-6 for mw in m_words)
        has_rotary = any(w.letter in _ROTARY_AXES for w in words)

        # Axis words on this line
        x_word = next((w for w in words if w.letter == "X"), None)
        y_word = next((w for w in words if w.letter == "Y"), None)
        z_word = next((w for w in words if w.letter == "Z"), None)
        has_xyz = (x_word is not None) or (y_word is not None) or (z_word is not None)

        # Effective original motion on this line
        if cancels_cycle:
            effective_orig_motion = None
        elif explicit_motion_code is not None:
            effective_orig_motion = explicit_motion_code
        else:
            effective_orig_motion = orig_motion

        # Update original program feedrate
        line_f_raw = f_words[-1].raw_value if f_words else None
        if line_f_raw is not None:
            program_f = line_f_raw

        # Target coordinates (only valid in G90 without G28/G30/G53/G92)
        has_non_wcs_move = any(
            any(abs(gw.value - code) < 1e-6 for code in (28, 30, 53, 92))
            for gw in g_words
        )

        # If WCS changed on this line before coordinates, prior WCS coords are unknown
        if has_wcs_change:
            cur_x = None
            cur_y = None
            cur_z = None

        target_x = x_word.value if (x_word is not None and line_distance_mode == 90) else cur_x
        target_y = y_word.value if (y_word is not None and line_distance_mode == 90) else cur_y
        target_z = z_word.value if (z_word is not None and line_distance_mode == 90) else cur_z

        # Decide whether this line is a conservative rapid candidate
        convert_to_rapid = False
        conversion_kind = ""

        if (
            line_distance_mode == 90
            and not has_blocking_g
            and not has_tool_change
            and not has_rotary
            and effective_orig_motion == 1
            and has_xyz
            and cur_z is not None
            and target_z is not None
        ):
            tol = options.tolerance
            xy_changed = (
                (x_word is not None and (cur_x is None or abs(x_word.value - cur_x) > tol))
                or (y_word is not None and (cur_y is None or abs(y_word.value - cur_y) > tol))
            )
            z_up = target_z > cur_z + tol
            z_down = target_z < cur_z - tol

            if not xy_changed and z_up:
                # Case B: Pure upward Z retract
                if (not options.retract_must_reach_safe_z) or (
                    target_z >= options.safe_z - tol
                ):
                    convert_to_rapid = True
                    conversion_kind = "z_retract"
            elif (
                cur_z >= options.safe_z - tol
                and target_z >= options.safe_z - tol
                and not z_down
            ):
                # Case A: Safe-Z XY (or XY + upward Z) positioning move
                convert_to_rapid = True
                conversion_kind = "xy_rapid"

        # Rewrite line as needed
        new_line = line
        edits: List[Tuple[int, int, str]] = []

        if convert_to_rapid:
            if explicit_motion_word is not None:
                edits.append(
                    (explicit_motion_word.start, explicit_motion_word.end, "G0")
                )
            elif machine_motion != 0:
                ins_pos, prefix = _motion_insert_offset(line, words)
                edits.append((ins_pos, ins_pos, f"{prefix}G0 "))

            if options.strip_feedrate_on_rapid and f_words:
                for fw in f_words:
                    # Also strip one preceding space if present
                    s = fw.start - 1 if fw.start > 0 and line[fw.start - 1] == " " else fw.start
                    edits.append((s, fw.end, ""))

            if edits:
                new_line = _apply_span_edits(line, edits)

            # Record stats
            stats.converted_total += 1
            if conversion_kind == "z_retract":
                stats.converted_z_retracts += 1
            else:
                stats.converted_xy_rapids += 1

            # Accumulate distance if start and end are known
            dx = (target_x - cur_x) if (cur_x is not None and target_x is not None) else 0.0
            dy = (target_y - cur_y) if (cur_y is not None and target_y is not None) else 0.0
            dz = (target_z - cur_z) if (cur_z is not None and target_z is not None) else 0.0
            stats.rapid_distance += math.sqrt(dx * dx + dy * dy + dz * dz)

            if len(stats.sample_changes) < 15:
                stats.sample_changes.append((line_idx, line.strip(), new_line.strip()))

            machine_motion = 0
            if not options.strip_feedrate_on_rapid and line_f_raw is not None:
                machine_f = line_f_raw
        else:
            # Not converted to rapid: ensure UCCNC modal G1 and modal F are intact!
            needs_feed_motion = effective_orig_motion in (1, 2, 3) or (
                effective_orig_motion in _CANNED_CYCLE_GCODES
            )

            # Restore G1 if this line relies on modal G1 (no explicit motion G-code)
            # and we previously switched the machine to G0.
            if (
                has_xyz
                and not has_non_wcs_move
                and explicit_motion_word is None
                and effective_orig_motion == 1
                and machine_motion != 1
            ):
                ins_pos, prefix = _motion_insert_offset(line, words)
                edits.append((ins_pos, ins_pos, f"{prefix}G1 "))
                stats.g1_restored_count += 1
                machine_motion = 1
            elif explicit_motion_code is not None:
                machine_motion = explicit_motion_code
            elif cancels_cycle:
                machine_motion = None

            # Re-inject F if this line executes a feed move (G1/G2/G3/canned cycle)
            # and the machine's modal F drifted because we stripped F on an earlier G0.
            if (
                needs_feed_motion
                and has_xyz
                and not has_non_wcs_move
                and line_f_raw is None
                and program_f is not None
                and machine_f != program_f
            ):
                end_pos = _code_end_offset(line, words)
                edits.append((end_pos, end_pos, f" F{program_f}"))
                stats.feedrate_reinjected_count += 1
                machine_f = program_f
            elif line_f_raw is not None:
                machine_f = line_f_raw

            if edits:
                new_line = _apply_span_edits(line, edits)

        out_lines.append(new_line)

        # Advance persistent modal state
        distance_mode = line_distance_mode
        orig_motion = effective_orig_motion

        # Update known WCS position
        if has_non_wcs_move:
            if not has_xyz:
                cur_x = cur_y = cur_z = None
            else:
                if x_word is not None:
                    cur_x = None
                if y_word is not None:
                    cur_y = None
                if z_word is not None:
                    cur_z = None
        elif has_tool_change:
            cur_z = None
        elif line_distance_mode == 90:
            if x_word is not None:
                cur_x = x_word.value
            if y_word is not None:
                cur_y = y_word.value
            if z_word is not None:
                cur_z = z_word.value
        else:
            # G91 incremental
            if x_word is not None and cur_x is not None:
                cur_x += x_word.value
            elif x_word is not None:
                cur_x = None
            if y_word is not None and cur_y is not None:
                cur_y += y_word.value
            elif y_word is not None:
                cur_y = None
            if z_word is not None and cur_z is not None:
                cur_z += z_word.value
            elif z_word is not None:
                cur_z = None

    if options.add_header_comment:
        header = (
            f"(UCCNC Rapid Restore: {stats.converted_total} G1->G0 moves "
            f"[{stats.converted_z_retracts} Z retracts, {stats.converted_xy_rapids} Safe-Z XY], "
            f"Safe Z >= {options.safe_z:g} {stats.units})"
        )
        # Keep '%' program start marker on line 1 if present
        if out_lines and out_lines[0].strip() == "%":
            out_lines.insert(1, header)
        else:
            out_lines.insert(0, header)

    trailing_newline = line_ending if gcode_text.endswith(("\n", "\r")) else ""
    return line_ending.join(out_lines) + trailing_newline, stats


def build_default_output_path(input_path: str, suffix: str = "_rapids") -> str:
    """Return default output path next to input file, e.g. part.nc -> part_rapids.nc."""
    root, ext = os.path.splitext(input_path)
    return f"{root}{suffix}{ext or '.nc'}"


def restore_rapids_file(
    input_path: str,
    output_path: Optional[str] = None,
    options: Optional[RestoreOptions] = None,
) -> Tuple[str, RestoreStats]:
    """Read input G-code file, restore conservative rapids, and write output file."""
    with open(input_path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    converted, stats = restore_rapids_text(text, options=options)
    target_path = output_path or build_default_output_path(input_path)

    with open(target_path, "w", encoding="utf-8", newline="") as f:
        f.write(converted)

    return target_path, stats


def format_stats_report(
    input_path: str,
    output_path: Optional[str],
    options: RestoreOptions,
    stats: RestoreStats,
    dry_run: bool = False,
) -> str:
    """Build a concise human-readable summary of conversion results."""
    mode_label = "DRY RUN (no file written)" if dry_run else f"Saved to: {output_path}"
    lines = [
        f"Source: {input_path}",
        mode_label,
        f"Safe Z threshold: {options.safe_z:g} {stats.units}",
        f"Converted G1 -> G0 moves: {stats.converted_total} "
        f"({stats.converted_z_retracts} Z retracts, {stats.converted_xy_rapids} Safe-Z XY)",
        f"Modal G1 restored after rapid: {stats.g1_restored_count}",
        f"Feedrate (F) re-injected: {stats.feedrate_reinjected_count}",
        f"Total rapid distance restored: {stats.rapid_distance:.1f} {stats.units}",
    ]
    if stats.sample_changes:
        lines.append("")
        lines.append("First converted lines:")
        for line_no, before, after in stats.sample_changes[:8]:
            lines.append(f"  L{line_no}: {before}  -->  {after}")
    return "\n".join(lines)


def _cli(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Restore conservative G0 rapid moves in Fusion Personal UCCNC G-code."
    )
    parser.add_argument("input", help="Path to input .nc / .tap G-code file")
    parser.add_argument(
        "-o", "--output", default=None, help="Output file path (default: <name>_rapids<ext>)"
    )
    parser.add_argument(
        "--safe-z",
        type=float,
        default=None,
        help="Safe Z height in file units (default: auto-detect from G43 Z)",
    )
    parser.add_argument(
        "--allow-sub-safe-retracts",
        action="store_true",
        help="Allow pure upward Z retracts even when target Z is below Safe Z",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Analyze and preview conversions without writing an output file",
    )
    args = parser.parse_args(argv)

    with open(args.input, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    summary = inspect_gcode_text(text)
    safe_z = args.safe_z if args.safe_z is not None else summary.suggested_safe_z
    options = RestoreOptions(
        safe_z=safe_z,
        retract_must_reach_safe_z=not args.allow_sub_safe_retracts,
    )

    if args.dry_run:
        _, stats = restore_rapids_text(text, options=options)
        print(format_stats_report(args.input, None, options, stats, dry_run=True))
    else:
        out_path, stats = restore_rapids_file(args.input, args.output, options=options)
        print(format_stats_report(args.input, out_path, options, stats, dry_run=False))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
