"""The contract between `/record-review` and the Review tab, checked on both sides.

`reference/review-points.schema.json` is a JSON Schema (draft 2020-12) of the report
`review-points.py` writes to `.human-review/review-points.json`. It is checked where the
report is written (`review-points.py`, which `record-review.py finish` runs) and where it
is read (`hrbuild/tabs/review.py`, before a single pile renders). A report that does not
match is refused loudly on both sides: a pile that renders half a shape reads on the page
exactly like a pile that was meant to look that way.

No `jsonschema` dependency, for the reason `review-points.py` has no PyYAML: this runs on
every trainee laptop that installs the skill. The schema uses a small subset of the
vocabulary — `type`, `const`, `enum`, `required`, `properties`, `additionalProperties`,
`items`, `minItems`, `minLength`, `minimum`, `maximum`, `pattern`, `oneOf` and local
`$ref` — and this module implements exactly that subset. Anything else in the schema is a
hard error here, so the file cannot grow a keyword this checker silently ignores.
`test_review_points.py` holds it to the real `jsonschema` package when one is installed.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "reference" / "review-points.schema.json"
#: The `schema` value every report carries. Bumped together with the schema file.
SCHEMA_VERSION = "review-points/2"

_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}
_KNOWN = {"$schema", "$id", "$defs", "title", "description", "type", "const", "enum",
          "required", "properties", "additionalProperties", "items", "minItems",
          "minLength", "minimum", "maximum", "pattern", "oneOf", "$ref"}


@lru_cache(maxsize=1)
def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _is(value, kind: str) -> bool:
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPES[kind])


def _check(value, node: dict, root: dict, at: str, out: list[str]) -> None:
    unknown = set(node) - _KNOWN
    if unknown:
        raise ValueError(f"schema keyword(s) {sorted(unknown)} at {at} are not supported "
                         "by review_points_schema.py — extend the checker first")
    if "$ref" in node:
        ref = node["$ref"]
        if not ref.startswith("#/$defs/"):
            raise ValueError(f"only local #/$defs refs are supported, got {ref}")
        _check(value, root["$defs"][ref[len("#/$defs/"):]], root, at, out)
        return
    if "oneOf" in node:
        matches = 0
        for option in node["oneOf"]:
            trial: list[str] = []
            _check(value, option, root, at, trial)
            matches += not trial
        if matches != 1:
            out.append(f"{at}: matches {matches} of the {len(node['oneOf'])} allowed shapes")
        return
    if "const" in node and value != node["const"]:
        out.append(f"{at}: must be {node['const']!r}, got {value!r}")
        return
    if "enum" in node and value not in node["enum"]:
        out.append(f"{at}: must be one of {node['enum']}, got {value!r}")
        return
    kinds = node.get("type")
    if kinds is not None:
        kinds = [kinds] if isinstance(kinds, str) else kinds
        if not any(_is(value, k) for k in kinds):
            out.append(f"{at}: must be {' or '.join(kinds)}, got {type(value).__name__}")
            return
    if isinstance(value, str):
        if len(value) < node.get("minLength", 0):
            out.append(f"{at}: must not be empty")
        if "pattern" in node and not re.search(node["pattern"], value):
            out.append(f"{at}: {value[:60]!r} does not match {node['pattern']}")
    if _is(value, "number"):
        if "minimum" in node and value < node["minimum"]:
            out.append(f"{at}: {value} is below {node['minimum']}")
        if "maximum" in node and value > node["maximum"]:
            out.append(f"{at}: {value} is above {node['maximum']}")
    if isinstance(value, list):
        if len(value) < node.get("minItems", 0):
            out.append(f"{at}: needs at least {node['minItems']} item(s)")
        if "items" in node:
            for i, item in enumerate(value):
                _check(item, node["items"], root, f"{at}[{i}]", out)
    if isinstance(value, dict):
        for key in node.get("required", []):
            if key not in value:
                out.append(f"{at}: missing required `{key}`")
        props = node.get("properties", {})
        extra = node.get("additionalProperties", True)
        for key, item in value.items():
            if key in props:
                _check(item, props[key], root, f"{at}.{key}", out)
            elif extra is False:
                out.append(f"{at}: unknown key `{key}`")
            elif isinstance(extra, dict):
                _check(item, extra, root, f"{at}.{key}", out)


def problems(doc, schema: dict | None = None) -> list[str]:
    """Every way `doc` departs from the schema, as `$.path: what` lines. Empty is valid."""
    schema = schema or load_schema()
    out: list[str] = []
    _check(doc, schema, schema, "$", out)
    return out


#: The other contract `/record-review` writes: what the change cost before its page existed
#: (`review-cost.json`, committed beside review-points.md). Same checker, its own schema —
#: the cost has a different reader (`review-cost.py --ledger`) and a different lifetime
#: (rewritten on every CI round), so it is not a key of the review-points report.
COST_SCHEMA_PATH = SCHEMA_PATH.parent / "review-cost.schema.json"


@lru_cache(maxsize=1)
def load_cost_schema() -> dict:
    return json.loads(COST_SCHEMA_PATH.read_text(encoding="utf-8"))


def cost_problems(doc) -> list[str]:
    """`problems()` against `reference/review-cost.schema.json`."""
    return problems(doc, load_cost_schema())
