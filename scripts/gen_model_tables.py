"""Generate RESEARCH_MODEL.md §2/§3/§7 tables from schema/graph_schema.yaml.

Usage: python scripts/gen_model_tables.py [--check]
"""
import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schema" / "graph_schema.yaml"
DOC = ROOT / "docs" / "RESEARCH_MODEL.md"


def load():
    return yaml.safe_load(SCHEMA.read_text(encoding="utf-8"))


def _alt(xs):
    return "\\|".join(xs)


def render(s):
    n = ["| NodeKind | Префикс | Поля (обязательные — жирным) | Создаёт задача | Из вывода | Смысл |",
         "|---|---|---|---|---|---|"]
    n += [f"| `{x['kind']}` | {x['prefix']} | {x['fields'].replace('|', chr(92) + '|')} | {x['created_by']} | "
          f"{x['from_output'].replace('|', chr(92) + '|')} | {x['text_ru']} |" for x in s["nodes"]]
    e = ["| Ребро | From → To | Кардинальность | Кем выводится |", "|---|---|---|---|"]
    e += [f"| `{x['name']}` | {_alt(x['from'])} → {_alt(x['to'])} | {x['card']} | {x['by']} |" for x in s["edges"]]
    r = ["| rule_id | Узлы | Проверка |", "|---|---|---|"]
    r += [f"| `{x['rule_id']}` | {x['nodes']} | {x['check'].replace('|', chr(92) + '|')} |" for x in s["rules"]]
    return {"nodes": "\n".join(n), "edges": "\n".join(e), "rules": "\n".join(r)}


def apply(text, tables):
    for name, body in tables.items():
        pat = re.compile(rf"(<!-- gen:{name}:start -->\n).*?(<!-- gen:{name}:end -->)", re.S)
        if not pat.search(text):
            raise SystemExit(f"marker gen:{name} missing in {DOC}")
        text = pat.sub(lambda m: m.group(1) + body + "\n" + m.group(2), text)
    return text


def main():
    old = DOC.read_text(encoding="utf-8")
    new = apply(old, render(load()))
    if "--check" in sys.argv:
        sys.exit(0 if new == old else "RESEARCH_MODEL.md out of date: run scripts/gen_model_tables.py")
    DOC.write_text(new, encoding="utf-8")


if __name__ == "__main__":
    main()
