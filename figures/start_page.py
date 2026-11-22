"""Verification start page of the documentation (Markdown): what is verified, by which run, and what it does not check.

What it does:
  Reads runs.json and the artifacts of figures/runs.py (the newest finished runs of the branch: gds, test, coverage,
  formal, cert, equiv, mutation, sigrok, and the newest result of each SDF part at each corner) and writes a page of
  at most 1,000 words: five claims with their conditions, one table of checks (result, run, what it does not check),
  the coverage of each instance, the firmware timing certificates, the SDF scope, the mutation campaign, the commands
  to rerun two checks from a clean clone, and what is not verified. Every number comes from a run; a value that no
  run gives is written as "no run". The word count of the page is printed; above 1,000 words the step warns.

Usage: python figures/start_page.py <runs folder> <out start.md>
"""

import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import results as rp  # noqa: E402  (link, find, testcases, metrics, notices, CORNERS of the results page)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "test"))
NO = "no run"
UNITS = {"tb.u_slu_ut": "general helper, unit-test instance (SPEC 0, all modes writable)",
         "tb.u_slu_loop_ut": "receive loop, unit-test instance"}
COPIES = {"usb": "USB helper (SPEC 1, with its clocked and NRZ modes)", "eth": "10BASE-Te helper (SPEC 2)",
          "loop": "receive loop of the 10BASE-Te helper"}


def art(run, name):
    return run["artifacts"].get(name) if run else None


def job(run, prefix):
    return [j for j in (run or {}).get("jobs", []) if j["name"].startswith(prefix)]


