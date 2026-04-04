import sys
from contextlib import ExitStack, contextmanager
from enum import Enum, auto
from time import perf_counter
from types import SimpleNamespace, FrameType, ModuleType
from typing import List
from bdb import BdbQuit
from inspect import currentframe

import cadquery as cq
from cadquery.graph_exec import IncrementalExecutionSession, UnsupportedScriptError
from PyQt5 import QtCore
from PyQt5.QtCore import (
    Qt,
    QObject,
    pyqtSlot,
    pyqtSignal,
    QEventLoop,
    QAbstractTableModel,
    )
from PyQt5.QtWidgets import QAction, QTableView

from logbook import info
from path import Path
from pyqtgraph.parametertree import Parameter
from ..icons import icon
from random import randrange as rrr, seed

from ..cq_utils import find_cq_objects, reload_cq
from ..mixins import ComponentMixin

DUMMY_FILE = "<cq_editor-string>"


class DbgState(Enum):

    STEP = auto()
    CONT = auto()
    STEP_IN = auto()
    RETURN = auto()


class DbgEevent(object):

    LINE = "line"
    CALL = "call"
    RETURN = "return"


class LocalsModel(QAbstractTableModel):

    HEADER = ("Name", "Type", "Value")

    def __init__(self, parent):

        super(LocalsModel, self).__init__(parent)
        self.frame = None

    def update_frame(self, frame):

        self.frame = [
            (k, type(v).__name__, str(v))
            for k, v in frame.items()
            if not k.startswith("_")
        ]

    def rowCount(self, parent=QtCore.QModelIndex()):

        if self.frame:
            return len(self.frame)
        else:
            return 0

    def columnCount(self, parent=QtCore.QModelIndex()):

        return 3

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.HEADER[section]
        return QAbstractTableModel.headerData(self, section, orientation, role)

    def data(self, index, role):
        if role == QtCore.Qt.DisplayRole:
            i = index.row()
            j = index.column()
            return self.frame[i][j]
        else:
            return QtCore.QVariant()


class LocalsView(QTableView, ComponentMixin):

    name = "Variables"

    def __init__(self, parent):

        super(LocalsView, self).__init__(parent)
        ComponentMixin.__init__(self)

        header = self.horizontalHeader()
        header.setStretchLastSection(True)

        vheader = self.verticalHeader()
        vheader.setVisible(False)

    @pyqtSlot(dict)
    def update_frame(self, frame):

        model = LocalsModel(self)
        model.update_frame(frame)

        self.setModel(model)


