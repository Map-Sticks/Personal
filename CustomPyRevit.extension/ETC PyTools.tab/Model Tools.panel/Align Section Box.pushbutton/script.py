# -*- coding: utf-8 -*-
"""
NAME: Align Section Box (Final)
DESCRIPTION: Aligns Section Box to Walls, Faces, or Links. 
Uses explicit transaction handling to prevent "Outside of Transaction" errors.
"""

# --- IMPORTS ---
from pyrevit import revit, forms
import math
# Direct imports to avoid version conflicts
from Autodesk.Revit import DB as db 
from Autodesk.Revit.UI.Selection import ObjectType
from Autodesk.Revit.Exceptions import OperationCanceledException

# --- VARIABLES ---
doc = revit.doc
uidoc = revit.uidoc
view = revit.active_view

# --- HELPER FUNCTIONS ---

def get_closest_edge_vector(face, pick_point):
    """
    If User clicks a Face (surface), find the nearest edge to the click.
    """
    best_vector = None
    min_dist = float('inf')

    # Scan all edges on the face
    for edge_array in face.EdgeLoops:
        for edge in edge_array:
            try:
                # Check distance from click to edge
                projection = edge.Project(pick_point)
                if projection:
                    dist = projection.Distance
                    if dist < min_dist:
                        min_dist = dist
                        # Get direction
                        if isinstance(edge, db.Line):
                            best_vector = edge.Direction
                        else:
                            # For curves, use tangent
                            best_vector = edge.ComputeDerivatives(0.5).BasisX
            except:
                continue
    return best_vector

def get_vector_from_reference(ref):
    """
    Extracts vector from Reference, handling Links and Faces.
    """
    picked_element = doc.GetElement(ref)
    transform = db.Transform.Identity 
    element_to_process = picked_element

    # 1. HANDLE LINKS
    if isinstance(picked_element, db.RevitLinkInstance):
        transform = picked_element.GetTotalTransform()
        link_doc = picked_element.GetLinkDocument()
        if not link_doc: return None
        try:
            element_to_process = link_doc.GetElement(ref.LinkedElementId)
        except: return None

    # 2. GET GEOMETRY
    geom_object = None
    try:
        geom_object = element_to_process.GetGeometryObjectFromReference(ref)
    except: pass

    local_vector = None

    # A. Edge/Line
    if isinstance(geom_object, db.Edge) and isinstance(geom_object.AsCurve(), db.Line):
        local_vector = geom_object.AsCurve().Direction
    elif isinstance(geom_object, db.Line):
        local_vector = geom_object.Direction

    # B. Planar Face (Wall/Ceiling Surface)
    elif isinstance(geom_object, db.PlanarFace):
        # Vertical Face (Standard Wall)
        if abs(geom_object.FaceNormal.Z) < 0.01: 
            # Perpendicular to normal is the wall direction
            local_vector = db.XYZ(-geom_object.FaceNormal.Y, geom_object.FaceNormal.X, 0)
        # Horizontal Face (Ceiling/Floor) -> Find closest edge
        else:
            inverse_t = transform.Inverse
            local_pick_point = inverse_t.OfPoint(ref.GlobalPoint)
            local_vector = get_closest_edge_vector(geom_object, local_pick_point)

    # C. Fallback (Grids/Centerlines)
    if not local_vector:
        if hasattr(element_to_process, "HandOrientation"):
             local_vector = element_to_process.HandOrientation
        elif hasattr(element_to_process, "Location") and hasattr(element_to_process.Location, "Curve"):
             local_vector = element_to_process.Location.Curve.Direction
        elif isinstance(element_to_process, db.Grid):
             local_vector = element_to_process.Curve.Direction

    if not local_vector: return None

    # 3. TRANSFORM AND RETURN
    return transform.OfVector(local_vector)

def get_angle(vector):
    flat_vector = db.XYZ(vector.X, vector.Y, 0)
    if flat_vector.IsZeroLength(): return None
    return math.atan2(flat_vector.Y, flat_vector.X)

# --- MAIN RUN ---

def run():
    # 1. Validation
    if view.ViewType != db.ViewType.ThreeD:
        forms.alert("Run in 3D View.", exitscript=True)
    if not view.IsSectionBoxActive:
        forms.alert("Enable Section Box.", exitscript=True)

    # 2. Selection
    try:
        ref = uidoc.Selection.PickObject(ObjectType.PointOnElement, "Pick an Edge or Face.")
    except OperationCanceledException:
        return

    # 3. Calculations
    vector = get_vector_from_reference(ref)
    if not vector:
        forms.alert("Could not detect a straight line.")
        return

    angle = get_angle(vector)
    if angle is None: return

    # 4. EXPLICIT TRANSACTION (The Fix)
    t = db.Transaction(doc, "Align Section Box")
    t.Start() # Force the transaction to open
    
    try:
        s_box = view.GetSectionBox()
        b_min, b_max = s_box.Min, s_box.Max
        
        # Rotate
        new_t = db.Transform.CreateRotation(db.XYZ.BasisZ, angle)
        new_t.Origin = s_box.Transform.Origin
        
        s_box.Transform = new_t
        s_box.Min = b_min
        s_box.Max = b_max
        
        view.SetSectionBox(s_box)
        
        t.Commit() # Save changes
        
    except Exception as e:
        t.RollBack() # Cancel if something breaks
        forms.alert("Error: {}".format(e))

if __name__ == "__main__":
    run()
