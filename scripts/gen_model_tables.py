"""Generate RESEARCH_MODEL.md tables (§2, §3, §7) from packaged graph_schema.yaml.

Single source: coscientist/schema/graph_schema.yaml (packaged with the wheel).

Usage: python scripts/gen_model_tables.py [--check]
"""

import pathlib
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal, Optional, Union

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "coscientist" / "schema" / "graph_schema.yaml"
DOC = ROOT / "docs" / "RESEARCH_MODEL.md"


def load():
    return yaml.safe_load(SCHEMA.read_text(encoding="utf-8"))


def _esc(s):
    return str(s).replace("|", "\\|")


def _card(c):
    extra = "".join(f" ({k}: {v})" for k, v in c.get("out_by_src", {}).items())
    return f"{c['out']}{extra} / {c['in']}"


def _field(f):
    s = f"**{f['name']}**" if f["required"] else f["name"]
    s += f": {f['type']}" + (" [computed]" if f.get("computed") else "")
    return s + (f" ({f['note']})" if f.get("note") else "")


def _match_label(match: dict) -> str:
    return ",".join(f"{k}={v!r}" for k, v in match.items())


def _node_fields(node: dict) -> str:
    parts = [_field(f) for f in node.get("fields") or []]
    for v in node.get("variants") or []:
        label = _match_label(v["match"])
        body = "; ".join(_field(f) for f in v.get("fields") or [])
        parts.append(f"[when {label}: {body}]")
    return "; ".join(parts)


def _edge_attr(a):
    """Render one edge attribute for the note column."""
    s = f"attrs.{a['name']}: {a['type']}"
    if a.get("default") is not None:
        s += f" (default {a['default']})"
    elif not a.get("required", False):
        s += " (optional)"
    return s


def _edge_note(e):
    parts = []
    if e.get("note"):
        parts.append(str(e["note"]))
    attrs = e.get("attrs") or []
    if attrs:
        parts.append("; ".join(_edge_attr(a) for a in attrs))
    return "; ".join(parts)


def render(s):
    n = [
        "| NodeKind | Префикс | Поля (обязательные — жирным) | Создаёт задача | Из вывода | Смысл |",
        "|---|---|---|---|---|---|",
    ]
    n += [
        f"| `{x['kind']}` | {x['prefix']} | {_esc(_node_fields(x))} | "
        f"{x['created_by']} | {_esc(x['from_output'])} | {x['text_ru']} |"
        for x in s["nodes"]
    ]
    e = ["| Ребро | Пары (from → to) | out / in | Кем выводится | Примечание |", "|---|---|---|---|---|"]
    e += [
        f"| `{x['name']}` | {', '.join(f'{a} → {b}' for a, b in x['pairs'])} | "
        f"{_card(x['card'])} | {x['by']} | {_esc(_edge_note(x))} |"
        for x in s["edges"]
    ]
    b = ["| # | id | Узлы | CodeCheck.kind | Проверка |", "|---|---|---|---|---|"]
    b += [
        f"| {x['num']} | {x['id']} | {', '.join(x['nodes'])} | {', '.join(x['check_kinds'])} | {_esc(x['check'])} |"
        for x in s["volume_b"]
    ]
    r = ["| rule_id | Узлы | Проверка |", "|---|---|---|"]
    r += [f"| `{x['rule_id']}` | {', '.join(x['nodes'])} | {_esc(x['check'])} |" for x in s["rules"]]
    return {"nodes": "\n".join(n), "edges": "\n".join(e), "volume_b": "\n".join(b), "rules": "\n".join(r)}


@dataclass(frozen=True)
class RefTo:
    """Annotated marker: the str is an id of a node of one of these kinds (Ref[NodeKind])."""

    kinds: tuple


def py_type(t):
    """Map a schema field type to a python annotation (for pydantic.create_model)."""
    m = re.fullmatch(r"(\w+)\((.*)\)", t)
    if not m:
        return {"str": str, "int": int, "bool": bool, "float": float, "datetime": datetime, "digest": str}[t]
    head, arg = m.groups()
    if head == "enum":
        return Literal[tuple(arg.split("|"))]
    if head == "ref":
        return Annotated[str, RefTo(tuple(arg.split("|")))]
    if head == "list":
        return list[py_type(arg)]
    if head == "obj":
        return dict
    raise ValueError(t)


def _fields_list(node_or_variant: dict) -> list:
    fields = node_or_variant.get("fields")
    if fields is None:
        return []
    if not isinstance(fields, list):
        raise TypeError(f"fields must be a list, got {type(fields).__name__}")
    return fields


def _match_tag(match: dict) -> str:
    return "|".join(f"{k}={v!r}" for k, v in match.items())


def _literal_type(value: object):
    if type(value) is bool or type(value) is int or type(value) is str:
        return Literal[value]
    raise TypeError(f"unsupported match value type: {type(value).__name__}")


