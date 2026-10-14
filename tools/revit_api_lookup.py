"""Look up Revit API members from the XML docs shipped with a local Revit install.

Use this instead of guessing API names. Runs on regular Python 3 (not in Revit).

Examples:
    python tools/revit_api_lookup.py ExternalEvent --ui
    python tools/revit_api_lookup.py Autodesk.Revit.DB.Transaction.Commit --year 2023
    python tools/revit_api_lookup.py FilteredElementCollector --members
"""
import argparse
import glob
import os
import re
import sys
import xml.etree.ElementTree as ET

KINDS = {"T": "Type", "M": "Method", "P": "Property", "F": "Field", "E": "Event"}


def find_xml(year, ui):
    name = "RevitAPIUI.xml" if ui else "RevitAPI.xml"
    base = os.environ.get("ProgramFiles", r"C:\Program Files")
    found = glob.glob(os.path.join(base, "Autodesk", "Revit " + (year or "*"), name))
    if not found:
        sys.exit("Could not find %s under Program Files\\Autodesk\\Revit <year>." % name)
    return sorted(found)[-1]


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query", help="Case-insensitive substring of the member id")
    ap.add_argument("--year", help="Revit year (default: newest installed)")
    ap.add_argument("--ui", action="store_true", help="Search RevitAPIUI.xml instead of RevitAPI.xml")
    ap.add_argument("--members", action="store_true", help="Include methods/properties (default: types only unless query contains a '.' member path)")
    ap.add_argument("--limit", type=int, default=40)
    args = ap.parse_args()

    path = find_xml(args.year, args.ui)
    print("# Source: %s" % path)
    q = args.query.lower()
    shown = 0
    for _, el in ET.iterparse(path):
        if el.tag != "member":
            continue
        kind, _, ident = el.get("name", "").partition(":")
        if q in ident.lower() and (args.members or kind == "T" or "." in args.query):
            print("[%s] %s\n    %s" % (KINDS.get(kind, kind), ident, clean(el.findtext("summary"))))
            shown += 1
            if shown >= args.limit:
                print("... limit reached; refine the query or raise --limit")
                break
        el.clear()
    if not shown:
        print("No matches.")


if __name__ == "__main__":
    main()
