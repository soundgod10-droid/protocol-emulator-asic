# SPDX-License-Identifier: Apache-2.0
"""Protocol count from the registry flex/protocols.csv, by the rule of docs/counting.md.

What it does:
  For each row of the registry it checks the four conditions of docs/counting.md section 2:
    (a) fw/<program> assembles with asm/pe_asm.py (which stops at 512 program words);
    (b) the row names a public document (its issue and address) and a model, and the model, with every function and
        class that it reaches in the test files, uses nothing from asm/, model/ or fw/ (read from the source with ast);
    (c) every test of the column rtl_tests passed in the RTL results.xml files;
    (d) every test of the column gl_tests passed in the gate-level results.xml files.
  A protocol is in the strict count when one of its rows meets (a) to (d), in the broad count with (a) to (c). The
  script fails when a program in fw/ has no row, a row names a missing program, or a row claims more (column claim)
  than its evidence gives. It writes the table of counts as Markdown (job summary) and as JSON.

Usage: python flex/count.py --rtl F [F ...] [--gl F ...] [--note TEXT ...] [--summary FILE] [--out DIR]
"""

import argparse
import ast
import csv
import glob
import json
import os
import sys
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "asm"))
import pe_asm  # noqa: E402

FORBIDDEN = {"pe_asm", "pe_model"}  # the assembler and the reference model (asm/, model/)
SEARCH = [os.path.join(ROOT, "test"), os.path.join(ROOT, "test", "linemodel")]


# ----- condition (b): what a model reaches in the source -----

class Module:
    def __init__(self, path):
        self.path = path
        with open(path, encoding="utf-8") as f:
            self.tree = ast.parse(f.read(), path)
        self.defs, self.imports = {}, {}
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.defs[node.name] = node
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        self.defs[t.id] = node
            elif isinstance(node, ast.Import):
                for a in node.names:
                    self.imports[a.asname or a.name.split(".")[0]] = ("module", a.name, 0)
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    self.imports[a.asname or a.name] = ("from", node.module or "", a.name, node.level)


_modules = {}


def module(path):
    if path not in _modules:
        _modules[path] = Module(path)
    return _modules[path]


def resolve(name, level, here):
    """Path of a repository module imported as `name` (relative with level > 0), or None (not ours)."""
    base = [os.path.dirname(here)] if level else SEARCH
    parts = name.split(".") if name else []
    for d in base:
        p = os.path.join(d, *parts) if parts else d
        if os.path.isfile(p + ".py"):
            return p + ".py"
        if os.path.isfile(os.path.join(p, "__init__.py")):
            return os.path.join(p, "__init__.py")
    return None


