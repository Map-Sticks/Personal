# Fusion UCCNC Rapid Restore (Fusion 360 Add-In)

A conservative Fusion 360 Add-In that restores `G0` rapid moves in G-code (`.nc` / `.tap`) posted from **Fusion for Personal Use** for **UCCNC** controllers—without modifying your Fusion design.

## How It Works (Conservative Mode)

Fusion for Personal Use replaces rapid moves with `G1` feed moves at cutting feedrate. This add-in scans your posted UCCNC G-code while tracking full modal state (`G90`/`G91`, `G20`/`G21`, `G0`/`G1`/`G2`/`G3`, canned cycles, `F`, and WCS coordinates) and only converts moves that are provably safe:

1. **Pure upward `Z` retracts** (`Z` moving strictly up, with no `X`/`Y` motion) that reach at least **Safe Z** (configurable).
2. **`X`/`Y` positioning moves at or above Safe Z** with no downward `Z` motion.

### UCCNC Safety Guarantees

- **Modal `G1` restoration**: If a `G1` move was converted to `G0` and a later plunge/cut line relied on modal `G1` (e.g., `Z5.`), the tool automatically injects `G1` so UCCNC never rapid-plunges.
- **Modal feedrate (`F`) preservation**: When `F` is stripped from a converted `G0` line, the tool tracks the programmed feedrate and automatically re-injects `F<value>` on the next `G1`/`G2`/`G3` or canned-cycle line if UCCNC's active modal feedrate would otherwise differ.
- **Never touches unsafe/special blocks**: `G91` incremental moves, `G28`/`G30`/`G53` machine/home moves, `G43` tool-length offset lines, `G2`/`G3` arcs, `G81`–`G89` canned cycles, cutter comp (`G41`/`G42`), rotary axes (`A`/`B`/`C`), and comments are left intact. `G91.1` (incremental IJK arc mode emitted by Fusion's UCCNC post) is properly distinguished from `G91`.
- **Non-destructive by default**: Writes `<filename>_rapids<ext>` next to your original file and inserts a header comment summarizing the Safe Z threshold and converted move count.

## Installing in Fusion 360

1. Open Fusion 360.
2. Press **`Shift + S`** (or go to **Utilities → Add-Ins → Scripts and Add-Ins**).
3. Switch to the **Add-Ins** tab.
4. Click the green **`+`** next to **My Add-Ins** and select the add-in folder:
   `C:\Users\jmria\.copilot\repos\Pyrevit\fusion-uccnc-rapid-restore\FusionUccncRapidRestore`
5. Select **FusionUccncRapidRestore** and click **Run** (optionally check **Run on Startup**).

The **UCCNC Rapid Restore** button appears in:
- **Manufacture → Actions**
- **Design → Utilities → Add-Ins**

## Typical Workflow

1. Post-process your toolpaths in Fusion using your normal UCCNC post-processor (e.g. `part.nc`).
2. Click **UCCNC Rapid Restore** in Fusion.
3. Click **Select G-code File...** — the dialog inspects the file and shows its units (`mm`/`in`), `Z` range, and detected `G43 Z` heights.
4. Confirm or adjust **Safe Z** (in the G-code file's own units — set to your operation Retract Height or Clearance Height).
5. Run once with **Dry run** checked if you want to preview the converted lines, then run normally to generate `part_rapids.nc`.
6. Load `part_rapids.nc` in UCCNC and verify the toolpath preview before cutting.

## Optional Command-Line Usage

The core engine can also be run directly with Python:

```powershell
python FusionUccncRapidRestore\rapid_restore_core.py "C:\path\to\part.nc" --safe-z 5 --dry-run
python FusionUccncRapidRestore\rapid_restore_core.py "C:\path\to\part.nc" --safe-z 5
```
