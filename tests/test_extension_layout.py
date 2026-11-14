"""Verify the consolidated bundles and the visible pyRevit ribbon layout."""

import hashlib
import json
from pathlib import Path
import re
import struct
import unittest
import zlib


EXTENSION = Path(__file__).resolve().parents[1] / "CustomPyRevit.extension"
TAB = EXTENSION / "ETC PyTools.tab"
LEGACY_TAB = EXTENSION / "Custom Tools.tab"

BUTTONS = {
    "Asset Management.panel": ["NWB Asset ID"],
    "Electrical.panel": [
        "CableTrayCoordinator", "CircuitHub", "Draw Switchlines", "FindLinkedFixtures",
        "Manual Wall Placement", "ParameterBrowser", "ParameterBrowser WebView",
        "Place Ceiling Devices", "Place Fixtures", "Route Cable Along Tray",
    ],
    "Family Tools.panel": ["Family Offset Setup", "Middle Family Parameters", "Switch Colour Setup"],
    "Getting Started.panel": ["Document Info", "List Sheets"],
    "Model Tools.panel": ["Align Section Box", "Color Scheme Editor", "Detail Line Fillets",
                          "Flip Workplane", "Focus Select"],
    "Revisions.panel": ["Apply Revisions", "Custom Revision Setup", "Remove Revisions"],
    "Spaces.panel": ["Align Elevation Rows", "RoomRegions", "Space Plans Stage 3"],
    "Templates.panel": ["Modeless Window"],
    "Views and Sheets.panel": ["Legend Builder", "Normalise Sheet Origins", "Number Viewports"],
}

PANEL_LAYOUTS = {
    "Electrical.panel": [
        ("Place Fixtures", "Place\nFixtures"),
        ("Manual Wall Placement", "Manual Wall\nPlacement"),
        ("FindLinkedFixtures", "Find Linked\nFixtures"),
        ("Draw Switchlines", "Draw\nSwitchlines"),
        ("CircuitHub", "Circuit\nHub"),
        ("CableTrayCoordinator", "Tray\nCoordinator"),
        ("ParameterBrowser", "Parameter\nBrowser"),
        ("ParameterBrowser WebView", "Parameter\nWebView"),
    ],
    "Model Tools.panel": [
        ("Focus Select", "Focus\nSelect"),
        ("Align Section Box", "Align\nSection Box"),
        ("Flip Workplane", "Flip\nWorkplane"),
        ("Color Scheme Editor", "Color Scheme\nEditor"),
        ("Detail Line Fillets", "Detail Line\nFillets"),
    ],
    "Family Tools.panel": [
        ("Family Offset Setup", "Offset\nSetup"),
        ("Middle Family Parameters", "Middle Family\nParams"),
        ("Switch Colour Setup", "Switch\nColour Setup"),
    ],
    "Spaces.panel": [
        ("Space Plans Stage 3", "Space Plans\nStage 3.6"),
        ("Align Elevation Rows", "Align\nElev. Rows"),
        ("RoomRegions", "Room\nRegions"),
    ],
    "Views and Sheets.panel": [
        ("Legend Builder", "Legend\nBuilder"),
        ("Number Viewports", "Number\nViewports"),
        ("Normalise Sheet Origins", "Sheet Origin\nAlign"),
    ],
    "Revisions.panel": [
        ("Custom Revision Setup", "Revision\nSetup"),
        ("Apply Revisions", "Apply\nRevisions"),
        ("Remove Revisions", "Remove\nRevisions"),
    ],
    "Asset Management.panel": [("NWB Asset ID", "NWB\nAsset ID")],
    "Getting Started.panel": [
        ("Document Info", "Document\nInfo"),
        ("List Sheets", "List\nSheets"),
    ],
}

HIDDEN_BUTTONS = {
    ("Electrical.panel", "Place Ceiling Devices"),
    ("Electrical.panel", "Route Cable Along Tray"),
    ("Templates.panel", "Modeless Window"),
}

