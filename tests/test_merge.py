"""Unit tests for the three-way merge algorithm and JSON Pointer handling."""

import pytest

from app.merge import (
    DELETE_TOKEN,
    MARKER_KEY,
    apply_resolutions,
    is_marker,
    no_markers,
    parse_pointer,
    three_way_merge,
)


def test_no_changes_returns_base():
    base = {"a": 1, "b": {"c": 2}}
    merged, conflicts = three_way_merge(base, base, base)
    assert merged == base
    assert conflicts == []


def test_one_sided_changes_are_auto_adopted():
    base = {"a": 1, "b": 2, "keep": "x"}
    left = {"a": 10, "b": 2, "keep": "x"}   # changed a
    right = {"a": 1, "b": 20, "keep": "x"}  # changed b
    merged, conflicts = three_way_merge(base, left, right)
    assert merged == {"a": 10, "b": 20, "keep": "x"}
    assert conflicts == []


def test_same_change_on_both_sides_merges():
    base = {"a": 1}
    left = {"a": 42}
    right = {"a": 42}
    merged, conflicts = three_way_merge(base, left, right)
    assert merged == {"a": 42}
    assert conflicts == []


def test_diverging_changes_create_conflict_placeholder_and_candidates():
    base = {"a": 1}
    left = {"a": 10}
    right = {"a": 20}
    merged, conflicts = three_way_merge(base, left, right)
    assert is_marker(merged["a"])
    assert merged["a"][MARKER_KEY] == {"base": 1, "left": 10, "right": 20}
    assert conflicts == [{"pointer": "/a", "base": 1, "left": 10, "right": 20}]


def test_deletion_against_edit_is_a_conflict_with_delete_token():
    base = {"a": 1}
    left = {}
    right = {"a": 99}
    merged, conflicts = three_way_merge(base, left, right)
    assert conflicts[0]["pointer"] == "/a"
    assert conflicts[0]["left"] == DELETE_TOKEN
    assert conflicts[0]["right"] == 99
    assert is_marker(merged["a"])


def test_both_delete_same_key_removes_it():
    base = {"a": 1, "b": 2}
    left = {"b": 2}
    right = {"b": 2}
    merged, conflicts = three_way_merge(base, left, right)
    assert merged == {"b": 2}
    assert conflicts == []


def test_one_sided_delete_and_other_untouched_adopts_delete():
    base = {"a": 1, "b": 2}
    left = {}
    right = {"a": 1, "b": 2}
    merged, conflicts = three_way_merge(base, left, right)
    assert merged == {}
    assert conflicts == []


def test_additions_on_different_keys_merge_recursively():
    base = {"nested": {"x": 1}}
    left = {"nested": {"x": 1, "y": 2}}
    right = {"nested": {"x": 1, "z": 3}}
    merged, conflicts = three_way_merge(base, left, right)
    assert merged == {"nested": {"x": 1, "y": 2, "z": 3}}
    assert conflicts == []


def test_nested_conflict_uses_json_pointer():
    base = {"nested": {"deep": {"v": 0}}}
    left = {"nested": {"deep": {"v": 1}}}
    right = {"nested": {"deep": {"v": 2}}}
    merged, conflicts = three_way_merge(base, left, right)
    assert conflicts[0]["pointer"] == "/nested/deep/v"
    assert is_marker(merged["nested"]["deep"]["v"])


def test_recursion_continues_below_keys_where_both_changed_objects():
    base = {"o": {"a": 1, "b": 1}}
    left = {"o": {"a": 10, "b": 1}}
    right = {"o": {"a": 1, "b": 20}}
    merged, conflicts = three_way_merge(base, left, right)
    assert merged == {"o": {"a": 10, "b": 20}}
    assert conflicts == []


def test_arrays_are_atomic_leaves():
    base = {"a": [1, 2]}
    left = {"a": [1, 2, 3]}
    right = {"a": [1, 2, 4]}
    merged, conflicts = three_way_merge(base, left, right)
    assert is_marker(merged["a"])
    assert len(conflicts) == 1


def test_null_is_distinct_from_delete():
    base = {"a": 1}
    left = {"a": None}
    right = {"a": None}
    merged, conflicts = three_way_merge(base, left, right)
    assert merged == {"a": None}
    assert conflicts == []


def test_root_level_scalar_conflict():
    merged, conflicts = three_way_merge(1, 2, 3)
    assert is_marker(merged)
    assert conflicts == [{"pointer": "", "base": 1, "left": 2, "right": 3}]


def test_root_level_auto_merge_when_only_one_side_changes():
    merged, conflicts = three_way_merge({"x": 1}, {"x": 1}, {"x": 2})
    assert merged == {"x": 2}
    assert conflicts == []


def test_pointer_with_escaped_characters():
    base = {"a/b": {"~tilde": 1}}
    left = {"a/b": {"~tilde": 2}}
    right = {"a/b": {"~tilde": 3}}
    merged, conflicts = three_way_merge(base, left, right)
    assert conflicts[0]["pointer"] == "/a~1b/~0tilde"
    resolved, remaining = apply_resolutions(
        merged, conflicts, {conflicts[0]["pointer"]: 7}
    )
    assert remaining == []
    assert resolved == {"a/b": {"~tilde": 7}}


def test_parse_pointer_roundtrip():
    assert parse_pointer("") == []
    assert parse_pointer("/a/b") == ["a", "b"]
    assert parse_pointer("/a~1b/~0") == ["a/b", "~"]
    with pytest.raises(ValueError):
        parse_pointer("noslash")


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def test_apply_partial_then_full_resolution():
    base = {"a": 1, "b": 1}
    left = {"a": 10, "b": 10}
    right = {"a": 20, "b": 20}
    merged, conflicts = three_way_merge(base, left, right)
    assert {c["pointer"] for c in conflicts} == {"/a", "/b"}

    merged, remaining = apply_resolutions(merged, conflicts, {"/a": 10})
    assert merged["a"] == 10
    assert is_marker(merged["b"])
    assert [c["pointer"] for c in remaining] == ["/b"]

    merged, remaining = apply_resolutions(merged, remaining, {"/b": DELETE_TOKEN})
    assert merged == {"a": 10}
    assert remaining == []
    assert no_markers(merged)


def test_resolution_rejects_unknown_pointer():
    base = {"a": 1}
    merged, conflicts = three_way_merge(base, {"a": 2}, {"a": 3})
    with pytest.raises(Exception):
        apply_resolutions(merged, conflicts, {"/nope": 1})


def test_resolution_rejects_empty_batch():
    merged, conflicts = three_way_merge({"a": 1}, {"a": 2}, {"a": 3})
    with pytest.raises(Exception):
        apply_resolutions(merged, conflicts, {})


def test_resolution_does_not_mutate_input_on_failure():
    merged, conflicts = three_way_merge(
        {"a": 1, "b": 1}, {"a": 2, "b": 2}, {"a": 3, "b": 3}
    )
    snapshot = repr(merged)
    with pytest.raises(Exception):
        # "/b/x" traverses into the conflict marker at /b, which is invalid;
        # the whole batch must then be rejected without applying /a either.
        apply_resolutions(merged, conflicts, {"/a": 2, "/b/x": 9})
    assert repr(merged) == snapshot
    assert is_marker(merged["a"]) and is_marker(merged["b"])
