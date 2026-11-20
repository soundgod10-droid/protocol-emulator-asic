"""Instrumented copies of the line-helper RTL for the unreachability proofs of coverage points.

Usage: python formal/unreach/instrument.py [--exclusions coverage/exclusions.csv] [--teeth formal/unreach/teeth.csv]
                                           [--src src] [--out formal/unreach/build]

A coverage point leaves the chip figure (coverage/scope.py) only with a proof that the chip cannot reach it.
For each proof job named in the exclusion file (column proof; several jobs joined by +), this script writes
<out>/<job>/:
  pe_slu.v, pe_slu_loop.v  copies of src/ in which the statement of each point of the job starts with
                           "assert (1'b0);", so the assertion fails exactly when the statement is executed.
                           Verilog's own rules (the enclosing always block, if, case) give the condition; no
                           condition is written by hand. For a branch point of an if (Verilator branch/if,
                           branch/else, and the line points of an if or else block) the assertion opens that
                           branch (an absent else is added); a case item's statement and an always block's body
                           are opened the same way. An operator branch (?:, Verilator cond_then, cond_else) has
                           no statement of its own: its row gives the condition (column cond), and
                           "if (cond) assert (1'b0);" goes in front of the statement that holds the operator, or
                           into an always @* block after a continuous assignment.
                           Every insertion stays on its line, so the line numbers are those of src/.
                           Two more edits, both giving the copy more behaviour than the chip has, so that a
                           proof still holds for the chip: the falling-edge sample of DIN (din_f1) takes a free
                           value in every cycle, so one clock edge per proof step covers both edges (as
                           formal/slu_fv.v does); and the invariants of the fixed configuration of the helper
                           copy are asserted (proved with the points, never assumed).
  <job>.sby                k-induction of the copy inside formal/unreach/unreach_fv.v, with the SPEC of the job
                           (a job name that ends in usb: SPEC 1, FULL 0; in eth: SPEC 2, FULL 1).
  points.txt               every inserted text with its line, for review.
The recorded source text of each point (column source) must equal its line in src/; else the row is stale and
the script fails. A tooth (--teeth, same columns) is a point that the tests reach: its job must fail, which shows
that a wrong exclusion is caught.
"""

import argparse
import csv
import os
import re
import sys

UNREACH = "assert (1'b0);"
TOKEN = re.compile(r"""
    (?P<id>[A-Za-z_][A-Za-z0-9_$]*)
  | (?P<num>\d*'[sS]?[bodhBODH][0-9a-fA-FxXzZ_?]+|\d+)
  | (?P<sys>\$[A-Za-z_][A-Za-z0-9_$]*)
  | (?P<dir>`[A-Za-z_][A-Za-z0-9_]*)
  | (?P<str>"(?:[^"\\]|\\.)*")
  | (?P<op><<<|>>>|===|!==|<=|>=|==|!=|&&|\|\||<<|>>|~&|~\||~\^|\^~|\+:|-:|->|\*\*|\S)
""", re.X)

# Fixed configuration of the dedicated helper forms (src/pe_slu.v: reset to F_*, written only for SPEC 0; in
# SPEC 1 also SEL, MODE bits 0, 1, 3, 4, 9, TXPER[7:0] and LTC[12:8]; SEL is writable in both).
CFG = ["mode", "txper", "loop0", "loop1", "loop2", "loop3", "pat", "pat2", "pa", "ra", "crcb", "crcw", "tail",
       "lta", "ltb", "ltc", "sq", "lp"]
SPEC1_MASK = {"mode": "16'h021B", "txper": "16'h00FF", "ltc": "16'h1F00"}


def invariants():
    eq2 = " ".join("assert (c_%s == F_%s);" % (r, r.upper()) for r in CFG)
    eq1 = " ".join(("assert ((c_%s & ~%s) == (F_%s & ~%s));" % (r, SPEC1_MASK[r], r.upper(), SPEC1_MASK[r]))
                   if r in SPEC1_MASK else "assert (c_%s == F_%s);" % (r, r.upper()) for r in CFG)
    return "always @* begin if (SPEC == 2) begin %s end if (SPEC == 1) begin %s end end " % (eq2, eq1)


