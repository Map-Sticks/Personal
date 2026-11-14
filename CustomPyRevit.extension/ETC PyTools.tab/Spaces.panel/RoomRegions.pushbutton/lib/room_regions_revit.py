# -*- coding: utf-8 -*-
"""Revit API operations for the modeless Room Regions window."""

import traceback

from System.Collections.Generic import List
from Autodesk.Revit.Exceptions import RegenerationFailedException
from pyrevit import DB, forms, revit, script

import room_regions_geometry as geometry
import room_regions_logic as logic


logger = script.get_logger()
LEVEL_TOLERANCE_FT = 1.0 / 304.8
PLAN_TYPES = (DB.ViewType.FloorPlan, DB.ViewType.CeilingPlan,
              DB.ViewType.AreaPlan, DB.ViewType.EngineeringPlan)
SOURCE_CATEGORIES = {
    "space": DB.BuiltInCategory.OST_MEPSpaces,
    "room": DB.BuiltInCategory.OST_Rooms,
    "link": DB.BuiltInCategory.OST_Rooms,
}
_PENDING_FINALIZERS = []


class UserError(Exception):
    pass


class BatchAbort(Exception):
    pass


class PendingTransaction(BatchAbort):
    pass


class RegionTransactionError(Exception):
    pass


class PendingGroupFinalizer(DB.ITransactionFinalizer):
    """Close the group only after an unexpectedly pending transaction finishes."""

    def __init__(self, group, notify):
        self.group = group
        self.notify = notify
        self.pending = False
        self.transaction = None
        self.options = None

    def defer(self, transaction, options):
        self.transaction = transaction
        self.options = options
        self.pending = True
        _PENDING_FINALIZERS.append(self)

    def OnCommitted(self, document, transaction_name):
        self._finish()

    def OnRolledBack(self, document, transaction_name):
        self._finish()

    def _finish(self):
        if not self.pending:
            return
        try:
            status = self.group.RollBack()
            if status != DB.TransactionStatus.RolledBack:
                raise BatchAbort("Revit returned {} for the group rollback.".format(status))
            message = "Pending failure handling finished; all Room Regions from this run were rolled back."
        except Exception as ex:
            message = ("Pending failure handling finished, but the Room Regions group could not be "
                       "rolled back: {}. Inspect the model and Revit's Undo history.".format(ex))
            logger.error("{}\n{}".format(message, traceback.format_exc()))
        for resource in (self.options, self.transaction, self.group):
            try:
                resource.Dispose()
            except Exception as ex:
                detail = "Room Regions cleanup failed: {}.".format(ex)
                message += " " + detail
                logger.error("{}\n{}".format(detail, traceback.format_exc()))
        self.pending = False
        _PENDING_FINALIZERS.remove(self)
        if self.notify is not None:
            try:
                self.notify(message)
            except Exception:
                logger.error("Could not display pending-transaction result:\n{}".format(traceback.format_exc()))
        logger.error(message)


class RegionFailures(DB.IFailuresPreprocessor):
    def __init__(self):
        self.warnings = []
        self.errors = []
        self.corrupted = False

    def PreprocessFailures(self, accessor):
        for failure in accessor.GetFailureMessages():
            description = _text(failure.GetDescriptionText())
            severity = failure.GetSeverity()
            if severity == DB.FailureSeverity.Warning:
                self.warnings.append(description)
                logger.warning("Room Regions transaction warning: {}".format(description))
                accessor.DeleteWarning(failure)
            else:
                self.errors.append(description)
                if severity == DB.FailureSeverity.DocumentCorruption:
                    self.corrupted = True
        if self.errors:
            return DB.FailureProcessingResult.ProceedWithRollBack
        return DB.FailureProcessingResult.Continue


def _text(value):
    return logic.text_type(value) if value is not None else u""


def _id_value(element_id):
    return int(logic.element_id_value(element_id))


def _name(element):
    name = revit.query.get_name(element)
    if not name:
        raise UserError("An element has no readable name (id {}).".format(
            _id_value(element.Id)))
    return _text(name)


def _view_phase(view):
    parameter = view.get_Parameter(DB.BuiltInParameter.VIEW_PHASE)
    if parameter is None or not parameter.HasValue:
        return None
    phase_id = parameter.AsElementId()
    return phase_id if _id_value(phase_id) >= 0 else None


