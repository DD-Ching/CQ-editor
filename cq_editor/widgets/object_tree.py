from time import perf_counter

from PyQt5.QtWidgets import (
    QTreeWidget,
    QTreeWidgetItem,
    QAction,
    QMenu,
    QWidget,
    QAbstractItemView,
)
from PyQt5.QtCore import Qt, pyqtSlot, pyqtSignal

from pyqtgraph.parametertree import Parameter, ParameterTree

from OCP.AIS import AIS_Line
from OCP.Geom import Geom_Line
from OCP.gp import gp_Dir, gp_Pnt, gp_Ax1

from cadquery.viewer_diff import ViewerShapeRecord, apply_diff_to_viewer, compute_diff

from ..mixins import ComponentMixin
from ..icons import icon
from ..cq_utils import (
    make_AIS,
    export,
    to_occ_color,
    is_obj_empty,
    get_occ_color,
    set_color,
)
from .viewer import DEFAULT_FACE_COLOR
from ..utils import splitter, layout, get_save_filename


class TopTreeItem(QTreeWidgetItem):

    def __init__(self, *args, **kwargs):

        super(TopTreeItem, self).__init__(*args, **kwargs)


class ObjectTreeItem(QTreeWidgetItem):

    props = [
        {"name": "Name", "type": "str", "value": "", "readonly": True},
        # {"name": "Color", "type": "color", "value": "#f4a824"},
        # {"name": "Alpha", "type": "float", "value": 0, "limits": (0, 1), "step": 1e-1},
        {"name": "Visible", "type": "bool", "value": True},
    ]

    def __init__(
        self,
        name,
        ais=None,
        shape=None,
        shape_display=None,
        sig=None,
        viewer_key=None,
        node_id=None,
        cache_status="miss",
        alpha=0.0,
        color="#f4a824",
        **kwargs,
    ):

        super(ObjectTreeItem, self).__init__([name], **kwargs)
        self.setFlags(self.flags() | Qt.ItemIsUserCheckable)
        self.setCheckState(0, Qt.Checked)

        self.ais = ais
        self.shape = shape
        self.shape_display = shape_display
        self.sig = sig
        self.viewer_key = viewer_key or name
        self.node_id = node_id
        self.cache_status = cache_status

        self.properties = Parameter.create(name="Properties", children=self.props)

        self.properties["Name"] = name
        # Alpha and Color from this panel fight with the options in show_object and so they are
        # disabled for now until a better solution is found
        # self.properties["Alpha"] = ais.Transparency()
        # self.properties["Color"] = (
        #     get_occ_color(ais)
        #     if ais and ais.HasColor()
        #     else get_occ_color(DEFAULT_FACE_COLOR)
        # )
        self.properties.sigTreeStateChanged.connect(self.propertiesChanged)

    def propertiesChanged(self, properties, changed):

        changed_prop = changed[0][0]

        self.setData(0, 0, self.properties["Name"])

        # if changed_prop.name() == "Alpha":
        #     self.ais.SetTransparency(self.properties["Alpha"])

        # if changed_prop.name() == "Color":
        #     set_color(self.ais, to_occ_color(self.properties["Color"]))

        # self.ais.Redisplay()

        if self.properties["Visible"]:
            self.setCheckState(0, Qt.Checked)
        else:
            self.setCheckState(0, Qt.Unchecked)

        if self.sig:
            self.sig.emit()


class CQRootItem(TopTreeItem):

    def __init__(self, *args, **kwargs):

        super(CQRootItem, self).__init__(["CQ models"], *args, **kwargs)


class HelpersRootItem(TopTreeItem):

    def __init__(self, *args, **kwargs):

        super(HelpersRootItem, self).__init__(["Helpers"], *args, **kwargs)