class Source:
    """A Verilog file with its tokens and the statements of its always blocks, functions and tasks."""

    def __init__(self, text):
        self.text = text
        clean = re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)
        clean = re.sub(r"//[^\n]*", lambda m: " " * len(m.group(0)), clean)
        self.line_start = [0] + [m.end() for m in re.finditer("\n", text)]
        self.toks = []
        for m in TOKEN.finditer(clean):
            line = self.line_of(m.start())
            self.toks.append((m.group(0), line, m.start() - self.line_start[line - 1] + 1, m.start(), m.end()))
        self.ifs, self.items, self.blocks, self.simple = {}, [], {}, []
        i = 0
        while i < len(self.toks):
            t = self.toks[i][0]
            if t in ("always", "initial"):
                i = self.stmt(i)
            elif t in ("function", "task"):
                j = i
                while self.toks[j][0] != ";":
                    j += 1
                j += 1
                end = "end" + t
                while self.toks[j][0] != end:
                    j = self.stmt(j)
                self.functions.append((self.toks[i][1], self.toks[j][1]))
                i = j + 1
            else:
                i += 1

    def line_of(self, off):
        lo, hi = 0, len(self.line_start) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.line_start[mid] <= off:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    def t(self, i):
        return self.toks[i][0]

    def match(self, i, a, b):
        assert self.t(i) == a, "expected %s at line %d" % (a, self.toks[i][1])
        depth = 0
        while True:
            if self.t(i) == a:
                depth += 1
            elif self.t(i) == b:
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1

    def stmt(self, i):
        """Index after the statement that starts at token i (records ifs, case items, blocks)."""
        t = self.t(i)
        if t == "begin":
            j = i + 1
            if self.t(j) == ":":       # named block
                j += 2
            while self.t(j) != "end":
                j = self.stmt(j)
            j += 1
            if j < len(self.toks) and self.t(j) == ":":
                j += 2
            return j
        if t == "if":
            c = self.match(i + 1, "(", ")")
            then_end = self.stmt(c)
            rec = {"if": i, "then": (c, then_end), "else_kw": None, "else": None}
            j = then_end
            if self.t(j) == "else":
                rec["else_kw"] = j
                j = self.stmt(j + 1)
                rec["else"] = (then_end + 1, j)
            self.ifs[i] = rec
            return j
        if t in ("case", "casez", "casex"):
            j = self.match(i + 1, "(", ")")
            while self.t(j) != "endcase":
                label, colon = j, None
                if self.t(j) == "default":
                    j += 1
                    if self.t(j) == ":":
                        colon = j
                        j += 1
                else:
                    depth = 0
                    while not (self.t(j) == ":" and depth == 0):
                        depth += self.t(j) in "([{"
                        depth -= self.t(j) in ")]}"
                        j += 1
                    colon = j
                    j += 1
                end = self.stmt(j)
                self.items.append({"label": label, "colon": colon, "stmt": (j, end)})
                j = end
            return j + 1
        if t == "for":
            return self.stmt(self.match(i + 1, "(", ")"))
        if t in ("always", "initial"):
            j = i + 1
            if self.t(j) == "@":
                j += 1
                j = j + 1 if self.t(j) == "*" else self.match(j, "(", ")")
            end = self.stmt(j)
            self.blocks[i] = (j, end)
            return end
        if t == ";":
            return i + 1
        j, depth = i, 0
        while not (self.t(j) == ";" and depth == 0):
            depth += self.t(j) in "([{"
            depth -= self.t(j) in ")]}"
            j += 1
        self.simple.append((i, j + 1))
        return j + 1

    def at(self, line, col):
        for k, tok in enumerate(self.toks):
            if tok[1] == line and tok[2] == col:
                return k
        return None


