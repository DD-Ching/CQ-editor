import argparse
import json
import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cadquery import cqgi


@dataclass
class CapabilityCase:
    name: str
    prompt: str


@dataclass
class CapabilityResult:
    name: str
    prompt: str
    success: bool
    detail: str
    script: str = ""
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0


DEFAULT_CASES = {
    "sphere": CapabilityCase("sphere", "Draw a sphere."),
    "circle": CapabilityCase("circle", "Draw a circle."),
    "box": CapabilityCase("box", "Draw a box."),
}


def _base_prompt(user_request):

    return "\n".join(
        (
            "Write one CadQuery Python script for CQ-editor.",
            "Return only raw Python code with no markdown fences or explanation.",
            "Use import cadquery as cq.",
            "Assign the final shape to result and call show_object(result).",
            'For simple primitives, prefer direct Workplane primitives such as cq.Workplane("XY").circle(r), sphere(r), box(x, y, z), or cylinder(h, r).',
            "Keep the script minimal and direct.",
            f"User request: {user_request}",
        )
    )


def _normalize_script(text):

    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


def _extract_usage(stdout_text):

    usage = {}
    for line in stdout_text.splitlines():
        raw = line.strip()
        if not raw.startswith("{"):
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "turn.completed":
            usage = event.get("usage", {}) or {}
    return usage


def run_codex_generation(case, effort="medium", timeout_s=120, cwd=None, codex_path=None):

    codex_path = codex_path or shutil.which("codex")
    if not codex_path:
        return CapabilityResult(case.name, case.prompt, False, "codex CLI not found")

    fd, output_path = tempfile.mkstemp(prefix=f"codex_verify_{case.name}_", suffix=".py")
    os.close(fd)
    try:
        result = subprocess.run(
            [
                codex_path,
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "--json",
                "--color",
                "never",
                "-c",
                f'reasoning_effort="{effort}"',
                "-C",
                str(cwd or Path.cwd()),
                "-o",
                output_path,
                _base_prompt(case.prompt),
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        usage = _extract_usage(stdout)

        if result.returncode != 0:
            detail = (stderr or stdout).strip().splitlines()
            return CapabilityResult(
                case.name,
                case.prompt,
                False,
                detail[-1] if detail else "codex exec failed",
                input_tokens=int(usage.get("input_tokens", 0) or 0),
                cached_input_tokens=int(usage.get("cached_input_tokens", 0) or 0),
                output_tokens=int(usage.get("output_tokens", 0) or 0),
            )

        script = _normalize_script(Path(output_path).read_text(encoding="utf-8"))
        if not script:
            return CapabilityResult(
                case.name,
                case.prompt,
                False,
                "codex returned an empty script",
                input_tokens=int(usage.get("input_tokens", 0) or 0),
                cached_input_tokens=int(usage.get("cached_input_tokens", 0) or 0),
                output_tokens=int(usage.get("output_tokens", 0) or 0),
            )

        return CapabilityResult(
            case.name,
            case.prompt,
            True,
            "generated",
            script=script,
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            cached_input_tokens=int(usage.get("cached_input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
        )
    except subprocess.TimeoutExpired:
        return CapabilityResult(case.name, case.prompt, False, f"timed out after {timeout_s}s")
    finally:
        if Path(output_path).exists():
            Path(output_path).unlink()


def _built_shape(script):

    build = cqgi.parse(script).build()
    if not build.success:
        return False, str(build.exception), None
    if not build.results:
        return False, "build succeeded but produced no show_object result", None

    obj = build.results[0].shape if hasattr(build.results[0], "shape") else build.results[0]
    if hasattr(obj, "val"):
        obj = obj.val()
    return True, "build ok", obj


def validate_sphere_script(script):

    ok, detail, shape = _built_shape(script)
    if not ok:
        return False, detail

    if shape.ShapeType() != "Solid":
        return False, f"expected Solid, got {shape.ShapeType()}"

    bb = shape.BoundingBox()
    dims = [bb.xlen, bb.ylen, bb.zlen]
    avg = sum(dims) / 3.0
    if avg <= 0:
        return False, "sphere bbox is degenerate"
    max_dev = max(abs(dim - avg) for dim in dims)
    if max_dev > max(0.5, avg * 0.1):
        return False, f"sphere bbox not isotropic: {dims}"

    return True, f"solid sphere-like bbox {dims}"


def validate_circle_script(script):

    ok, detail, shape = _built_shape(script)
    if not ok:
        return False, detail

    shape_type = shape.ShapeType()
    if shape_type not in ("Wire", "Edge"):
        return False, f"expected Wire or Edge, got {shape_type}"

    bb = shape.BoundingBox()
    if bb.zlen > 1e-6:
        return False, f"circle should be planar, got zlen={bb.zlen}"
    if not math.isclose(bb.xlen, bb.ylen, rel_tol=0.1, abs_tol=0.5):
        return False, f"circle bbox not round: {(bb.xlen, bb.ylen, bb.zlen)}"
    if hasattr(shape, "IsClosed") and not shape.IsClosed():
        return False, "circle wire is not closed"

    return True, f"closed circle-like {shape_type} bbox {(bb.xlen, bb.ylen, bb.zlen)}"


def validate_box_script(script):

    ok, detail, shape = _built_shape(script)
    if not ok:
        return False, detail

    if shape.ShapeType() != "Solid":
        return False, f"expected Solid, got {shape.ShapeType()}"
    bb = shape.BoundingBox()
    if min(bb.xlen, bb.ylen, bb.zlen) <= 0:
        return False, f"box bbox is degenerate: {(bb.xlen, bb.ylen, bb.zlen)}"
    return True, f"solid box-like bbox {(bb.xlen, bb.ylen, bb.zlen)}"


VALIDATORS = {
    "sphere": validate_sphere_script,
    "circle": validate_circle_script,
    "box": validate_box_script,
}


def verify_case(case_name, effort="medium", timeout_s=120, cwd=None, codex_path=None):

    case = DEFAULT_CASES[case_name]
    generation = run_codex_generation(
        case, effort=effort, timeout_s=timeout_s, cwd=cwd, codex_path=codex_path
    )
    if not generation.success:
        return generation

    ok, detail = VALIDATORS[case_name](generation.script)
    generation.success = ok
    generation.detail = detail
    return generation


def main():

    parser = argparse.ArgumentParser(description="Run live Codex CAD capability checks.")
    parser.add_argument(
        "--case",
        action="append",
        choices=sorted(DEFAULT_CASES.keys()),
        help="Capability case to run. Defaults to sphere and circle.",
    )
    parser.add_argument("--effort", default="medium", choices=("medium", "high", "xhigh"))
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--cwd", default=str(Path.cwd()))
    args = parser.parse_args()

    cases = args.case or ["sphere", "circle"]
    failures = 0
    for case_name in cases:
        result = verify_case(
            case_name,
            effort=args.effort,
            timeout_s=args.timeout,
            cwd=args.cwd,
        )
        usage = (
            f"in={result.input_tokens} cached={result.cached_input_tokens} out={result.output_tokens}"
        )
        print(
            f"[{case_name}] {'PASS' if result.success else 'FAIL'} | {result.detail} | {usage}"
        )
        if not result.success:
            failures += 1

    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
