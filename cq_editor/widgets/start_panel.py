import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from time import perf_counter

from cadquery import cqgi
from logbook import info, warning
from PyQt5.QtCore import QProcess, QTimer, Qt, pyqtSlot
from PyQt5.QtWidgets import (
    QApplication,
    QWidget,
    QLabel,
    QPushButton,
    QCheckBox,
    QPlainTextEdit,
    QHBoxLayout,
    QComboBox,
    QProgressBar,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
)

from ..icons import icon
from ..mixins import ComponentMixin
from ..utils import layout


STARTER_SCRIPT = """import cadquery as cq

result = cq.Workplane("XY").box(20, 20, 10).edges("|Z").fillet(1.0)
show_object(result)
"""

NATIVE_OP_WEIGHTS = {
    "box": 1,
    "rect": 1,
    "circle": 1,
    "cylinder": 1,
    "sphere": 2,
    "extrude": 3,
    "revolve": 3,
    "loft": 3,
    "sweep": 3,
    "mirrorX": 4,
    "mirrorY": 4,
    "rarray": 4,
    "parray": 4,
    "hole": 3,
    "fillet": 2,
    "chamfer": 2,
    "cut": 2,
    "union": 2,
}

MANUAL_PATTERN_WEIGHTS = {
    "translate": (r"\.translate\(", 2),
    "rotate": (r"\.rotate\(", 1),
    "loop": (r"\bfor\b", 4),
    "append": (r"\.append\(", 3),
    "compound": (r"makeCompound\(", 4),
}


