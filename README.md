# Custom pyRevit Buttons

This repository is a starter workspace for writing custom pyRevit buttons. The
button scripts are in `CustomPyRevit.extension`; edit them in VS Code or
Codespaces, then load and test them in Revit on Windows.

> A Codespace is useful for editing and reviewing Python, but it cannot run
> Revit or the pyRevit/Revit API. Run and verify model-changing commands in a
> Revit installation.

## Included example

`Document Info` is a read-only pushbutton that reports the active document name
and the number of selected elements in the pyRevit output window.

```text
CustomPyRevit.extension/
└── Custom Tools.tab/
    └── Getting Started.panel/
        └── Document Info.pushbutton/
            ├── bundle.yaml
            └── script.py
```

## Load the extension in Revit

1. Install pyRevit in a supported Revit environment.
2. In Revit, open pyRevit Settings and add this repository's root directory to
   **Custom Extension Directories**. That directory contains
   `CustomPyRevit.extension`.
3. Reload pyRevit from its ribbon tab. The **Custom Tools** tab and
   **Getting Started** panel should appear.
4. After editing a script or its bundle metadata, reload pyRevit to see the
   changes. Test model-changing tools in a disposable project first.

## Add a button

1. Create a folder ending in `.pushbutton` inside an existing `.panel` folder.
   The folder name becomes the command name unless `bundle.yaml` provides a
   title.
2. Add `script.py` as the command entry point. Add `bundle.yaml` when you want
   to set metadata such as the button title or tooltip.
3. Add optional assets such as `icon.png` in the button folder. Shared Python
   modules can live in a `lib/` folder at the button, panel, tab, or extension
   level.
4. Reload pyRevit and test the button in Revit.

The same folder-suffix convention applies when adding tabs (`.tab`) and panels
(`.panel`). See the [pyRevit extension documentation](https://docs.pyrevitlabs.io/extensions/)
for bundle types, metadata, script engines, and the Revit API environment.
