from pyrevit import revit, script


output = script.get_output()
doc = revit.doc

if doc is None:
    output.print_md("No Revit document is currently open.")
else:
    selected_elements = revit.get_selection()
    output.print_md("## Document Info")
    output.print_md("- **Document:** {}".format(doc.Title))
    output.print_md("- **Selected elements:** {}".format(len(selected_elements)))
