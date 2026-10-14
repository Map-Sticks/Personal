# pyRevit / Python engine versions

Why this matters: the engine decides which Python syntax and libraries work in a button.
**Always confirm the target engine before writing code. Default to IronPython 2.7-safe code.**

| pyRevit release | Date | IronPython (default) | Other engines shipped | Source |
|---|---|---|---|---|
| 4.8.x (4.8.9 – 4.8.16) | 2021-12 – 2024-04 | 2.7.11 (`IPY2711PR`) | IronPython 3.4.0 preview (`IPY340PR`), CPython 3.7.8 / 3.8.5 (`CPY378`, `CPY385`) | `bin/engines` listing at tag `v4.8.16.24121+2117` |
| 5.0.0 – 5.3.1 | 2025-02 – 2025-11 | 2.7.12 (`IPY2712PR`) | IronPython 3.4.2 (`IPY342`), CPython 3.12.3 (`CPY3123`) | `bin/netfx/engines`, `bin/netcore/engines`, `bin/cengines` at tags v5.0.0 and v5.3.1 |
| 6.0.0 (installed locally) | 2026-02 | 2.7.12 (`IPY2712PR`) | IronPython 3.4.2, CPython 3.12.3 | Verified in `%APPDATA%\pyRevit-Master\bin` and `pyRevit_config.ini` (`cpyengine = 3123`) |
| 6.1 – 6.5.5 | 2026-02 – 2026-08 | not individually verified | not individually verified | Run `pyrevit env` / inspect the install |
| 7.0.0 | 2026-10 | 2.7.12 or 3.4.2 (follows attached engine) | CPython via pythonnet | 7.0.0 release notes |

Notes:
- "2.7.11 / 2.7.12" are pyRevit's patched IronPython builds. Language level is Python 2.7 either way.
- 5.x+ ships both .NET Framework (`netfx`, Revit 2024 and earlier) and .NET (`netcore`, Revit 2025+) engine folders.
  Revit 2025/2026 run on .NET 8; Revit 2027 on .NET 10 (6.0.0 release notes).
- A script chooses its engine by shebang: `#! python3` (CPython) or `#! python` / none (IronPython 2.7 default).
  IronPython 3.4.2 can be selected in pyRevit Settings. **Assume IronPython 2.7 unless told otherwise.**
- This user: pyRevit 6.0.0 at home, an older pyRevit (likely 4.8.x/5.x) at work, both IronPython 2.7.x.
  Code must therefore run on both: stick to the common subset below.

## IronPython 2.7 rules (the common subset)

- No f-strings, no type hints, no `nonlocal`, no `yield from`, no `pathlib`, no `print(..., end=)`
  (use `print("x")` with parentheses only for single arguments, or `from __future__ import print_function`).
- Use `"{}".format(x)`; add `# -*- coding: utf-8 -*-` for non-ASCII text.
- `str` vs `unicode`: Revit API strings are .NET strings; cast with `str()`/`unicode()` carefully.
- Libraries: only the standard library, `clr`, .NET (System.*), pyRevit modules, and whatever is in
  `pyrevitlib` / `site-packages`. No pip packages (numpy, pandas, requests-from-pip, etc.).
- Revit year differences: 2023/2024 = .NET Framework 4.8; 2025+ = .NET 8. Avoid API calls removed in newer
  years (e.g. `ElementId.IntegerValue` is deprecated in 2024+; use `.Value` on 2024+ with a version check).
  Verify with `python tools/revit_api_lookup.py <name> --year <year>`.

## How to check what is actually installed

```powershell
pyrevit --version                                   # CLI/pyRevit version
pyrevit env                                         # clones, engines, Revit installs
Get-ChildItem $env:APPDATA\pyRevit-Master\bin\netfx\engines   # engine folders
gc $env:APPDATA\pyRevit\pyRevit_config.ini          # cpyengine, rocketmode, etc.
```

Re-check upstream releases: `gh api "repos/pyrevitlabs/pyRevit/releases?per_page=30" --jq ".[].tag_name"`,
and list engines for a tag: `gh api "repos/pyrevitlabs/pyRevit/contents/bin/netfx/engines?ref=<url-encoded-tag>" --jq ".[].name"`.