class Edits:
    def __init__(self, src):
        self.src = src
        self.ins = []          # (offset, sequence, text)

    def add(self, off, text):
        self.ins.append((off, len(self.ins), text))

    def open_branch(self, rng):
        a, b = rng
        s = self.src
        if s.t(a) == "begin":
            self.add(s.toks[a][4], " " + UNREACH)
        else:
            self.add(s.toks[a][3], "begin " + UNREACH + " ")
            self.add(s.toks[b - 1][4], " end")

    def add_else(self, rec):
        s = self.src
        a, b = rec["then"]
        if s.t(a) != "begin":          # wrap the then branch, so the new else cannot bind to an inner if
            self.add(s.toks[a][3], "begin ")
            self.add(s.toks[b - 1][4], " end")
        self.add(s.toks[b - 1][4], " else begin " + UNREACH + " end")

    def apply(self):
        text = self.src.text
        for off, _seq, ins in sorted(self.ins, key=lambda x: (x[0], -x[1]), reverse=True):
            text = text[:off] + ins + text[off:]
        return text


def place(src, edits, kind, comment, line, col, cond):
    """Put the assertion of one point into the edit list; returns a short description."""
    k = src.at(line, col)
    if cond:
        # operator branch: in front of the procedural statement that holds the operator, or after the
        # continuous assignment.
        for a, b in src.simple:
            if src.toks[a][1] <= line <= src.toks[b - 1][1] and (k is None or a <= k < b):
                edits.add(src.toks[a][3], "begin if (%s) %s " % (cond, UNREACH))
                edits.add(src.toks[b - 1][4], " end")
                return "procedural operator branch, condition %s" % cond
        # continuous assignment: find the ; that ends the statement on or after the line
        for tok in src.toks:
            if tok[1] >= line and tok[0] == ";":
                edits.add(tok[4], " always @* if (%s) %s" % (cond, UNREACH))
                return "continuous operator branch, condition %s" % cond
        raise ValueError("no statement for the operator branch")
    if kind == "branch" and comment == "else":
        k = src.at(line, col - 1)
        rec = src.ifs.get(k)
        if rec is None:
            raise ValueError("no if at column %d" % (col - 1))
        if rec["else"]:
            edits.open_branch(rec["else"])
            return "else branch of the if at column %d" % (col - 1)
        edits.add_else(rec)
        return "added else of the if at column %d" % (col - 1)
    if k is None:
        raise ValueError("no token at line %d column %s" % (line, col))
    if k in src.ifs and (kind, comment) in (("branch", "if"), ("line", "if"), ("line", "elsif")):
        edits.open_branch(src.ifs[k]["then"])
        return "then branch of the if"
    for rec in src.ifs.values():
        if rec["else_kw"] == k:
            edits.open_branch(rec["else"])
            return "else branch (else at this column)"
    if kind == "line" and comment == "else":
        # Verilator's else block can be named by the if that forms the whole else branch (else if)
        for rec in src.ifs.values():
            if rec["else"] and rec["else"][0] == k:
                edits.open_branch(rec["else"])
                return "else branch whose statement starts here"
    if k in src.blocks:
        edits.open_branch(src.blocks[k])
        return "body of the always block"
    for it in src.items:
        if it["label"] == k or it["stmt"][0] == k:
            edits.open_branch(it["stmt"])
            return "statement of the case item"
    raise ValueError("token %r at line %d column %s is no branch, case item or block" % (src.t(k), line, col))


