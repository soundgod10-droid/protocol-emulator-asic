"""Instrumented copies of the line-helper RTL for the unreachability proofs of coverage points.

Usage: python formal/unreach/instrument.py [--exclusions coverage/exclusions.csv] [--teeth formal/unreach/teeth.csv]
                                           [--holes coverage_holes.csv --mode cover] [--src src]
                                           [--out formal/unreach/build]

A coverage point leaves the chip figure (coverage/scope.py) only with a proof that the chip cannot reach it.
For each proof job named in the exclusion file (column proof; several jobs joined by +), this script writes
<out>/<job>/:
  pe_slu.v, pe_slu_loop.v  copies of src/ in which the statement of each point of the job starts with a labelled
                           "assert (1'b0);" (pt_<n>), so the assertion fails exactly when the statement is executed.
                           Verilog's own rules (the enclosing always block, if, case) give the condition; no
                           condition is written by hand. Verilator names a point by line and column: an if branch
                           by the column of the if keyword (its else by that column + 1; the line point of an else
                           block also sits on the line of its if, at the column + 1), a case item
                           by the column of its colon (or of default). The assertion opens that branch (an absent
                           else is added) or that case item's statement.
                           An operator branch (?:, Verilator cond_then / cond_else) has no statement of its own; its
                           column is that of the then-operand (its operator for a binary expression, its ? for a
                           nested ?:), cond_else that column + 1. The condition is taken from the source text: the
                           operator's own condition, and those of the ?: operators it sits in (then arm: true, else
                           arm: false). "if (cond) assert (1'b0);" goes in front of the statement that holds the
                           operator, or into an always @* block after a continuous assignment. A point inside a
                           function needs its condition written in formal/unreach/conds.csv (it is checked in an
                           always @* block before endmodule).
                           Every insertion stays on its line, so the line numbers are those of src/.
                           Two more edits, both giving the copy more behaviour than the chip has, so that a proof
                           still holds for the chip: the falling-edge sample of DIN (din_f1) takes a free value in
                           every cycle, so one clock edge per proof step covers both edges (as formal/slu_fv.v);
                           and (prove mode) the invariants of the fixed configuration of the helper copy are
                           asserted, proved with the points, never assumed.
  <job>.sby                k-induction (prove mode) or bounded cover (cover mode: cover (1'b1) in place of the
                           assertion, for the triage of the not-hit points: a reached cover is a reachable point)
                           of the copy inside formal/unreach/unreach_fv.v, with the SPEC of the job (a job name
                           that ends in usb: SPEC 1, FULL 0; in eth: SPEC 2, FULL 1).
  points.txt               every inserted text with its label and line, for review.
An assertion on a branch whose condition is a constant of the parameters (SPEC, FULL) is removed when the
copy is elaborated; checks.txt (written by the job) lists the assertions that remain, so the report can say
which points the solver proved and which the elaboration removed.
The recorded source text of each point (column source) must equal its line in src/; else the row is stale and
the script fails. A tooth (--teeth, same columns) is a point that the tests reach: its job must fail, which
shows that a wrong exclusion is caught.
"""

import argparse
import csv
import os
import re
import sys

