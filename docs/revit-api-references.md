# Revit API & pyRevit references (use these, do not guess)

## Local, authoritative (matches the installed Revit year)

| What | Where |
|---|---|
| Revit API assemblies | `C:\Program Files\Autodesk\Revit <year>\RevitAPI.dll`, `RevitAPIUI.dll` (2023, 2024, 2025 installed here) |
| Revit API doc comments (all types/members) | `...\Revit <year>\RevitAPI.xml`, `RevitAPIUI.xml` |
| Search them | `python tools/revit_api_lookup.py <TypeOrMember> [--ui] [--year 2025] [--members]` |
| pyRevit library source | `%APPDATA%\pyRevit-Master\pyrevitlib\pyrevit` (`forms`, `revit`, `script`, `framework`) |
| Real pyRevit buttons to copy from | `%APPDATA%\pyRevit-Master\extensions\pyRevitTools.extension` |
| Modeless + ExternalEvent examples | `...\pyRevitTools.extension\pyRevit.tab\Modify.panel\3D.pulldown\Section Box Navigator.pushbutton`, `Analysis.panel\ColorSplasher.pushbutton` |
| `forms.WPFWindow` source | `pyrevitlib\pyrevit\forms\__init__.py` (class `WPFWindow`) |
| `revit.Transaction` source | `pyrevitlib\pyrevit\revit\db\transaction.py` |
| Event handler helper | `pyrevitlib\pyrevit\revit\events.py` (`FuncAsEventHandler`) |

The pyRevit paths are per-machine; on another PC they are the same under `%APPDATA%` for a standard
install (check `pyrevit env`).

## Online

- Revit API docs (browsable by year): https://www.revitapidocs.com
- Autodesk Revit API developer guide: https://help.autodesk.com/view/RVT/2025/ENU/?guid=Revit_API_Revit_API_Developers_Guide_html
- pyRevit docs: https://docs.pyrevitlabs.io  |  source: https://github.com/pyrevitlabs/pyRevit
- pyRevit release notes (engine/version changes): https://github.com/pyrevitlabs/pyRevit/releases
- The Building Coder (practical Revit API how-tos): https://thebuildingcoder.typepad.com
- Revit API forum: https://forums.autodesk.com/t5/revit-api-forum/bd-p/160

## Workflow for the AI

1. Look up every API type/member you are not 100% sure of with `tools/revit_api_lookup.py`.
2. Prefer copying a pattern from the template button or pyRevitTools over inventing one.
3. Say which Revit year / engine the code targets and which parts could not be verified outside Revit.
