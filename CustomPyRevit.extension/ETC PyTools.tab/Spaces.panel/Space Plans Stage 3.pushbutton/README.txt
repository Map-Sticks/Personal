SPACE PLANS STAGE 3.3

Restores grouped Space mode, full elevation geometry controls, separate Floor/RCP/Elevation templates, separate viewport types, remembered settings, Stage 3.2 dedicated plan/elevation zones, balanced centred elevation rows, right-side titleblock reservation, documentation/review sheets, and automatic overflow sheets.

Space selections still start as None. Grouped Spaces must share one Level. One combined Floor Plan and RCP are created for a group; elevations are generated for each source Space and packed in source-Space order.

STAGE 3.5 PERFORMANCE PRIORITY
- Keeps the pushbutton folder name SpacePlansStage3.pushbutton.
- Creates all candidate viewports on a sheet before regenerating and measuring.
- Removes regeneration from individual viewport creation and row movement.
- Uses one measurement regeneration and one final movement regeneration per sheet.
- Calculates packing and overflow decisions in Python between those regeneration points.
- Uses GetPositionAtViewAnchor when supported, with viewport-bottom fallback on older Revit APIs.
- Retains elevation-generator regenerations required for marker direction verification.

STAGE 3.6 VIEW OUTPUT OPTIONS
- Added independent Floor Plan, RCP and Wall Elevation checkboxes.
- Added Create sheet and place selected views checkbox.
- Supports RCP-only, Floor-Plan-only, Elevations-only, or any combination.
- Unticking sheet creation generates views in the Project Browser without creating sheets or viewports.
- Elevations without a Floor Plan output use an internal _Elevation Host Floor Plan.
- View and sheet choices are remembered; Space selections remain unremembered.