def reads_fw(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            v = n.value.replace("\\", "/")
            if v == "fw" or v.startswith("fw/") or "/fw/" in v:
                return True
    return False


def model_faults(spec):
    """Faults of a model given as 'path::name' (a function or class) or 'path' (a whole module)."""
    path, _, name = spec.partition("::")
    full = os.path.join(ROOT, path)
    if not os.path.isfile(full):
        return [f"model file {path} not found"]
    faults, seen = [], set()

    def visit_import(mod, ident, attr=None):
        kind = mod.imports[ident]
        if kind[0] == "module":
            if kind[1].split(".")[0] in FORBIDDEN:
                faults.append(f"{os.path.relpath(mod.path, ROOT)}: uses {kind[1]}")
                return
            target = resolve(kind[1], 0, mod.path)
            if target and attr:
                visit(target, attr)
        else:
            src, orig, level = kind[1], kind[2], kind[3]
            if src.split(".")[0] in FORBIDDEN or orig in FORBIDDEN:
                faults.append(f"{os.path.relpath(mod.path, ROOT)}: uses {src}.{orig}")
                return
            target = resolve(src, level, mod.path)
            if target:
                sub = resolve(orig, 1, target) if os.path.basename(target) == "__init__.py" else None
                if sub:
                    visit(sub, attr)
                else:
                    visit(target, orig)

    def visit(p, ident):
        if (p, ident) in seen:
            return
        seen.add((p, ident))
        mod = module(p)
        if ident is None:
            nodes = list(mod.defs.values())
            for imp in list(mod.imports):
                visit_import(mod, imp)
        elif ident in mod.defs:
            nodes = [mod.defs[ident]]
        elif ident in mod.imports:
            visit_import(mod, ident)
            return
        else:
            return
        for node in nodes:
            if reads_fw(node):
                faults.append(f"{os.path.relpath(p, ROOT)}: {getattr(node, 'name', ident)} reads a file in fw/")
            for n in ast.walk(node):
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in mod.imports:
                    visit_import(mod, n.value.id, n.attr)
                elif isinstance(n, ast.Name):
                    if n.id in mod.imports and mod.imports[n.id][0] == "from":
                        visit_import(mod, n.id)
                    elif n.id in mod.imports:
                        if mod.imports[n.id][1].split(".")[0] in FORBIDDEN:
                            faults.append(f"{os.path.relpath(p, ROOT)}: uses {mod.imports[n.id][1]}")
                    elif n.id in mod.defs and n.id != ident:
                        visit(p, n.id)

    m = module(full)
    if name and name not in m.defs:
        return [f"{name} not found in {path}"]
    visit(full, name or None)
    return sorted(set(faults))


# ----- conditions (c) and (d): test results -----

def outcomes(files):
    out = {}
    for f in files:
        for tc in ET.parse(f).getroot().iter("testcase"):
            if tc.find("failure") is not None or tc.find("error") is not None:
                o = "fail"
            elif tc.find("skipped") is not None:
                o = "skip"
            else:
                o = "pass"
            out.setdefault(tc.get("name"), set()).add(o)
    return out


def passed(res, tests):
    names = [t for t in tests.split(";") if t]
    missing = [t for t in names if not ("pass" in res.get(t, set()) and "fail" not in res.get(t, set()))]
    return bool(names) and not missing, missing


# ----- the count -----

def count(rtl_files, gl_files):
    with open(os.path.join(ROOT, "flex", "protocols.csv"), newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    errors = []
    names = [r["program"] for r in rows]
    fw = sorted(os.path.basename(p) for p in glob.glob(os.path.join(ROOT, "fw", "*.s")))
    for p in fw:
        if names.count(p) != 1:
            errors.append(f"fw/{p}: {names.count(p)} rows in flex/protocols.csv (one is needed)")
    for p in names:
        if p not in fw:
            errors.append(f"flex/protocols.csv: row {p}, but fw/{p} does not exist")
    rtl, gl = outcomes(rtl_files), outcomes(gl_files)
    for r in rows:
        path = os.path.join(ROOT, "fw", r["program"])
        r["words"], r["a"] = None, False
        try:
            r["words"] = pe_asm.assemble_file(path).size
            r["a"] = True
        except (pe_asm.AsmError, OSError) as e:
            r["a_fault"] = str(e).splitlines()[0]
        doc_ok = all(r[k] not in ("", "-") for k in ("document", "document_id", "document_url"))
        if r["class"] == "protocol" and r["model"] not in ("", "-"):
            r["b_faults"] = model_faults(r["model"])
        else:
            r["b_faults"] = ["no model"]
        r["b"] = doc_ok and not r["b_faults"]
        r["c"], r["c_missing"] = passed(rtl, r["rtl_tests"])
        r["d"], r["d_missing"] = passed(gl, r["gl_tests"])
        protocol = r["class"] == "protocol"
        r["strict"] = protocol and r["a"] and r["b"] and r["c"] and r["d"]
        r["broad"] = protocol and r["a"] and r["b"] and r["c"]
        level = "strict" if r["strict"] else "broad" if r["broad"] else "none"
        rank = {"none": 0, "broad": 1, "strict": 2}
        if r["claim"] not in rank:
            errors.append(f"{r['program']}: claim '{r['claim']}' is not strict, broad or none")
        elif rank[r["claim"]] > rank[level]:
            why = []
            if not protocol:
                why.append(f"class {r['class']}")
            if not r["a"]:
                why.append(f"(a) {r.get('a_fault', 'does not assemble')}")
            if not r["b"]:
                why.append("(b) " + ("; ".join(r["b_faults"]) if r["b_faults"] else "no document"))
            if not r["c"]:
                why.append(f"(c) RTL tests not passed: {', '.join(r['c_missing']) or 'none named'}")
            if not r["d"] and r["claim"] == "strict":
                why.append(f"(d) gate-level tests not passed: {', '.join(r['d_missing']) or 'none named'}")
            errors.append(f"{r['program']}: claims {r['claim']}, evidence gives {level}: {'; '.join(why)}")
    protos = {}
    for r in rows:
        if r["class"] == "protocol":
            protos.setdefault(r["protocol"], []).append(r)
    strict = sorted(p for p, rs in protos.items() if any(r["strict"] for r in rs))
    broad = sorted(p for p, rs in protos.items() if any(r["broad"] for r in rs))
    result = {
        "strict": strict, "broad": broad,
        "programs_strict": sum(1 for r in rows if r["strict"]),
        "roles": {p: len(rs) for p, rs in protos.items()},
        "families_strict": sorted({r["family"] for r in rows if r["strict"]}),
        "device_interface_strict": sorted(p for p in strict if all(r["device_interface"] == "yes" for r in protos[p])),
        "sigrok_declared": sorted(p for p, rs in protos.items() if any(r["sigrok"] for r in rs)),
        "functions": sorted(r["program"] for r in rows if r["class"] == "function"),
        "examples": sorted(r["program"] for r in rows if r["class"] == "example"),
        "layered": sorted(r["program"] for r in rows if r["class"] == "layered"),
        "errors": errors,
        "rows": [{k: r[k] for k in ("program", "protocol", "class", "role", "tier", "words", "a", "b", "c", "d",
                                    "strict", "broad", "claim", "b_faults", "c_missing", "d_missing")} for r in rows],
    }
    return result, protos


def markdown(res, protos, notes):
    yes = lambda v: "yes" if v else "**no**"  # noqa: E731
    lines = ["## Protocol count (docs/counting.md)", ""]
    lines += [f"- {n}" for n in notes]
    lines += ["",
              f"**Strict: {len(res['strict'])} protocols** ({res['programs_strict']} programs). "
              f"**Broad: {len(res['broad'])} protocols.** Not counted: {len(res['functions'])} function, "
              f"{len(res['examples'])} outline programs, {len(res['layered'])} layered. "
              f"Families in the strict count: {len(res['families_strict'])}. Device interfaces in it: "
              f"{len(res['device_interface_strict'])}. Protocols with a sigrok decoder on their RTL pins (declared in "
              f"the registry, run by the docs workflow): {len(res['sigrok_declared'])}.", "",
              "| Protocol | Program (words) | Role | Tier | (a) fits | (b) model | (c) RTL | (d) gate level | Strict | Claim |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for p in sorted(protos, key=str.lower):
        for r in protos[p]:
            lines.append(f"| {p} | {r['program']} ({r['words']}) | {r['role']} | {r['tier']} | {yes(r['a'])} | "
                         f"{yes(r['b'])} | {yes(r['c'])} | {yes(r['d'])} | {yes(r['strict'])} | {r['claim']} |")
    if res["errors"]:
        lines += ["", "### Errors", ""] + [f"- {e}" for e in res["errors"]]
    return "\n".join(lines) + "\n"


def escape(text):
    """Text as the message of a workflow command (escapes % CR LF)."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--rtl", nargs="+", required=True)
    ap.add_argument("--gl", nargs="*", default=[])
    ap.add_argument("--note", action="append", default=[])
    ap.add_argument("--summary")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    res, protos = count(a.rtl, a.gl)
    md = markdown(res, protos, a.note)
    print(md)
    if a.summary:
        with open(a.summary, "a", encoding="utf-8") as f:
            f.write(md)
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        with open(os.path.join(a.out, "count.md"), "w", encoding="utf-8") as f:
            f.write(md)
        with open(os.path.join(a.out, "count.json"), "w", encoding="utf-8") as f:
            json.dump(res, f, indent=1)
    line = (f"strict {len(res['strict'])} protocols, broad {len(res['broad'])}, sigrok declared "
            f"{len(res['sigrok_declared'])}: {', '.join(res['strict'])}")
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::notice title=protocol count::{line}")
        # A job summary is shown only to signed-in readers; the run's annotations are public, so the table is one too.
        print(f"::notice title=protocol count table::{escape(md)}")
        for e in res["errors"]:
            print(f"::error title=protocol count::{e}")
    print(line)
    return 1 if res["errors"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
