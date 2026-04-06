import pytest
from types import SimpleNamespace
from pathlib import Path

from PyQt5.QtWidgets import QApplication, QMainWindow, QTreeWidgetItem

from cq_editor.widgets.start_panel import StartPanel


class DummySignal:
    def connect(self, *_args, **_kwargs):
        pass


class DummyProcess:
    def __init__(self, *_args, **_kwargs):
        self.started = DummySignal()
        self.readyReadStandardOutput = DummySignal()
        self.readyReadStandardError = DummySignal()
        self.finished = DummySignal()
        self.program = None
        self.arguments = None

    def setProgram(self, program):
        self.program = program

    def setArguments(self, arguments):
        self.arguments = arguments

    def closeWriteChannel(self):
        pass

    def start(self):
        pass


class DummyEditor:
    filename = ""
    sigFilenameChanged = DummySignal()

    def confirm_discard(self):
        return True

    def set_text(self, text):
        self._text = text

    def toPlainText(self):
        return getattr(self, "_text", "")

    def open(self):
        pass

    def reset_modified(self):
        pass


class DummyDebugger:
    sigRenderState = DummySignal()
    sigTraceback = DummySignal()

    def render(self):
        pass


class DummyViewer:
    def __init__(self):
        self.calls = []

    def capture_reference_views(self, output_dir, views=("front", "left", "top", "iso")):
        self.calls.append((output_dir, tuple(views)))
        dumped = {}
        for name in views:
            path = Path(output_dir) / f"{name}.png"
            path.write_bytes(b"png")
            dumped[name] = str(path)
        return dumped


class DummyMain(QMainWindow):
    def __init__(self):
        super().__init__()
        self.components = {
            "editor": DummyEditor(),
            "debugger": DummyDebugger(),
            "viewer": DummyViewer(),
        }


class DummyValue:
    def __init__(self, value):
        self._value = value

    def currentText(self):
        return self._value

    def currentData(self):
        return self._value

    def value(self):
        return self._value

    def toPlainText(self):
        return self._value


@pytest.fixture
def panel():
    app = QApplication.instance() or QApplication([])
    main = DummyMain()
    widget = StartPanel(main)
    widget.setParent(None)
    widget._test_main = main
    return widget


def test_native_feature_audit_passes_for_mirrored_pattern_script(panel):
    panel._last_prompt_text = "Create a symmetric bracket with a 2x2 bolt pattern."
    script = """# PLAN: mirrored bracket tabs with a native hole pattern
# NATIVE_OPS: extrude, mirrorY, rarray, hole
# CHECKS: mirrored tabs, centered pattern, compact base
import cadquery as cq
base = cq.Workplane("XY").box(60, 24, 8)
tabs = cq.Workplane("XY").center(0, 16).rect(18, 8).mirrorY().extrude(8)
result = base.union(tabs).faces(">Z").workplane().rarray(36, 12, 2, 2).hole(4)
show_object(result)
"""

    audit = panel._audit_script(script)
    build_ok, build_error = panel._validate_script_build(script)

    assert build_ok, build_error
    assert audit["passed"] is True
    assert audit["score"] >= 70
    assert "mirrorYx1" in audit["ops"]
    assert "rarrayx1" in audit["ops"]


def test_native_feature_audit_warns_on_manual_pattern_script(panel):
    panel._last_prompt_text = "Create a symmetric bracket with a 2x2 bolt pattern."
    script = """import cadquery as cq
parts = []
for x in (-20, -10, 0, 10, 20):
    parts.append(cq.Workplane("XY").box(5, 5, 5).translate((x, 0, 0)).val())
parts.append(cq.Workplane("XY").box(5, 5, 5).translate((0, 10, 0)).val())
parts.append(cq.Workplane("XY").box(5, 5, 5).translate((0, 20, 0)).val())
parts.append(cq.Workplane("XY").box(5, 5, 5).translate((0, 30, 0)).val())
result = cq.Workplane("XY").add(cq.Compound.makeCompound(parts))
show_object(result)
"""

    audit = panel._audit_script(script)

    assert audit["passed"] is True
    assert audit["score"] < 70
    assert any("mirror primitive" in warning for warning in audit["warnings"])
    assert any("array primitive" in warning for warning in audit["warnings"])


