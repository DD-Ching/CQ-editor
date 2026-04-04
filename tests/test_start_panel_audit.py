import pytest
from types import SimpleNamespace

from PyQt5.QtWidgets import QApplication, QMainWindow

from cq_editor.widgets.start_panel import StartPanel


class DummySignal:
    def connect(self, *_args, **_kwargs):
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


class DummyMain(QMainWindow):
    def __init__(self):
        super().__init__()
        self.components = {"editor": DummyEditor(), "debugger": DummyDebugger()}


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


def test_native_feature_audit_blocks_manual_pattern_script(panel):
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

    assert audit["passed"] is False
    assert audit["score"] < 70
    assert any("mirror primitive" in blocker for blocker in audit["blockers"])
    assert any("array primitive" in blocker for blocker in audit["blockers"])


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
    panel._active_stage = {"task_id": "modeler", "effort": "medium"}
    panel._codex_event_summaries = ["CLI: turn started"]
    panel._codex_stderr = ["INFO: starting\n", "ERROR: request timed out upstream\n"]

    lines = panel._failure_lines("Generation timed out after 90s.", timed_out=True)

    assert any("Problem: Generation timed out after 90s." in line for line in lines)
    assert any("Stage: Draft model (medium)" in line for line in lines)
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


def test_stage_plan_uses_single_modeler_pass_by_default(panel):
    panel.worker_effort = DummyValue("medium")
    panel.planner_effort = DummyValue("xhigh")
    panel.iterations_spin = DummyValue(1)
    panel.mode_combo = DummyValue("new")

    stages = panel._build_stage_plan()

    assert [stage["task_id"] for stage in stages] == ["modeler"]
    assert [stage["effort"] for stage in stages] == ["medium"]


def test_stage_plan_uses_worker_then_high_then_xhigh(panel):
    panel.worker_effort = DummyValue("medium")
    panel.planner_effort = DummyValue("xhigh")
    panel.iterations_spin = DummyValue(3)
    panel.mode_combo = DummyValue("new")

    stages = panel._build_stage_plan()

    assert [stage["task_id"] for stage in stages] == ["modeler", "review_1", "review_2"]
    assert [stage["effort"] for stage in stages] == ["medium", "high", "xhigh"]


def test_review_stage_prompt_contains_audit_feedback(panel):
    panel.prompt = DummyValue("Create a symmetric bracket with a 2x2 bolt pattern.")
    panel.mode_combo = DummyValue("new")
    stage = {
        "task_id": "review_1",
        "kind": "review",
        "effort": "high",
        "review_index": 1,
        "review_total": 2,
    }
    script = """import cadquery as cq
parts = []
for x in (-20, -10, 0, 10, 20):
    parts.append(cq.Workplane("XY").box(5, 5, 5).translate((x, 0, 0)).val())
result = cq.Workplane("XY").add(cq.Compound.makeCompound(parts))
show_object(result)
"""

    prompt = panel._build_stage_prompt(stage, script)

    assert "Native score:" in prompt
    assert "Blockers:" in prompt
    assert "mirror" in prompt.lower()
