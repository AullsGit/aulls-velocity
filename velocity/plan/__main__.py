"""`python -m velocity.plan --schema [ficheiro]`: escreve o JSON Schema do Plano.
`python -m velocity.plan --check plano.json`: valida um plano (ou Timeline legada) e lista os problemas."""
import argparse
import json
import sys

from velocity.engine import get_engine
from velocity.plan import Plan, load_plan, validate_plan


def main(argv=None):
    ap = argparse.ArgumentParser(prog="velocity.plan")
    ap.add_argument("--schema", nargs="?", const="-", help="escreve o JSON Schema (ficheiro ou '-' para stdout)")
    ap.add_argument("--check", help="plano JSON a validar")
    ap.add_argument("--engine", default=None, help="valida também contra os limites deste motor")
    a = ap.parse_args(argv)
    if a.schema:
        js = json.dumps(Plan.model_json_schema(), ensure_ascii=False, indent=1)
        if a.schema == "-":
            print(js)
        else:
            open(a.schema, "w", encoding="utf-8").write(js + "\n")
        return 0
    if a.check:
        plan = load_plan(json.load(open(a.check, encoding="utf-8")))
        issues = validate_plan(plan) + get_engine(a.engine).validate(plan)
        for i in issues:
            print(i)
        print("OK" if not any(i.level == "error" for i in issues) else "COM ERROS")
        return 1 if any(i.level == "error" for i in issues) else 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