def test_build_validation_detects_invalid_script(panel):
    script = """import cadquery as cq
base = cq.Workplane("XY").box(60,24,8)
tabs = cq.Workplane("XY").center(0,16).rect(18,8).extrude(8)
tabs = tabs.combine().mirrorY()
result = base.union(tabs).faces(">Z").workplane().rarray(36,12,2,2).hole(4)
show_object(result)
"""

    build_ok, build_error = panel._validate_script_build(script)

    assert build_ok is False
    assert build_error


def test_single_pass_audit_allows_simple_sphere(panel):
    panel._last_prompt_text = "Draw a sphere."
    script = """import cadquery as cq
result = cq.Workplane("XY").sphere(10)
show_object(result)
"""

    audit = panel._audit_script(script)
    build_ok, build_error = panel._validate_script_build(script)

    assert build_ok, build_error
    assert audit["passed"] is True


def test_sections_start_collapsed_and_can_expand():
    app = QApplication.instance() or QApplication([])
    main = DummyMain()
    panel = StartPanel(main)

    assert panel.workflow_widget.isHidden() is True
    assert panel.history_widget.isHidden() is True

    panel.workflow_button.click()
    panel.history_button.click()

    assert panel.workflow_widget.isHidden() is False
    assert panel.history_widget.isHidden() is False


def test_failure_lines_include_stage_hint_and_cli_tail(panel):
    panel._active_stage = {"task_id": "codex", "effort": "medium"}
    panel._codex_event_summaries = ["CLI: turn started"]
    panel._codex_stderr = ["INFO: starting\n", "ERROR: request timed out upstream\n"]

    lines = panel._failure_lines("Generation timed out after 90s.", timed_out=True)

    assert any("Problem: Generation timed out after 90s." in line for line in lines)
    assert any("Stage: Codex CLI (medium)" in line for line in lines)
    assert any("Iterations = 1" in line for line in lines)
    assert any("CLI tail:" in line and "request timed out upstream" in line for line in lines)


def test_handle_codex_event_updates_detail_and_history():
    app = QApplication.instance() or QApplication([])
    main = DummyMain()
    panel = StartPanel(main)
    panel._active_stage = {"task_id": "modeler", "effort": "medium"}
    panel._start_history_entry()

    panel._handle_codex_event({"type": "turn.started"})

    assert panel._codex_event_summaries[-1] == "CLI: turn started"
    assert "turn started" in panel.detail_label.text()
    assert panel._active_history_item.childCount() >= 3


def test_handle_traceback_shows_render_specific_failure():
    app = QApplication.instance() or QApplication([])
    main = DummyMain()
    panel = StartPanel(main)
    panel._codex_event_summaries = ["CLI: stage completed"]
    panel._codex_stderr = ["ERROR: transport closed\n"]
    panel.history_tree.insertTopLevelItem(0, QTreeWidgetItem(["Round 1 · Ready"]))

    exc_info = (RuntimeError, RuntimeError("Standard_Failure: ChFi3d_Builder:only 2 faces"), None)
    panel.handle_traceback(exc_info, "")

    text = panel.failure_label.text()
    assert "Stage: CQ render" in text
    assert "Hint:" in text
    assert "CLI tail:" not in text
    assert "fillet" in text.lower() or "chamfer" in text.lower()


def test_auto_repair_escalates_render_failure(monkeypatch):
    app = QApplication.instance() or QApplication([])
    main = DummyMain()
    panel = StartPanel(main)
    panel._codex_path = "codex"
    panel._codex_login_ok = True
    panel._auto_repair_budget = 1
    calls = []

    monkeypatch.setattr(
        "cq_editor.widgets.start_panel.QTimer.singleShot",
        lambda _ms, fn: fn(),
    )
    monkeypatch.setattr(
        panel,
        "generate_with_codex",
        lambda *args, **kwargs: calls.append(kwargs),
    )

    assert panel._begin_auto_repair("RuntimeError: render failed") is True
    assert panel._auto_repair_budget == 0
    assert calls == [
        {
            "auto_prompt": "Repair the current CadQuery script with the smallest viable change. Fix this render error: RuntimeError: render failed",
            "auto_mode": "repair",
            "auto_effort": "xhigh",
            "skip_confirm": True,
        }
    ]


def test_context_summary_detects_project_agents(panel):
    summary = panel._context_summary()

    assert "AGENTS.md" in summary