def _spatial_phase(spatial):
    parameter = spatial.get_Parameter(DB.BuiltInParameter.ROOM_PHASE)
    if parameter is None or not parameter.HasValue:
        return None
    phase_id = parameter.AsElementId()
    return phase_id if _id_value(phase_id) >= 0 else None


def _mapped_phase(doc, link, view_phase):
    if view_phase is None:
        return None
    link_type = doc.GetElement(link.GetTypeId())
    if not isinstance(link_type, DB.RevitLinkType):
        raise UserError("The linked model's type is unavailable.")
    mapping = link_type.GetPhaseMap()
    if not mapping.ContainsKey(view_phase):
        raise UserError("The link has no mapping for the active view's phase.")
    return mapping[view_phase]


def _context(uiapp):
    uidoc = uiapp.ActiveUIDocument
    if uidoc is None:
        return None, None, None, "Open a Revit project and a plan view."
    doc = uidoc.Document
    view = uidoc.ActiveView
    if doc.IsFamilyDocument:
        return uidoc, doc, view, "Room Regions requires a project document."
    if view is None:
        return uidoc, doc, view, "Open a plan view."
    if view.IsTemplate or view.ViewType not in PLAN_TYPES:
        return uidoc, doc, view, (
            "Use a floor, ceiling, area or engineering plan; templates and other views are unsupported.")
    if view.GenLevel is None:
        return uidoc, doc, view, "The active plan has no associated level."
    return uidoc, doc, view, None


def _token(doc, view):
    return (_text(doc.ProjectInformation.UniqueId), int(doc.GetHashCode()),
            _text(view.UniqueId))


def _level_matches(spatial, source_doc, transform, host_level):
    level = source_doc.GetElement(spatial.LevelId)
    if level is None:
        return u"", False
    name = _name(level)
    if transform is None:
        return name, _id_value(level.Id) == _id_value(host_level.Id)
    location = spatial.Location
    point = location.Point if isinstance(location, DB.LocationPoint) else None
    x, y = (point.X, point.Y) if point is not None else (0.0, 0.0)
    transformed = transform.OfPoint(DB.XYZ(x, y, level.ProjectElevation))
    return name, abs(transformed.Z - host_level.ProjectElevation) <= LEVEL_TOLERANCE_FT


def _status(spatial, boundary_options):
    if spatial.Area > 0:
        return "Placed"
    if spatial.Location is None:
        return "Unplaced"
    boundaries = spatial.GetBoundarySegments(boundary_options)
    return "Redundant" if boundaries and len(boundaries) else "Not enclosed"


def _parameter_text(element, parameter_id):
    parameter = element.get_Parameter(parameter_id)
    return _text(parameter.AsString()) if parameter is not None and parameter.HasValue else u""


def _area_text(spatial):
    parameter = spatial.get_Parameter(DB.BuiltInParameter.ROOM_AREA)
    if parameter is not None and parameter.HasValue:
        formatted = parameter.AsValueString()
        if formatted:
            return _text(formatted)
    return u"{:.2f} sq ft".format(spatial.Area)


def _source_record(spatial, source_doc, transform, host_level, expected_phase,
                   kind, link_id, source_name, boundary_options):
    level_name, level_match = _level_matches(spatial, source_doc, transform, host_level)
    phase = _spatial_phase(spatial)
    phase_match = (phase is not None and expected_phase is not None
                   and _id_value(phase) == _id_value(expected_phase))
    number = _text(spatial.Number)
    name = _parameter_text(spatial, DB.BuiltInParameter.ROOM_NAME)
    if not name:
        name = _name(spatial)
    status = _status(spatial, boundary_options)
    area = _area_text(spatial)
    record = {
        "key": (kind, _text(spatial.UniqueId), link_id),
        "number": number, "name": name, "level": level_name, "area_value": float(spatial.Area),
        "source": source_name, "area": area, "status": status,
        "level_match": level_match, "phase_match": phase_match, "phase_known": phase is not None,
    }
    record["search"] = u" ".join((number, name, level_name, source_name, area, status))
    return record


