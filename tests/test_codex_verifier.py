from cq_editor.codex_verifier import (
    validate_box_script,
    validate_circle_script,
    validate_sphere_script,
)


def test_validate_sphere_script_accepts_simple_sphere():
    script = """import cadquery as cq
result = cq.Workplane("XY").sphere(10)
show_object(result)
"""

    ok, detail = validate_sphere_script(script)

    assert ok, detail


def test_validate_circle_script_accepts_simple_circle():
    script = """import cadquery as cq
result = cq.Workplane("XY").circle(10)
show_object(result)
"""

    ok, detail = validate_circle_script(script)

    assert ok, detail


def test_validate_box_script_accepts_simple_box():
    script = """import cadquery as cq
result = cq.Workplane("XY").box(10, 12, 14)
show_object(result)
"""

    ok, detail = validate_box_script(script)

    assert ok, detail


def test_validate_sphere_script_rejects_flat_circle():
    script = """import cadquery as cq
result = cq.Workplane("XY").circle(10)
show_object(result)
"""

    ok, detail = validate_sphere_script(script)

    assert not ok
    assert "Solid" in detail


def test_validate_circle_script_rejects_box():
    script = """import cadquery as cq
result = cq.Workplane("XY").box(10, 10, 10)
show_object(result)
"""

    ok, detail = validate_circle_script(script)

    assert not ok
    assert "Wire or Edge" in detail