def test_refresh_cli_status_reports_login_and_smoke(monkeypatch):
    app = QApplication.instance() or QApplication([])
    main = DummyMain()
    panel = StartPanel(main)
    panel._codex_path = "codex"

    def fake_run(cmd, **_kwargs):
        return SimpleNamespace(
            returncode=0, stdout="Logged in using ChatGPT\n", stderr=""
        )

    monkeypatch.setattr("cq_editor.widgets.start_panel.subprocess.run", fake_run)
    monkeypatch.setattr(panel, "_run_codex_smoke_check", lambda: (True, "exec ok (1.2s)"))

    panel.refresh_cli_status(run_smoke=True)

    assert "Logged in using ChatGPT" in panel.cli_label.text()
    assert "login ok" in panel.check_label.text()
    assert "exec ok" in panel.check_label.text()


def test_codex_timeout_scales_with_effort():
    app = QApplication.instance() or QApplication([])
    main = DummyMain()
    panel = StartPanel(main)
    panel.lean_tokens.setChecked(True)
    panel._active_stage = {"effort": "xhigh"}

    assert panel._codex_timeout_ms() == 210000


def test_stage_plan_uses_single_codex_pass_by_default(panel):
    panel.planner_effort = DummyValue("xhigh")
    panel.iterations_spin = DummyValue(1)
    panel.mode_combo = DummyValue("new")

    stages = panel._build_stage_plan()

    assert [stage["task_id"] for stage in stages] == ["codex"]
    assert [stage["effort"] for stage in stages] == ["xhigh"]


def test_stage_plan_ignores_iterations_and_workers(panel):
    panel.worker_effort = DummyValue("low")
    panel.planner_effort = DummyValue("xhigh")
    panel.iterations_spin = DummyValue(3)
    panel.mode_combo = DummyValue("new")

    stages = panel._build_stage_plan()

    assert [stage["task_id"] for stage in stages] == ["codex"]
    assert [stage["effort"] for stage in stages] == ["xhigh"]


def test_build_stage_prompt_uses_single_cli_prompt(panel):
    panel.prompt = DummyValue("Draw a gear.")
    panel.mode_combo = DummyValue("new")
    panel.planner_effort = DummyValue("high")
    panel.lean_tokens = SimpleNamespace(isChecked=lambda: True)
    stage = {
        "task_id": "codex",
        "kind": "new",
        "effort": "high",
    }

    prompt = panel._build_stage_prompt(stage, "")

    assert "Write one CadQuery Python script for CQ-editor." in prompt
    assert 'cq.Workplane("XY").circle(r)' in prompt
    assert "reasoning effort high" in prompt
    assert "User request: Draw a gear." in prompt


def test_capture_view_images_attaches_reference_views_for_refine():
    app = QApplication.instance() or QApplication([])
    main = DummyMain()
    panel = StartPanel(main)
    panel.mode_combo.setCurrentIndex(panel.mode_combo.findData("refine"))

    images = panel._capture_view_images()

    assert tuple(images.keys()) == ("front", "left", "top", "iso")
    assert panel._main_window.components["viewer"].calls
    panel._cleanup_codex_images()


def test_build_stage_prompt_mentions_attached_views(panel):
    panel.prompt = DummyValue("Refine this bicycle.")
    panel.mode_combo = DummyValue("refine")
    panel.planner_effort = DummyValue("high")
    panel.lean_tokens = SimpleNamespace(isChecked=lambda: True)
    panel._codex_image_paths = {
        "front": "/tmp/front.png",
        "left": "/tmp/left.png",
        "top": "/tmp/top.png",
        "iso": "/tmp/iso.png",
    }

    prompt = panel._build_stage_prompt({"task_id": "codex", "kind": "refine", "effort": "high"})

    assert "Attached images show the current model from these views" in prompt
    assert "front, left, top, iso" in prompt


def test_launch_stage_passes_images_to_codex(monkeypatch):
    app = QApplication.instance() or QApplication([])
    main = DummyMain()
    panel = StartPanel(main)
    panel._codex_path = "codex"
    panel._main_window.components["editor"].filename = "/Users/ddh/Downloads/CAnend/worktrees/cadquery-runtime/examples/incremental/viewer_diff_plate.py"
    stage = {"task_id": "codex", "kind": "refine", "effort": "high"}

    monkeypatch.setattr("cq_editor.widgets.start_panel.QProcess", DummyProcess)
    monkeypatch.setattr(
        panel,
        "_capture_view_images",
        lambda: {
            "front": "/tmp/front.png",
            "left": "/tmp/left.png",
        },
    )

    panel._launch_stage(stage)

    assert panel._codex_process.arguments.count("-i") == 2
    assert "/tmp/front.png" in panel._codex_process.arguments
    assert "/tmp/left.png" in panel._codex_process.arguments