def capture(uiapp, scope=None):
    """Read metadata only, or explicitly selected spatial sources into UI-safe data."""
    uidoc, doc, view, error = _context(uiapp)
    result = {
        "document": _text(doc.Title) if doc is not None else u"",
        "view": _text(view.Name) if view is not None else u"",
        "error": error, "token": None, "read_only": False,
        "records": [], "links": [], "types": [], "warnings": [],
    }
    if error:
        return result
    result["token"] = _token(doc, view)
    if scope is not None and scope["token"] != result["token"]:
        raise UserError("The active project or view changed. Click Refresh active view before loading.")
    result["read_only"] = bool(doc.IsReadOnly)
    if result["read_only"]:
        result["warnings"].append("The project is read-only; creation is disabled.")

    for region_type in DB.FilteredElementCollector(doc).OfClass(DB.FilledRegionType):
        result["types"].append({
            "id": _text(region_type.UniqueId), "name": _name(region_type),
        })
    result["types"].sort(key=lambda item: item["name"].lower())

    links = list(DB.FilteredElementCollector(doc).OfClass(DB.RevitLinkInstance))
    for link in links:
        result["links"].append({
            "id": _text(link.UniqueId),
            "name": u"{} (id {})".format(_name(link), _id_value(link.Id)),
        })
    if scope is None:
        return result

    kinds = set(scope["sources"])
    link_ids = set(scope["link_ids"]) if "link" in kinds else set()
    if not kinds or not kinds.issubset(set(SOURCE_CATEGORIES)):
        raise UserError("Choose at least one valid source before loading.")
    if kinds == set(["link"]) and not link_ids:
        raise UserError("Select at least one link instance before loading linked Rooms.")
    missing_links = link_ids - set(item["id"] for item in result["links"])
    for missing in sorted(missing_links):
        result["warnings"].append(
            "Selected link instance no longer exists: {}. Refresh active view.".format(missing))

    phase = _view_phase(view)
    if phase is None:
        result["warnings"].append(
            "The view phase is unavailable. Turn off 'Follow view phase' to list sources.")
    sources = []
    for kind in ("space", "room"):
        if kind not in kinds:
            continue
        elements = list(DB.FilteredElementCollector(doc).OfCategory(SOURCE_CATEGORIES[kind])
                        .WhereElementIsNotElementType())
        sources.append((kind, doc, None, u"", u"Host", phase, elements))

    for link in links:
        link_id = _text(link.UniqueId)
        if link_id not in link_ids:
            continue
        link_name = u"{} (id {})".format(_name(link), _id_value(link.Id))
        link_doc = link.GetLinkDocument()
        if link_doc is None:
            result["warnings"].append("Skipped unloaded link: {}.".format(link_name))
            continue
        try:
            linked_phase = _mapped_phase(doc, link, phase)
        except RegenerationFailedException:
            raise
        except Exception as ex:
            linked_phase = None
            result["warnings"].append("{}: phase mapping unavailable: {}.".format(link_name, ex))
        elements = list(DB.FilteredElementCollector(link_doc).OfCategory(SOURCE_CATEGORIES["link"])
                        .WhereElementIsNotElementType())
        sources.append(("link", link_doc, link.GetTotalTransform(), link_id, link_name,
                        linked_phase, elements))

    total = sum(len(source[-1]) for source in sources)
    processed = 0
    options = DB.SpatialElementBoundaryOptions()
    try:
        with forms.ProgressBar(
                title="Reading rooms and Spaces {value} of {max_value} (Cancel to stop)",
                cancellable=True) as progress:
            for kind, source_doc, transform, link_id, label, expected_phase, elements in sources:
                for spatial in elements:
                    if progress.cancelled:
                        raise UserError("Source loading cancelled; the previous list was kept.")
                    processed += 1
                    progress.update_progress(processed, total)
                    if progress.cancelled:
                        raise UserError("Source loading cancelled; the previous list was kept.")
                    _, current_doc, current_view, current_error = _context(uiapp)
                    if current_error or _token(current_doc, current_view) != result["token"]:
                        raise UserError(
                            "The active project or view changed during source loading; the previous list was kept.")
                    try:
                        result["records"].append(_source_record(
                            spatial, source_doc, transform, view.GenLevel, expected_phase,
                            kind, link_id, label, options))
                    except RegenerationFailedException:
                        raise
                    except Exception as ex:
                        message = "Could not read {} id {}: {}".format(
                            label, _id_value(spatial.Id), ex)
                        logger.error("{}\n{}".format(message, traceback.format_exc()))
                        result["warnings"].append(message)
    finally:
        options.Dispose()
    unknown_phases = sum(1 for record in result["records"] if not record["phase_known"])
    if unknown_phases:
        result["warnings"].append(
            "{} source(s) have no readable phase and are hidden while "
            "'Follow view phase' is enabled.".format(unknown_phases))
    return result