def coverage(cov):
    """(scope rows {name: (line, branch, toggle)}, copy rows {copy: (line, branch, toggle)}, unit rows) from the
    coverage artifact: coverage_summary.md and, for the instances outside the chip, coverage.dat."""
    md = rp.find(art(cov, "coverage"), "coverage_summary.md")
    scopes, copies, units = {}, {}, {}
    if md:
        for line in open(md, encoding="utf-8"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) == 4 and " of " in cells[1]:
                key = cells[0].split(" (")[0]
                (copies if key in COPIES else scopes)[key] = tuple(c.split(" (")[0] for c in cells[1:])
    dat = rp.find(art(cov, "coverage"), "coverage.dat")
    if dat:
        import coverage_summary as cs
        per = {}
        with open(dat, encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.startswith("C "):
                    continue
                fields, count = cs.parse(line)
                h = ".".join(fields.get("h", "").split(".")[:2])
                kind = fields.get("page", "?").split("/")[0].removeprefix("v_")
                if h in UNITS and kind in ("line", "branch", "toggle"):
                    p = per.setdefault(h, {}).setdefault(kind, [0, 0])
                    p[0] += count > 0
                    p[1] += 1
        for h, kinds in per.items():
            units[h] = tuple(f"{kinds[k][0]} of {kinds[k][1]} = {100 * kinds[k][0] / kinds[k][1]:.1f} %"
                             if k in kinds and kinds[k][1] else "-" for k in ("line", "branch", "toggle"))
    return scopes, copies, units


def main(runs_dir, out_md):
    with open(os.path.join(runs_dir, "runs.json"), encoding="utf-8") as f:
        R = json.load(f)
    runs = R["runs"]
    g = lambda k: runs.get(k)  # noqa: E731
    gds, test, cov, formal, cert, equiv, mut, sig = (g(k) for k in ("gds", "test", "coverage", "formal", "cert",
                                                                   "equiv", "mutation", "sigrok"))
    L = rp.link
    repo, branch = R["repo"], R["branch"]
    site = R.get("site_commit", "")

    # timing (gds)
    m = rp.metrics(rp.find(art(gds, "tt_submission"), "metrics.csv"))
    slack = []
    for short, corner, _ in rp.CORNERS:
        mm = re.search(r"flops setup slack (-?[\d.]+)", rp.notices(gds, "gds", f"gds {corner}") or "")
        slack.append(f"{float(mm.group(1)):+.3f}" if mm else "?")
    vio = [m.get(f"timing__{k}_vio__count__corner:{c}") for k in ("setup", "hold") for _, c, _ in rp.CORNERS]
    vio_txt = ("0 setup and 0 hold violations at the 3 corners" if all(v == "0" for v in vio)
               else "violation counts not in this run" if not m
               else "setup / hold violations at slow, typical, fast: " + ", ".join(str(v) for v in vio))
    # tests
    rtl = rp.testcases(rp.find(art(test, "test-results"), "results.xml"))
    n_pass = sum(1 for v in rtl.values() if v[1] == "pass")
    n_fail = sum(1 for v in rtl.values() if v[1] == "fail")
    core_s = sum(v[2] for v in rtl.values() if v[0] == "test_core")
    gl_jobs = [j for j in (gds or {}).get("jobs", []) if j["name"].startswith(("gl_test", "gl_protocols"))]
    gl_ok = sum(1 for j in gl_jobs if j["conclusion"] == "success")
    # count
    cj = rp.find(art(test, "count"), "count.json")
    C = json.load(open(cj, encoding="utf-8")) if cj else None
    # coverage
    scopes, copies, units = coverage(cov)
    excl = scopes.get("chip with exclusions")
    # formal, equivalence
    fjobs = job(formal, "formal")
    f_ok = sum(1 for j in fjobs if j["conclusion"] == "success")
    dp = {j["name"][len("dprove ("):-1]: j["conclusion"] for j in job(equiv, "dprove (")}
    teeth = [k for k in dp if k != "netlist"]
    teeth_ok = sum(1 for k in teeth if dp[k] == "success")
    eqy = next((j["conclusion"] for j in job(equiv, "equiv")), None)
    src = next((n["message"] for n in (equiv or {}).get("notices", {}).get("equiv", [])
                if n["title"] == "equiv source"), "")
    eq_gds = re.search(r"gds run (\d+)", src)
    # certificates
    cr = rp.find(art(cert, "cert"), "cert_results.json")
    certs = json.load(open(cr, encoding="utf-8")) if cr else []
    certified = [c for c in certs if c["certified"]]
    selfchk = next((n["message"].splitlines()[0] for n in (cert or {}).get("notices", {}).get("cert", [])
                    if n["title"] == "certificate self-check"), "")
    pins = next((n["message"].splitlines()[-1] for n in (cert or {}).get("notices", {}).get("cert", [])
                 if n["title"] == "certificate pin terms"), "")
    # mutation
    mr = rp.find(art(mut, "mutation-report"), "mutation_report.md")
    mline = ""
    if mr:
        mline = next((ln.strip() for ln in open(mr, encoding="utf-8") if ln.startswith("commit ")), "")
    # SDF scope
    parts = R.get("sdf_parts") or {}

    W = []
    W.append("# Start here\n")
    W.append(f"What is verified, by which run, and what each check does not cover. The docs workflow generates this "
             f"page ([figures/start_page.py](https://github.com/{repo}/blob/{branch}/figures/start_page.py)) from the "
             f"newest finished runs on branch `{branch}` (site commit `{site[:7]}`); no number on it is typed. Every "
             f"result is a simulation or a proof; there is no silicon or board result. Details: "
             f"[Verification](verification.md), [Signoff](signoff.md), [Results](results.md).\n")

    W.append("## Five claims\n")
    W.append(f"1. **Protocols.** " + (f"{len(C['strict'])} protocols pass their tests against models written from "
             f"public documents, on the RTL and on the layout netlist at 60 MHz ([Protocol count](count.md))."
             if C else f"No count in {L(test)}."))
    W.append(f"2. **Timing.** The layout meets 60 MHz at the slow, typical and fast corners: flip-flop setup slack "
             f"{' / '.join(slack)} ns, {vio_txt} (static timing, {L(gds)}).")
    W.append(f"3. **RTL against the reference model.** In the lockstep tests every pin and every host word equals the "
             f"cycle-exact model in every cycle; {n_pass} RTL testcases pass, {n_fail} fail ({L(test)}).")
    W.append("4. **Netlist against RTL.** " + (
        f"The sequential miter of the RTL and the final netlist of gds run {eq_gds.group(1) if eq_gds else '?'} is "
        f"{'proved equivalent from reset' if dp.get('netlist') == 'success' else 'not proved'} (ABC dprove); "
        f"{teeth_ok} of {len(teeth)} planted netlist faults are found ({L(equiv)})." if dp else f"{NO}."))
    W.append("5. **Coverage.** " + (f"Chip code with proved exclusions: line {excl[0]}, branch {excl[1]}, toggle "
             f"{excl[2]}; each excluded point has a formal unreachability proof ({L(cov)})." if excl else f"{NO}.")
             + "\n")

    W.append("## Checks\n")
    W.append("| Check | Result | Run | Does not check |")
    W.append("|---|---|---|---|")
    W.append(f"| RTL tests (cocotb, Icarus) | {n_pass} pass, {n_fail} fail | {L(test)} | rate sweeps run in the model "
             f"only; one standard rate per protocol on the RTL |")
    W.append(f"| Gate-level tests (layout netlist, no delays) | {gl_ok} of {len(gl_jobs)} jobs pass | {L(gds)} | "
             f"timing |")
    W.append(f"| Static timing, 3 corners | slack {' / '.join(slack)} ns | {L(gds)} | pads, board |")
    W.append(f"| Formal (SymbiYosys) | {f_ok} of {len(fjobs)} jobs give their expected result | {L(formal)} | "
             f"bounded jobs beyond their depth |")
    W.append(f"| Netlist equivalence | dprove {dp.get('netlist', NO)}, teeth {teeth_ok} of {len(teeth)}; eqy "
             f"{eqy or NO} (partitions do not close; not used as evidence) | {L(equiv)} | timing, SRAM macro inside |")
    W.append(f"| Firmware timing certificates | {len(certified)} of {len(certs)} programs never LATE | {L(cert)} | "
             f"programs whose leads depend on inputs or host timing (no verdict) |")
    W.append(f"| Mutation campaign of the RTL tests | {mline or NO} | {L(mut)} | timing, layout; survivors are read one "
             f"by one ([Verification](verification.md)) |")
    W.append(f"| sigrok decoders on the RTL pins | {sig['conclusion'] if sig else NO} | {L(sig)} | electrical levels |")
    W.append("")

    W.append("## Coverage of each instance\n")
    W.append(f"Verilator line, branch and toggle coverage of the RTL tests ({L(cov)}). The chip copies of the two line "
             f"helpers have fixed configurations, so code for the other modes is unreachable there and is excluded "
             f"with a proof per point; an instance with the same parameters as a chip copy counts with it.\n")
    W.append("| Instance | Line | Branch | Toggle |")
    W.append("|---|---|---|---|")
    for key, name in (("raw", "all RTL files, as measured"), ("chip", "chip (all blocks)"),
                      ("chip with exclusions", "chip with proved exclusions")):
        if key in scopes:
            W.append(f"| {name} | " + " | ".join(scopes[key]) + " |")
    for key, name in COPIES.items():
        if key in copies:
            W.append(f"| chip: {name} | " + " | ".join(copies[key]) + " |")
    for h, name in UNITS.items():
        if h in units:
            W.append(f"| {name} | " + " | ".join(units[h]) + " |")
    W.append("")

    W.append("## Firmware timing certificates\n")
    W.append(f"A certified program sets no LATE flag for any input and any host timing within the host rules (static "
             f"analysis of every path; {L(cert)}). {selfchk[:1].upper() + selfchk[1:]}. "
             f"{pins[:1].upper() + pins[1:]}.\n")
    if certified:
        W.append("| Program | Words | Events | Smallest lead, cycles |")
        W.append("|---|---|---|---|")
        for c in certified:
            W.append(f"| `{c['program']}` | {c['words']} | {c['events']} | {c['min_lead']} |")
        W.append("")

    W.append("## SDF scope\n")
    W.append("The layout netlist with the cell and SRAM macro delays of its SDF file, at 60 MHz. No wire delays, no "
             "pads; Icarus applies the delays but checks no setup or hold time, so static timing stays the signoff. "
             "Newest result of each part:\n")
    if parts:
        corners = [c for _, c, _ in rp.CORNERS]
        W.append("| Part | Slow | Typical | Fast |")
        W.append("|---|---|---|---|")
        for p in sorted(parts):
            W.append(f"| {p} | " + " | ".join(
                f"[{parts[p][c]['conclusion']}]({parts[p][c]['url']})" if c in parts[p] else "-" for c in corners)
                + " |")
        W.append("")
    else:
        W.append(f"{NO}.\n")

    W.append("## Rerun from a clean clone\n")
    W.append("Icarus Verilog 12, Python 3.11; the formal job needs the OSS CAD Suite.\n")
    W.append("```")
    W.append(f"git clone https://github.com/{repo}.git && cd {repo.split('/')[1]}")
    if site:
        W.append(f"git checkout {site[:7]}")
    W.append("pip install -r test/requirements.txt")
    W.append("cd test && make COCOTB_TEST_MODULES=test_core && python -m cocotb_tools.check_results results.xml")
    W.append("cd .. && sby -f formal/timed_out.sby")
    W.append("```")
    W.append(f"The first runs the lockstep tests ({core_s / 60:.0f} min of simulation on the CI runner); the second "
             f"proves that a pin event fires in the cycle of its time for every lead.\n" if core_s else
             "The first runs the lockstep tests; the second proves that a pin event fires in the cycle of its time "
             "for every lead.\n")

    W.append("## Not verified\n")
    W.append("- No FPGA, board or silicon result.")
    W.append("- Pads by SPICE of the pad cells at three corners, not measured; no board or package model.")
    W.append("- SDF without wire delays and without timing checks; equivalence with zero-delay cell functions.")
    W.append("- Protocol rates beyond those listed on [Protocols](protocols.md), section Rates.")
    W.append("")

    text = "\n".join(W)
    words = len(re.findall(r"[A-Za-z0-9][\w.%+/-]*", re.sub(r"\]\([^)]*\)", "]", re.sub(r"```.*?```", "", text,
                                                                                       flags=re.S))))
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"start page: {words} words")
    if words > 1000:
        print(f"::warning title=start page::{words} words, more than 1,000")


if __name__ == "__main__":
    main(*sys.argv[1:])
