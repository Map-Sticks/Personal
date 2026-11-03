"""Parameter Browser WebView tests: payload helpers, script wiring with mock Revit, and app.js via node."""

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


BUNDLE = (Path(__file__).resolve().parents[1] / "CustomPyRevit.extension"
          / "Custom Tools.tab" / "Electrical.panel" / "ParameterBrowser WebView.pushbutton")
CLASSIC_LOGIC = (Path(__file__).resolve().parents[1] / "CustomPyRevit.extension" / "Custom Tools.tab"
                 / "Electrical.panel" / "ParameterBrowser.pushbutton" / "lib" / "parameter_browser_logic.py")
NODE = shutil.which("node")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


logic = load("parameter_browser_logic", BUNDLE / "lib" / "parameter_browser_logic.py")
with patch.dict(sys.modules, {"parameter_browser_logic": logic}):
    pbdata = load("parameter_browser_webview_data", BUNDLE / "lib" / "parameter_browser_webview_data.py")


def parse_data_script(js):
    prefix = "window.PBWV_DATA = "
    assert js.startswith(prefix), js[:40]
    return json.loads(js[len(prefix):js.index(";\n")])


# --- Revit doubles ---------------------------------------------------------------------------
class Id:
    def __init__(self, value):
        self.Value = value


class Param:
    def __init__(self, name, text=None, storage="String", value=None):
        self.Definition = SimpleNamespace(Name=name)
        self.text = text
        self.StorageType = storage
        self.value = value
        self.HasValue = value is not None or text is not None

    def AsValueString(self):
        return self.text

    def AsString(self):
        return self.value

    def AsElementId(self):
        return self.value


class BrokenParam:
    Definition = SimpleNamespace(Name="Broken")

    def AsValueString(self):
        raise RuntimeError("boom")

    @property
    def StorageType(self):
        raise RuntimeError("boom")


class LegacyOutput(object):
    renderer = object()


class WebViewOutput(object):
    def __init__(self):
        self.calls = []
        self.styles = []
        self.scripts = []

    @property
    def renderer(self):
        raise NotImplementedError("WebView2")

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append(name)
        return record

    def add_style(self, css):
        self.styles.append(css)

    def inject_script(self, code, attribs=None, body=False):
        self.scripts.append((code, body))


