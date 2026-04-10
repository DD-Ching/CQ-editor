import sys

from PyQt5.QtCore import QObject, Qt, pyqtSignal
from PyQt5.QtGui import QPalette, QColor
from PyQt5.QtWidgets import (
    QLabel,
    QMainWindow,
    QToolBar,
    QDockWidget,
    QAction,
    QApplication,
    QMenu,
)
from logbook import Logger
import cadquery as cq

from .widgets.editor import Editor
from .widgets.start_panel import StartPanel
from .widgets.viewer import OCCViewer
from .widgets.console import ConsoleWidget
from .widgets.object_tree import ObjectTree
from .widgets.traceback_viewer import TracebackPane
from .widgets.debugger import Debugger, LocalsView
from .widgets.cq_object_inspector import CQObjectInspector
from .widgets.log import LogViewer

from . import __version__
from .utils import (
    dock,
    add_actions,
    open_url,
    about_dialog,
    check_gtihub_for_updates,
    confirm,
)
from .mixins import MainMixin
from .icons import icon
from pyqtgraph.parametertree import Parameter
from .preferences import PreferencesWidget


class _PrintRedirectorSingleton(QObject):
    """This class monkey-patches `sys.stdout.write` to emit a signal.
    It is instanciated as `.main_window.PRINT_REDIRECTOR` and should not be instanciated again.
    """

    sigStdoutWrite = pyqtSignal(str)

    def __init__(self):
        super().__init__()

        original_stdout_write = sys.stdout.write

        def new_stdout_write(text: str):
            self.sigStdoutWrite.emit(text)
            return original_stdout_write(text)

        sys.stdout.write = new_stdout_write


PRINT_REDIRECTOR = _PrintRedirectorSingleton()