class Debugger(QObject, ComponentMixin):

    name = "Debugger"

    preferences = Parameter.create(
        name="Preferences",
        children=[
            {"name": "Reload CQ", "type": "bool", "value": False},
            {"name": "Add script dir to path", "type": "bool", "value": True},
            {"name": "Change working dir to script dir", "type": "bool", "value": True},
            {"name": "Reload imported modules", "type": "bool", "value": True},
            {"name": "Incremental execution (MVP)", "type": "bool", "value": True},
        ],
    )

    sigRendered = pyqtSignal(dict)
    sigLocals = pyqtSignal(dict)
    sigTraceback = pyqtSignal(object, str)
    sigRenderState = pyqtSignal(dict)

    sigFrameChanged = pyqtSignal(object)
    sigLineChanged = pyqtSignal(int)
    sigLocalsChanged = pyqtSignal(dict)
    sigCQChanged = pyqtSignal(dict, bool)
    sigDebugging = pyqtSignal(bool)

    _frames: List[FrameType]
    _stop_debugging: bool

    def __init__(self, parent):

        super(Debugger, self).__init__(parent)
        ComponentMixin.__init__(self)

        self.inner_event_loop = QEventLoop(self)

        self._actions = {
            "Run": [
                QAction(
                    icon("run"), "Render", self, shortcut="F5", triggered=self.render
                ),
                QAction(
                    icon("debug"),
                    "Debug",
                    self,
                    checkable=True,
                    shortcut="ctrl+F5",
                    triggered=self.debug,
                ),
                QAction(
                    icon("arrow-step-over"),
                    "Step",
                    self,
                    shortcut="ctrl+F10",
                    triggered=lambda: self.debug_cmd(DbgState.STEP),
                ),
                QAction(
                    icon("arrow-step-in"),
                    "Step in",
                    self,
                    shortcut="ctrl+F11",
                    triggered=lambda: self.debug_cmd(DbgState.STEP_IN),
                ),
                QAction(
                    icon("arrow-continue"),
                    "Continue",
                    self,
                    shortcut="ctrl+F12",
                    triggered=lambda: self.debug_cmd(DbgState.CONT),
                ),
            ]
        }

        self._frames = []
        self._stop_debugging = False
        self._incremental_session = IncrementalExecutionSession()
        self._incremental_filename = None

    def get_current_script(self):

        return self.parent().components["editor"].get_text_with_eol()

    def get_current_script_path(self):

        filename = self.parent().components["editor"].filename
        if filename:
            return Path(filename).absolute()

    def get_breakpoints(self):

        return self.parent().components["editor"].debugger.get_breakpoints()

    def set_breakpoints(self, breakpoints):
        return self.parent().components["editor"].debugger.set_breakpoints(breakpoints)

    def compile_code(self, cq_script, cq_script_path=None):

        try:
            module = ModuleType("__cq_main__")
            if cq_script_path:
                module.__dict__["__file__"] = cq_script_path
            cq_code = compile(cq_script, DUMMY_FILE, "exec")
            return cq_code, module
        except Exception:
            self.sigTraceback.emit(sys.exc_info(), cq_script)
            return None, None

    def _exec(self, code, locals_dict, globals_dict):

        with self._execution_context():
            exec(code, locals_dict, globals_dict)

    @contextmanager
    def _execution_context(self):

        with ExitStack() as stack:
            p = (self.get_current_script_path() or Path("")).absolute().dirname()

            if self.preferences["Add script dir to path"] and p.exists():
                sys.path.insert(0, p)
                stack.callback(sys.path.remove, p)
            if self.preferences["Change working dir to script dir"] and p.exists():
                stack.enter_context(p)
            if self.preferences["Reload imported modules"]:
                stack.enter_context(module_manager())

            yield

    @staticmethod
    def _rand_color(alpha=0.0, cfloat=False):
        # helper function to generate a random color dict
        # for CQ-editor's show_object function
        lower = 10
        upper = 100  # not too high to keep color brightness in check
        if cfloat:  # for two output types depending on need
            return (
                (rrr(lower, upper) / 255),
                (rrr(lower, upper) / 255),
                (rrr(lower, upper) / 255),
                alpha,
            )
        return {
            "alpha": alpha,
            "color": (
                rrr(lower, upper),
                rrr(lower, upper),
                rrr(lower, upper),
            ),
        }

    def _inject_locals(self, module):

        cq_objects = {}

        def _show_object(obj, name=None, options=None, **kwargs):

            if options is None:
                options = {}
            else:
                options = dict(options)
            options.update(kwargs)

            if name is None:
                name = self._resolve_object_name(module, obj)

            node_id = module.__dict__.get("__cq_node_id__")
            cache_status = module.__dict__.get("__cq_cache_status__", "miss")
            output_index = max(int(module.__dict__.get("__cq_output_index__", 1)) - 1, 0)
            viewer_key = self._viewer_key(node_id, name, output_index)

            cq_objects.update(
                {
                    name: SimpleNamespace(
                        shape=obj,
                        options=options,
                        node_id=node_id,
                        cache_status=cache_status,
                        viewer_key=viewer_key,
                        display_name=name,
                    )
                }
            )

        def _debug(obj, name=None):

            _show_object(obj, name, options=dict(color="red", alpha=0.2))

        module.__dict__["show_object"] = _show_object
        module.__dict__["debug"] = _debug
        module.__dict__["rand_color"] = self._rand_color
        module.__dict__["log"] = lambda x: info(str(x))
        module.__dict__["cq"] = cq

        return cq_objects, set(module.__dict__) - {"cq"}

    def _resolve_object_name(self, module, obj):

        frame = currentframe()
        caller = frame.f_back.f_back if frame and frame.f_back and frame.f_back.f_back else None
        d = caller.f_locals if caller else {}

        try:
            return list(d.keys())[list(d.values()).index(obj)]
        except ValueError:
            for key, value in module.__dict__.items():
                if key.startswith("_"):
                    continue
                if value is obj:
                    return key

        return str(id(obj))

    def _viewer_key(self, node_id, name, output_index):

        if node_id:
            return f"{node_id}:{name}:{output_index}"

        return f"{name}:{output_index}"

    def _cleanup_locals(self, module, injected_names):

        for name in injected_names:
            module.__dict__.pop(name, None)

    def _format_render_names(self, names, limit=6):

        if not names:
            return "-"
        if len(names) <= limit:
            return ", ".join(names)
        return f"{', '.join(names[:limit])}, +{len(names) - limit} more"

    def _log_render_status(self, incremental, exec_time_s):

        viewer_stats = self.parent().components["object_tree"].last_viewer_update_stats
        info(
            "\n".join(
                (
                    f"Incremental render: {'yes' if incremental else 'no'}",
                    f"Updated: {self._format_render_names(viewer_stats.get('updated_names', []))}",
                    f"Reused: {self._format_render_names(viewer_stats.get('reused_names', []))}",
                    f"Exec: {exec_time_s:.3f}s",
                    f"Viewer: {viewer_stats.get('apply_time_s', 0.0):.4f}s",
                )
            )
        )

    def _emit_render_state(self, payload):

        self.sigRenderState.emit(payload)

    def _render_finish_state(self, incremental, exec_time_s):

        viewer_stats = self.parent().components["object_tree"].last_viewer_update_stats
        return {
            "event": "finish",
            "mode": "incremental" if incremental else "full",
            "incremental": incremental,
            "exec_time_s": exec_time_s,
            "viewer_apply_time_s": viewer_stats.get("apply_time_s", 0.0),
            "updated_names": viewer_stats.get("updated_names", []),
            "reused_names": viewer_stats.get("reused_names", []),
            "updated_count": viewer_stats.get("updated", 0),
            "reused_count": viewer_stats.get("reused", 0),
            "added_count": viewer_stats.get("added", 0),
            "removed_count": viewer_stats.get("removed", 0),
        }

    @pyqtSlot(bool)
    def render(self):

        seed(59798267586177)
        if self.preferences["Reload CQ"]:
            reload_cq()
            self._incremental_session.reset()

        cq_script = self.get_current_script()
        cq_script_path = self.get_current_script_path()
        current_filename = str(cq_script_path or DUMMY_FILE)
        if current_filename != self._incremental_filename:
            self._incremental_session.reset()
            self._incremental_filename = current_filename

        if self.preferences["Incremental execution (MVP)"]:
            try:
                cq_objects, module_dict, metrics = self._render_incremental(
                    cq_script, cq_script_path
                )
                self.sigRendered.emit(cq_objects)
                self.sigTraceback.emit(None, cq_script)
                self.sigLocals.emit(module_dict)
                self._log_render_status(True, metrics.total_runtime_s)
                self._emit_render_state(
                    self._render_finish_state(True, metrics.total_runtime_s)
                )
                return
            except UnsupportedScriptError:
                self._incremental_session.reset()
                self._emit_render_state(
                    {
                        "event": "fallback",
                        "mode": "incremental",
                        "message": "Unsupported script shape. Falling back to full render.",
                    }
                )
            except Exception:
                exc_info = sys.exc_info()
                sys.last_traceback = exc_info[-1]
                self._emit_render_state(
                    {
                        "event": "error",
                        "mode": "incremental",
                        "message": str(exc_info[1]),
                    }
                )
                self.sigTraceback.emit(exc_info, cq_script)
                return

        cq_code, module = self.compile_code(cq_script, cq_script_path)

        if cq_code is None:
            self._emit_render_state(
                {
                    "event": "error",
                    "mode": "full",
                    "message": "Compile failed",
                }
            )
            return

        cq_objects, injected_names = self._inject_locals(module)
        render_start = perf_counter()
        self._emit_render_state(
            {
                "event": "start",
                "mode": "full",
            }
        )

        try:
            self._exec(cq_code, module.__dict__, module.__dict__)

            # remove the special methods
            self._cleanup_locals(module, injected_names)

            # collect all CQ objects if no explicit show_object was called
            if len(cq_objects) == 0:
                cq_objects = find_cq_objects(module.__dict__)
            self.sigRendered.emit(cq_objects)
            self.sigTraceback.emit(None, cq_script)
            self.sigLocals.emit(module.__dict__)
            exec_time_s = perf_counter() - render_start
            self._log_render_status(False, exec_time_s)
            self._emit_render_state(self._render_finish_state(False, exec_time_s))
        except Exception:
            exc_info = sys.exc_info()
            sys.last_traceback = exc_info[-1]
            self._emit_render_state(
                {
                    "event": "error",
                    "mode": "full",
                    "message": str(exc_info[1]),
                }
            )
            self.sigTraceback.emit(exc_info, cq_script)

    def _render_incremental(self, cq_script, cq_script_path):

        module = ModuleType("__cq_main__")
        if cq_script_path:
            module.__dict__["__file__"] = cq_script_path

        cq_objects, injected_names = self._inject_locals(module)

        try:
            hooks = {
                name: module.__dict__[name]
                for name in ("show_object", "debug")
                if name in module.__dict__
            }

            with self._execution_context():
                result = self._incremental_session.execute(
                    cq_script,
                    env=module.__dict__,
                    filename=str(cq_script_path or DUMMY_FILE),
                    hooks=hooks,
                    progress_callback=self._emit_render_state,
                )

            if not result.success:
                raise result.exception

            self._cleanup_locals(module, injected_names)

            if len(cq_objects) == 0:
                cq_objects = find_cq_objects(module.__dict__)

            return cq_objects, module.__dict__, result.instrumentation
        finally:
            if injected_names:
                self._cleanup_locals(module, injected_names)

    @property
    def breakpoints(self):
        return [el[0] for el in self.get_breakpoints()]

    @pyqtSlot(bool)
    def debug(self, value):

        # used to stop the debugging session early
        self._stop_debugging = False

        if value:
            self.previous_trace = previous_trace = sys.gettrace()

            self.sigDebugging.emit(True)
            self.state = DbgState.CONT

            self.script = self.get_current_script()
            cq_script_path = self.get_current_script_path()
            code, module = self.compile_code(self.script, cq_script_path)

            if code is None:
                self.sigDebugging.emit(False)
                self._actions["Run"][1].setChecked(False)
                return

            cq_objects, injected_names = self._inject_locals(module)

            # clear possible traceback
            self.sigTraceback.emit(None, self.script)

            try:
                sys.settrace(self.trace_callback)
                exec(code, module.__dict__, module.__dict__)
            except BdbQuit:
                pass
            except Exception:
                exc_info = sys.exc_info()
                sys.last_traceback = exc_info[-1]
                self.sigTraceback.emit(exc_info, self.script)
            finally:
                sys.settrace(previous_trace)
                self.sigDebugging.emit(False)
                self._actions["Run"][1].setChecked(False)

                if len(cq_objects) == 0:
                    cq_objects = find_cq_objects(module.__dict__)
                self.sigRendered.emit(cq_objects)

                self._cleanup_locals(module, injected_names)
                self.sigLocals.emit(module.__dict__)

                self._frames = []
                self.inner_event_loop.exit(0)
        else:
            self._stop_debugging = True
            self.inner_event_loop.exit(0)

    def debug_cmd(self, state=DbgState.STEP):

        self.state = state
        self.inner_event_loop.exit(0)

    def trace_callback(self, frame, event, arg):

        filename = frame.f_code.co_filename

        if filename == DUMMY_FILE:
            if not self._frames:
                self._frames.append(frame)
            self.trace_local(frame, event, arg)
            return self.trace_callback

        else:
            return None

    def trace_local(self, frame, event, arg):

        lineno = frame.f_lineno

        if event in (DbgEevent.LINE,):
            if (
                self.state in (DbgState.STEP, DbgState.STEP_IN)
                and frame is self._frames[-1]
            ) or (lineno in self.get_breakpoints()):

                if lineno in self.get_breakpoints():
                    self._frames.append(frame)

                self.sigLineChanged.emit(lineno)
                self.sigFrameChanged.emit(frame)
                self.sigLocalsChanged.emit(frame.f_locals)
                self.sigCQChanged.emit(find_cq_objects(frame.f_locals), True)

                self.inner_event_loop.exec_()

        elif event in (DbgEevent.RETURN):
            self.sigLocalsChanged.emit(frame.f_locals)
            self._frames.pop()

        elif event == DbgEevent.CALL:
            func_filename = frame.f_code.co_filename
            if self.state == DbgState.STEP_IN and func_filename == DUMMY_FILE:
                self.sigLineChanged.emit(lineno)
                self.sigFrameChanged.emit(frame)
                self.state = DbgState.STEP
                self._frames.append(frame)

        if self._stop_debugging:
            raise BdbQuit  # stop debugging if requested


@contextmanager
def module_manager():
    """unloads any modules loaded while the context manager is active"""
    loaded_modules = set(sys.modules.keys())

    try:
        yield
    finally:
        new_modules = set(sys.modules.keys()) - loaded_modules
        for module_name in new_modules:
            del sys.modules[module_name]