class PayloadTests(unittest.TestCase):
    def test_bundled_logic_matches_the_classic_button(self):
        self.assertEqual(CLASSIC_LOGIC.read_text(encoding="utf-8"),
                         (BUNDLE / "lib" / "parameter_browser_logic.py").read_text(encoding="utf-8"))

    def test_parameters_are_read_once_and_first_duplicate_wins(self):
        element = SimpleNamespace(Parameters=[
            Param("Name", "Office"), Param("Name", "Other"), Param("Level", "L1"),
            Param("Panel", None, "ElementId", Id(7)), BrokenParam(), Param("", "x")])
        values = pbdata.read_parameter_values(element, lambda eid: "Panel %s" % eid.Value)
        self.assertEqual({"Name": "Office", "Panel": "Panel 7", "Broken": ""}, values)
        self.assertEqual({}, pbdata.read_parameter_values(SimpleNamespace()))

    def test_source_fields_add_pseudo_and_count_fields(self):
        fields = pbdata.source_fields(["zeta", "Area", "Name"])
        for expected in ["Element Id", "Category", "Level", "Area", "Name", "zeta",
                         "Count: Lighting Fixtures", "Count: Nurse Call Devices"]:
            self.assertIn(expected, fields)
        self.assertEqual(sorted(fields, key=lambda x: x.lower()), fields)

    def test_settings_file_seeds_defaults(self):
        path = pbdata.settings_path({"APPDATA": "/appdata"})
        self.assertTrue(path.startswith("/appdata"))
        self.assertTrue(path.endswith("ParameterBrowser.settings.json"))
        self.assertEqual({}, pbdata.load_settings(str(BUNDLE / "missing.json")))
        seed = pbdata.seed_settings({"source": "Rooms", "scope": "Level", "fields": ["Area"],
                                     "content_categories": ["Data Devices", "Bogus"]})
        self.assertEqual({"source": "Rooms", "scope": "Level", "fields": ["Area"],
                          "content_categories": ["Data Devices"]}, seed)
        self.assertEqual(logic.CONTENT_LABELS, pbdata.seed_settings("junk")["content_categories"])

    def make_payload(self):
        big = 2 ** 53 + 1   # not representable as a JavaScript number
        sources = {"Spaces": {"fields": ["Name"], "records": [{
            "id": big, "label": "101 Office", "category": "Spaces", "level": "L1", "visible": True,
            "values": {"Name": u"Office :fire: </script> \u00e9\nline"}, "counts": {"Data Devices": 2}}]}}
        contents = [{"id": 5, "category": "Data Devices", "family": "F", "type": "T", "mark": "",
                     "level": "L1", "space": big, "room": None}]
        return pbdata.build_payload("Proj", "Level 1", sources, contents, {"Spaces": "note"},
                                    {"fields": ["Name"]}, "CustomPyRevit-ctrl", "2026-10-17 09:00")

    def test_payload_is_json_safe_with_exact_string_ids(self):
        payload = self.make_payload()
        js = pbdata.data_script(payload)
        js.encode("ascii")
        data = parse_data_script(js)
        rec = data["sources"]["Spaces"]["records"][0]
        self.assertEqual(str(2 ** 53 + 1), rec["id"])
        self.assertEqual(u"Office :fire: </script> \u00e9\nline", rec["values"]["Name"])
        self.assertEqual({"Data Devices": 2}, rec["counts"])
        self.assertEqual(str(2 ** 53 + 1), data["contents"][0]["space"])
        self.assertIsNone(data["contents"][0]["room"])
        self.assertEqual([], data["sources"]["Rooms"]["records"])
        self.assertIn("Count: Data Devices", data["sources"]["Rooms"]["fields"])
        self.assertEqual("CustomPyRevit-ctrl", data["control_id"])
        self.assertEqual(logic.DEFAULT_FIELDS, data["defaults"]["fields"])
        self.assertEqual(logic.COUNT_PREFIX, data["defaults"]["count_prefix"])

    def test_non_ascii_values_do_not_use_ironpython_broken_ensure_ascii(self):
        # IronPython 2.7: json.dumps(ensure_ascii=True) raises UnicodeDecodeError on u"m\xb2".
        real_dumps = json.dumps

        def ironpython_like_dumps(obj, **kwargs):
            if kwargs.get("ensure_ascii", True):
                raise UnicodeDecodeError("unknown", b"\xb2", 0, 1, "IronPython json bug")
            return real_dumps(obj, **kwargs)

        text = u"12 m\u00b2 caf\u00e9 \U0001F600 a\u2028b \u4e2d"
        with patch.object(pbdata.json, "dumps", ironpython_like_dumps):
            js = pbdata.data_script({"values": {u"Area \u00b2": text}})
        js.encode("ascii")
        self.assertIn("\\u00b2", js)
        self.assertIn("\\ud83d\\ude00", js)
        self.assertEqual({u"Area \u00b2": text}, parse_data_script(js)["values"])
        if NODE:
            result = subprocess.run([NODE, "-e", "const w={};new Function('window',require('fs').readFileSync(0,'utf8'))(w);"
                                     "process.stdout.write(JSON.stringify(w.PBWV_DATA))"],
                                    input=js, capture_output=True, text=True, check=True)
            self.assertEqual({u"Area \u00b2": text}, json.loads(result.stdout)["values"])