MOVED_RESOURCES = {
    ("Electrical.panel", "CableTrayCoordinator"): ["CONTEXT.md", "ui.xaml", "lib/cable_tray_logic.py"],
    ("Electrical.panel", "CircuitHub"): ["CONTEXT.md", "ui.xaml", "lib/circuit_hub_logic.py"],
    ("Electrical.panel", "Draw Switchlines"): ["icon.png", "window.xaml", "lib/switchline_logic.py"],
    ("Electrical.panel", "FindLinkedFixtures"): [
        "icon.png", "ui.xaml", "find_linked_fixtures_settings.json",
    ],
    ("Electrical.panel", "ParameterBrowser"): ["README.txt", "UI.xaml", "lib/parameter_browser_logic.py"],
    ("Electrical.panel", "ParameterBrowser WebView"): [
        "README.txt", "app.css", "app.js", "lib/parameter_browser_logic.py",
        "lib/parameter_browser_webview_data.py",
    ],
    ("Electrical.panel", "Place Ceiling Devices"): ["CONTEXT.md"],
    ("Electrical.panel", "Route Cable Along Tray"): ["CONTEXT.md"],
    ("Templates.panel", "Modeless Window"): ["window.xaml"],
    ("Views and Sheets.panel", "Normalise Sheet Origins"): ["lib/sheet_origins_logic.py"],
}


def read_layout(directory):
    """Read the simple scalar layout lists without a third-party YAML dependency."""
    lines = (directory / "bundle.yaml").read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.strip() == "layout:")
    entries = []
    for line in lines[start + 1:]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            break
        match = re.fullmatch(r"  - (.+)", line)
        if not match:
            raise ValueError("Unsupported layout line in {}: {!r}".format(directory, line))
        value = match.group(1).strip()
        if value.startswith("'") and value.endswith("'"):
            value = value[1:-1].replace("''", "'")
        elif value.startswith('"'):
            value = json.loads(value)
        entries.append(value)
    if not entries:
        raise ValueError("Empty layout in {}".format(directory))
    return entries


def parse_titled_entry(entry):
    match = re.fullmatch(r"(.+)\[title:(.*)\]", entry)
    if not match:
        raise ValueError("Expected a title directive in {!r}".format(entry))
    # pyRevit unescapes the layout directive after the YAML scalar is read.
    return match.group(1), match.group(2).encode("ascii").decode("unicode_escape")