class ObjectTree(QWidget, ComponentMixin):

    name = "Object Tree"
    _stash = []

    preferences = Parameter.create(
        name="Preferences",
        children=[
            {"name": "Viewer diff (MVP)", "type": "bool", "value": True},
            {"name": "Preserve properties on reload", "type": "bool", "value": False},
            {"name": "Clear all before each run", "type": "bool", "value": True},
            {"name": "STL precision", "type": "float", "value": 0.1},
        ],
    )

    sigObjectsAdded = pyqtSignal([list], [list, bool])
    sigObjectsRemoved = pyqtSignal(list)
    sigCQObjectSelected = pyqtSignal(object)
    sigAISObjectsSelected = pyqtSignal(list)
    sigItemChanged = pyqtSignal(QTreeWidgetItem, int)
    sigObjectPropertiesChanged = pyqtSignal()

    def __init__(self, parent):

        super(ObjectTree, self).__init__(parent)

        self.tree = tree = QTreeWidget(
            self, selectionMode=QAbstractItemView.ExtendedSelection
        )
        self.properties_editor = ParameterTree(self)

        tree.setHeaderHidden(True)
        tree.setItemsExpandable(False)
        tree.setRootIsDecorated(False)
        tree.setContextMenuPolicy(Qt.ActionsContextMenu)

        # forward itemChanged singal
        tree.itemChanged.connect(lambda item, col: self.sigItemChanged.emit(item, col))
        # handle visibility changes form tree
        tree.itemChanged.connect(self.handleChecked)

        self.CQ = CQRootItem()
        self.Helpers = HelpersRootItem()

        root = tree.invisibleRootItem()
        root.addChild(self.CQ)
        root.addChild(self.Helpers)

        tree.expandToDepth(1)

        self._export_STL_action = QAction(
            "Export as STL",
            self,
            enabled=False,
            triggered=lambda: self.export("stl", self.preferences["STL precision"]),
        )

        self._export_STEP_action = QAction(
            "Export as STEP", self, enabled=False, triggered=lambda: self.export("step")
        )

        self._clear_current_action = QAction(
            icon("delete"),
            "Clear current",
            self,
            enabled=False,
            triggered=self.removeSelected,
        )

        self._toolbar_actions = [
            QAction(
                icon("delete-many"), "Clear all", self, triggered=self.removeObjects
            ),
            self._clear_current_action,
        ]

        self.prepareMenu()

        tree.itemSelectionChanged.connect(self.handleSelection)
        tree.customContextMenuRequested.connect(self.showMenu)

        self.previous_shapes = {}
        self.current_shapes = {}
        self._cq_items_by_key = {}
        self._pending_removed_ais = []
        self._pending_added_ais = []
        self._pending_request_fit = False
        self._pending_props_by_key = {}
        self.last_viewer_update_stats = {
            "mode": "full",
            "removed": 0,
            "added": 0,
            "updated": 0,
            "reused": 0,
            "updated_names": [],
            "reused_names": [],
            "compute_time_s": 0.0,
            "apply_time_s": 0.0,
            "total_time_s": 0.0,
        }

        self.prepareLayout()

    def prepareMenu(self):

        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)

        self._context_menu = QMenu(self)
        self._context_menu.addActions(self._toolbar_actions)
        self._context_menu.addActions(
            (self._export_STL_action, self._export_STEP_action)
        )

    def prepareLayout(self):

        self._splitter = splitter(
            (self.tree, self.properties_editor),
            stretch_factors=(2, 1),
            orientation=Qt.Vertical,
        )
        layout(self, (self._splitter,), top_widget=self)

        self._splitter.show()

    def showMenu(self, position):

        self._context_menu.exec_(self.tree.viewport().mapToGlobal(position))

    def menuActions(self):

        return {"Tools": [self._export_STL_action, self._export_STEP_action]}

    def toolbarActions(self):

        return self._toolbar_actions

    def addLines(self):

        origin = (0, 0, 0)
        ais_list = []

        for name, color, direction in zip(
            ("X", "Y", "Z"),
            ("red", "lawngreen", "blue"),
            ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
        ):
            line_placement = Geom_Line(gp_Ax1(gp_Pnt(*origin), gp_Dir(*direction)))
            line = AIS_Line(line_placement)
            line.SetColor(to_occ_color(color))

            self.Helpers.addChild(ObjectTreeItem(name, ais=line))

            ais_list.append(line)

        self.sigObjectsAdded.emit(ais_list)

    def _current_properties(self):

        current_params = {}
        for i in range(self.CQ.childCount()):
            child = self.CQ.child(i)
            current_params[child.properties["Name"]] = child.properties

        return current_params

    def _current_properties_by_key(self):

        current_params = {}
        for i in range(self.CQ.childCount()):
            child = self.CQ.child(i)
            current_params[getattr(child, "viewer_key", child.properties["Name"])] = (
                child.properties
            )

        return current_params

    def _restore_properties(self, obj, properties):

        for p in properties[obj.properties["Name"]]:
            obj.properties[p.name()] = p.value()

    def _restore_properties_by_key(self, obj, properties_by_key, key):

        if key not in properties_by_key:
            return

        for p in properties_by_key[key]:
            obj.properties[p.name()] = p.value()

    @pyqtSlot(dict, bool)
    @pyqtSlot(dict)
    def addObjects(self, objects, clean=False, root=None):
        add_start = perf_counter()

        if root is None:
            root = self.CQ

        request_fit_view = True if root.childCount() == 0 else False
        preserve_props = self.preferences["Preserve properties on reload"]

        if preserve_props:
            current_props = self._current_properties()
            current_props_by_key = self._current_properties_by_key()
        else:
            current_props = {}
            current_props_by_key = {}

        # remove empty objects
        objects_f = {k: v for k, v in objects.items() if not is_obj_empty(v.shape)}

        use_viewer_diff = (
            root is self.CQ
            and self.preferences["Viewer diff (MVP)"]
            and not clean
        )

        if use_viewer_diff:
            try:
                current_shapes = self._shape_records(objects_f)
                self._pending_removed_ais = []
                self._pending_added_ais = []
                self._pending_request_fit = request_fit_view
                self._pending_props_by_key = current_props_by_key
                diff = compute_diff(self.previous_shapes, current_shapes)
                apply_diff_to_viewer(self, diff)
                self.previous_shapes = current_shapes
                self.current_shapes = current_shapes
                self.last_viewer_update_stats = {
                    "mode": "diff",
                    **diff.stats(),
                    "updated_names": self._shape_names(current_shapes, diff.updated_keys),
                    "reused_names": self._shape_names(current_shapes, diff.reused_keys),
                    "total_time_s": perf_counter() - add_start,
                }
                return
            except Exception:
                self.previous_shapes = {}
                self.current_shapes = {}
                self._cq_items_by_key = {}

        if clean or self.preferences["Clear all before each run"]:
            self.removeObjects()

        ais_list = []

        for name, obj in objects_f.items():
            ais, shape_display = make_AIS(obj.shape, obj.options)

            child = ObjectTreeItem(
                name,
                shape=obj.shape,
                shape_display=shape_display,
                ais=ais,
                sig=self.sigObjectPropertiesChanged,
                viewer_key=getattr(obj, "viewer_key", name),
                node_id=getattr(obj, "node_id", None),
                cache_status=getattr(obj, "cache_status", "miss"),
            )

            if preserve_props and name in current_props:
                self._restore_properties(child, current_props)

            if child.properties["Visible"]:
                ais_list.append(ais)

            root.addChild(child)
            if root is self.CQ:
                self._cq_items_by_key[child.viewer_key] = child

        if root is self.CQ:
            full_apply_time = perf_counter() - add_start
            self.previous_shapes = self._shape_records(objects_f)
            self.current_shapes = dict(self.previous_shapes)
            self.last_viewer_update_stats = {
                "mode": "full",
                "removed": 0,
                "added": len(ais_list),
                "updated": len(ais_list),
                "reused": 0,
                "updated_names": list(objects_f.keys()),
                "reused_names": [],
                "compute_time_s": 0.0,
                "apply_time_s": full_apply_time,
                "total_time_s": full_apply_time,
            }

        if request_fit_view:
            self.sigObjectsAdded[list, bool].emit(ais_list, True)
        else:
            self.sigObjectsAdded[list].emit(ais_list)

    @pyqtSlot(object, str, object)
    def addObject(self, obj, name="", options=None):

        if options is None:
            options = {}

        root = self.CQ

        ais, shape_display = make_AIS(obj, options)

        root.addChild(
            ObjectTreeItem(
                name,
                shape=obj,
                shape_display=shape_display,
                ais=ais,
                sig=self.sigObjectPropertiesChanged,
                viewer_key=name,
            )
        )

        self.sigObjectsAdded.emit([ais])

    @pyqtSlot(list)
    @pyqtSlot()
    def removeObjects(self, objects=None):

        if objects:
            removed_items = [self.CQ.takeChild(i) for i in objects]
            removed_items_ais = [item.ais for item in removed_items if item is not None]
            for item in removed_items:
                if item is not None:
                    self._cq_items_by_key.pop(getattr(item, "viewer_key", ""), None)
        else:
            removed_children = self.CQ.takeChildren()
            removed_items_ais = [ch.ais for ch in removed_children]
            self._cq_items_by_key = {}
            self.previous_shapes = {}
            self.current_shapes = {}

        self.sigObjectsRemoved.emit(removed_items_ais)

    @pyqtSlot(bool)
    def stashObjects(self, action: bool):

        if action:
            self._stash = self.CQ.takeChildren()
            removed_items_ais = [ch.ais for ch in self._stash]
            self._cq_items_by_key = {}
            self.sigObjectsRemoved.emit(removed_items_ais)
        else:
            self.removeObjects()
            self.CQ.addChildren(self._stash)
            ais_list = [el.ais for el in self._stash]
            self._cq_items_by_key = {
                getattr(el, "viewer_key", el.properties["Name"]): el for el in self._stash
            }
            self.sigObjectsAdded.emit(ais_list)

    def _shape_records(self, objects):

        records = {}
        for name, obj in objects.items():
            key = getattr(obj, "viewer_key", name)
            if key in records:
                raise ValueError(f"Duplicate viewer key detected: {key}")
            records[key] = ViewerShapeRecord(
                key=key,
                name=getattr(obj, "display_name", name),
                shape=obj.shape,
                options=dict(obj.options),
                node_id=getattr(obj, "node_id", None),
                cache_status=getattr(obj, "cache_status", "miss"),
            )

        return records

    def _shape_names(self, records, keys):

        return [records[key].name for key in keys if key in records]

    def remove_diff_items(self, keys):

        for key in keys:
            item = self._cq_items_by_key.pop(key, None)
            if item is None:
                continue

            index = self.CQ.indexOfChild(item)
            if index != -1:
                removed = self.CQ.takeChild(index)
                self._pending_removed_ais.append(removed.ais)

    def move_diff_item(self, key, index):

        item = self._cq_items_by_key.get(key)
        if item is None:
            return

        current_index = self.CQ.indexOfChild(item)
        if current_index == -1 or current_index == index:
            return

        moved = self.CQ.takeChild(current_index)
        self.CQ.insertChild(index, moved)

    def upsert_diff_item(self, key, record, index):

        ais, shape_display = make_AIS(record.shape, record.options)
        child = ObjectTreeItem(
            record.name,
            shape=record.shape,
            shape_display=shape_display,
            ais=ais,
            sig=self.sigObjectPropertiesChanged,
            viewer_key=key,
            node_id=record.node_id,
            cache_status=record.cache_status,
        )

        self._restore_properties_by_key(child, self._pending_props_by_key, key)

        self.CQ.insertChild(index, child)
        self._cq_items_by_key[key] = child
        if child.properties["Visible"]:
            self._pending_added_ais.append(ais)

    def record_viewer_diff(self, diff):

        if self._pending_removed_ais:
            self.sigObjectsRemoved.emit(self._pending_removed_ais)

        if self._pending_added_ais:
            if self._pending_request_fit:
                self.sigObjectsAdded[list, bool].emit(self._pending_added_ais, True)
            else:
                self.sigObjectsAdded[list].emit(self._pending_added_ais)

        self.last_viewer_update_stats = {
            "mode": "diff",
            **diff.stats(),
            "updated_names": self._shape_names(diff.current_shapes, diff.updated_keys),
            "reused_names": self._shape_names(diff.current_shapes, diff.reused_keys),
        }
        self._pending_removed_ais = []
        self._pending_added_ais = []
        self._pending_request_fit = False
        self._pending_props_by_key = {}

    @pyqtSlot()
    def removeSelected(self):

        ixs = self.tree.selectedIndexes()
        rows = [ix.row() for ix in ixs]

        self.removeObjects(rows)

    def export(self, export_type, precision=None):

        items = self.tree.selectedItems()

        # if CQ models is selected get all children
        if [item for item in items if item is self.CQ]:
            CQ = self.CQ
            shapes = [CQ.child(i).shape for i in range(CQ.childCount())]
        # otherwise collect all selected children of CQ
        else:
            shapes = [item.shape for item in items if item.parent() is self.CQ]

        fname = get_save_filename(export_type)
        if fname != "":
            export(shapes, export_type, fname, precision)

    @pyqtSlot()
    def handleSelection(self):

        items = self.tree.selectedItems()
        if len(items) == 0:
            self._export_STL_action.setEnabled(False)
            self._export_STEP_action.setEnabled(False)
            return

        # emit list of all selected ais objects (might be empty)
        ais_objects = [item.ais for item in items if item.parent() is self.CQ]
        self.sigAISObjectsSelected.emit(ais_objects)

        # handle context menu and emit last selected CQ  object (if present)
        item = items[-1]
        if item.parent() is self.CQ:
            self._export_STL_action.setEnabled(True)
            self._export_STEP_action.setEnabled(True)
            self._clear_current_action.setEnabled(True)
            self.sigCQObjectSelected.emit(item.shape)
            self.properties_editor.setParameters(item.properties, showTop=False)
            self.properties_editor.setEnabled(True)
        elif item is self.CQ and item.childCount() > 0:
            self._export_STL_action.setEnabled(True)
            self._export_STEP_action.setEnabled(True)
        else:
            self._export_STL_action.setEnabled(False)
            self._export_STEP_action.setEnabled(False)
            self._clear_current_action.setEnabled(False)
            self.properties_editor.setEnabled(False)
            self.properties_editor.clear()

    @pyqtSlot(list)
    def handleGraphicalSelection(self, shapes):

        self.tree.clearSelection()

        CQ = self.CQ
        for i in range(CQ.childCount()):
            item = CQ.child(i)
            for shape in shapes:
                if item.ais.Shape().IsEqual(shape):
                    item.setSelected(True)

    @pyqtSlot(QTreeWidgetItem, int)
    def handleChecked(self, item, col):

        if type(item) is ObjectTreeItem:
            if item.checkState(0):
                item.properties["Visible"] = True
            else:
                item.properties["Visible"] = False