def read_rows(path):
    if not path or not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return [r for r in csv.DictReader(f) if r.get("point", "").strip() and not r["point"].startswith("#")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exclusions", default="coverage/exclusions.csv")
    ap.add_argument("--teeth", default="formal/unreach/teeth.csv")
    ap.add_argument("--src", default="src")
    ap.add_argument("--out", default="formal/unreach/build")
    ap.add_argument("--depth", type=int, default=4)
    args = ap.parse_args()
    here = os.path.dirname(os.path.abspath(__file__))
    files = {f: open(os.path.join(args.src, f), encoding="utf-8").read() for f in ("pe_slu.v", "pe_slu_loop.v")}
    lines = {f: t.splitlines() for f, t in files.items()}
    jobs = {}
    errors = []
    for path, tooth in ((args.exclusions, False), (args.teeth, True)):
        for row in read_rows(path):
            pid = row["point"].strip()
            copy, f, line, col, kind, comment = pid.split(":", 5)
            line = int(line)
            text = lines[f][line - 1].strip() if 0 < line <= len(lines[f]) else None
            if text != row.get("source", "").strip():
                errors.append("%s: source line changed or missing (stale row): %r" % (pid, text))
                continue
            for job in row["proof"].strip().split("+"):
                jobs.setdefault(job, {"tooth": tooth, "points": []})["points"].append(
                    (pid, f, line, int(col), kind, comment, row.get("cond", "").strip()))
    for job, spec in jobs.items():
        if not (job.endswith("usb") or job.endswith("eth")):
            errors.append("job %s: the name must end in usb (SPEC 1) or eth (SPEC 2)" % job)
    if errors:
        print("\n".join("ERROR: " + e for e in errors))
        return 1

    for job, spec in sorted(jobs.items()):
        sp = 1 if job.endswith("usb") else 2
        out = os.path.join(args.out, job)
        os.makedirs(out, exist_ok=True)
        srcs = {f: Source(t) for f, t in files.items()}
        edits = {f: Edits(s) for f, s in srcs.items()}
        report = []
        for pid, f, line, col, kind, comment, cond in spec["points"]:
            try:
                how = place(srcs[f], edits[f], kind, comment, line, col, cond)
            except (ValueError, AssertionError, IndexError) as e:
                errors.append("%s: %s" % (pid, e))
                continue
            report.append((pid, how))
        # environment: free falling-edge DIN sample; configuration invariants before endmodule of pe_slu
        s = srcs["pe_slu.v"]
        e = edits["pe_slu.v"]
        m1 = list(re.finditer(r"din_f1 <= din_pin;", s.text))
        m2 = list(re.finditer(r"reg din_f1;", s.text))
        m3 = [m.start() for m in re.finditer(r"^endmodule", s.text, re.M)]
        if len(m1) != 1 or len(m2) != 1 or len(m3) != 1:
            errors.append("pe_slu.v: din_f1 sample, its declaration or endmodule not found once")
            continue
        e.add(m1[0].start() + len("din_f1 <= "), "din_f1_free; // ")
        e.add(m2[0].end(), " (* anyseq *) wire din_f1_free;")
        e.add(m3[0], invariants())
        n_inv = len(CFG)
        for f in files:
            new = edits[f].apply()
            if new.count("\n") != files[f].count("\n"):
                errors.append("%s %s: an insertion changed the line count" % (job, f))
            open(os.path.join(out, f), "w", encoding="utf-8").write(new)
        wrapper = os.path.relpath(os.path.join(here, "unreach_fv.v"), out).replace("\\", "/")
        n = len(report) + n_inv
        sby = "\n".join([
            "# %s: %s of %d coverage point(s) of coverage/exclusions.csv%s (formal/unreach/instrument.py)."
            % (job, "expected counterexample" if spec["tooth"] else "unreachability", len(report),
               " (tooth: the tests reach it)" if spec["tooth"] else ""),
            "[options]", "mode prove", "depth %d" % args.depth, "", "[engines]", "smtbmc z3", "",
            "[script]", "read -formal -sv pe_slu_loop.v pe_slu.v unreach_fv.v",
            "hierarchy -top unreach_fv -chparam SPEC %d" % sp, "prep -top unreach_fv",
            "# Stop with an error if an assertion was removed: %d points and %d configuration invariants." % (
                len(report), n_inv),
            "select -assert-min %d t:$check t:$assert" % n, "",
            "[files]", "pe_slu_loop.v", "pe_slu.v", wrapper, ""])
        open(os.path.join(out, job + ".sby"), "w", encoding="utf-8").write(sby)
        with open(os.path.join(out, "points.txt"), "w", encoding="utf-8") as fh:
            for pid, how in report:
                f, line = pid.split(":")[1], int(pid.split(":")[2])
                fh.write("%s\n  %s\n  src : %s\n  copy: %s\n" % (pid, how, lines[f][line - 1].strip(),
                                                                    open(os.path.join(out, f), encoding="utf-8")
                                                                    .read().splitlines()[line - 1].strip()))
        print("%s: SPEC %d, %d point(s)%s" % (job, sp, len(report), ", tooth" if spec["tooth"] else ""))
    for err in errors:
        print("ERROR: " + err)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
