"""Three-way merge for JSON configuration documents.

Merge rules (applied recursively key by key, using JSON Pointer paths):

* both sides made the *same* change (or both left the value untouched)
  -> that value is kept;
* only one side changed the value relative to ``base``
  -> that side's value is adopted automatically;
* both sides changed the value differently
  -> a conflict placeholder is inserted into the merged document and both
     candidates are recorded for later resolution.

Arrays and scalars are treated as atomic leaf values (no element-level
matching); nested objects are merged recursively.
"""

from __future__ import annotations

from typing import Any

# Reserved keys used to mark a conflict inside the merged document. The outer
# "__conflict__" wrapper identifies the placeholder (its name is unlikely to
# collide with a real configuration key); the inner candidate keys mirror the
# public base/left/right naming.
MARKER_KEY = "__conflict__"
MARKER_BASE = "base"
MARKER_LEFT = "left"
MARKER_RIGHT = "right"
MARKER_KEYS = frozenset(
    {MARKER_KEY, MARKER_BASE, MARKER_LEFT, MARKER_RIGHT}
)

# Sentinel used inside candidate lists to say "the key is absent / deleted".
# An explicit JSON ``null`` is the JSON value ``None`` and is distinct.
DELETE_TOKEN: Any = {"__delete__": True}

# Used internally to mean "no value at this path" (missing key).
_MISSING = object()


# ---------------------------------------------------------------------------
# Marker / sentinel helpers
# ---------------------------------------------------------------------------

def is_marker(node: Any) -> bool:
    """Return True if *node* is a conflict placeholder we inserted."""
    return (
        isinstance(node, dict)
        and len(node) == 1
        and MARKER_KEY in node
    )


def is_delete(value: Any) -> bool:
    """Return True if *value* is the explicit delete sentinel."""
    return isinstance(value, dict) and value == DELETE_TOKEN


def _present(value: Any) -> Any:
    """Convert the internal missing sentinel to the public delete token."""
    return DELETE_TOKEN if value is _MISSING else value


def make_marker(base: Any, left: Any, right: Any) -> dict:
    return {
        MARKER_KEY: {
            MARKER_BASE: _present(base),
            MARKER_LEFT: _present(left),
            MARKER_RIGHT: _present(right),
        }
    }


# ---------------------------------------------------------------------------
# JSON Pointer (RFC 6901)
# ---------------------------------------------------------------------------