class MainWindow(QMainWindow, MainMixin):

    name = "CQ-Editor"
    org = "CadQuery"

    preferences = Parameter.create(
        name="Preferences",
        children=[
            {
                "name": "Light/Dark Theme",
                "type": "list",
                "value": "Light",
                "values": [
                    "Light",
                    "Dark",
                ],
            },
        ],
    )

    def __init__(self, parent=None, filename=None):

        super(MainWindow, self).__init__(parent)
        MainMixin.__init__(self)

        self.setWindowIcon(icon("app"))

        # Windows workaround - makes the correct task bar icon show up.
        if sys.platform == "win32":
            import ctypes

            myappid = "cq-editor"  # arbitrary string
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)

        self.viewer = OCCViewer(self)
        self.setCentralWidget(self.viewer.canvas)

        self.prepare_panes()
        self.registerComponent("viewer", self.viewer)
        self.prepare_toolbar()
        self.prepare_menubar()

        self.prepare_statusbar()
        self.prepare_actions()

        self.components["object_tree"].addLines()

        self.prepare_console()

        self.fill_dummy()

        self.setup_logging()

        # Allows us to react to the top-level settings for this window being changed
        self.preferences.sigTreeStateChanged.connect(self.preferencesChanged)

        self.restorePreferences()
        self.restoreWindow()

        # Handle the event of the editor being hidden or shown
        self.editor_dock = self.docks["editor"]
        self.editor_dock.visibilityChanged.connect(self.handleEditorVisiblityChange)
        self.handleEditorVisiblityChange(not self.editor_dock.isHidden())

        # Let the user know when the file has been modified
        self.components["editor"].document().modificationChanged.connect(
            self.update_window_title
        )

        if filename:
            self.components["editor"].load_from_file(filename)

        self.restoreComponentState()
        self.configure_workspace()

    def handleEditorVisiblityChange(self, visible):
        """
        Does the work required to enable/disable menu items when the Editor visibility is changed.
        """
        self.toggle_comment_action.setEnabled(visible)
        self.autocomplete_action.setEnabled(visible)

    def preferencesChanged(self, param, changes):
        """
        Triggered when the preferences for this window are changed.
        """

        # Use the default light theme/palette
        if self.preferences["Light/Dark Theme"] == "Light":
            QApplication.instance().setStyleSheet("")
            QApplication.instance().setPalette(QApplication.style().standardPalette())

            # The console theme needs to be changed separately
            self.components["console"].app_theme_changed("Light")
        # Use the dark theme/palette
        elif self.preferences["Light/Dark Theme"] == "Dark":
            QApplication.instance().setStyle("Fusion")

            # Now use a palette to switch to dark colors:
            white_color = QColor(255, 255, 255)
            black_color = QColor(0, 0, 0)
            red_color = QColor(255, 0, 0)
            palette = QPalette()
            palette.setColor(QPalette.Window, QColor(53, 53, 53))
            palette.setColor(QPalette.WindowText, white_color)
            palette.setColor(QPalette.Base, QColor(25, 25, 25))
            palette.setColor(QPalette.AlternateBase, QColor(53, 53, 53))
            palette.setColor(QPalette.ToolTipBase, black_color)
            palette.setColor(QPalette.ToolTipText, white_color)
            palette.setColor(QPalette.Text, white_color)
            palette.setColor(QPalette.Button, QColor(53, 53, 53))
            palette.setColor(QPalette.ButtonText, white_color)
            palette.setColor(QPalette.BrightText, red_color)
            palette.setColor(QPalette.Link, QColor(42, 130, 218))
            palette.setColor(QPalette.Highlight, QColor(42, 130, 218))
            palette.setColor(QPalette.HighlightedText, black_color)
            QApplication.instance().setPalette(palette)

            # The console theme needs to be changed separately
            self.components["console"].app_theme_changed("Dark")

        # We alter the color of the toolbar separately to avoid having separate dark theme icons
        p = self.toolbar.palette()
        if self.preferences["Light/Dark Theme"] == "Dark":
            p.setColor(QPalette.Background, QColor(120, 120, 120))

            # TWeak the QMenu items palette for dark theme
            menu_palette = self.menuBar().palette()
            menu_palette.setColor(QPalette.Base, QColor(80, 80, 80))
            for menu in self.menuBar().findChildren(QMenu):
                menu.setPalette(menu_palette)
        else:
            p.setColor(QPalette.Background, QColor(240, 240, 240))

            # Revert the QMenu items palette for dark theme
            menu_palette = self.menuBar().palette()
            menu_palette.setColor(QPalette.Base, QColor(240, 240, 240))
            for menu in self.menuBar().findChildren(QMenu):
                menu.setPalette(menu_palette)

        self.toolbar.setPalette(p)

    def closeEvent(self, event):

        self.saveWindow()
        self.savePreferences()
        self.saveComponentState()

        if self.components["editor"].document().isModified():

            rv = confirm(self, "Confirm close", "Close without saving?")

            if rv:
                event.accept()
                super(MainWindow, self).closeEvent(event)
            else:
                event.ignore()
        else:
            super(MainWindow, self).closeEvent(event)

    def prepare_panes(self):

        self.registerComponent(
            "editor",
            Editor(self),
            lambda c: dock(c, "Editor", self, defaultArea="left"),
        )

        self.registerComponent(
            "object_tree",
            ObjectTree(self),
            lambda c: dock(c, "Objects", self, defaultArea="right"),
        )

        self.registerComponent(
            "traceback_viewer",
            TracebackPane(self),
            lambda c: dock(c, "Current traceback", self, defaultArea="bottom"),
        )

        self.registerComponent("debugger", Debugger(self))

        self.registerComponent(
            "start",
            StartPanel(self),
            lambda c: dock(c, "Start", self, defaultArea="left"),
        )

        self.registerComponent(
            "console",
            ConsoleWidget(self),
            lambda c: dock(c, "Console", self, defaultArea="bottom"),
        )

        self.registerComponent(
            "variables_viewer",
            LocalsView(self),
            lambda c: dock(c, "Variables", self, defaultArea="right"),
        )

        self.registerComponent(
            "cq_object_inspector",
            CQObjectInspector(self),
            lambda c: dock(c, "CQ object inspector", self, defaultArea="right"),
        )
        self.registerComponent(
            "log",
            LogViewer(self),
            lambda c: dock(c, "Log viewer", self, defaultArea="bottom"),
        )

        for d in self.docks.values():
            d.show()

        PRINT_REDIRECTOR.sigStdoutWrite.connect(
            lambda text: self.components["log"].append(text)
        )

    def prepare_menubar(self):

        menu = self.menuBar()

        menu_file = menu.addMenu("&File")
        menu_edit = menu.addMenu("&Edit")
        menu_tools = menu.addMenu("&Tools")
        menu_run = menu.addMenu("&Run")
        menu_view = menu.addMenu("&View")
        menu_help = menu.addMenu("&Help")

        # per component menu elements
        menus = {
            "File": menu_file,
            "Edit": menu_edit,
            "Run": menu_run,
            "Tools": menu_tools,
            "View": menu_view,
            "Help": menu_help,
        }

        for comp in self.components.values():
            self.prepare_menubar_component(menus, comp.menuActions())

        # global menu elements
        menu_view.addSeparator()
        for d in self.findChildren(QDockWidget):
            menu_view.addAction(d.toggleViewAction())

        menu_view.addSeparator()
        for t in self.findChildren(QToolBar):
            menu_view.addAction(t.toggleViewAction())

        menu_view.addSeparator()
        self.reset_layout_action = QAction(
            "Reset Layout",
            self,
            shortcut="Ctrl+Shift+0",
            triggered=self.reset_layout,
        )
        menu_view.addAction(self.reset_layout_action)

        self.toggle_comment_action = QAction(
            icon("toggle-comment"),
            "Toggle Comment",
            self,
            shortcut="ctrl+/",
            triggered=self.components["editor"].toggle_comment,
        )
        menu_edit.addAction(self.toggle_comment_action)

        # Add the menu action to toggle auto-completion
        self.autocomplete_action = QAction(
            icon("search"),
            "Auto-Complete",
            self,
            shortcut="alt+/",
            triggered=self.components["editor"]._trigger_autocomplete,
        )
        menu_edit.addAction(self.autocomplete_action)

        # Add the menu action to open the code search controls
        self.search_action = QAction(
            icon("search"),
            "Search",
            self,
            shortcut="ctrl+F",
            triggered=self.components["editor"].search_widget.show_search,
        )
        menu_edit.addAction(self.search_action)

        menu_edit.addAction(
            QAction(
                icon("preferences"),
                "Preferences",
                self,
                triggered=self.edit_preferences,
            )
        )

        menu_help.addAction(
            QAction(icon("help"), "Documentation", self, triggered=self.documentation)
        )

        menu_help.addAction(
            QAction("CQ documentation", self, triggered=self.cq_documentation)
        )

        menu_help.addAction(QAction(icon("about"), "About", self, triggered=self.about))

        menu_help.addAction(
            QAction(
                "Check for CadQuery updates", self, triggered=self.check_for_cq_updates
            )
        )

    def prepare_menubar_component(self, menus, comp_menu_dict):

        for name, action in comp_menu_dict.items():
            menus[name].addActions(action)

    def prepare_toolbar(self):

        self.toolbar = QToolBar("Main toolbar", self, objectName="Main toolbar")

        for c in self.components.values():
            add_actions(self.toolbar, c.toolbarActions())

        self.addToolBar(self.toolbar)

    def prepare_statusbar(self):

        self.status_label = QLabel("", parent=self)
        self.statusBar().insertPermanentWidget(0, self.status_label)

    def prepare_actions(self):

        self.components["debugger"].sigRendered.connect(
            self.components["object_tree"].addObjects
        )
        self.components["debugger"].sigTraceback.connect(
            self.handle_traceback
        )
        self.components["debugger"].sigLocals.connect(
            self.components["variables_viewer"].update_frame
        )
        self.components["debugger"].sigLocals.connect(
            self.components["console"].push_vars
        )

        self.components["object_tree"].sigObjectsAdded[list].connect(
            self.components["viewer"].display_many
        )
        self.components["object_tree"].sigObjectsAdded[list, bool].connect(
            self.components["viewer"].display_many
        )
        self.components["object_tree"].sigItemChanged.connect(
            self.components["viewer"].update_item
        )
        self.components["object_tree"].sigObjectsRemoved.connect(
            self.components["viewer"].remove_items
        )
        self.components["object_tree"].sigCQObjectSelected.connect(
            self.components["cq_object_inspector"].setObject
        )
        self.components["object_tree"].sigObjectPropertiesChanged.connect(
            self.components["viewer"].redraw
        )
        self.components["object_tree"].sigAISObjectsSelected.connect(
            self.components["viewer"].set_selected
        )

        self.components["viewer"].sigObjectSelected.connect(
            self.components["object_tree"].handleGraphicalSelection
        )

        self.components["traceback_viewer"].sigHighlightLine.connect(
            self.components["editor"].go_to_line
        )

        self.components["cq_object_inspector"].sigDisplayObjects.connect(
            self.components["viewer"].display_many
        )
        self.components["cq_object_inspector"].sigRemoveObjects.connect(
            self.components["viewer"].remove_items
        )
        self.components["cq_object_inspector"].sigShowPlane.connect(
            self.components["viewer"].toggle_grid
        )
        self.components["cq_object_inspector"].sigShowPlane[bool, float].connect(
            self.components["viewer"].toggle_grid
        )
        self.components["cq_object_inspector"].sigChangePlane.connect(
            self.components["viewer"].set_grid_orientation
        )

        self.components["debugger"].sigLocalsChanged.connect(
            self.components["variables_viewer"].update_frame
        )
        self.components["debugger"].sigLineChanged.connect(
            self.components["editor"].set_debug_line
        )
        self.components["debugger"].sigDebugging.connect(
            self.components["object_tree"].stashObjects
        )
        self.components["debugger"].sigDebugging.connect(
            lambda active: (
                self.components["editor"].clear_debug_line() if not active else None
            )
        )
        self.components["debugger"].sigCQChanged.connect(
            self.components["object_tree"].addObjects
        )
        self.components["debugger"].sigRenderState.connect(self.handle_render_state)

        # trigger re-render when file is modified externally or saved
        self.components["editor"].triggerRerender.connect(
            self.components["debugger"].render
        )
        self.components["editor"].sigFilenameChanged.connect(
            self.handle_filename_change
        )
        # Allows updating of the status bar from the Editor
        self.components["editor"].statusChanged.connect(self.update_statusbar)

    def prepare_console(self):

        console = self.components["console"]
        obj_tree = self.components["object_tree"]

        # application related items
        console.push_vars({"self": self})

        # CQ related items
        console.push_vars(
            {
                "show": obj_tree.addObject,
                "show_object": obj_tree.addObject,
                "rand_color": self.components["debugger"]._rand_color,
                "cq": cq,
                "log": Logger(self.name).info,
            }
        )

    def fill_dummy(self):

        self.components["editor"].set_text(
            'import cadquery as cq\nresult = cq.Workplane("XY" ).box(3, 3, 0.5).edges("|Z").fillet(0.125)\nshow_object(result)'
        )
        self.components["editor"].reset_modified()

    def setup_logging(self):

        from logbook.compat import redirect_logging
        from logbook import INFO, Logger

        redirect_logging()
        self.components["log"].handler.level = INFO
        self.components["log"].handler.push_application()

        self._logger = Logger(self.name)

        def handle_exception(exc_type, exc_value, exc_traceback):

            if issubclass(exc_type, KeyboardInterrupt):
                sys.__excepthook__(exc_type, exc_value, exc_traceback)
                return

            self._logger.error(
                "Uncaught exception occurred",
                exc_info=(exc_type, exc_value, exc_traceback),
            )

        sys.excepthook = handle_exception

    def edit_preferences(self):

        prefs = PreferencesWidget(self, self.components)
        prefs.exec_()

    def about(self):

        about_dialog(
            self,
            f"About CQ-editor",
            f"PyQt GUI for CadQuery.\nVersion: {__version__}.\nSource Code: https://github.com/CadQuery/CQ-editor",
        )

    def check_for_cq_updates(self):

        check_gtihub_for_updates(self, cq)

    def documentation(self):

        open_url("https://github.com/CadQuery")

    def cq_documentation(self):

        open_url("https://cadquery.readthedocs.io/en/latest/")

    def handle_filename_change(self, fname):

        new_title = fname if fname else "*"
        self.setWindowTitle(f"{self.name}: {new_title}")

    def reset_layout(self):

        self.settings.remove("geometry")
        self.settings.remove("windowState")

        for dock in self.docks.values():
            dock.setFloating(False)

        self.configure_workspace()
        self.saveWindow()
        self.update_statusbar("Layout reset")

    def configure_workspace(self):

        self.setDockNestingEnabled(True)
        self.setStyleSheet(
            self.styleSheet()
            + "\nQMainWindow::separator { width: 10px; height: 10px; background: rgba(90, 90, 90, 0.45); }"
            + "\nQMainWindow::separator:hover { background: rgba(120, 160, 255, 0.7); }"
        )

        self.tabifyDockWidget(self.docks["object_tree"], self.docks["variables_viewer"])
        self.tabifyDockWidget(
            self.docks["object_tree"], self.docks["cq_object_inspector"]
        )
        self.tabifyDockWidget(self.docks["log"], self.docks["console"])
        self.tabifyDockWidget(self.docks["log"], self.docks["traceback_viewer"])

        self.docks["editor"].show()
        self.docks["start"].show()
        self.docks["object_tree"].show()
        self.docks["log"].show()

        self.docks["editor"].raise_()
        self.docks["start"].raise_()
        self.docks["object_tree"].raise_()
        self.docks["log"].raise_()
        self.docks["variables_viewer"].hide()
        self.docks["cq_object_inspector"].hide()
        self.docks["console"].hide()
        self.docks["traceback_viewer"].hide()

        self.resizeDocks(
            [self.docks["editor"], self.docks["start"]],
            [460, 220],
            Qt.Vertical,
        )
        self.resizeDocks(
            [self.docks["editor"], self.docks["object_tree"]],
            [760, 260],
            Qt.Horizontal,
        )
        self.resizeDocks(
            [
                self.docks["traceback_viewer"],
                self.docks["log"],
                self.docks["console"],
            ],
            [320, 200, 140],
            Qt.Vertical,
        )

    def handle_traceback(self, exc_info, code):

        self.components["traceback_viewer"].addTraceback(exc_info, code)

        if not exc_info:
            self.docks["log"].raise_()
            return

        exc_type, exc_value, _tb = exc_info
        self.docks["traceback_viewer"].show()
        self.docks["traceback_viewer"].raise_()
        self.resizeDocks(
            [
                self.docks["traceback_viewer"],
                self.docks["log"],
                self.docks["console"],
            ],
            [320, 180, 140],
            Qt.Vertical,
        )
        self.update_statusbar(f"{exc_type.__name__}: {exc_value}")

    def handle_render_state(self, state):

        event = state.get("event")
        mode = state.get("mode", "render")

        if event == "start":
            text = f"Rendering ({mode})..."
        elif event == "node":
            text = (
                f"Rendering ({mode}) step {state.get('index', 0)}/"
                f"{state.get('total_nodes', 0)}"
            )
        elif event == "fallback":
            text = state.get("message", "Falling back to full render")
        elif event == "finish":
            text = (
                f"Render done ({mode}) "
                f"{state.get('exec_time_s', 0.0):.3f}s "
                f"viewer {state.get('viewer_apply_time_s', 0.0):.4f}s"
            )
        elif event == "error":
            text = f"{mode} failed: {state.get('message', 'unknown error')}"
        else:
            return

        self.update_statusbar(text)

    def update_window_title(self, modified):
        """
        Allows updating the window title to show that the document has been modified.
        """
        title = self.windowTitle().rstrip("*")
        if modified:
            title += "*"
        self.setWindowTitle(title)

    def update_statusbar(self, status_text):
        """
        Allow updating the status bar with information.
        """

        # Update the statusbar text
        self.status_label.setText(status_text)


if __name__ == "__main__":

    pass