class ScriptTests(unittest.TestCase):
    def run_script(self, output, doc=None, version=7):
        self.alert = Mock()
        modules = {}

        def module(name, **members):
            result = ModuleType(name)
            result.__dict__.update(members)
            modules[name] = result
            return result

        class FamilyInstance(object):
            pass

        phase = SimpleNamespace(Id=Id(30), Name="New Construction")
        space = SimpleNamespace(Id=Id(100))
        level_names = {1: "Level 1"}
        spaces = [
            SimpleNamespace(Id=Id(100), LevelId=Id(1), Number="101", Category=SimpleNamespace(Name="Spaces"),
                            Parameters=[Param("Name", "Office"), Param("Department", "Elec")],
                            get_Parameter=lambda bip: Param("x", "Office" if bip == "ROOM_NAME" else None,
                                                            "ElementId", Id(30) if bip == "ROOM_PHASE" else None),
                            CreatedPhaseId=Id(30)),
            SimpleNamespace(Id=Id(101), LevelId=Id(1), Number="102", Category=SimpleNamespace(Name="Spaces"),
                            Parameters=[Param("Name", "Store")],
                            get_Parameter=lambda bip: None, CreatedPhaseId=Id(30)),
        ]

        def fixture(eid, in_space):
            item = FamilyInstance()
            item.Id = Id(eid)
            item.LevelId = Id(1)
            item.Symbol = SimpleNamespace(FamilyName="Downlight")
            item.Name = "600x600"
            item.get_Parameter = lambda bip: Param("Mark", "L%d" % eid)
            item.get_Space = lambda ph: space if in_space else None
            item.get_Room = lambda ph: None
            return item

        fixtures = [fixture(500, True), fixture(501, False)]

        class Collector(object):
            def __init__(self, doc, view_id=None):
                self.view_id = view_id

            def OfCategory(self, cat):
                self.cat = cat
                return self

            def WhereElementIsNotElementType(self):
                return self

            def ToElements(self):
                return {"OST_MEPSpaces": spaces, "OST_LightingFixtures": fixtures}.get(self.cat, [])

            def ToElementIds(self):
                return [spaces[0].Id] if self.cat == "OST_MEPSpaces" else []

        def get_element(eid):
            if eid.Value == 30:
                return phase
            return SimpleNamespace(Name=level_names.get(eid.Value, "E%s" % eid.Value))

        bic = SimpleNamespace(**dict((n, n) for _, n in logic.CONTENT_CATEGORIES))
        bic.OST_MEPSpaces = "OST_MEPSpaces"
        bic.OST_Rooms = "OST_Rooms"
        db = SimpleNamespace(BuiltInCategory=bic, FilteredElementCollector=Collector, FamilyInstance=FamilyInstance,
                             BuiltInParameter=SimpleNamespace(ROOM_NAME="ROOM_NAME", ROOM_PHASE="ROOM_PHASE",
                                                              ALL_MODEL_MARK="ALL_MODEL_MARK"))
        if doc == "project":
            doc = SimpleNamespace(Title="Proj", IsFamilyDocument=False, Phases=[phase], GetElement=get_element)
        uidoc = SimpleNamespace(ActiveView=SimpleNamespace(Id=Id(9), Name="Level 1 Lighting"))

        pyrevit = module("pyrevit", DB=db, EXEC_PARAMS=SimpleNamespace(command_controlid="CustomPyRevit-PB-WebView"),
                         forms=SimpleNamespace(alert=self.alert), revit=SimpleNamespace(doc=doc, uidoc=uidoc),
                         script=SimpleNamespace(get_output=lambda: output, get_logger=Mock(),
                                                get_bundle_file=lambda name: str(BUNDLE / name)))
        if version is not None:
            pyrevit.VERSION_MAJOR = version
        modules["parameter_browser_logic"] = logic
        modules["parameter_browser_webview_data"] = pbdata
        with patch.dict(sys.modules, modules):
            return load("parameter_browser_webview_script", BUNDLE / "script.py")

    def test_old_pyrevit_is_refused(self):
        output = LegacyOutput()
        self.run_script(output, doc="project", version=6)
        self.assertIn("pyRevit 7", self.alert.call_args[0][0])

    def test_renderer_check_detects_webview_without_version(self):
        output = WebViewOutput()
        self.run_script(output, doc=None, version=None)
        self.assertIn("Open a Revit project", self.alert.call_args[0][0])
        self.assertEqual([], output.scripts)

    def test_reads_model_and_injects_app(self):
        output = WebViewOutput()
        mod = self.run_script(output, doc="project")
        self.alert.assert_not_called()
        self.assertFalse(getattr(mod, "__persistentengine__", False))
        self.assertIn("close_others", output.calls)
        self.assertEqual([(BUNDLE / "app.css").read_text(encoding="utf-8")], output.styles)
        (code, body), = output.scripts
        self.assertTrue(body)
        self.assertTrue(code.endswith((BUNDLE / "app.js").read_text(encoding="utf-8")))
        data = parse_data_script(code)
        self.assertEqual("Proj", data["document"])
        self.assertEqual("Level 1 Lighting", data["view"])
        self.assertEqual("CustomPyRevit-PB-WebView", data["control_id"])
        recs = data["sources"]["Spaces"]["records"]
        self.assertEqual(["100", "101"], [r["id"] for r in recs])
        self.assertEqual([True, False], [r["visible"] for r in recs])
        self.assertEqual("101 Office", recs[0]["label"])
        self.assertEqual({"Name": "Office", "Department": "Elec"}, recs[0]["values"])
        self.assertEqual({"Lighting Fixtures": 1}, recs[0]["counts"])
        self.assertEqual("Level 1", recs[0]["level"])
        self.assertIn("Department", data["sources"]["Spaces"]["fields"])
        self.assertEqual([("500", "100"), ("501", None)], [(i["id"], i["space"]) for i in data["contents"]])
        self.assertEqual("L500", data["contents"][0]["mark"])
        self.assertIn("New Construction", data["phase_notes"]["Spaces"])
        self.assertEqual("No rooms found in this model.", data["phase_notes"]["Rooms"])


