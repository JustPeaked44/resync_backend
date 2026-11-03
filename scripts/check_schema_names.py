"""Check literal Supabase table, RPC, query column, and payload column names."""

import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads(Path(r"D:\RESYNC_DEV\handoff\session\db\build\final_schema.json").read_text(encoding="utf-8"))["tables"]
RPCS = {"get_credit_balance", "debit_scan_credit", "refund_scan_credit", "purchase_credits", "unlock_scan_report"}


def literal(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def method(node):
    return node.func.attr if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) else None


def check(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    nodes = list(ast.walk(tree))
    assignments = {}
    constants = {}
    for node in nodes:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    assignments.setdefault(target.id, []).append((node.lineno, node.value))
                    if literal(node.value):
                        constants[target.id] = literal(node.value)

    errors = []

    def report(node, value, kind, allowed):
        if value not in allowed:
            errors.append(f"{path.relative_to(ROOT)}:{node.lineno}: unknown {kind} {value!r}")

    def table_for(node):
        while isinstance(node, ast.Call):
            if method(node) == "table" and node.args:
                arg = node.args[0]
                return literal(arg) or (constants.get(arg.id) if isinstance(arg, ast.Name) else None)
            node = node.func.value if isinstance(node.func, ast.Attribute) else None
        return None

    def assigned(name, before):
        matches = [(line, value) for line, value in assignments.get(name, []) if line < before]
        return max(matches, default=(0, None), key=lambda item: item[0])

    def payload_keys(expr, before, seen=None):
        seen = seen or set()
        if isinstance(expr, ast.Dict):
            return [(key, literal(key)) for key in expr.keys if key is not None]
        if isinstance(expr, ast.ListComp):
            return payload_keys(expr.elt, before, seen)
        if isinstance(expr, ast.List):
            return [pair for item in expr.elts for pair in payload_keys(item, before, seen)]
        if isinstance(expr, ast.Name) and expr.id not in seen:
            seen.add(expr.id)
            line, value = assigned(expr.id, before)
            found = payload_keys(value, line, seen) if value else []
            for node in nodes:
                if line < getattr(node, "lineno", 0) < before and method(node) == "append" \
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == expr.id and node.args:
                    found.extend(payload_keys(node.args[0], node.lineno, set()))
                if line < getattr(node, "lineno", 0) < before and isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name) and target.value.id == expr.id:
                            found.append((target.slice, literal(target.slice)))
            return found
        return []

    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        name = method(node)
        if name == "table" and node.args:
            value = literal(node.args[0]) or (constants.get(node.args[0].id) if isinstance(node.args[0], ast.Name) else None)
            if value:
                report(node, value, "table", SCHEMA)
        if name == "rpc" and node.args and literal(node.args[0]):
            report(node, literal(node.args[0]), "RPC", RPCS)
        table = table_for(node)
        if table not in SCHEMA:
            continue
        columns = SCHEMA[table]
        if name in {"select", "eq", "neq", "order", "in_"} and node.args and literal(node.args[0]):
            values = literal(node.args[0]).split(",") if name == "select" else [literal(node.args[0])]
            for value in values:
                value = value.strip()
                if value != "*":
                    report(node, value, f"{table} column", columns)
        if name in {"insert", "update", "upsert"} and node.args:
            for key, value in payload_keys(node.args[0], node.lineno):
                if value:
                    report(key, value, f"{table} payload column", columns)
        if name == "upsert":
            for keyword in node.keywords:
                if keyword.arg == "on_conflict" and literal(keyword.value):
                    for value in literal(keyword.value).split(","):
                        report(keyword.value, value.strip(), f"{table} conflict column", columns)
    return errors


if __name__ == "__main__":
    paths = [ROOT / "main.py", *(ROOT / "services").glob("*.py")]
    failures = [failure for path in paths for failure in check(path)]
    print("\n".join(failures) if failures else f"PASS: {len(paths)} files; schema names valid")
    raise SystemExit(bool(failures))
