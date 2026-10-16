PARAMETER BROWSER 1.2
Replace the previous ParameterBrowser.pushbutton folder completely.

Fixes:
- Initial Spaces and Rooms are captured synchronously before the modeless window opens.
- Source, scope, level, columns and search filters operate immediately against cached model data.
- DataGrid bindings use explicit properties rather than Python dictionary index bindings.
- Refresh, Select and Show use ExternalEvent only where Revit API context is required.
- Persistent engine enabled.

FIX 1.2.1:
- Uses System.Windows.Controls.DataGridLength for programmatic DataGrid column widths.
- Uses a Double for MinWidth for strict IronPython/WPF type compatibility.
