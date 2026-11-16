"""A small JSON Schema (draft 2020-12 subset) checker, stdlib only.

The schemas in validator/schemas/*.schema.json are the single source of the formats of the index; this checker runs
them in CI and in the tests; any full JSON Schema implementation can run them too. Supported keywords: type, enum,
const, properties, required, additionalProperties, patternProperties, propertyNames, items, minItems, maxItems,
uniqueItems, minLength, maxLength, pattern, minimum, maximum, $ref (#/$defs/... only), anyOf, oneOf.
"""

import json
import os
import re

SCHEMA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schemas")
_cache = {}


def load(name):
    if name not in _cache:
        with open(os.path.join(SCHEMA_DIR, name + ".schema.json"), encoding="utf-8") as f:
            _cache[name] = json.load(f)
    return _cache[name]


def _type_ok(value, t):
    if t == "object":
        return isinstance(value, dict)
    if t == "array":
        return isinstance(value, list)
    if t == "string":
        return isinstance(value, str)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "null":
        return value is None
    raise ValueError("unknown type %s" % t)


def _same(a, b):
    """JSON equality: true is not 1 (Python says it is)"""
    return a == b and isinstance(a, bool) == isinstance(b, bool)


def validate(value, schema_name):
    """-> list of error strings (empty - valid)"""
    root = load(schema_name)
    errors = []
    _check(value, root, root, "$", errors)
    return errors


def _check(value, schema, root, where, errors):
    if len(errors) > 50:
        return
    if "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/$defs/"):
            raise ValueError("unsupported $ref %s" % ref)
        _check(value, root["$defs"][ref[len("#/$defs/"):]], root, where, errors)
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_type_ok(value, x) for x in types):
            errors.append("%s: expected %s" % (where, "/".join(types)))
            return
    if "const" in schema and not _same(value, schema["const"]):
        errors.append("%s: must be %r" % (where, schema["const"]))
    if "enum" in schema and not any(_same(value, e) for e in schema["enum"]):
        errors.append("%s: %r is not one of %s" % (where, value, schema["enum"]))
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append("%s: shorter than %d" % (where, schema["minLength"]))
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append("%s: longer than %d" % (where, schema["maxLength"]))
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append("%s: %r does not match %s" % (where, value, schema["pattern"]))
    if _type_ok(value, "number"):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append("%s: below %s" % (where, schema["minimum"]))
        if "maximum" in schema and value > schema["maximum"]:
            errors.append("%s: above %s" % (where, schema["maximum"]))
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append("%s: fewer than %d items" % (where, schema["minItems"]))
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append("%s: more than %d items" % (where, schema["maxItems"]))
        if schema.get("uniqueItems"):
            seen = [json.dumps(v, sort_keys=True) for v in value]
            if len(seen) != len(set(seen)):
                errors.append("%s: items are not unique" % where)
        if "items" in schema:
            for i, item in enumerate(value):
                _check(item, schema["items"], root, "%s[%d]" % (where, i), errors)
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append("%s: missing %s" % (where, key))
        props = schema.get("properties", {})
        patterns = schema.get("patternProperties", {})
        if "propertyNames" in schema:
            for key in value:
                _check(key, schema["propertyNames"], root, "%s.<key %s>" % (where, key), errors)
        for key, item in value.items():
            sub = "%s.%s" % (where, key)
            matched = False
            if key in props:
                _check(item, props[key], root, sub, errors)
                matched = True
            for pat, s in patterns.items():
                if re.search(pat, key):
                    _check(item, s, root, sub, errors)
                    matched = True
            if not matched:
                extra = schema.get("additionalProperties", True)
                if extra is False:
                    errors.append("%s: unexpected key" % sub)
                elif isinstance(extra, dict):
                    _check(item, extra, root, sub, errors)
    for key in ("anyOf", "oneOf"):
        if key in schema:
            ok = 0
            for option in schema[key]:
                sub_errors = []
                _check(value, option, root, where, sub_errors)
                ok += not sub_errors
            if (key == "anyOf" and ok == 0) or (key == "oneOf" and ok != 1):
                errors.append("%s: matches %d of the %s options" % (where, ok, key))