def _resolve(doc, view, key, follow_phase):
    kind, unique_id, link_id = key
    if kind not in SOURCE_CATEGORIES:
        raise UserError("Unknown source kind; refresh the list.")
    source_doc, transform = doc, None
    expected_phase = _view_phase(view)
    if kind == "link":
        link = doc.GetElement(link_id)
        if not isinstance(link, DB.RevitLinkInstance):
            raise UserError("The link instance was deleted; refresh the list.")
        source_doc = link.GetLinkDocument()
        if source_doc is None:
            raise UserError("The link is unloaded; refresh the list.")
        transform = link.GetTotalTransform()
        if follow_phase:
            expected_phase = _mapped_phase(doc, link, expected_phase)
    spatial = source_doc.GetElement(unique_id)
    if spatial is None or spatial.Category is None:
        raise UserError("The source was deleted; refresh the list.")
    if _id_value(spatial.Category.Id) != int(SOURCE_CATEGORIES[kind]):
        raise UserError("The source category changed; refresh the list.")
    _, matches_level = _level_matches(spatial, source_doc, transform, view.GenLevel)
    if not matches_level:
        raise UserError("The source is not on the active view's level.")
    if follow_phase:
        phase = _spatial_phase(spatial)
        if (phase is None or expected_phase is None
                or _id_value(phase) != _id_value(expected_phase)):
            raise UserError("The source does not match the active view's phase.")
    if spatial.Area <= 0 or spatial.Location is None:
        raise UserError("The source is unplaced, not enclosed or redundant.")
    return spatial, transform


def _defer_pending(finalizer, transaction, options):
    finalizer.defer(transaction, options)
    raise PendingTransaction(
        "Revit has pending failure handling despite forced modal mode. Resolve Revit's failure "
        "dialog; the entire Room Regions run will be rolled back after it finishes.")


def _create_one(doc, view, region_type, boundaries, finalizer):
    transaction = DB.Transaction(doc, "Create Room Region")
    processor = RegionFailures()
    options = None
    try:
        if transaction.Start() != DB.TransactionStatus.Started:
            raise RegionTransactionError("Revit could not start the region transaction.")
        options = transaction.GetFailureHandlingOptions()
        options.SetTransactionFinalizer(finalizer)
        options.SetForcedModalHandling(True)
        options.SetFailuresPreprocessor(processor)
        options.SetClearAfterRollback(True)
        transaction.SetFailureHandlingOptions(options)
        region = DB.FilledRegion.Create(doc, region_type.Id, view.Id, boundaries)
        unique_id = _text(region.UniqueId)
        status = transaction.Commit()
        if status == DB.TransactionStatus.Pending:
            _defer_pending(finalizer, transaction, options)
        if processor.corrupted:
            raise BatchAbort("Revit reported document corruption; the batch was aborted: {}."
                             .format("; ".join(processor.errors)))
        if status != DB.TransactionStatus.Committed:
            details = "; ".join(processor.errors) or _text(status)
            if processor.warnings:
                details += "; warnings: {}".format("; ".join(processor.warnings))
            raise RegionTransactionError("Revit did not commit this region: {}.".format(details))
        return unique_id, processor.warnings
    except Exception as ex:
        if finalizer.pending:
            raise
        status = transaction.GetStatus()
        if status == DB.TransactionStatus.Pending:
            _defer_pending(finalizer, transaction, options)
        if status == DB.TransactionStatus.Started:
            try:
                rollback_status = transaction.RollBack()
            except Exception as rollback_error:
                if transaction.GetStatus() == DB.TransactionStatus.Pending:
                    _defer_pending(finalizer, transaction, options)
                raise BatchAbort("Revit could not roll back an interrupted region transaction: {}."
                                 .format(rollback_error))
            if rollback_status == DB.TransactionStatus.Pending:
                _defer_pending(finalizer, transaction, options)
            if rollback_status != DB.TransactionStatus.RolledBack:
                raise BatchAbort("Revit did not roll back an interrupted region transaction: {}."
                                 .format(ex))
        if isinstance(ex, RegenerationFailedException):
            raise BatchAbort("Revit regeneration failed; the batch was aborted: {}.".format(ex))
        raise
    finally:
        if not finalizer.pending:
            if options is not None:
                options.Dispose()
            transaction.Dispose()