def escape_token(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def parse_pointer(pointer: str) -> list[str]:
    """Parse a JSON Pointer into its reference tokens.

    ``""`` denotes the whole document (no tokens).
    """
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise ValueError(
            f"invalid JSON Pointer {pointer!r}: must be empty or start with '/'"
        )
    tokens: list[str] = []
    # Skip the leading slash; a trailing slash yields one empty token.
    for raw in pointer[1:].split("/"):
        # Unescape in this order (RFC 6901): "~1" -> "/", then "~0" -> "~".
        token = raw.replace("~1", "/").replace("~0", "~")
        tokens.append(token)
    return tokens


def _format_pointer(tokens: list[str]) -> str:
    if not tokens:
        return ""
    return "/" + "/".join(escape_token(t) for t in tokens)


# ---------------------------------------------------------------------------
# Three-way merge
# ---------------------------------------------------------------------------

def _leaf_conflict(base: Any, left: Any, right: Any) -> bool:
    """Both sides changed the leaf and disagree."""
    return left != base and right != base and left != right


def _merge_node(base: Any, left: Any, right: Any, path: list[str]):
    """Recursively merge one node.

    Returns ``(merged_node, conflicts)`` where *conflicts* is a list of
    ``{"pointer", "base", "left", "right"}`` dicts.
    """
    if isinstance(base, dict) and isinstance(left, dict) and isinstance(right, dict):
        merged: dict[str, Any] = {}
        conflicts: list[dict[str, Any]] = []
        # Sorted for deterministic pointer ordering in the response.
        for key in sorted(set(base) | set(left) | set(right)):
            b = base.get(key, _MISSING)
            l = left.get(key, _MISSING)
            r = right.get(key, _MISSING)
            child_path = path + [key]
            if l == r:
                # Both identical (including both untouched or both deleted).
                if l is not _MISSING:
                    merged[key] = l
            elif l == b:
                # Only right changed.
                if r is not _MISSING:
                    merged[key] = r
            elif r == b:
                # Only left changed.
                if l is not _MISSING:
                    merged[key] = l
            else:
                # Both sides diverged from base. Recurse when all three
                # values at this key are objects; otherwise it is a leaf
                # conflict (arrays and scalars are compared atomically).
                if (
                    b is not _MISSING
                    and l is not _MISSING
                    and r is not _MISSING
                    and isinstance(b, dict)
                    and isinstance(l, dict)
                    and isinstance(r, dict)
                ):
                    child, child_conflicts = _merge_node(b, l, r, child_path)
                    merged[key] = child
                    conflicts.extend(child_conflicts)
                else:
                    pointer = _format_pointer(child_path)
                    merged[key] = make_marker(b, l, r)
                    conflicts.append(
                        {
                            "pointer": pointer,
                            "base": _present(b),
                            "left": _present(l),
                            "right": _present(r),
                        }
                    )
        return merged, conflicts

    # At least one side is a leaf/array (or missing): atomic comparison.
    if left == right:
        return left, []
    if left == base:
        return right, []
    if right == base:
        return left, []
    if _leaf_conflict(base, left, right):
        pointer = _format_pointer(path)
        return (
            make_marker(base, left, right),
            [
                {
                    "pointer": pointer,
                    "base": _present(base),
                    "left": _present(left),
                    "right": _present(right),
                }
            ],
        )
    # Defensive fallback; the branches above cover every combination.
    return left, []


def three_way_merge(base: Any, left: Any, right: Any):
    """Merge three JSON documents.

    Returns ``(merged, conflicts)``. When the root itself conflicts the
    merged document is a single conflict placeholder and ``conflicts``
    contains the ``""`` pointer.
    """
    if isinstance(base, dict) and isinstance(left, dict) and isinstance(right, dict):
        return _merge_node(base, left, right, [])

    if left == right:
        return left, []
    if left == base:
        return right, []
    if right == base:
        return left, []
    if _leaf_conflict(base, left, right):
        return make_marker(base, left, right), [
            {
                "pointer": "",
                "base": _present(base),
                "left": _present(left),
                "right": _present(right),
            }
        ]
    return left, []


# ---------------------------------------------------------------------------
# Conflict resolution
# ---------------------------------------------------------------------------

class ResolutionError(Exception):
    """Raised when a batch of conflict resolutions cannot be applied."""


def apply_resolutions(merged: Any, conflicts: list[dict], resolutions: dict[str, Any]):
    """Apply chosen values to conflict placeholders.

    *resolutions* maps JSON Pointer strings to the chosen value. The value
    ``{"__delete__": true}`` removes the key at that path.

    Returns a new ``(merged, remaining_conflicts)`` tuple. All pointers are
    validated before any change is applied, so a bad batch mutates nothing.
    """
    if not isinstance(resolutions, dict) or not resolutions:
        raise ResolutionError("resolutions must be a non-empty object")

    conflict_pointers = {c["pointer"] for c in conflicts}
    unknown = [p for p in resolutions if p not in conflict_pointers]
    if unknown:
        raise ResolutionError(
            f"pointers are not current unresolved conflicts: {sorted(unknown)}"
        )

    tokens_by_pointer = {p: parse_pointer(p) for p in resolutions}

    # Validate every pointer before mutating anything.
    for pointer, tokens in tokens_by_pointer.items():
        node = merged
        for i, token in enumerate(tokens):
            if not isinstance(node, dict) or token not in node:
                raise ResolutionError(
                    f"pointer {pointer!r} does not point at an unresolved conflict "
                    "(conflict paths cannot traverse arrays or scalars)"
                )
            node = node[token]
            if i == len(tokens) - 1 and not is_marker(node):
                raise ResolutionError(
                    f"pointer {pointer!r} does not point at an unresolved conflict"
                )

    result = merged
    if "" in resolutions:
        # Whole-document conflict: replace the root outright (delete at root
        # means the final document becomes null, JSON has no other "absent").
        chosen = resolutions[""]
        result = None if is_delete(chosen) else chosen
    else:
        result = _deep_copy(merged)
        for pointer, chosen in resolutions.items():
            tokens = tokens_by_pointer[pointer]
            node = result
            for token in tokens[:-1]:
                node = node[token]
            last_key = tokens[-1]
            if is_delete(chosen):
                del node[last_key]
            else:
                node[last_key] = chosen

    remaining = [c for c in conflicts if c["pointer"] not in resolutions]
    return result, remaining


def _deep_copy(value: Any) -> Any:
    import json

    return json.loads(json.dumps(value))


def no_markers(node: Any) -> bool:
    """Verify a finalized document contains no leftover conflict markers."""
    if is_marker(node):
        return False
    if isinstance(node, dict):
        return all(no_markers(v) for v in node.values())
    if isinstance(node, list):
        return all(no_markers(v) for v in node)
    return True
