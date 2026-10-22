"""Generate RESEARCH_MODEL.md tables (§2, §3, §7) from schema/graph_schema.yaml.

Usage: python scripts/gen_model_tables.py [--check]
"""
import pathlib
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal, Optional

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schema" / "graph_schema.yaml"
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


def render(s):
    n = ["| NodeKind | Префикс | Поля (обязательные — жирным) | Создаёт задача | Из вывода | Смысл |",
         "|---|---|---|---|---|---|"]
    n += [f"| `{x['kind']}` | {x['prefix']} | {_esc('; '.join(_field(f) for f in x['fields']))} | "
          f"{x['created_by']} | {_esc(x['from_output'])} | {x['text_ru']} |" for x in s["nodes"]]
    e = ["| Ребро | Пары (from → to) | out / in | Кем выводится | Примечание |", "|---|---|---|---|---|"]
    e += [f"| `{x['name']}` | {', '.join(f'{a} → {b}' for a, b in x['pairs'])} | "
          f"{_card(x['card'])} | {x['by']} | {_esc(x.get('note', ''))} |" for x in s["edges"]]
    b = ["| # | id | Узлы | CodeCheck.kind | Проверка |", "|---|---|---|---|---|"]
    b += [f"| {x['num']} | {x['id']} | {', '.join(x['nodes'])} | {', '.join(x['check_kinds'])} | {_esc(x['check'])} |"
          for x in s["volume_b"]]
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


def models(s=None):
    """Role-input models: frozen, extra='forbid'; computed fields are excluded (passing them is an error)."""
    from pydantic import ConfigDict, create_model

    s = s or load()
    cfg = ConfigDict(frozen=True, extra="forbid")
    out = {}
    for n in s["nodes"]:
        spec = {}
        for f in n["fields"]:
            if f.get("computed"):
                continue
            t = py_type(f["type"])
            spec[f["name"]] = (t, ...) if f["required"] else (Optional[t], None)
        out[n["kind"]] = create_model(n["kind"].title(), __config__=cfg, **spec)
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