def create(uiapp, request, on_pending_finished=None):
    """Create one region per source; one failed transaction does not stop the rest."""
    uidoc, doc, view, error = _context(uiapp)
    if error:
        raise UserError(error)
    if _token(doc, view) != request["token"]:
        raise UserError("The active project or view changed. Click Refresh before creating.")
    if doc.IsReadOnly:
        raise UserError("The project is read-only; nothing was created.")
    region_type = doc.GetElement(request["type_id"])
    if not isinstance(region_type, DB.FilledRegionType):
        raise UserError("The selected filled-region type no longer exists. Click Refresh.")
    if not request["sources"]:
        raise UserError("Tick at least one room or Space.")

    group = DB.TransactionGroup(doc, "Create Room Regions")
    finalizer = PendingGroupFinalizer(group, on_pending_finished)
    created, report = [], []
    cancelled = False
    stopped_reason = u""
    try:
        if group.Start() != DB.TransactionStatus.Started:
            raise UserError("Revit could not start the Room Regions transaction group.")
        with forms.ProgressBar(
                title="Creating Room Regions {value} of {max_value} (Cancel to stop)",
                cancellable=True) as progress:
            total = len(request["sources"])
            for index, item in enumerate(request["sources"]):
                if progress.cancelled:
                    cancelled = True
                    stopped_reason = "Cancelled by user."
                    for remaining in request["sources"][index:]:
                        report.append(("Skipped", remaining["label"], "Cancelled before processing."))
                    break
                progress.update_progress(index + 1, total)
                if progress.cancelled:
                    cancelled = True
                    stopped_reason = "Cancelled by user."
                    for remaining in request["sources"][index:]:
                        report.append(("Skipped", remaining["label"], "Cancelled before processing."))
                    break
                _, current_doc, current_view, current_error = _context(uiapp)
                if current_error or _token(current_doc, current_view) != request["token"]:
                    stopped_reason = "The active project or view changed."
                    for remaining in request["sources"][index:]:
                        report.append(("Skipped", remaining["label"], stopped_reason))
                    break
                try:
                    spatial, transform = _resolve(doc, view, item["key"], request["follow_phase"])
                    boundaries, warnings = geometry.build_boundaries(
                        doc, view, spatial, transform, request["boundary"],
                        request["include_islands"], request["simplify"],
                        float(doc.Application.ShortCurveTolerance))
                    unique_id, transaction_warnings = _create_one(
                        doc, view, region_type, boundaries, finalizer)
                    created.append(unique_id)
                    message = "; ".join(warnings + transaction_warnings) or "Created."
                    report.append(("Created", item["label"], message))
                except BatchAbort:
                    raise
                except RegenerationFailedException as ex:
                    raise BatchAbort("Revit regeneration failed; the batch was aborted: {}.".format(ex))
                except (UserError, logic.GeometryError) as ex:
                    report.append(("Skipped", item["label"], _text(ex)))
                except Exception as ex:
                    logger.error("Room Regions failed for {}:\n{}".format(
                        item["label"], traceback.format_exc()))
                    report.append(("Failed", item["label"], _text(ex)))
        if created:
            if group.Assimilate() != DB.TransactionStatus.Committed:
                raise UserError("Revit did not commit the transaction group; no success was recorded.")
        else:
            if group.RollBack() != DB.TransactionStatus.RolledBack:
                raise BatchAbort("Revit did not roll back the empty transaction group.")
    except Exception:
        if not finalizer.pending and group.GetStatus() == DB.TransactionStatus.Started:
            if group.RollBack() != DB.TransactionStatus.RolledBack:
                raise BatchAbort("Revit did not roll back the interrupted transaction group.")
        raise
    finally:
        if not finalizer.pending:
            group.Dispose()
    return {"created": created, "report": report, "cancelled": cancelled,
            "stopped_reason": stopped_reason}


def select_created(uiapp, token, unique_ids):
    uidoc, doc, view, error = _context(uiapp)
    if error:
        raise UserError(error)
    if _token(doc, view) != token:
        raise UserError("Return to the project and view where the regions were created.")
    ids = List[DB.ElementId]()
    missing = 0
    for unique_id in unique_ids:
        region = doc.GetElement(unique_id)
        if region is None:
            missing += 1
        else:
            ids.Add(region.Id)
    if not ids.Count:
        raise UserError("No created regions remain to select.")
    uidoc.Selection.SetElementIds(ids)
    return ids.Count, missing
