# Universal Project Data Scheduler

Copy `Universal Scheduler.pushbutton` into a pyRevit panel and reload pyRevit.

## Features
- Any loaded model category
- Instance and type parameters
- Synthetic identity, room and MEP-space fields
- Cross-category grouping and quantity counts
- Multiple filters
- Interactive preview
- Revit Select and Show links
- CSV export
- Browser-local saved presets

## Important design note
The standard pyRevit output window is used. Python gathers model data once, then WebView2 performs configuration, grouping and filtering in the browser. This avoids unsafe background Revit API calls. Re-run the button to refresh model data.

## Limits
The default safety limit is 50,000 elements and 300 parameters per element. Change `MAX_ELEMENTS` and `MAX_PARAMS_PER_ELEMENT` near the top of `script.py` if required. Very large projects may need a later server-backed or modeless ExternalEvent architecture rather than preloading the whole project.