class StartPanel(QWidget, ComponentMixin):

    name = "Start"

    def __init__(self, parent):

        super(StartPanel, self).__init__(parent)
        ComponentMixin.__init__(self)

        self._main_window = parent
        self._codex_path = shutil.which("codex")
        self._codex_process = None
        self._codex_output_path = None
        self._codex_stdout = []
        self._codex_stdout_buffer = ""
        self._codex_stderr = []
        self._codex_usage = {}
        self._codex_event_summaries = []
        self._codex_login_ok = False
        self._codex_check_ok = None
        self._activity_started_at = None
        self._activity_kind = "Idle"
        self._last_traceback_text = ""
        self._last_prompt_text = ""
        self._codex_abort_reason = None
        self._pipeline_queue = []
        self._active_stage = None
        self._pipeline_script = ""
        self._pipeline_usage_totals = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
        }
        self._generation_round = 0
        self._active_history_item = None
        self._task_items = {}
        self._task_states = {}
        self._text_state = {}
        self._section_buttons = {}
        self._section_widgets = {}
        self._section_titles = {}

        self.title = QLabel("Start a CAD script")
        self.file_label = QLabel("Current file: Untitled")
        self.cli_label = QLabel("Codex CLI: checking...")
        self.hint_label = QLabel("Run `codex login` once in your terminal before using Generate.")
        self.check_label = QLabel("Check: not run yet")
        self.usage_label = QLabel("Usage: idle")
        self.status_label = QLabel("Status: Idle")
        self.detail_label = QLabel("Stage: waiting")
        self.failure_label = QLabel("")
        self.elapsed_label = QLabel("Elapsed: 0.0s")
        self.progress = QProgressBar(self)
        self.progress.setTextVisible(True)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.mode_combo = QComboBox(self)
        self.mode_combo.addItem("New", "new")
        self.mode_combo.addItem("Refine", "refine")
        self.mode_combo.addItem("Repair", "repair")
        self.iterations_spin = QSpinBox(self)
        self.iterations_spin.setRange(1, 3)
        self.iterations_spin.setValue(1)
        self.iterations_spin.setToolTip(
            "Total passes. 1 = fastest single draft, 2-3 enable staged review."
        )
        self.lean_tokens = QCheckBox("Lean tokens", self)
        self.lean_tokens.setChecked(True)
        self.translate_button = QPushButton("中文", self)
        self.translate_button.setCheckable(True)
        self.planner_effort = QComboBox(self)
        self.worker_effort = QComboBox(self)
        self.planner_effort.addItems(["xhigh", "high", "medium"])
        self.worker_effort.addItems(["high", "medium", "low"])
        self.planner_effort.setCurrentText("high")
        self.worker_effort.setCurrentText("medium")
        self.controls_widget = QWidget(self)
        controls_row = QHBoxLayout(self.controls_widget)
        controls_row.setContentsMargins(0, 0, 0, 0)
        self.mode_label = QLabel("Mode", self.controls_widget)
        self.iterations_label = QLabel("Iterations", self.controls_widget)
        controls_row.addWidget(self.mode_label)
        controls_row.addWidget(self.mode_combo)
        controls_row.addWidget(self.iterations_label)
        controls_row.addWidget(self.iterations_spin)
        controls_row.addStretch(1)
        controls_row.addWidget(self.lean_tokens)
        controls_row.addWidget(self.translate_button)
        self.strategy_widget = QWidget(self)
        strategy_row = QHBoxLayout(self.strategy_widget)
        strategy_row.setContentsMargins(0, 0, 0, 0)
        self.planner_label = QLabel("Planner", self.strategy_widget)
        self.worker_label = QLabel("Workers", self.strategy_widget)
        strategy_row.addWidget(self.planner_label)
        strategy_row.addWidget(self.planner_effort)
        strategy_row.addWidget(self.worker_label)
        strategy_row.addWidget(self.worker_effort)
        self.task_tree = QTreeWidget(self)
        self.task_tree.setHeaderHidden(True)
        self.task_tree.setRootIsDecorated(False)
        self.task_tree.setItemsExpandable(True)
        self.task_tree.setMinimumHeight(90)
        self.task_tree.setMaximumHeight(150)
        self.history_tree = QTreeWidget(self)
        self.history_tree.setHeaderHidden(True)
        self.history_tree.setRootIsDecorated(False)
        self.history_tree.setItemsExpandable(True)
        self.history_tree.setMinimumHeight(90)
        self.history_tree.setMaximumHeight(170)
        self.prompt = QPlainTextEdit(self)
        self.prompt.setPlaceholderText(
            "Describe the part you want, for example: create a mounting plate with four corner holes."
        )
        self.prompt.setFixedHeight(80)
        self.hint_label.setWordWrap(True)
        self.detail_label.setWordWrap(True)
        self.failure_label.setWordWrap(True)
        self.failure_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.failure_label.setStyleSheet("QLabel { color: #cc3b3b; }")
        self.failure_label.hide()

        self.workflow_widget = QWidget(self)
        layout(
            self.workflow_widget,
            (self.strategy_widget, self.task_tree),
            top_widget=self.workflow_widget,
            spacing=2,
        )
        self.history_widget = QWidget(self)
        layout(
            self.history_widget,
            (self.history_tree,),
            top_widget=self.history_widget,
            spacing=2,
        )
        self.workflow_button = self._register_section(
            "workflow", "Workflow", self.workflow_widget, expanded=False
        )
        self.history_button = self._register_section(
            "history", "Rounds", self.history_widget, expanded=False
        )

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(250)
        self._status_timer.timeout.connect(self._update_elapsed)
        self._codex_timeout_timer = QTimer(self)
        self._codex_timeout_timer.setSingleShot(True)
        self._codex_timeout_timer.timeout.connect(self._handle_codex_timeout)

        self.new_button = QPushButton(icon("new"), "New Script", self)
        self.open_button = QPushButton(icon("open"), "Open Script", self)
        self.render_button = QPushButton(icon("run"), "Render", self)
        self.refresh_button = QPushButton("Refresh Codex", self)
        self.generate_button = QPushButton(icon("run"), "Generate With Codex", self)
        self.stop_button = QPushButton("Stop", self)
        self.stop_button.setEnabled(False)

        row = QHBoxLayout()
        row.addWidget(self.new_button)
        row.addWidget(self.open_button)
        row.addWidget(self.render_button)

        codex_row = QHBoxLayout()
        codex_row.addWidget(self.refresh_button)
        codex_row.addWidget(self.generate_button)
        codex_row.addWidget(self.stop_button)

        layout(
            self,
            (
                self.title,
                self.file_label,
                self.cli_label,
                self.hint_label,
                self.check_label,
                self.controls_widget,
                self.strategy_widget,
                self.usage_label,
                self.status_label,
                self.detail_label,
                self.failure_label,
                self.elapsed_label,
                self.progress,
                self.workflow_button,
                self.workflow_widget,
                self.history_button,
                self.history_widget,
                self.prompt,
            ),
            top_widget=self,
        )
        self.layout().addLayout(row)
        self.layout().addLayout(codex_row)

        self.new_button.clicked.connect(self.new_script)
        self.open_button.clicked.connect(self.open_script)
        self.render_button.clicked.connect(parent.components["debugger"].render)
        self.refresh_button.clicked.connect(lambda: self.refresh_cli_status(run_smoke=True))
        self.generate_button.clicked.connect(self.generate_with_codex)
        self.stop_button.clicked.connect(self.stop_codex_generation)
        parent.components["editor"].sigFilenameChanged.connect(self.update_filename)
        parent.components["debugger"].sigRenderState.connect(self.handle_render_state)
        parent.components["debugger"].sigTraceback.connect(self.handle_traceback)
        self.translate_button.toggled.connect(self._refresh_language)
        self.mode_combo.currentIndexChanged.connect(self._rebuild_task_graph)
        self.iterations_spin.valueChanged.connect(self._rebuild_task_graph)
        self.planner_effort.currentTextChanged.connect(self._rebuild_task_graph)
        self.worker_effort.currentTextChanged.connect(self._rebuild_task_graph)

        self._set_text("hint_label", self.hint_label, self.hint_label.text())
        self._set_text("check_label", self.check_label, self.check_label.text())
        self._set_text("usage_label", self.usage_label, self.usage_label.text())
        self._set_text("status_label", self.status_label, self.status_label.text())
        self._set_text("detail_label", self.detail_label, self.detail_label.text())
        self._set_text("elapsed_label", self.elapsed_label, self.elapsed_label.text())
        self.update_filename(parent.components["editor"].filename)
        self.refresh_cli_status()
        self._refresh_language()
        self._rebuild_task_graph()

    def _is_zh(self):

        return self.translate_button.isChecked()

    def _translate_text(self, text):

        if not self._is_zh():
            return text

        replacements = (
            ("Start a CAD script", "開始 CAD 腳本"),
            ("Current file:", "目前檔案:"),
            ("Codex CLI:", "Codex CLI:"),
            ("Run `codex login` once in your terminal before using Generate.", "使用 Generate 前，請先在終端機執行一次 `codex login`。"),
            ("Usage:", "Token:"),
            ("Check:", "檢查:"),
            ("Status:", "狀態:"),
            ("Stage:", "階段:"),
            ("Problem:", "問題:"),
            ("Next:", "下一步:"),
            ("CLI tail:", "CLI 尾端:"),
            ("Context:", "上下文:"),
            ("Elapsed:", "已耗時:"),
            ("Idle", "待命"),
            ("waiting", "等待中"),
            ("Rendering", "渲染中"),
            ("Generating with Codex", "Codex 生成中"),
            ("Done", "完成"),
            ("failed", "失敗"),
            ("Falling back to full render", "回退到全量渲染"),
            ("building node graph", "建立節點圖"),
            ("executing script", "執行腳本"),
            ("updated", "更新"),
            ("reused", "重用"),
            ("viewer", "檢視器"),
            ("script inserted into editor", "腳本已放入編輯器"),
            ("Round", "輪次"),
            ("Prompt:", "提示:"),
            ("Strategy:", "策略:"),
            ("Result:", "結果:"),
            ("Running", "執行中"),
            ("Ready", "完成"),
            ("Failed", "失敗"),
            ("Repair", "修復"),
            ("Refine", "細化"),
            ("New", "新建"),
            ("Mode", "模式"),
            ("Iterations", "迭代"),
            ("Planner", "規劃"),
            ("Workers", "執行"),
            ("Workflow", "工作流"),
            ("Show", "顯示"),
            ("Hide", "收起"),
            ("Lean tokens", "節省 Token"),
            ("Task graph", "任務拓撲"),
            ("Rounds", "輪次"),
            ("Supervisor", "監督者"),
            ("Draft model", "建模"),
            ("Review", "審查"),
            ("CQ render", "CQ 渲染"),
            ("Repair path", "修錯路徑"),
            ("New Script", "新建腳本"),
            ("Open Script", "開啟腳本"),
            ("Render", "渲染"),
            ("Refresh Codex", "刷新 Codex"),
            ("Generate With Codex", "用 Codex 生成"),
            ("Stop", "停止"),
            ("Codex stopped", "Codex 已停止"),
            ("Codex timed out", "Codex 已逾時"),
            ("No active stage", "目前沒有執行中的階段"),
            ("CLI: thread started", "CLI: 執行緒已建立"),
            ("CLI: turn started", "CLI: 回合已開始"),
            ("CLI: stage completed", "CLI: 階段已完成"),
            ("CLI: agent message received", "CLI: 已收到代理回覆"),
            ("not run yet", "尚未檢查"),
            ("login ok", "登入正常"),
            ("login failed", "登入失敗"),
            ("exec ok", "執行正常"),
            ("exec failed", "執行失敗"),
            ("exec timeout", "執行逾時"),
            ("Generation timed out", "生成逾時"),
            ("Codex stopped by user.", "使用者已停止 Codex。"),
            ("after", "後"),
            ("Native ops:", "原生操作:"),
            ("Native score:", "原生比例:"),
            ("Validation:", "驗證:"),
            ("passed", "通過"),
            ("blocked", "攔截"),
            ("Build failed:", "建模失敗:"),
            ("Warnings:", "警告:"),
            ("PLAN header missing", "缺少 PLAN 標頭"),
            ("idle", "待命"),
            ("running", "執行中"),
            ("waiting", "等待中"),
            ("done", "完成"),
            ("error", "錯誤"),
        )

        translated = text
        for source, target in replacements:
            translated = translated.replace(source, target)
        return translated

    def _set_text(self, key, widget, text):

        self._text_state[key] = text
        widget.setText(self._translate_text(text))

    def _clear_failure(self):

        self._text_state["failure_label"] = ""
        self.failure_label.hide()
        self.failure_label.setText("")

    def _active_stage_text(self):

        if self._active_stage is None:
            return "No active stage"
        return self._stage_title(self._active_stage)

    def _workspace_root(self):

        starts = []
        editor = self._main_window.components.get("editor")
        if editor is not None and getattr(editor, "filename", ""):
            starts.append(Path(editor.filename).resolve().parent)
        starts.append(Path.cwd())

        seen = set()
        for start in starts:
            for candidate in (start, *start.parents):
                if candidate in seen:
                    continue
                seen.add(candidate)
                if (candidate / ".git").exists():
                    return candidate
        return starts[0]

    def _context_paths(self):

        workspace_root = self._workspace_root()
        candidates = [
            workspace_root / "AGENTS.md",
            Path.home() / ".codex" / "AGENTS.md",
        ]
        memory_dir = Path.home() / ".codex" / "memories"
        if memory_dir.exists():
            candidates.extend(sorted(memory_dir.glob("*.md"))[:3])

        paths = []
        seen = set()
        for path in candidates:
            if path in seen or not path.exists() or path.stat().st_size == 0:
                continue
            seen.add(path)
            paths.append(path)
        return paths

    def _context_summary(self):

        paths = self._context_paths()
        return ", ".join(path.name for path in paths) if paths else "none"

    def _load_context_blocks(self, max_chars=4000):

        blocks = []
        remaining = max_chars
        for path in self._context_paths():
            if remaining <= 0:
                break
            text = path.read_text(encoding="utf-8", errors="ignore").strip()
            if not text:
                continue
            excerpt = text[:remaining].strip()
            blocks.append(f"[{path.name}]\n{excerpt}")
            remaining -= len(excerpt)
        return "\n\n".join(blocks)

    def _process_tail(self, limit=3):

        lines = list(self._codex_event_summaries[-limit:])
        stderr_lines = [line.strip() for line in "".join(self._codex_stderr).splitlines() if line.strip()]
        filtered = [
            line
            for line in stderr_lines
            if "shell_snapshot" not in line and "plugins::manager" not in line
        ]
        lines.extend(filtered[-limit:])
        return lines[-limit:]

    def _failure_lines(self, detail, timed_out=False, stopped=False, stage_text=None):

        check_text = self._text_state.get("check_label", "Check: not run yet")
        lines = [
            f"Problem: {detail}",
            f"Stage: {stage_text or self._active_stage_text()}",
            f"Context: {self._context_summary()}",
            check_text,
        ]
        if timed_out:
            lines.append(
                "Next: Try Iterations = 1 first, or lower Workers/Planner effort before retrying."
            )
        elif stopped:
            lines.append("Next: Retry when you are ready. No new script was applied.")
        elif "output file" in detail.lower() or "empty script" in detail.lower():
            lines.append("Next: Retry once with a shorter prompt or Lean tokens enabled.")
        elif "login" in detail.lower():
            lines.append("Next: Refresh Codex login before retrying.")

        tail = self._process_tail()
        if tail:
            lines.append(f"CLI tail: {' | '.join(tail)}")
        return lines

    def _show_failure(self, detail, timed_out=False, stopped=False, stage_text=None):

        lines = self._failure_lines(
            detail, timed_out=timed_out, stopped=stopped, stage_text=stage_text
        )
        text = "\n".join(lines)
        self._text_state["failure_label"] = text
        self.failure_label.setText(self._translate_text(text))
        self.failure_label.show()

    def _record_cli_event(self, summary):

        if not summary:
            return
        if not self._codex_event_summaries or self._codex_event_summaries[-1] != summary:
            self._codex_event_summaries.append(summary)
            self._codex_event_summaries = self._codex_event_summaries[-8:]
            self._add_history_line(summary)

        if self._active_stage is not None and "completed" not in summary.lower():
            self._set_text(
                "detail_label",
                self.detail_label,
                f"Stage: {self._stage_title(self._active_stage)} · {summary.replace('CLI: ', '')}",
            )

    def _handle_codex_event(self, event):

        event_type = event.get("type")
        if event_type == "thread.started":
            self._record_cli_event("CLI: thread started")
            return
        if event_type == "turn.started":
            self._record_cli_event("CLI: turn started")
            return
        if event_type == "turn.completed":
            self._record_cli_event("CLI: stage completed")
            return
        if event_type == "item.completed":
            item = event.get("item", {}) or {}
            if item.get("type") == "agent_message":
                self._record_cli_event("CLI: agent message received")

    def _section_button_text(self, title, expanded):

        verb = "Hide" if expanded else "Show"
        return self._translate_text(f"{verb} {title}")

    def _register_section(self, key, title, widget, expanded=False):

        button = QPushButton(self._section_button_text(title, expanded), self)
        button.setCheckable(True)
        button.toggled.connect(
            lambda visible, section_key=key: self._set_section_visible(
                section_key, visible
            )
        )
        self._section_titles[key] = title
        self._section_buttons[key] = button
        self._section_widgets[key] = widget
        self._set_section_visible(key, expanded, sync_button=False)
        button.setChecked(expanded)
        return button

    def _set_section_visible(self, key, visible, sync_button=True):

        widget = self._section_widgets.get(key)
        button = self._section_buttons.get(key)
        if widget is None or button is None:
            return

        widget.setVisible(visible)
        if sync_button:
            button.blockSignals(True)
            button.setChecked(visible)
            button.blockSignals(False)
        button.setText(self._section_button_text(self._section_titles[key], visible))

    def _refresh_language(self):

        self.title.setText(self._translate_text("Start a CAD script"))
        self.mode_label.setText(self._translate_text("Mode"))
        self.iterations_label.setText(self._translate_text("Iterations"))
        self.planner_label.setText(self._translate_text("Planner"))
        self.worker_label.setText(self._translate_text("Workers"))
        self.lean_tokens.setText(self._translate_text("Lean tokens"))
        self.translate_button.setText("EN" if self._is_zh() else "中文")
        self.new_button.setText(self._translate_text("New Script"))
        self.open_button.setText(self._translate_text("Open Script"))
        self.render_button.setText(self._translate_text("Render"))
        self.refresh_button.setText(self._translate_text("Refresh Codex"))
        self.generate_button.setText(self._translate_text("Generate With Codex"))
        self.stop_button.setText(self._translate_text("Stop"))
        self.prompt.setPlaceholderText(
            self._translate_text(
                "Describe the part you want, for example: create a mounting plate with four corner holes."
            )
        )

        mode_labels = ("New", "Refine", "Repair")
        current_mode = self.mode_combo.currentData()
        self.mode_combo.blockSignals(True)
        for index, label in enumerate(mode_labels):
            self.mode_combo.setItemText(index, self._translate_text(label))
        self.mode_combo.setCurrentIndex(max(self.mode_combo.findData(current_mode), 0))
        self.mode_combo.blockSignals(False)

        for key, widget in (
            ("file_label", self.file_label),
            ("cli_label", self.cli_label),
            ("hint_label", self.hint_label),
            ("check_label", self.check_label),
            ("usage_label", self.usage_label),
            ("status_label", self.status_label),
            ("detail_label", self.detail_label),
            ("elapsed_label", self.elapsed_label),
        ):
            if key in self._text_state:
                widget.setText(self._translate_text(self._text_state[key]))

        if self._text_state.get("failure_label"):
            self.failure_label.setText(
                self._translate_text(self._text_state["failure_label"])
            )

        for section_key, button in self._section_buttons.items():
            title = self._section_titles[section_key]
            expanded = self._section_widgets[section_key].isVisible()
            button.setText(self._section_button_text(title, expanded))

        self._rebuild_task_graph()

    def _history_text(self, text):

        return self._translate_text(text)

    def _add_history_line(self, text):

        if self._active_history_item is None:
            return

        self._active_history_item.addChild(QTreeWidgetItem([self._history_text(text)]))

    def _compact_script_context(self, script):

        if not script or not self.lean_tokens.isChecked():
            return script

        kept = []
        previous_blank = False
        for raw_line in script.splitlines():
            line = raw_line.rstrip()
            stripped = line.strip()
            if stripped.startswith("#") and not stripped.startswith(
                ("# PLAN:", "# NATIVE_OPS:", "# CHECKS:")
            ):
                continue
            if not stripped:
                if previous_blank:
                    continue
                previous_blank = True
            else:
                previous_blank = False
            kept.append(line)

        return "\n".join(kept).strip()

    def _extract_script_header(self, script):

        header = []
        for line in script.splitlines():
            stripped = line.strip()
            if stripped.startswith("# PLAN:") or stripped.startswith("# NATIVE_OPS:") or stripped.startswith("# CHECKS:"):
                header.append(stripped[2:].strip())
            if len(header) == 3:
                break
        return header

    @pyqtSlot(str)
    def update_filename(self, fname):

        current = fname if fname else "Untitled"
        self._set_text("file_label", self.file_label, f"Current file: {current}")

    def new_script(self):

        editor = self._main_window.components["editor"]
        if not editor.confirm_discard():
            return

        editor.filename = ""
        editor.set_text(STARTER_SCRIPT)

    def open_script(self):

        self._main_window.components["editor"].open()

    def _smoke_check_timeout_s(self):

        return 8 if self.lean_tokens.isChecked() else 12

    def _run_codex_smoke_check(self):

        fd, output_path = tempfile.mkstemp(prefix="cq_codex_check_", suffix=".txt")
        os.close(fd)
        start = perf_counter()
        try:
            result = subprocess.run(
                [
                    self._codex_path,
                    "exec",
                    "--skip-git-repo-check",
                    "--ephemeral",
                    "--json",
                    "--color",
                    "never",
                    "-C",
                    str(self._workspace_root()),
                    "-o",
                    output_path,
                ],
                input="Reply with one word: ok.\n",
                capture_output=True,
                text=True,
                timeout=self._smoke_check_timeout_s(),
            )
            elapsed = perf_counter() - start
            message = (result.stderr or result.stdout).strip()
            output_text = (
                Path(output_path).read_text(encoding="utf-8", errors="ignore").strip()
                if Path(output_path).exists()
                else ""
            )
            if result.returncode == 0 and output_text:
                return True, f"exec ok ({elapsed:.1f}s)"
            summary = self._summarize_codex_error(message)
            return False, f"exec failed ({elapsed:.1f}s): {summary}"
        except subprocess.TimeoutExpired:
            elapsed = perf_counter() - start
            return False, f"exec timeout ({elapsed:.1f}s)"
        except Exception as exc:
            return False, f"exec failed: {exc}"
        finally:
            if Path(output_path).exists():
                Path(output_path).unlink()

    def refresh_cli_status(self, *_args, run_smoke=False):

        if not self._codex_path:
            self._set_text("cli_label", self.cli_label, "Codex CLI: not found")
            self._set_text("check_label", self.check_label, "Check: login failed")
            self._codex_login_ok = False
            self._codex_check_ok = False
            self.generate_button.setEnabled(False)
            self.hint_label.setVisible(True)
            return

        try:
            result = subprocess.run(
                [self._codex_path, "login", "status"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            message = (result.stdout or result.stderr).strip() or "status unavailable"
            self._set_text("cli_label", self.cli_label, f"Codex CLI: {message}")
            self._codex_login_ok = result.returncode == 0
            if not self._codex_login_ok:
                self._set_text("check_label", self.check_label, "Check: login failed")
                self._codex_check_ok = False
                self.generate_button.setEnabled(False)
                self.hint_label.setVisible(True)
                return

            self._set_text("check_label", self.check_label, "Check: login ok")
            self.generate_button.setEnabled(True)
            self.hint_label.setVisible(False)
            if run_smoke:
                ok, summary = self._run_codex_smoke_check()
                self._codex_check_ok = ok
                self._set_text("check_label", self.check_label, f"Check: login ok · {summary}")
                if not ok:
                    self._set_text(
                        "hint_label",
                        self.hint_label,
                        "Codex preflight failed. Generate may still work, but try Iterations = 1 or lower effort first.",
                    )
                    self.hint_label.setVisible(True)
        except Exception as exc:
            self._set_text("cli_label", self.cli_label, f"Codex CLI: {exc}")
            self._set_text("check_label", self.check_label, f"Check: login failed")
            self._codex_login_ok = False
            self._codex_check_ok = False
            self.generate_button.setEnabled(False)
            self.hint_label.setVisible(True)

    def _build_codex_prompt(self):

        request = self.prompt.toPlainText().strip()
        current_script = self._compact_script_context(
            self._main_window.components["editor"].toPlainText().strip()
        )
        worker_effort = self.worker_effort.currentText()
        mode = self.mode_combo.currentData()
        iterations = self.iterations_spin.value()
        prompt_lines = [
            "Write a single CadQuery Python script for CQ-editor.",
            "Return only raw Python code with no markdown fences or explanation.",
            "Use import cadquery as cq.",
            "Assign the final shape to result and call show_object(result).",
            "Do deep planning first, then keep code edits compact and incremental.",
            f"If you internally split work into smaller passes, keep worker effort at {worker_effort} or lower.",
            f"Internally review and refine up to {iterations} pass(es) before finalizing.",
            "Prefer native CadQuery operations where applicable: extrude, revolve, loft, sweep, union, cut, hole, fillet, chamfer, mirrorX, mirrorY, rarray, parray.",
            "Avoid manually recreating repeated or symmetric geometry when a native mirror or pattern tool fits.",
            "At least 70% of repeated, symmetric, or patterned geometry must rely on native CadQuery feature operations instead of one-by-one placement.",
            "If symmetry is requested, use mirrorX or mirrorY unless there is a concrete reason not to.",
            "If a hole pattern or repeated feature is requested, use rarray or parray unless there is a concrete reason not to.",
            "Add a compact comment header with PLAN:, NATIVE_OPS:, and CHECKS: before the modeling code.",
            "In CHECKS, summarize proportion, symmetry, and front/side/top sanity checks without exposing hidden reasoning.",
        ]

        if self.lean_tokens.isChecked():
            prompt_lines.extend(
                (
                    "Keep the script compact. Do not emit alternative versions or long comments.",
                    "Prefer the smallest viable change instead of rewriting unrelated parts.",
                )
            )

        if mode == "new":
            prompt_lines.append("Create a new model from scratch.")
        elif mode == "refine":
            prompt_lines.append(
                "Refine the current CadQuery script instead of rewriting it from scratch."
            )
        else:
            prompt_lines.append(
                "Repair the current CadQuery script with the smallest viable changes."
            )

        prompt_lines.extend((f"User request: {request}", ""))

        if mode in ("refine", "repair"):
            prompt_lines.extend(
                ("Current script for context:", current_script or "# none", "")
            )

        if mode == "repair" and self._last_traceback_text:
            prompt_lines.extend(("Current error to fix:", self._last_traceback_text))

        return "\n".join(prompt_lines)

    def _set_generating(self, running):

        self.generate_button.setEnabled(not running and bool(self._codex_path) and self._codex_login_ok)
        self.refresh_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.new_button.setEnabled(not running)
        self.open_button.setEnabled(not running)
        self.mode_combo.setEnabled(not running)
        self.iterations_spin.setEnabled(not running)
        self.planner_effort.setEnabled(not running)
        self.worker_effort.setEnabled(not running)
        self.lean_tokens.setEnabled(not running)
        self.translate_button.setEnabled(not running)

    def _codex_timeout_ms(self):

        base = 120000 if self.lean_tokens.isChecked() else 180000
        stage = self._active_stage or {}
        effort = stage.get("effort")
        if effort == "high":
            base += 45000
        elif effort == "xhigh":
            base += 90000
        return base

    def _restart_codex_timeout(self):

        if self._codex_process is None:
            return
        self._codex_timeout_timer.start(self._codex_timeout_ms())

    def _abort_codex_generation(self, reason, kind="user"):

        if self._codex_process is None:
            return

        self._codex_abort_reason = {"kind": kind, "detail": reason}
        self._codex_timeout_timer.stop()
        if self._codex_process.state() != QProcess.NotRunning:
            self._codex_process.terminate()
            if not self._codex_process.waitForFinished(1000):
                self._codex_process.kill()

    def _handle_codex_timeout(self):

        self._abort_codex_generation(
            f"Generation timed out after {self._codex_timeout_ms() / 1000:.0f}s.",
            kind="timeout",
        )

    def stop_codex_generation(self):

        self._abort_codex_generation("Codex stopped by user.", kind="user")

    def _begin_activity(self, kind, status, detail, busy=False, total=1, value=0):

        self._activity_kind = kind
        self._activity_started_at = perf_counter()
        self._clear_failure()
        self._set_text("status_label", self.status_label, f"Status: {status}")
        self._set_text("detail_label", self.detail_label, f"Stage: {detail}")
        self.progress.setRange(0, 0 if busy else max(int(total), 1))
        if not busy:
            self.progress.setValue(min(max(int(value), 0), self.progress.maximum()))
        self._update_elapsed()
        self._status_timer.start()

    def _finish_activity(self, status, detail, total=1, value=1):

        self._set_text("status_label", self.status_label, f"Status: {status}")
        self._set_text("detail_label", self.detail_label, f"Stage: {detail}")
        self.progress.setRange(0, max(int(total), 1))
        self.progress.setValue(min(max(int(value), 0), self.progress.maximum()))
        self._update_elapsed()
        self._status_timer.stop()

    def _update_elapsed(self):

        if self._activity_started_at is None:
            self._set_text("elapsed_label", self.elapsed_label, "Elapsed: 0.0s")
            return

        elapsed = perf_counter() - self._activity_started_at
        self._set_text("elapsed_label", self.elapsed_label, f"Elapsed: {elapsed:.1f}s")

    def _estimate_tokens(self, text):

        return max(1, len(text.encode("utf-8")) // 4)

    def _format_token_count(self, value):

        if value >= 1000:
            return f"{value / 1000:.1f}k"
        return str(value)

    def _set_usage_summary(self, input_tokens=None, cached_tokens=None, output_tokens=None):

        if input_tokens is None and output_tokens is None:
            self._set_text("usage_label", self.usage_label, "Usage: unavailable")
            return

        pieces = []
        if input_tokens is not None:
            pieces.append(f"in {self._format_token_count(int(input_tokens))}")
        if cached_tokens:
            pieces.append(f"cached {self._format_token_count(int(cached_tokens))}")
        if output_tokens is not None:
            pieces.append(f"out {self._format_token_count(int(output_tokens))}")
        self._set_text("usage_label", self.usage_label, f"Usage: {' | '.join(pieces)}")

    def _accumulate_usage(self, usage):

        for key in self._pipeline_usage_totals:
            self._pipeline_usage_totals[key] += int(usage.get(key, 0) or 0)

        self._set_usage_summary(
            input_tokens=self._pipeline_usage_totals["input_tokens"],
            cached_tokens=self._pipeline_usage_totals["cached_input_tokens"],
            output_tokens=self._pipeline_usage_totals["output_tokens"],
        )

    def _extract_usage_from_stdout(self):

        usage = {}
        for line in "".join(self._codex_stdout).splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "turn.completed":
                usage = event.get("usage", {}) or {}

        self._codex_usage = usage
        return usage

    def _history_title(self, status):

        return (
            f"Round {self._generation_round} · {self.mode_combo.itemText(self.mode_combo.currentIndex())} "
            f"· x{self.iterations_spin.value()} · {self._translate_text(status)}"
        )

    def _history_prompt_summary(self, prompt):

        line = " ".join(prompt.split())
        if len(line) <= 80:
            return line
        return line[:77] + "..."

    def _start_history_entry(self):

        self._generation_round += 1
        prompt_text = self.prompt.toPlainText().strip()
        self._set_section_visible("history", True)
        item = QTreeWidgetItem([self._history_title("Running")])
        item.addChild(
            QTreeWidgetItem([self._history_text(f"Prompt: {self._history_prompt_summary(prompt_text)}")])
        )
        item.addChild(
            QTreeWidgetItem(
                [
                    self._history_text(
                        f"Strategy: planner {self.planner_effort.currentText()} · "
                        f"workers {self.worker_effort.currentText()}"
                    )
                ]
            )
        )
        self.history_tree.insertTopLevelItem(0, item)
        item.setExpanded(True)
        self._active_history_item = item

    def _finish_history_entry(self, status, detail, expand=False):

        if self._active_history_item is None:
            return

        self._active_history_item.setText(0, self._history_title(status))
        self._add_history_line(f"Result: {detail}")
        if self.usage_label.text():
            self._add_history_line(self.usage_label.text())
        self._active_history_item.setExpanded(expand)
        self._active_history_item = None

    def _inspect_script(self, script, prompt_text=None):

        ops = []
        for name in (
            "extrude",
            "revolve",
            "loft",
            "sweep",
            "sphere",
            "mirrorX",
            "mirrorY",
            "rarray",
            "parray",
            "hole",
            "fillet",
            "chamfer",
            "cut",
            "union",
        ):
            count = len(re.findall(rf"\.{name}\(", script))
            if count:
                ops.append(f"{name}x{count}")

        warnings = []
        prompt_text = prompt_text if prompt_text is not None else self._last_prompt_text
        translate_count = len(re.findall(r"\.translate\(", script))
        if translate_count >= 4 and not any(op.startswith(prefix) for op in ops for prefix in ("mirror", "rarray", "parray")):
            warnings.append("Repeated placements detected; consider mirror/array primitives.")
        if "for " in script and not any(op.startswith(prefix) for op in ops for prefix in ("rarray", "parray")):
            warnings.append("Loop-based geometry found; verify native CQ pattern tools are used where possible.")
        if re.search(r"(symmetric|symmetry|mirror|mirrored|對稱|鏡像)", prompt_text, re.IGNORECASE) and not any(
            op.startswith("mirror") for op in ops
        ):
            warnings.append("Prompt requested symmetry; verify mirror primitives are used where suitable.")
        if not self._extract_script_header(script):
            warnings.append("PLAN header missing; ask Codex to emit PLAN/NATIVE_OPS/CHECKS.")

        return ops, warnings

    def _intent_requirements(self, prompt_text):

        prompt_text = prompt_text or ""
        lowered = prompt_text.lower()

        return {
            "mirror": bool(
                re.search(r"(symmetric|symmetry|mirror|mirrored|對稱|鏡像)", lowered)
            ),
            "pattern": bool(
                re.search(
                    r"(pattern|array|2x2|4 holes|four holes|bolt pattern|陣列|四個孔|4個孔|孔位)",
                    lowered,
                )
            ),
        }

    def _audit_script(self, script, prompt_text=None):

        prompt_text = prompt_text if prompt_text is not None else self._last_prompt_text
        op_counts = {}
        weighted_native = 0
        for name, weight in NATIVE_OP_WEIGHTS.items():
            count = len(re.findall(rf"\.{name}\(", script))
            op_counts[name] = count
            weighted_native += count * weight

        manual_counts = {}
        weighted_manual = 0
        for name, (pattern, weight) in MANUAL_PATTERN_WEIGHTS.items():
            count = len(re.findall(pattern, script))
            manual_counts[name] = count
            weighted_manual += count * weight

        denominator = weighted_native + weighted_manual
        native_score = int(round((100 * weighted_native / denominator), 0)) if denominator else 0
        requirements = self._intent_requirements(prompt_text)
        ops, warnings = self._inspect_script(script, prompt_text=prompt_text)
        blockers = []
        strict_mode = self._review_stage_total() > 0
        enforce_native_gate = (
            requirements["mirror"] or requirements["pattern"] or weighted_manual >= 6
        )

        if requirements["mirror"] and not any(op.startswith("mirror") for op in ops):
            blockers.append("Symmetry was requested but no mirror primitive was used.")
        if requirements["pattern"] and not any(op.startswith(prefix) for op in ops for prefix in ("rarray", "parray")):
            blockers.append("Patterned features were requested but no native array primitive was used.")
        if enforce_native_gate and native_score < 70:
            blockers.append(f"Native feature score {native_score}% is below the 70% gate.")
        if strict_mode and not self._extract_script_header(script):
            blockers.append("PLAN/NATIVE_OPS/CHECKS header is required.")

        if not strict_mode and blockers:
            warnings.extend(blockers)
            blockers = []

        return {
            "score": native_score,
            "ops": ops,
            "warnings": warnings,
            "blockers": blockers,
            "passed": not blockers,
            "op_counts": op_counts,
            "manual_counts": manual_counts,
        }

    def _validate_script_build(self, script):

        model = cqgi.parse(script)
        result = model.build()
        return result.success, "" if result.success else str(result.exception)

    def _planner_review_effort(self, index, total):

        planner_effort = self.planner_effort.currentText()
        if planner_effort == "xhigh" and total >= 2 and index < total:
            return "high"
        return planner_effort

    def _iterations_value(self):

        try:
            return int(self.iterations_spin.value())
        except RuntimeError:
            return 1

    def _review_stage_total(self):

        return max(self._iterations_value() - 1, 0)

    def _stage_title(self, stage):

        if stage["task_id"] == "modeler":
            return f"Draft model ({stage['effort']})"
        return f"Review {stage['review_index']} ({stage['effort']})"

    def _build_stage_plan(self):

        mode = self.mode_combo.currentData()
        stages = [
            {
                "task_id": "modeler",
                "kind": mode if mode in ("refine", "repair") else "draft",
                "effort": self.worker_effort.currentText(),
            }
        ]
        review_total = self._review_stage_total()
        for index in range(1, review_total + 1):
            stages.append(
                {
                    "task_id": f"review_{index}",
                    "kind": "review",
                    "effort": self._planner_review_effort(index, review_total),
                    "review_index": index,
                    "review_total": review_total,
                }
            )
        return stages

    def _build_stage_prompt(self, stage, script=""):

        request = self.prompt.toPlainText().strip()
        mode = self.mode_combo.currentData()
        context_block = self._load_context_blocks()

        if stage["task_id"] == "modeler":
            prompt_lines = [
                "You are the worker agent in a staged CAD pipeline.",
                "Write a single CadQuery Python script for CQ-editor.",
                "Return only raw Python code with no markdown fences or explanation.",
                "Use import cadquery as cq.",
                "Assign the final shape to result and call show_object(result).",
                "Use CadQuery native features first: extrude, revolve, loft, sweep, union, cut, hole, fillet, chamfer, mirrorX, mirrorY, rarray, parray.",
                "At least 70% of repeated, symmetric, or patterned geometry must use native CadQuery features rather than one-by-one placement.",
                "Add a compact comment header with PLAN:, NATIVE_OPS:, and CHECKS: before the modeling code.",
                "Keep the script compact. Do not include alternatives or long commentary.",
            ]
            if context_block:
                prompt_lines.extend(("Local agent context:", context_block, ""))
            if mode == "new":
                prompt_lines.append("Task: create the first working draft from scratch.")
            elif mode == "refine":
                prompt_lines.extend(
                    (
                        "Task: refine the current CadQuery script with the smallest viable changes.",
                        "",
                        "Current script:",
                        self._compact_script_context(
                            self._main_window.components["editor"].toPlainText().strip()
                        )
                        or "# none",
                    )
                )
            else:
                prompt_lines.extend(
                    (
                        "Task: repair the current CadQuery script with the smallest viable changes.",
                        "",
                        "Current script:",
                        self._compact_script_context(
                            self._main_window.components["editor"].toPlainText().strip()
                        )
                        or "# none",
                        "",
                        "Current error:",
                        self._last_traceback_text or "No traceback captured.",
                    )
                )
            prompt_lines.extend(("", f"User request: {request}"))
            return "\n".join(prompt_lines)

        audit = self._audit_script(script, prompt_text=request)
        prompt_lines = [
            "You are the review/refinement agent in a staged CAD pipeline.",
            "You must revise the supplied CadQuery script, not replace the design intent.",
            "Return only raw Python code with no markdown fences or explanation.",
            "Preserve import cadquery as cq, result, and show_object(result).",
            "Use the audit summary to fix weak spots while keeping edits compact.",
            "Prefer CadQuery native features over manual translate/loop/compound placement.",
            "If symmetry is requested, use mirrorX or mirrorY where suitable.",
            "If repeated holes or repeated features are requested, use rarray or parray where suitable.",
            "Keep or improve the PLAN:, NATIVE_OPS:, and CHECKS: header.",
            "",
            f"Stage: review {stage['review_index']} of {stage['review_total']}",
            f"User request: {request}",
            f"Native score: {audit['score']}%",
            "Warnings:",
        ]
        if context_block:
            prompt_lines[0:0] = ["Local agent context:", context_block, ""]
        prompt_lines.extend(f"- {message}" for message in audit["warnings"] or ["- none"])
        prompt_lines.append("Blockers:")
        prompt_lines.extend(f"- {message}" for message in audit["blockers"] or ["- none"])
        prompt_lines.extend(("", "Current script:", script or "# none"))
        return "\n".join(prompt_lines)

    def _cleanup_output_path(self):

        output_path = self._codex_output_path
        self._codex_output_path = None
        if output_path and Path(output_path).exists():
            Path(output_path).unlink()

    def _fail_pipeline(self, detail, history_detail=None, stopped=False, timed_out=False):

        self._codex_timeout_timer.stop()
        self._set_generating(False)
        self.refresh_cli_status()
        active_stage = self._active_stage
        stage_text = self._stage_title(active_stage) if active_stage is not None else "No active stage"
        self._cleanup_output_path()
        self._codex_process = None
        self._pipeline_queue = []
        self._set_task_status("supervisor", "error")
        if active_stage is not None:
            self._set_task_status(active_stage["task_id"], "error")
        self._set_task_status("repair", "waiting")
        self._active_stage = None
        self._set_section_visible("workflow", True)
        self._set_section_visible("history", True)
        failure_lines = self._failure_lines(
            detail, timed_out=timed_out, stopped=stopped, stage_text=stage_text
        )
        for line in failure_lines:
            self._add_history_line(line)
        self._show_failure(
            detail, timed_out=timed_out, stopped=stopped, stage_text=stage_text
        )
        status = "Codex timed out" if timed_out else ("Codex stopped" if stopped else "Codex failed")
        self._finish_activity(status, detail)
        self._finish_history_entry("Failed", history_detail or detail, expand=True)
        warning(detail)

    def _launch_stage(self, stage):

        fd, output_path = tempfile.mkstemp(prefix="cq_codex_", suffix=".py")
        os.close(fd)
        self._codex_output_path = output_path
        prompt_text = self._build_stage_prompt(stage, script=self._pipeline_script)
        process = QProcess(self)
        process.started.connect(process.closeWriteChannel)
        process.readyReadStandardOutput.connect(self._read_codex_stdout)
        process.readyReadStandardError.connect(self._read_codex_stderr)
        process.finished.connect(self._codex_finished)
        process.setProgram(self._codex_path)
        process.setArguments(
            [
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "--json",
                "--color",
                "never",
                "-c",
                f'reasoning_effort="{stage["effort"]}"',
                "-C",
                str(
                    Path(self._main_window.components["editor"].filename).resolve().parent
                    if self._main_window.components["editor"].filename
                    else Path.cwd()
                ),
                "-o",
                output_path,
                prompt_text,
            ]
        )

        self._active_stage = stage
        self._codex_process = process
        self._codex_stdout = []
        self._codex_stdout_buffer = ""
        self._codex_stderr = []
        self._codex_event_summaries = []
        self._codex_abort_reason = None
        self._set_task_status(stage["task_id"], "running")
        self._add_history_line(f"Stage: {self._stage_title(stage)}")
        self._begin_activity("Codex", "Generating with Codex", self._stage_title(stage), busy=True)
        self._set_text(
            "cli_label",
            self.cli_label,
            f"Codex CLI: generating {self._stage_title(stage)}...",
        )
        estimated = self._estimate_tokens(prompt_text)
        self._set_usage_summary(
            input_tokens=self._pipeline_usage_totals["input_tokens"] + estimated,
            cached_tokens=self._pipeline_usage_totals["cached_input_tokens"],
            output_tokens=self._pipeline_usage_totals["output_tokens"],
        )
        process.start()
        self._restart_codex_timeout()

    def _rebuild_task_graph(self, *_args):

        self.task_tree.clear()
        self._task_items = {}
        self._task_states = {}

        supervisor = QTreeWidgetItem(
            [self._translate_text(f"Supervisor ({self.planner_effort.currentText()}) · idle")]
        )
        self.task_tree.addTopLevelItem(supervisor)
        self._task_items["supervisor"] = supervisor
        self._task_states["supervisor"] = ("Supervisor", self.planner_effort.currentText(), "idle")

        modeler = QTreeWidgetItem(
            [self._translate_text(f"Draft model ({self.worker_effort.currentText()}) · waiting")]
        )
        supervisor.addChild(modeler)
        self._task_items["modeler"] = modeler
        self._task_states["modeler"] = ("Draft model", self.worker_effort.currentText(), "waiting")

        for index in range(1, self._review_stage_total() + 1):
            item = QTreeWidgetItem([self._translate_text(f"Review {index} ({self.planner_effort.currentText()}) · waiting")])
            supervisor.addChild(item)
            self._task_items[f"review_{index}"] = item
            self._task_states[f"review_{index}"] = ("Review {}".format(index), self.planner_effort.currentText(), "waiting")

        renderer = QTreeWidgetItem([self._translate_text("CQ render · waiting")])
        repair = QTreeWidgetItem([self._translate_text("Repair path · idle")])
        supervisor.addChild(renderer)
        supervisor.addChild(repair)
        self._task_items["renderer"] = renderer
        self._task_items["repair"] = repair
        self._task_states["renderer"] = ("CQ render", "", "waiting")
        self._task_states["repair"] = ("Repair path", "", "idle")
        supervisor.setExpanded(True)

    def _set_task_status(self, task_id, status):

        item = self._task_items.get(task_id)
        state = self._task_states.get(task_id)
        if item is None or state is None:
            return

        name, effort, _old_status = state
        suffix = f" ({effort})" if effort else ""
        item.setText(0, self._translate_text(f"{name}{suffix} · {status}"))
        self._task_states[task_id] = (name, effort, status)

    def _set_review_states(self, status):

        for index in range(1, self._review_stage_total() + 1):
            self._set_task_status(f"review_{index}", status)

    def _detect_level(self, state):

        haystack = " ".join(
            str(state.get(key, "")) for key in ("output_ref", "code_preview", "message")
        )
        match = re.search(r"(?:level|candidate|spec|evaluation)[_ =\"]*(\d+)", haystack)
        if match:
            return f"level {match.group(1)}"
        return None

    def _render_stage_text(self, state):

        level = self._detect_level(state)
        action = "reused" if state.get("cache_status") == "hit" else "recomputed"
        step_text = f"step {state.get('index', 0)}/{state.get('total_nodes', 0)}"
        line_start = state.get("line_start")
        line_end = state.get("line_end")
        line_text = (
            f"lines {line_start}-{line_end}"
            if line_start is not None and line_end is not None
            else state.get("semantic_type", "working")
        )
        if level:
            return f"{level} · {step_text} · {action} · {line_text}"
        return f"{step_text} · {action} · {line_text}"

    def handle_render_state(self, state):

        event = state.get("event")
        mode = state.get("mode", "render")

        if event == "start":
            self._set_section_visible("workflow", True)
            self._set_task_status("renderer", "running")
            if self.mode_combo.currentData() == "repair":
                self._set_task_status("repair", "running")
            total_nodes = state.get("total_nodes", 1)
            self._begin_activity(
                "Render",
                f"Rendering ({mode})",
                "building node graph" if mode == "incremental" else "executing script",
                busy=mode != "incremental",
                total=total_nodes,
            )
        elif event == "node":
            total_nodes = state.get("total_nodes", 1)
            if self._activity_kind != "Render":
                self._begin_activity(
                    "Render",
                    "Rendering (incremental)",
                    "building node graph",
                    total=total_nodes,
                )
            self._set_text("status_label", self.status_label, "Status: Rendering (incremental)")
            self._set_text("detail_label", self.detail_label, f"Stage: {self._render_stage_text(state)}")
            self.progress.setRange(0, max(int(total_nodes), 1))
            self.progress.setValue(min(int(state.get("index", 0)), self.progress.maximum()))
            self._update_elapsed()
        elif event == "fallback":
            self._set_text("status_label", self.status_label, "Status: Falling back to full render")
            self._set_text("detail_label", self.detail_label, f"Stage: {state.get('message', 'unsupported script')}")
        elif event == "finish":
            updated_count = int(state.get("updated_count", 0)) + int(
                state.get("added_count", 0)
            )
            reused_count = int(state.get("reused_count", 0))
            detail = (
                f"updated {updated_count}, reused {reused_count}, "
                f"viewer {state.get('viewer_apply_time_s', 0.0):.4f}s"
            )
            self._set_task_status("renderer", "done")
            if self.mode_combo.currentData() == "repair":
                self._set_task_status("repair", "done")
            self._finish_activity(f"Done ({mode})", detail)
            self._set_text(
                "elapsed_label",
                self.elapsed_label,
                f"Elapsed: {state.get('exec_time_s', 0.0):.3f}s",
            )
        elif event == "error":
            self._set_section_visible("workflow", True)
            self._set_task_status("renderer", "error")
            self._set_task_status("repair", "waiting")
            self._show_failure(state.get("message", "unknown error"))
            self._set_text("status_label", self.status_label, f"Status: {mode} failed")
            self._set_text("detail_label", self.detail_label, f"Stage: {state.get('message', 'unknown error')}")
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
            self._status_timer.stop()

        QApplication.processEvents()

    def handle_traceback(self, exc_info, _code):

        if not exc_info:
            self._last_traceback_text = ""
            return

        exc_type, exc, _tb = exc_info
        self._last_traceback_text = f"{exc_type.__name__}: {exc}"
        self._set_section_visible("workflow", True)
        self._set_task_status("renderer", "error")
        self._set_task_status("repair", "waiting")
        self._show_failure(self._last_traceback_text)
        self._set_text("status_label", self.status_label, "Status: Render failed")
        self._set_text("detail_label", self.detail_label, f"Stage: {exc_type.__name__}: {exc}")
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self._status_timer.stop()
        QApplication.processEvents()

    def _read_codex_stdout(self):

        if self._codex_process is None:
            return
        self._restart_codex_timeout()
        chunk = bytes(self._codex_process.readAllStandardOutput()).decode(
            "utf-8", errors="ignore"
        )
        self._codex_stdout.append(chunk)
        self._codex_stdout_buffer += chunk
        lines = self._codex_stdout_buffer.splitlines(keepends=True)
        self._codex_stdout_buffer = ""
        if lines and not lines[-1].endswith("\n"):
            self._codex_stdout_buffer = lines.pop()
        for line in lines:
            raw = line.strip()
            if not raw.startswith("{"):
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            self._handle_codex_event(event)

    def _read_codex_stderr(self):

        if self._codex_process is None:
            return
        self._restart_codex_timeout()
        self._codex_stderr.append(
            bytes(self._codex_process.readAllStandardError()).decode(
                "utf-8", errors="ignore"
            )
        )

    def _normalize_generated_script(self, text):

        stripped = text.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            stripped = "\n".join(lines).strip()

        return stripped

    def _summarize_codex_error(self, text):

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        errors = [line for line in lines if line.startswith("ERROR:")]
        if errors:
            return errors[-1]

        filtered = [
            line
            for line in lines
            if "shell_snapshot" not in line and "plugins::manager" not in line
        ]
        if filtered:
            return filtered[-1]

        return "Codex generation failed. Run `codex login` and try again."

    def generate_with_codex(self):

        if not self._codex_path:
            warning("Codex CLI not found. Install it first.")
            return

        if not self.prompt.toPlainText().strip():
            warning("Enter a CadQuery prompt first.")
            return

        editor = self._main_window.components["editor"]
        if not editor.confirm_discard():
            return

        self._pipeline_queue = self._build_stage_plan()
        self._pipeline_script = ""
        self._pipeline_usage_totals = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
        }
        self._last_prompt_text = self.prompt.toPlainText().strip()
        self._start_history_entry()
        self._add_history_line(f"Context: {self._context_summary()}")
        self._set_section_visible("workflow", True)
        self._set_task_status("supervisor", "running")
        self._set_task_status("modeler", "waiting")
        self._set_review_states("waiting")
        self._set_task_status("renderer", "waiting")
        self._set_task_status("repair", "idle")
        self._set_generating(True)
        self._set_text("cli_label", self.cli_label, "Codex CLI: staged generation running...")
        self._set_usage_summary(input_tokens=0, cached_tokens=0, output_tokens=0)
        info("Codex generation started")
        self._launch_stage(self._pipeline_queue.pop(0))

    def _codex_finished(self, exit_code, exit_status):

        self._codex_timeout_timer.stop()
        self._read_codex_stdout()
        self._read_codex_stderr()
        self._codex_timeout_timer.stop()
        active_stage = self._active_stage

        if self._codex_abort_reason:
            abort_kind = self._codex_abort_reason.get("kind", "user")
            error_text = self._codex_abort_reason.get("detail", "Codex stopped.")
            self._codex_abort_reason = None
            self._fail_pipeline(
                error_text,
                stopped=abort_kind == "user",
                timed_out=abort_kind == "timeout",
            )
            return

        leftover = self._codex_stdout_buffer.strip()
        if leftover.startswith("{"):
            try:
                self._handle_codex_event(json.loads(leftover))
            except json.JSONDecodeError:
                pass
        self._codex_stdout_buffer = ""

        usage = self._extract_usage_from_stdout()
        if usage:
            self._accumulate_usage(usage)

        output_path = self._codex_output_path
        self._codex_output_path = None

        if exit_code != 0:
            error_text = self._summarize_codex_error(
                "".join(self._codex_stderr).strip()
                or "".join(self._codex_stdout).strip()
            )
            if output_path and Path(output_path).exists():
                Path(output_path).unlink()
            self._fail_pipeline(error_text)
            return

        if not output_path or not Path(output_path).exists():
            self._fail_pipeline("Codex did not produce an output file.")
            return

        generated = self._normalize_generated_script(
            Path(output_path).read_text(encoding="utf-8")
        )
        Path(output_path).unlink()
        self._codex_process = None
        if not generated:
            self._fail_pipeline("Codex returned an empty script.")
            return

        if active_stage is not None:
            self._set_task_status(active_stage["task_id"], "done")
            self._add_history_line(f"Result: {self._stage_title(active_stage)} ready")

        self._pipeline_script = generated

        if self._pipeline_queue:
            self._launch_stage(self._pipeline_queue.pop(0))
            return

        audit = self._audit_script(generated)
        header = self._extract_script_header(generated)
        for line in header:
            self._add_history_line(line)
        self._add_history_line(
            "Native ops: " + (", ".join(audit["ops"]) if audit["ops"] else "none detected")
        )
        self._add_history_line(f"Native score: {audit['score']}%")
        for message in audit["warnings"]:
            self._add_history_line(f"Warnings: {message}")
        for message in audit["blockers"]:
            self._add_history_line(f"Validation: {message}")

        build_ok, build_error = self._validate_script_build(generated)
        if not build_ok:
            self._add_history_line(f"Build failed: {build_error}")
            self._set_review_states("error")
            self._fail_pipeline(
                f"build validation failed: {build_error}",
                history_detail="build validation failed",
            )
            return

        if not audit["passed"]:
            self._set_review_states("error")
            self._fail_pipeline(
                f"native feature gate blocked at {audit['score']}%",
                history_detail="native feature gate blocked",
            )
            return

        self._set_generating(False)
        self.refresh_cli_status()
        self._active_stage = None
        self._set_task_status("supervisor", "done")
        self._set_review_states("done")
        self._set_task_status("renderer", "waiting")
        self._set_task_status("repair", "idle")
        self._add_history_line("Validation: passed")

        editor = self._main_window.components["editor"]
        editor.set_text(generated + "\n")
        editor.reset_modified()
        self._finish_activity("Codex ready", "script inserted into editor")
        self._finish_history_entry("Ready", "script inserted into editor")
        self.prompt.clear()
        if self.mode_combo.currentData() == "new":
            self.mode_combo.setCurrentIndex(max(self.mode_combo.findData("refine"), 0))
        info("Codex generation completed")
        self._main_window.components["debugger"].render()