JS_PROBE = r"""
const fs = require('fs'), vm = require('vm');
const ctx = {console};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const c = ctx.PBWVCore;
const items = JSON.parse(process.argv[2]);
const cats = ['Lighting Fixtures', 'Data Devices'];
const out = {
  resolve: [c.resolveFields(['Name', 'Gone', 'Number', 'Name'], ['Number', 'Name'], ['Number']),
            c.resolveFields(['Gone'], ['Level', 'Number'], ['Number', 'Name', 'Level']),
            c.resolveFields(['Gone'], ['Other'], ['Number'])],
  levels: c.levelOptions([{level: 'b'}, {level: 'A'}, {level: ''}, {level: 'b'}]),
  scope: [c.filterRecords([{id: '1', visible: true}, {id: '2', visible: false}], 'Current view').length,
          c.filterRecords([{id: '1', level: 'L1'}], 'Level', '').length],
  value: ['Element Id', 'Level', 'Count: Data Devices', 'Count: Nope', 'Name', 'X'].map(f =>
          c.recordValue({id: '9', level: 'L1', counts: {'Data Devices': 3}, values: {Name: 'Office'}}, f, 'Count: ')),
  search: [c.matchesTokens(['Office', '101'], c.searchTokens('  off 10 ')),
           c.matchesTokens(['Office', '101'], c.searchTokens('ce1')),
           c.matchesTokens(['A'], [])],
  sort: ['Space 10', '', 'space 2', 'Space 1'].sort((a, b) => c.compareValues(a, b, false)),
  sortIds: ['10', '9007199254740993', '9', '9007199254740992'].sort((a, b) => c.compareValues(a, b, true)),
  groups: c.groupContents(items, cats),
  labels: items.map(c.itemLabel),
  url: c.selectUrl(['1', '22'], true),
  refresh: c.refreshUrl('Custom PyRevit-tab&x'),
  csv: c.delimited(['A', 'B'], [['x,y', 'q"t'], ['1\n2', 3]], ','),
  tsv: c.delimited(['A'], [['a\tb\nc']], '\t'),
  settings: c.normalizeSettings({source: 'Rooms', scope: 'Bad', fields: [' ', 'Area'], content_categories: ['Data Devices', 'X'],
                                 panelWidth: 50, colWidths: {Name: 300, Bad: 5}, sortDir: -1},
                                {source: 'Spaces', scope: 'Level', fields: ['Name'], content_categories: ['Lighting Fixtures']},
                                {sources: ['Spaces', 'Rooms'], scopes: ['Whole project', 'Level'], categories: cats}),
  seed: c.normalizeSettings(null, {source: 'Spaces', scope: 'Level', fields: ['Name'], content_categories: ['Lighting Fixtures']},
                            {sources: ['Spaces', 'Rooms'], scopes: ['Whole project', 'Level'], categories: cats}),
};
process.stdout.write(JSON.stringify(out));
"""


