"""Unit tests for the UCCNC conservative G-code rapid-move restorer."""

import os
import sys
import tempfile
import unittest

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "FusionUccncRapidRestore",
    ),
)

import rapid_restore_core as core  # noqa: E402


SAMPLE_FUSION_UCCNC = """\
(1001)
G90 G94 G91.1 G40 G49 G17
G21
G28 G91 Z0.
G90
(2D CONTOUR1)
T1 M6
S18000 M3
G54
G0 X10. Y20.
G43 Z15. H1
G1 Z5. F1000.
G1 Z-2. F333.
G1 X50. F1000.
G1 Z15.
X80. Y40.
Z5.
Z-2. F333.
G1 X100. F1000.
G2 X110. Y50. I0. J10.
G1 Z15. F1000.
M5
G28 G91 Z0.
G90
M30
"""


class TestRapidRestoreCore(unittest.TestCase):
    def test_inspect_detects_units_z_bounds_and_g43(self):
        summary = core.inspect_gcode_text(SAMPLE_FUSION_UCCNC)
        self.assertEqual(summary.units, "mm")
        self.assertAlmostEqual(summary.min_z, -2.0)
        self.assertAlmostEqual(summary.max_z, 15.0)
        self.assertEqual(summary.g43_z_values, [15.0])
        self.assertAlmostEqual(summary.suggested_safe_z, 15.0)

    def test_g91_1_is_not_treated_as_incremental_g91(self):
        out, stats = core.restore_rapids_text(
            SAMPLE_FUSION_UCCNC,
            core.RestoreOptions(safe_z=5.0, add_header_comment=False),
        )
        # Should convert:
        # 1. G1 Z15. -> G0 Z15. (Z retract)
        # 2. X80. Y40. -> stays modal G0 (Safe-Z XY)
        # 3. G1 Z15. F1000. -> G0 Z15. (Z retract)
        self.assertEqual(stats.converted_total, 3)
        self.assertEqual(stats.converted_z_retracts, 2)
        self.assertEqual(stats.converted_xy_rapids, 1)
        # Modal G1 must be restored on 'Z5.' plunge after 'X80. Y40.' rapid!
        self.assertEqual(stats.g1_restored_count, 1)
        self.assertIn("G0 Z15.", out)
        self.assertIn("G1 Z5.", out)
        # Arc G2 must remain untouched
        self.assertIn("G2 X110. Y50. I0. J10.", out)

    def test_feedrate_is_reinjected_when_stripped_from_converted_rapid(self):
        gcode = """\
G90 G21
G0 X0. Y0.
G43 Z15. H1
G1 Z-2. F333.
G1 Z15. F1200.
G1 X50. Y50.
G1 Z5.
G1 Z-2.
"""
        out, stats = core.restore_rapids_text(
            gcode,
            core.RestoreOptions(safe_z=5.0, add_header_comment=False),
        )
        lines = out.splitlines()
        # G1 Z15. F1200. -> G0 Z15. (F1200. stripped)
        self.assertEqual(lines[4], "G0 Z15.")
        # G1 X50. Y50. -> G0 X50. Y50.
        self.assertEqual(lines[5], "G0 X50. Y50.")
        # G1 Z5. must get F1200. re-injected because machine F was still 333.
        self.assertEqual(lines[6], "G1 Z5. F1200.")
        # Next G1 Z-2. already has machine F == 1200., no duplicate F needed
        self.assertEqual(lines[7], "G1 Z-2.")
        self.assertEqual(stats.feedrate_reinjected_count, 1)

    def test_modal_g1_and_feedrate_reinjected_with_line_numbers_and_comments(self):
        gcode = """\
N10 G90 G21
N20 G43 Z15. H1
N30 G1 Z-3. F250.
N40 G1 Z15. F900. (retract to clearance)
N50 X40. Y20. ; move over next pocket
N60 Z5. (plunge to retract height)
"""
        out, stats = core.restore_rapids_text(
            gcode,
            core.RestoreOptions(safe_z=5.0, add_header_comment=False),
        )
        lines = out.splitlines()
        self.assertEqual(lines[3], "N40 G0 Z15. (retract to clearance)")
        self.assertEqual(lines[4], "N50 X40. Y20. ; move over next pocket")
        self.assertEqual(lines[5], "N60 G1 Z5. F900. (plunge to retract height)")
        self.assertEqual(stats.g1_restored_count, 1)
        self.assertEqual(stats.feedrate_reinjected_count, 1)

    def test_diagonal_downward_move_above_safe_z_not_converted_in_conservative_mode(self):
        gcode = """\
G90 G21
G43 Z15. H1
G1 X20. Y20. Z8. F1000.
"""
        out, stats = core.restore_rapids_text(
            gcode,
            core.RestoreOptions(safe_z=5.0, add_header_comment=False),
        )
        self.assertEqual(stats.converted_total, 0)
        self.assertIn("G1 X20. Y20. Z8. F1000.", out)

    def test_undercut_upward_z_below_safe_z_is_protected_by_default(self):
        gcode = """\
G90 G21
G43 Z15. H1
G1 Z-10. F300.
G1 Z-4. F300.
G1 Z10. F300.
"""
        out_strict, stats_strict = core.restore_rapids_text(
            gcode,
            core.RestoreOptions(
                safe_z=5.0,
                retract_must_reach_safe_z=True,
                add_header_comment=False,
            ),
        )
        # Only Z10. reaches Safe Z (5.0); Z-4. stays G1
        self.assertEqual(stats_strict.converted_z_retracts, 1)
        self.assertIn("G1 Z-4. F300.", out_strict)
        self.assertIn("G0 Z10.", out_strict)

        out_relaxed, stats_relaxed = core.restore_rapids_text(
            gcode,
            core.RestoreOptions(
                safe_z=5.0,
                retract_must_reach_safe_z=False,
                add_header_comment=False,
            ),
        )
        self.assertEqual(stats_relaxed.converted_z_retracts, 2)
        self.assertIn("G0 Z-4.", out_relaxed)

    def test_never_converts_g91_g53_g28_canned_cycles_or_rotary_moves(self):
        gcode = """\
G90 G21
G43 Z20. H1
G53 G1 Z0. F1000.
G28 G91 Z0.
G90
G43 Z20. H1
G1 X10. A45. F1000.
G81 X20. Y20. Z-5. R2. F200.
X30. Y30.
G80
G91
G1 Z10. F1000.
G90
"""
        out, stats = core.restore_rapids_text(
            gcode,
            core.RestoreOptions(safe_z=5.0, add_header_comment=False),
        )
        self.assertEqual(stats.converted_total, 0)
        self.assertEqual(out, gcode)

    def test_wcs_change_and_tool_change_reset_known_z(self):
        gcode = """\
G90 G21
G43 Z15. H1
G1 X10. F1000.
T2 M6
G55
G1 X20. F1000.
G43 Z15. H2
G1 X30. F1000.
"""
        out, stats = core.restore_rapids_text(
            gcode,
            core.RestoreOptions(safe_z=5.0, add_header_comment=False),
        )
        lines = out.splitlines()
        self.assertEqual(lines[2], "G0 X10.")
        # After T2 M6 + G55, Z is unknown so G1 X20. MUST NOT be converted
        self.assertEqual(lines[5], "G1 X20. F1000.")
        # After G43 Z15. H2, Z is known (15.0 >= 5.0) so G1 X30. IS converted
        self.assertEqual(lines[7], "G0 X30.")
        self.assertEqual(stats.converted_total, 2)

    def test_percent_marker_stays_on_first_line_with_header_comment(self):
        gcode = "%\nG90 G21\nG43 Z15. H1\nG1 X10. F1000.\n%\n"
        out, _ = core.restore_rapids_text(
            gcode,
            core.RestoreOptions(safe_z=5.0, add_header_comment=True),
        )
        lines = out.splitlines()
        self.assertEqual(lines[0], "%")
        self.assertTrue(lines[1].startswith("(UCCNC Rapid Restore:"))

    def test_file_roundtrip_default_output_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "bracket.nc")
            with open(src, "w", encoding="utf-8") as f:
                f.write(SAMPLE_FUSION_UCCNC)

            out_path, stats = core.restore_rapids_file(
                src, options=core.RestoreOptions(safe_z=5.0)
            )
            self.assertEqual(out_path, os.path.join(tmpdir, "bracket_rapids.nc"))
            self.assertTrue(os.path.isfile(out_path))
            self.assertEqual(stats.converted_total, 3)


if __name__ == "__main__":
    unittest.main()