class ExtensionLayoutTests(unittest.TestCase):
    def test_all_current_buttons_exist_once_with_entry_point_and_metadata(self):
        for panel, names in BUTTONS.items():
            for name in names:
                with self.subTest(panel=panel, name=name):
                    bundle = TAB / panel / (name + ".pushbutton")
                    self.assertTrue((bundle / "script.py").is_file())
                    self.assertTrue((bundle / "bundle.yaml").is_file())
                    commands = [path for path in EXTENSION.rglob(name + ".pushbutton")
                                if (path / "script.py").is_file()]
                    self.assertEqual([bundle], commands)

    def test_inventory_includes_all_button_and_panel_directories(self):
        expected = {
            TAB / panel / (name + ".pushbutton")
            for panel, names in BUTTONS.items()
            for name in names
        }
        self.assertEqual(expected, set(TAB.glob("*.panel/*.pushbutton")))
        self.assertEqual(set(BUTTONS), {path.name for path in TAB.glob("*.panel")})

    def test_moved_buttons_keep_their_bundle_resources(self):
        for (panel, name), resources in MOVED_RESOURCES.items():
            for resource in resources:
                with self.subTest(panel=panel, name=name, resource=resource):
                    self.assertTrue((TAB / panel / (name + ".pushbutton") / resource).is_file())
        self.assertTrue((TAB / "Electrical.panel" / "FixtureTools.lib"
                         / "fixture_placement_core.py").is_file())

    def test_tab_layout_lists_visible_panels_once_in_order(self):
        panels = read_layout(TAB)
        self.assertEqual([name[:-6] for name in PANEL_LAYOUTS], panels)
        self.assertEqual(len(panels), len(set(panels)))
        self.assertNotIn("Templates", panels)
        for panel in panels:
            with self.subTest(panel=panel):
                self.assertTrue((TAB / (panel + ".panel")).is_dir())

    def test_panel_layouts_show_only_implemented_buttons_with_short_unique_titles(self):
        visible = set()
        titles = set()
        for panel, expected in PANEL_LAYOUTS.items():
            with self.subTest(panel=panel):
                entries = [parse_titled_entry(entry) for entry in read_layout(TAB / panel)]
                self.assertEqual(expected, entries)
                self.assertEqual(len(entries), len({name for name, _ in entries}))
                for name, title in entries:
                    self.assertTrue((TAB / panel / (name + ".pushbutton")).is_dir())
                    self.assertNotIn((panel, name), visible)
                    visible.add((panel, name))
                    lines = title.splitlines()
                    self.assertTrue(lines)
                    self.assertLessEqual(len(lines), 2)
                    self.assertTrue(all(0 < len(line) <= 13 for line in lines), title)
                    self.assertNotIn(title, titles)
                    titles.add(title)

        all_buttons = {(panel, name) for panel, names in BUTTONS.items() for name in names}
        self.assertEqual(28, len(visible))
        self.assertEqual(all_buttons - HIDDEN_BUTTONS, visible)
        self.assertTrue(HIDDEN_BUTTONS.isdisjoint(visible))

    def read_icon_pixels(self, path):
        """Validate and decode an 8-bit RGBA PNG so identical images can be detected."""
        data = path.read_bytes()
        self.assertEqual(b"\x89PNG\r\n\x1a\n", data[:8])
        chunks = []
        cursor = 8
        while cursor < len(data):
            self.assertLessEqual(cursor + 12, len(data), str(path))
            length = struct.unpack_from(">I", data, cursor)[0]
            end = cursor + 12 + length
            self.assertLessEqual(end, len(data), str(path))
            chunk_type = data[cursor + 4:cursor + 8]
            chunk_data = data[cursor + 8:cursor + 8 + length]
            checksum = struct.unpack_from(">I", data, cursor + 8 + length)[0]
            self.assertEqual(checksum, zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF)
            chunks.append((chunk_type, chunk_data))
            cursor = end
        self.assertEqual(b"IHDR", chunks[0][0])
        self.assertEqual(b"IEND", chunks[-1][0])
        self.assertEqual(
            (96, 96, 8, 6, 0, 0, 0),
            struct.unpack(">IIBBBBB", chunks[0][1]),
        )
        compressed = b"".join(chunk for kind, chunk in chunks if kind == b"IDAT")
        self.assertTrue(compressed, str(path))
        scanlines = zlib.decompress(compressed)
        stride = 96 * 4
        self.assertEqual(96 * (stride + 1), len(scanlines))
        previous = bytearray(stride)
        pixels = bytearray()
        for y in range(96):
            start = y * (stride + 1)
            filter_type = scanlines[start]
            self.assertIn(filter_type, range(5))
            row = bytearray(scanlines[start + 1:start + 1 + stride])
            for index in range(stride):
                left = row[index - 4] if index >= 4 else 0
                above = previous[index]
                upper_left = previous[index - 4] if index >= 4 else 0
                if filter_type == 1:
                    predictor = left
                elif filter_type == 2:
                    predictor = above
                elif filter_type == 3:
                    predictor = (left + above) // 2
                elif filter_type == 4:
                    estimate = left + above - upper_left
                    distances = (abs(estimate - left), abs(estimate - above),
                                 abs(estimate - upper_left))
                    predictor = (left, above, upper_left)[distances.index(min(distances))]
                else:
                    predictor = 0
                row[index] = (row[index] + predictor) & 0xFF
            pixels.extend(row)
            previous = row
        return bytes(pixels)

    def test_all_buttons_have_valid_distinct_icons(self):
        seen = {}
        for panel, names in BUTTONS.items():
            for name in names:
                path = TAB / panel / (name + ".pushbutton") / "icon.png"
                with self.subTest(icon=str(path)):
                    pixels = self.read_icon_pixels(path)
                    self.assertTrue(any(pixels[3::4]), "Icon must not be fully transparent")
                    digest = hashlib.sha256(pixels).hexdigest()
                    self.assertNotIn(digest, seen, "{} duplicates {}".format(path, seen.get(digest)))
                    seen[digest] = path

    def test_no_old_tab_commands_remain(self):
        self.assertEqual([], list(LEGACY_TAB.rglob("*.pushbutton/script.py")))


if __name__ == "__main__":
    unittest.main()
