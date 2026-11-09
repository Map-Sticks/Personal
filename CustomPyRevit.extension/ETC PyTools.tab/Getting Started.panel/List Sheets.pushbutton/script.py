from pyrevit import revit, script


def main():
    output = script.get_output()
    doc = revit.doc

    if doc is None:
        output.print_md("Open a Revit project to list its sheets.")
        return

    if doc.IsFamilyDocument:
        output.print_md("This is a family document. Open a Revit project to list its sheets.")
        return

    sheets = revit.query.get_sheets(
        include_placeholders=True, include_noappear=True, doc=doc
    )
    sheets = sorted(sheets, key=lambda sheet: sheet.SheetNumber.lower())

    if not sheets:
        output.print_md("This project contains no sheets.")
        return

    output.print_md("## Project Sheets")
    output.print_md("Total sheets: {}".format(len(sheets)))
    output.print_table(
        table_data=[
            [sheet.SheetNumber, sheet.Name, "Yes" if sheet.IsPlaceholder else "No"]
            for sheet in sheets
        ],
        columns=["Sheet Number", "Sheet Name", "Placeholder"],
    )


if __name__ == "__main__":
    main()
