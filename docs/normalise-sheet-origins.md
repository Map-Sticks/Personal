# Normalise Sheet Origins

`CustomPyRevit.extension/Custom Tools.tab/Getting Started.panel/Normalise Sheet Origins.pushbutton`

## Use

1. Click the button in a project.
2. Pick the **reference** sheet (one that is placed correctly). Close the picker to stop.
3. Choose the scope: **Audit all sheets** or **Choose sheets to audit...** (Esc stops).
4. **Audit** (read-only) runs with a progress bar; **Cancel** stops it and reports what was audited so
   far. The result lists how many are misaligned, aligned or skipped (no/multiple title blocks), with
   the X/Y offset of each misaligned sheet.
5. Choose **Align all misaligned sheets**, **Choose which sheets to align**, or **Audit only**. Aligning
   asks for a Yes/No confirmation first.
6. Alignment runs with a progress bar; **Cancel** stops after the current sheet. Remaining sheets are
   marked "Not moved (cancelled)". One Undo reverses all moves that were made.
7. The output window lists every audited sheet: offset, audit status and action (Moved / FAILED with
   reason / Not moved). Optionally save a **CSV record** (project, date, reference sheet and corner,
   then one row per sheet).

## What it does

- **Alignment point:** the bottom-left corner of the title block's bounding box on the sheet
  (`get_BoundingBox(sheet).Min`). Shift = reference corner - sheet corner, X/Y only.
- **What moves:** every element owned by the sheet (`ElementOwnerViewFilter`) that is a placed
  graphic: the title block, viewports, schedules, text, detail lines, filled regions, groups,
  images, revision clouds, etc. They go into one `ElementTransformUtils.MoveElements` call, so
  their relative positions are kept.
- **What is left to follow its host:** components nested in the title block, title-block revision
  schedules, group members, and anything Revit reports as a dependent of another moved element
  (`GetDependentElements`). View definitions, sketch planes and uncategorised internal elements are
  not graphics and are skipped.
- **Pinned** elements are unpinned for the move and re-pinned.
- **Check:** after the move the title block corner is measured again. If it is not within 0.01 mm
  of the reference, or Revit raises an error (e.g. element owned by another user), that sheet is
  rolled back and reported as FAILED. Other sheets are unaffected.

## Limits

- The corner comes from the title block's bounding box. If title block types draw different
  extents (e.g. text overhanging the border), the corners differ too.
- Workshared elements owned by another user make that sheet fail; ask them to relinquish and rerun.