TOKEN = re.compile(r"""
    (?P<id>[A-Za-z_][A-Za-z0-9_$]*)
  | (?P<num>\d*'[sS]?[bodhBODH][0-9a-fA-FxXzZ_?]+|\d+)
  | (?P<sys>\$[A-Za-z_][A-Za-z0-9_$]*)
  | (?P<dir>`[A-Za-z_][A-Za-z0-9_]*)
  | (?P<str>"(?:[^"\\]|\\.)*")
  | (?P<op><<<|>>>|===|!==|<=|>=|==|!=|&&|\|\||<<|>>|~&|~\||~\^|\^~|\+:|-:|->|\*\*|\S)
""", re.X)
OPEN, CLOSE = "([{", ")]}"
TOP = {"=", "<=", ";", "begin", "else", "assign"}     # an expression starts after these

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
        self.ifs, self.items, self.blocks, self.simple, self.functions = {}, [], {}, [], []
        i = 0
        while i < len(self.toks):
            t = self.toks[i][0]
            if t in ("always", "initial"):
                i = self.stmt(i)
            elif t in ("function", "task"):
                j = i
                while self.t(j) != ";":
                    j += 1
                j += 1
                end = "end" + t
                while self.t(j) != end:
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

    def text_of(self, a, b):
        return " ".join(self.t(i) for i in range(a, b))

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
            if self.t(j) == ":":           # named block
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
                        depth += self.t(j) in OPEN
                        depth -= self.t(j) in CLOSE
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
            clocked = any(self.t(x) in ("posedge", "negedge") for x in range(i + 1, j))
            self.blocks[i] = (j, end, clocked)
            return end
        if t == ";":
            return i + 1
        j, depth = i, 0
        while not (self.t(j) == ";" and depth == 0):
            depth += self.t(j) in OPEN
            depth -= self.t(j) in CLOSE
            j += 1
        self.simple.append((i, j + 1))
        return j + 1

    def at(self, line, col):
        for k, tok in enumerate(self.toks):
            if tok[1] == line and tok[2] == col:
                return k
        return None

    def clocked_at(self, k):
        """True if token k is inside an always block with a clock edge in its event list."""
        return any(a <= k < b and c for a, b, c in self.blocks.values())

    # ---- the ?: operator -------------------------------------------------------------------------
    def then_range(self, q):
        """Tokens of the then-operand of the ?: whose ? is token q: (q + 1, index of its :)."""
        j, depth, nest = q + 1, 0, 0
        while True:
            t = self.t(j)
            if t in OPEN:
                depth += 1
            elif t in CLOSE:
                if depth == 0:
                    return None
                depth -= 1
            elif depth == 0 and t == "?":
                nest += 1
            elif depth == 0 and t == ":":
                if nest == 0:
                    return q + 1, j
                nest -= 1
            elif depth == 0 and t == ";":
                return None
            j += 1

    def cond_start(self, q):
        """First token of the condition of the ?: whose ? is token q (?: binds weakest)."""
        j, depth = q - 1, 0
        while True:
            t = self.t(j)
            if t in CLOSE:
                depth += 1
            elif t in OPEN:
                if depth == 0:
                    return j + 1
                depth -= 1
            elif depth == 0 and (t in TOP or t in ("?", ":", ",")):
                return j + 1
            j -= 1

    def question_of(self, colon):
        """The ? that matches the : at token colon, or None (a case label or a part select)."""
        j, depth, need = colon - 1, 0, 1
        while j >= 0:
            t = self.t(j)
            if t in CLOSE:
                depth += 1
            elif t in OPEN:
                if depth == 0:
                    return None
                depth -= 1
            elif depth == 0 and t == ":":
                need += 1
            elif depth == 0 and t == "?":
                need -= 1
                if need == 0:
                    return j
            elif depth == 0 and t in TOP:
                return None
            j -= 1
        return None

    def path(self, start):
        """Conditions of the ?: arms that hold the expression starting at token start: [(text, polarity)]."""
        j, depth = start - 1, 0
        while j >= 0:
            t = self.t(j)
            if t in CLOSE:
                depth += 1
            elif t in OPEN:
                if depth > 0:
                    depth -= 1        # at depth 0 the group ends: its context is the enclosing expression
            elif depth == 0 and t == "?":
                cs = self.cond_start(j)
                return self.path(cs) + [(self.text_of(cs, j), True)]
            elif depth == 0 and t == ":":
                q = self.question_of(j)
                if q is None:
                    return []
                cs = self.cond_start(q)
                return self.path(cs) + [(self.text_of(cs, q), False)]
            elif depth == 0 and t in TOP:
                return []
            j -= 1
        return []

    def ternary_condition(self, k, then):
        """Condition under which the ?: whose then-operand holds token k takes its then (else) arm."""
        best = None
        for q, tok in enumerate(self.toks):
            if tok[0] != "?":
                continue
            r = self.then_range(q)
            if r and r[0] <= k < r[1] and (best is None or r[1] - r[0] < best[1][1] - best[1][0]):
                best = (q, r)
        if best is None:
            raise ValueError("no ?: whose then-operand holds this column")
        q = best[0]
        cs = self.cond_start(q)
        terms = self.path(cs) + [(self.text_of(cs, q), then)]
        return " && ".join(("(%s)" if pol else "!(%s)") % txt for txt, pol in terms)