@unittest.skipIf(NODE is None, "node is not installed")
class AppJsTests(unittest.TestCase):
    ITEMS = [
        {"id": "12", "category": "Data Devices", "family": "Jack", "type": "Cat6", "mark": "", "level": "L1"},
        {"id": "3", "category": "Lighting Fixtures", "family": "Down", "type": "A", "mark": "b", "level": "L1"},
        {"id": "2", "category": "Lighting Fixtures", "family": "Down", "type": "A", "mark": "B", "level": ""},
        {"id": "10", "category": "Lighting Fixtures", "family": "down", "type": "a", "mark": "a", "level": "L2"},
        {"id": "4", "category": "Security Devices", "family": "Cam", "type": "X", "mark": "", "level": "L1"},
    ]

    @classmethod
    def setUpClass(cls):
        app = str(BUNDLE / "app.js")
        subprocess.run([NODE, "--check", app], check=True, capture_output=True)
        result = subprocess.run([NODE, "-e", JS_PROBE, app, json.dumps(cls.ITEMS)],
                                capture_output=True, text=True)
        if result.returncode:
            raise AssertionError(result.stderr)
        cls.out = json.loads(result.stdout)

    def test_matches_python_field_and_scope_logic(self):
        self.assertEqual([["Name", "Number"], ["Number", "Level"], ["Element Id"]], self.out["resolve"])
        self.assertEqual(logic.resolve_fields(["Gone"], ["Level", "Number"]), self.out["resolve"][1])
        self.assertEqual(["A", "b"], self.out["levels"])
        self.assertEqual([1, 0], self.out["scope"])
        self.assertEqual(["9", "L1", 3, 0, "Office", ""], self.out["value"])

    def test_search_and_sort(self):
        self.assertEqual([True, False, True], self.out["search"])
        self.assertEqual(["Space 1", "space 2", "Space 10", ""], self.out["sort"])
        self.assertEqual(["9", "10", "9007199254740992", "9007199254740993"], self.out["sortIds"])

    def test_contents_grouping_matches_python(self):
        py_items = [dict(i, id=int(i["id"])) for i in self.ITEMS]
        expected = [{"name": cat, "ids": [str(i) for i in ids],
                     "types": [{"name": n, "ids": [str(i) for i in tids],
                                "items": [{"label": label, "id": str(iid)} for label, iid in leaves]}
                               for n, tids, leaves in types]}
                    for cat, ids, types in logic.group_contents(py_items, ["Lighting Fixtures", "Data Devices"])]
        self.assertEqual(expected, self.out["groups"])
        self.assertEqual([logic.item_label(i) for i in py_items], self.out["labels"])

    def test_revit_links_and_export(self):
        self.assertEqual("revit://outputhelpers?command=select&element[]=1,22&show=true", self.out["url"])
        self.assertEqual("revit://outputhelpers?command=button&controlid=Custom%20PyRevit-tab%26x", self.out["refresh"])
        self.assertEqual('A,B\r\n"x,y","q""t"\r\n"1\n2",3', self.out["csv"])
        self.assertEqual("A\r\na b c", self.out["tsv"])

    def test_settings_normalisation(self):
        s = self.out["settings"]
        self.assertEqual(("Rooms", "Level", ["Area"], ["Data Devices"], 380, {"Name": 300}, -1),
                         (s["source"], s["scope"], s["fields"], s["categories"], s["panelWidth"], s["colWidths"], s["sortDir"]))
        seed = self.out["seed"]
        self.assertEqual(("Spaces", "Level", ["Name"], ["Lighting Fixtures"]),
                         (seed["source"], seed["scope"], seed["fields"], seed["categories"]))


if __name__ == "__main__":
    unittest.main()