def _field_spec(fields: list, *, ctx: str, match: dict | None = None) -> dict:
    """Build create_model field map; ``match`` locks discriminator keys to Literals."""
    by_name: dict = {}
    for f in fields:
        if f.get("computed"):
            continue
        by_name[f["name"]] = dict(f)
    if match is not None:
        for key, value in match.items():
            prev = by_name.get(key)
            if prev is None:
                raise ValueError(f"{ctx}: match key {key!r} is not declared in fields")
            by_name[key] = {
                "name": key,
                "type": prev["type"],
                "required": True,
                "_literal": value,
            }
    spec: dict = {}
    for f in by_name.values():
        if "_literal" in f:
            t = _literal_type(f["_literal"])
        else:
            t = py_type(f["type"])
        spec[f["name"]] = (t, ...) if f["required"] else (Optional[t], None)
    return spec


def _role_model(name: str, cfg, fields: list, *, ctx: str, match: dict | None = None):
    from pydantic import create_model

    return create_model(name, __config__=cfg, **_field_spec(fields, ctx=ctx, match=match))


def _variant_union(kind: str, node: dict, cfg):
    """Discriminated union: one frozen create_model arm per schema variant."""
    from pydantic import Discriminator, RootModel, Tag, create_model

    common = _fields_list(node)
    variants = node.get("variants")
    if not isinstance(variants, list) or not variants:
        raise ValueError(f"{kind}: variants must be a non-empty list")
    arms: list = []
    arm_models: list[type] = []
    tags: list[str] = []
    for i, variant in enumerate(variants):
        match = variant.get("match")
        if not isinstance(match, dict) or not match:
            raise ValueError(f"{kind} variant[{i}]: match must be a non-empty mapping")
        tag = _match_tag(match)
        if tag in tags:
            raise ValueError(f"{kind}: duplicate variant match {tag}")
        tags.append(tag)
        merged = list(common) + _fields_list(variant)
        arm_name = f"{kind.title()}V{i}"
        arm = _role_model(arm_name, cfg, merged, match=match, ctx=f"{kind} variant[{i}]")
        arm_models.append(arm)
        arms.append(Annotated[arm, Tag(tag)])

    arm_tuple = tuple(arm_models)

    def _disc(value: object) -> str:
        if isinstance(value, dict):
            data = value
        elif isinstance(value, arm_tuple):
            data = value.model_dump()
        else:
            # Unknown shape → no tag match → TypeAdapter ValidationError (fail-closed).
            return f"{kind}:no_variant_match"
        for variant in variants:
            match = variant["match"]
            if all(data.get(k) == val for k, val in match.items()):
                return _match_tag(match)
        return f"{kind}:no_variant_match"

    union_type: object = arms[0]
    for arm in arms[1:]:
        union_type = Union[union_type, arm]
    union = Annotated[union_type, Discriminator(_disc)]
    # One role-model form: create_model RootModel over the discriminated union.
    # RootModel rejects model_config['extra']; arm models keep extra='forbid'.
    from pydantic import ConfigDict

    return create_model(
        f"{kind.title()}Role",
        __base__=RootModel[union],
        __config__=ConfigDict(frozen=True),
    )


def models(s=None):
    """Role-input models: frozen, extra='forbid'; computed fields are excluded (passing them is an error).

    Kinds with schema ``variants`` become a discriminated union (one arm per
    variant) so requiredness matches runtime KindSpec — e.g. criterion
    check=code|review requires metric/op/target; rubric arms follow the schema.
    """
    from pydantic import ConfigDict

    s = s or load()
    cfg = ConfigDict(frozen=True, extra="forbid")
    out = {}
    for n in s["nodes"]:
        kind = n["kind"]
        variants = n.get("variants")
        if variants is None:
            out[kind] = _role_model(kind.title(), cfg, _fields_list(n), ctx=kind)
        elif isinstance(variants, list) and variants:
            out[kind] = _variant_union(kind, n, cfg)
        else:
            raise ValueError(f"{kind}: variants must be a non-empty list when present")
    return out


def apply(text, tables):
    for name, body in tables.items():
        pat = re.compile(rf"(<!-- gen:{name}:start -->\n).*?(<!-- gen:{name}:end -->)", re.S)
        if not pat.search(text):
            raise SystemExit(f"marker gen:{name} missing in {DOC}")
        text = pat.sub(lambda m, b=body: m.group(1) + b + "\n" + m.group(2), text)
    return text


def main():
    old = DOC.read_text(encoding="utf-8")
    new = apply(old, render(load()))
    if "--check" in sys.argv:
        sys.exit(0 if new == old else "RESEARCH_MODEL.md out of date: run scripts/gen_model_tables.py")
    DOC.write_text(new, encoding="utf-8")


if __name__ == "__main__":
    main()