class Edits:
    def __init__(self, src, hit):
        self.src = src
        self.hit = hit
        self.pairs = []        # (start offset, end offset, open text, close text, tail after the close, sequence)

    def pair(self, start, end, open_text, close_text, tail=""):
        self.pairs.append((start, end, open_text, close_text, tail, len(self.pairs)))

    def add(self, off, text):
        self.pair(off, off, text, "")

    def open_branch(self, rng, stmt):
        a, b = rng
        s = self.src
        if s.t(a) == "begin":
            self.add(s.toks[a][4], " " + stmt)
        else:
            self.pair(s.toks[a][3], s.toks[b - 1][4], "begin %s " % stmt, " end")

    def add_else(self, rec, stmt):
        s = self.src
        a, b = rec["then"]
        # the then branch is wrapped (so the new else cannot bind to an inner if), the else follows the wrap
        wrap = s.t(a) != "begin"
        self.pair(s.toks[a][3], s.toks[b - 1][4], "begin " if wrap else "", " end" if wrap else "",
                  " else begin %s end" % stmt)

    def apply(self):
        # Nesting: a pair that starts earlier or ends later is outside; of two pairs on the same range the one
        # with a tail (an added else) is outside, so its else follows everything inserted in its then branch.
        order = sorted(self.pairs, key=lambda p: (p[0], -p[1], 0 if p[4] else 1, p[5]))
        events = []
        for rank, (start, end, op, cl, tail, _seq) in enumerate(order):
            if op:
                events.append((start, 1, rank, op))
            if cl or tail:
                events.append((end, 0, -rank, cl + tail))     # at one offset closes come first, inner first
        text, out, prev = self.src.text, [], 0
        for off, _kind, _r, ins in sorted(events):
            out.append(text[prev:off])
            out.append(ins)
            prev = off
        out.append(text[prev:])
        return "".join(out)


def place(src, edits, kind, comment, line, col, cond, label, flags):
    """Put the assertion (or cover) of one point into the edit list; returns a short description.

    In a combinational block the statement is the labelled assertion itself. In a clocked block (an always
    block with an asynchronous reset: Yosys's async2sync takes no assertion with two triggers) it sets the
    sticky flag <label>_f (initial value 0), and the flag is checked in an always @* block (flags)."""

    def S(k, clocked=None):
        if src.clocked_at(k) if clocked is None else clocked:
            flags.append(label)
            return "%s_f <= 1'b1;" % label
        return "%s: %s" % (label, edits.hit)

    in_function = any(a <= line <= b for a, b in src.functions)
    if in_function:
        if not cond:
            raise ValueError("point inside a function: its condition must be given in formal/unreach/conds.csv")
        # the condition is checked before the endmodule of the module that holds the function
        ends = [tok for tok in src.toks if tok[0] == "endmodule" and tok[1] > line]
        edits.add(ends[0][3], "always @* if (%s) %s: %s " % (cond, label, edits.hit))
        return "inside a function: always @* block before endmodule (line %d), condition %s" % (ends[0][1], cond)
    if comment in ("cond_then", "cond_else"):
        if not cond:
            k = src.at(line, col if comment == "cond_then" else col - 1)
            if k is None:
                raise ValueError("no token at the column of the operator branch")
            cond = src.ternary_condition(k, comment == "cond_then")
        else:
            k = src.at(line, col if comment == "cond_then" else col - 1)
        for a, b in src.simple:
            if src.toks[a][1] <= line <= src.toks[b - 1][1] and (k is None or a <= k < b):
                edits.pair(src.toks[a][3], src.toks[b - 1][4], "begin if (%s) %s: %s " % (cond, label, edits.hit),
                           " end")
                return "operator branch in a procedural statement, condition %s" % cond
        for j in range(k if k is not None else 0, len(src.toks)):
            if src.t(j) == ";":
                edits.add(src.toks[j][4], " always @* if (%s) %s: %s" % (cond, label, edits.hit))
                return "operator branch in a continuous assignment, condition %s" % cond
        raise ValueError("no statement for the operator branch")
    if (kind, comment) in (("branch", "else"), ("line", "else")):
        for k, rec in src.ifs.items():
            if src.toks[k][2] != col - 1:
                continue
            # branch/else and line/else (the else block) both sit on the line of their if, at its column + 1
            if src.toks[k][1] != line:
                continue
            if rec["else"]:
                edits.open_branch(rec["else"], label)
                return "else branch of the if at line %d column %d" % (src.toks[k][1], col - 1)
            edits.add_else(rec, label)
            return "added else of the if at line %d column %d" % (src.toks[k][1], col - 1)
        raise ValueError("no if at column %d for this else" % (col - 1))
    k = src.at(line, col)
    if k is None:
        raise ValueError("no token at line %d column %s" % (line, col))
    if k in src.ifs and kind in ("branch", "line"):
        edits.open_branch(src.ifs[k]["then"], label)
        return "then branch of the if"
    if k in src.blocks:
        edits.open_branch(src.blocks[k], label)
        return "body of the always block"
    for it in src.items:
        if it["colon"] == k or it["label"] == k:
            edits.open_branch(it["stmt"], label)
            return "statement of the case item"
    raise ValueError("token %r at line %d column %s is no branch, case item or block" % (src.t(k), line, col))


def read_rows(path):
    if not path or not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return [r for r in csv.DictReader(f) if r.get("point", "").strip() and not r["point"].startswith("#")]


def jobs_of(copy, prefix):
    if copy == "loop":
        return [prefix + "_loop_usb", prefix + "_loop_eth"]
    return [prefix + "_" + copy]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exclusions", default="coverage/exclusions.csv")
    ap.add_argument("--teeth", default="formal/unreach/teeth.csv")
    ap.add_argument("--holes", help="triage: every row of this hole list (coverage/scope.py --holes)")
    ap.add_argument("--conds", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "conds.csv"))
    ap.add_argument("--mode", choices=("prove", "cover"), default="prove")
    ap.add_argument("--src", default="src")
    ap.add_argument("--out", default="formal/unreach/build")
    ap.add_argument("--depth", type=int, default=0)
    args = ap.parse_args()
    depth = args.depth or (30 if args.mode == "cover" else 4)
    hit = "cover (1'b1);" if args.mode == "cover" else "assert (1'b0);"
    here = os.path.dirname(os.path.abspath(__file__))
    files = {f: open(os.path.join(args.src, f), encoding="utf-8").read() for f in ("pe_slu.v", "pe_slu_loop.v")}
    lines = {f: t.splitlines() for f, t in files.items()}
    conds = {r["point"].strip(): r["cond"].strip() for r in read_rows(args.conds)}
    rows = []
    if args.holes:
        for r in read_rows(args.holes):
            if r["type"] in ("line", "branch"):
                rows.append((r["point"], "+".join(jobs_of(r["copy"], "triage")), r["source"], "", False))
    else:
        for path, tooth in ((args.exclusions, False), (args.teeth, True)):
            for r in read_rows(path):
                rows.append((r["point"], r["proof"], r.get("source", ""), r.get("cond", ""), tooth))
    jobs, errors = {}, []
    for pid, proof, source, cond, tooth in rows:
        pid = pid.strip()
        copy, f, line, col, kind, comment = pid.split(":", 5)
        line = int(line)
        text = lines[f][line - 1].strip() if 0 < line <= len(lines[f]) else None
        if text != source.strip():
            errors.append("%s: source line changed or missing (stale row): %r" % (pid, text))
            continue
        for job in proof.strip().split("+"):
            if not (job.endswith("usb") or job.endswith("eth")):
                errors.append("job %s: the name must end in usb (SPEC 1) or eth (SPEC 2)" % job)
                continue
            jobs.setdefault(job, {"tooth": tooth, "points": []})["points"].append(
                (pid, f, line, int(col), kind, comment, cond.strip() or conds.get(pid, "")))
    if errors:
        print("\n".join("ERROR: " + e for e in errors))
        return 1

    for job, spec in sorted(jobs.items()):
        sp = 1 if job.endswith("usb") else 2
        out = os.path.join(args.out, job)
        os.makedirs(out, exist_ok=True)
        srcs = {f: Source(t) for f, t in files.items()}
        edits = {f: Edits(s, hit) for f, s in srcs.items()}
        report, module_level = [], []
        for n, (pid, f, line, col, kind, comment, cond) in enumerate(spec["points"]):
            label = "pt_%d" % n
            try:
                how = place(srcs[f], edits[f], kind, comment, line, col, cond, label, module_level)
            except (ValueError, AssertionError, IndexError) as e:
                errors.append("%s %s: %s" % (job, pid, e))
                continue
            report.append((label, pid, how))
        # environment: free falling-edge DIN sample; module-level checks and invariants before endmodule
        s, e = srcs["pe_slu.v"], edits["pe_slu.v"]
        m1 = list(re.finditer(r"din_f1 <= din_pin;\s*$", s.text, re.M))
        m2 = list(re.finditer(r"reg din_f1;", s.text))
        m3 = [m.start() for m in re.finditer(r"^endmodule", s.text, re.M)]
        if len(m1) != 1 or len(m2) != 1 or len(m3) != 1:
            errors.append("pe_slu.v: the din_f1 sample (at a line end), its declaration or endmodule not found once")
            continue
        e.add(m1[0].start() + len("din_f1 <= "), "din_f1_free; // ")
        e.add(m2[0].end(), " (* anyseq *) wire din_f1_free;")
        e.add(m3[0], "".join(module_level) + (invariants() if args.mode == "prove" else ""))
        for f in files:
            new = edits[f].apply()
            if new.count("\n") != files[f].count("\n"):
                errors.append("%s %s: an insertion changed the line count" % (job, f))
            open(os.path.join(out, f), "w", encoding="utf-8").write(new)
        wrapper = os.path.relpath(os.path.join(here, "unreach_fv.v"), out).replace("\\", "/")
        what = ("triage (cover) of %d not-hit coverage point(s)" % len(report) if args.mode == "cover" else
                "expected counterexample: tooth, %d point(s) that the tests reach" % len(report) if spec["tooth"] else
                "unreachability of %d coverage point(s) of coverage/exclusions.csv" % len(report))
        script = ["read -formal -sv pe_slu_loop.v pe_slu.v unreach_fv.v",
                  "hierarchy -top unreach_fv -chparam SPEC %d" % sp, "prep -top unreach_fv",
                  "tee -q -o checks.txt select -list t:$check t:$assert t:$cover"]
        if args.mode == "prove":
            script += ["# Stop with an error if the configuration invariants were removed (%d)." % len(CFG),
                       "select -assert-min %d t:$check t:$assert" % len(CFG)]
        sby = "\n".join(["# %s: %s (formal/unreach/instrument.py)." % (job, what),
                         "[options]", "mode %s" % args.mode, "depth %d" % depth, "",
                         "[engines]", "smtbmc z3", "", "[script]"] + script +
                        ["", "[files]", "pe_slu_loop.v", "pe_slu.v", wrapper, ""])
        open(os.path.join(out, job + ".sby"), "w", encoding="utf-8").write(sby)
        copy_lines = {f: open(os.path.join(out, f), encoding="utf-8").read().splitlines() for f in files}
        with open(os.path.join(out, "points.txt"), "w", encoding="utf-8") as fh:
            for label, pid, how in report:
                f, line = pid.split(":")[1], int(pid.split(":")[2])
                fh.write("%s %s\n  %s\n  src : %s\n  copy: %s\n" % (label, pid, how, lines[f][line - 1].strip(),
                                                                   copy_lines[f][line - 1].strip()))
        print("%s: SPEC %d, %d point(s), %s" % (job, sp, len(report), what))
    for err in errors:
        print("ERROR: " + err)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
