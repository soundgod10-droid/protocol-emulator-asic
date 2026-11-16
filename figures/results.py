"""Results page of the documentation (Markdown), generated from the CI runs and the figures of the docs workflow.

What it does:
  Reads runs.json and the artifacts of figures/runs.py (the newest completed gds, test, coverage and formal runs
  of the branch), the measurements of the waveform figures (waves.json) and the layout facts (layout.json), and
  writes one table of key results and detail tables. Every value links to the run it comes from. A value that a
  run does not give is written as "not in this run", never left out.

Usage: python figures/results.py <runs folder> <figures folder> <out results.md>
  Also writes results.json next to the Markdown file.
"""

import csv
import glob
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

CORNERS = (("slow", "nom_slow_1p08V_125C", "1.08 V, 125 °C"), ("typical", "nom_typ_1p20V_25C", "1.20 V, 25 °C"),
           ("fast", "nom_fast_1p32V_m40C", "1.32 V, -40 °C"))
PROTOCOLS = {
    "test_rtl_pull_rule": "host port: PULL rule", "test_rtl_uart_tx": "UART transmitter",
    "test_rtl_uart_rx": "UART receiver", "test_rtl_spi_ctl": "SPI controller", "test_rtl_spi_tgt": "SPI target",
    "test_rtl_i2c": "I2C controller", "test_rtl_can": "CAN 2.0A node", "test_rtl_jtag": "JTAG controller",
    "test_rtl_swd": "SWD host", "test_rtl_ps2": "PS/2 host and device", "test_rtl_trigger": "timed trigger",
    "test_rtl_four_protocols": "four protocols at once (UART, SPI, I2C, JTAG)",
    "test_rtl_onewire": "1-Wire controller (hold-out)", "test_rtl_am2302": "AM2302 sensor reader (hold-out)",
    "test_rtl_pdi": "AVR XMEGA PDI programmer (hold-out)",
}
NA = "not in this run"


def link(run, text=None):
    if not run:
        return "no run"
    return f"[{text or 'run ' + str(run['id'])}]({run['url']})"


def find(root, pattern):
    hits = sorted(glob.glob(os.path.join(root, "**", pattern), recursive=True)) if root else []
    return hits[0] if hits else None


def testcases(path):
    """{name: (classname, 'pass' | 'fail' | 'skip', time s)} from a cocotb results.xml."""
    out = {}
    if not path:
        return out
    for tc in ET.parse(path).getroot().iter("testcase"):
        kids = {c.tag for c in tc}
        state = "fail" if kids & {"failure", "error"} else "skip" if "skipped" in kids else "pass"
        out[tc.get("name")] = (tc.get("classname"), state, float(tc.get("time") or 0))
    return out


def counts(cases):
    states = [s for _, s, _ in cases.values()]
    return dict(total=len(states), passed=states.count("pass"), failed=states.count("fail"),
                skipped=states.count("skip"))


def metrics(path):
    if not path:
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        return {row[0]: row[1] for row in csv.reader(f) if len(row) >= 2}


def notices(run, job, title):
    if not run:
        return None
    for n in run["notices"].get(job, []):
        if n["title"] == title:
            return n["message"]
    return None


def num(v, nd=3):
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return NA


def main(runs_dir, fig_dir, out_md):
    with open(os.path.join(runs_dir, "runs.json"), encoding="utf-8") as f:
        R = json.load(f)
    runs = R["runs"]
    gds, test, cov, formal = (runs.get(k) for k in ("gds", "test", "coverage", "formal"))
    art = lambda run, name: run["artifacts"].get(name) if run else None  # noqa: E731
    sub = art(gds, "tt_submission")
    m = metrics(find(sub, "metrics.csv"))
    resolved = json.load(open(find(sub, "resolved.json"), encoding="utf-8")) if find(sub, "resolved.json") else {}
    info = open(find(sub, "info.yaml"), encoding="utf-8").read() if find(sub, "info.yaml") else ""
    clock_hz = re.search(r"clock_hz:\s*(\d+)", info)
    period = resolved.get("CLOCK_PERIOD")

    flops = {}
    for short, corner, _ in CORNERS:
        msg = notices(gds, "gds", f"gds {corner}") or ""
        mm = re.search(r"flops setup slack (-?[\d.]+) \((MET|VIOLATED)\)", msg)
        flops[short] = (float(mm.group(1)), mm.group(2)) if mm else None

    rtl = testcases(find(art(test, "test-results"), "results.xml"))
    gl = testcases(find(art(gds, "gatelevel_test_results"), "results.xml"))
    glp = {}
    for part in ("gl_protocols_1", "gl_protocols_2"):
        for name, val in testcases(find(art(gds, part), "results.xml")).items():
            if val[1] != "skip" or name not in glp:
                glp[name] = val
    c_rtl, c_gl, c_glp = counts(rtl), counts(gl), counts(glp)
    proto_rtl = {n: v for n, v in rtl.items() if v[0] == "test_rtl_fw"}

    cov_msg = notices(cov, "coverage", "coverage summary") or ""
    cov_all = {k: (h, p, pc) for k, h, p, pc in
               re.findall(r"\| (line|branch|toggle) \| \(all RTL files\) \| (\d+) \| (\d+) \| ([\d.]+) % \|", cov_msg)}

    fjobs = [j for j in (formal or {}).get("jobs", []) if j["name"].startswith("formal")]
    f_ok = [j for j in fjobs if j["conclusion"] == "success"]

    pre_md = find(art(gds, "precheck_reports"), "results.md")
    pre_rows = re.findall(r"^\| (.+?) \| (.+?) \|$", open(pre_md, encoding="utf-8").read(), re.M) if pre_md else []
    pre_rows = [(a, b) for a, b in pre_rows if a not in ("Check", "-----------")]
    pre_pass = sum(1 for _, b in pre_rows if "✅" in b)
    pre_job = next((j for j in (gds or {}).get("jobs", []) if j["name"] == "precheck"), None)

    waves = json.load(open(os.path.join(fig_dir, "waves.json"), encoding="utf-8")) \
        if os.path.exists(os.path.join(fig_dir, "waves.json")) else {}
    layout = json.load(open(os.path.join(fig_dir, "layout.json"), encoding="utf-8")) \
        if os.path.exists(os.path.join(fig_dir, "layout.json")) else {}

    def vio(kind):
        vals = [m.get(f"{kind}__corner:{c}") for _, c, _ in CORNERS]
        return "0 at the 3 corners" if all(v == "0" for v in vals) else (
            ", ".join(f"{s} {v}" for (s, _, _), v in zip(CORNERS, vals)) if all(vals) else NA)

    site = R.get("site_commit", "")[:7]
    docs_run = waves.get("run", "")  # the run that simulated the pin VCDs (the workflow can reuse them from its cache)
    this_run = (f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/"
                f"{os.environ['GITHUB_RUN_ID']}") if os.environ.get("GITHUB_RUN_ID") else ""
    L = []
    L.append("# Results\n")
    L.append(f"This page is generated on every push by the docs workflow ([.github/workflows/docs.yaml]"
             f"(https://github.com/{R['repo']}/blob/{R['branch']}/.github/workflows/docs.yaml), "
             f"[figures/results.py](https://github.com/{R['repo']}/blob/{R['branch']}/figures/results.py)) from the "
             f"newest completed run of each workflow on branch `{R['branch']}`. Site built from commit `{site}`"
             + (f" in [this docs run]({docs_run})" if docs_run else "") + ". Each value links to its run; the commit "
             "of each run is in the last table. A run of an older commit is used until the run of the newest "
             "commit has finished.\n")
    L.append("## Key results\n")
    L.append("| Item | Result | Source |")
    L.append("|---|---|---|")
    clk = f"{int(clock_hz.group(1)) / 1e6:g} MHz declared (`info.yaml`)" if clock_hz else NA
    if period:
        clk += f"; layout timed at CLOCK_PERIOD {period} ns"
    L.append(f"| Clock | {clk} | {link(gds)} |")
    for short, corner, cond in CORNERS:
        fl = flops[short]
        L.append(f"| Setup slack, flip-flop paths, {short} corner ({cond}) | "
                 + (f"{fl[0]:+.3f} ns ({fl[1]})" if fl else NA) + f" | {link(gds)} job `gds` |")
    L.append("| Worst hold slack, slow / typical / fast | " + " / ".join(
        (num(m.get(f"timing__hold__ws__corner:{c}")) + " ns") if m.get(f"timing__hold__ws__corner:{c}") else NA
        for _, c, _ in CORNERS) + f" | {link(gds)} |")
    L.append(f"| Setup violations / hold violations | {vio('timing__setup_vio__count')} / "
             f"{vio('timing__hold_vio__count')} | {link(gds)} |")
    L.append(f"| Max slew / max capacitance violations | {vio('design__max_slew_violation__count')} / "
             f"{vio('design__max_cap_violation__count')} | {link(gds)} |")
    L.append(f"| Max fanout pins over the library limit 8 | {vio('design__max_fanout_violation__count')} "
             f"(antenna-diode loads, explained in [Signoff](signoff.md)) | {link(gds)} |")
    L.append(f"| Routing DRC / LVS errors / antenna violations | {m.get('route__drc_errors', NA)} / "
             f"{m.get('design__lvs_error__count', NA)} / {m.get('antenna__violating__nets', NA)} | {link(gds)} |")
    L.append(f"| Lint errors / warnings (Verilator, flow) | {m.get('design__lint_error__count', NA)} / "
             f"{m.get('design__lint_warning__count', NA)} | {link(gds)} |")
    L.append(f"| Tiny Tapeout precheck | " + (f"{pre_pass} of {len(pre_rows)} checks pass" if pre_rows else NA)
             + (f", job {pre_job['conclusion']}" if pre_job else "") + f" | {link(gds)} job `precheck` |")
    if m:
        L.append(f"| Standard cells, area, utilisation, power | {int(m['design__instance__count__stdcell']):,} cells, "
                 f"{float(m['design__instance__area__stdcell']):,.0f} um2, "
                 f"{100 * float(m['design__instance__utilization']):.1f} %, "
                 f"{1000 * float(m['power__total']):.2f} mW (flow estimate) | {link(gds)} |")
    if layout:
        L.append(f"| Tile | {layout['width_um']:.2f} × {layout['height_um']:.2f} um | {link(gds)} |")
    L.append(f"| RTL tests (cocotb, Icarus Verilog) | {c_rtl['passed']} pass, {c_rtl['failed']} fail, "
             f"{c_rtl['skipped']} skipped, of {c_rtl['total']} | {link(test)} |")
    np_ = sum(1 for v in proto_rtl.values() if v[1] == "pass")
    L.append(f"| Protocol tests on the RTL (`test_rtl_fw.py`) | {np_} of {len(proto_rtl)} pass | {link(test)} |")
    ngl = sum(1 for v in glp.values() if v[1] == "pass")
    L.append(f"| Protocol tests on the gate-level netlist | {ngl} of {len(glp)} pass | {link(gds)} jobs "
             f"`gl_protocols` |")
    L.append(f"| Gate-level test (template `gl_test`) | {c_gl['passed']} pass, {c_gl['failed']} fail, "
             f"{c_gl['skipped']} skipped (model-only tests) | {link(gds)} job `gl_test` |")
    if cov_all:
        L.append("| Code coverage of the RTL tests (Verilator) | " + ", ".join(
            f"{k} {int(cov_all[k][0]):,} of {int(cov_all[k][1]):,} ({cov_all[k][2]} %)"
            for k in ("line", "branch", "toggle") if k in cov_all) + f" | {link(cov)} |")
    else:
        L.append(f"| Code coverage of the RTL tests | {NA} | {link(cov)} |")
    L.append(f"| Formal jobs (SymbiYosys, Z3) | {len(f_ok)} of {len(fjobs)} give their expected result | "
             f"{link(formal)} |")
    L.append("")
    L.append("Conditions: static timing at the three corners of the layout flow (LibreLane, IHP SG13CMOS5L), "
             "with the parasitics of the routed layout; the tests are logic simulations (RTL, and the gate-level "
             "netlist with functional cell models, no delays). No silicon result exists.\n")

    if waves.get("figures"):
        L.append("## Waveform measurements\n")
        L.append(f"From the pin VCD of the RTL tests in [the docs run]({docs_run}); figures on the "
                 "[Protocols](protocols.md) page.\n")
        L.append("| Test | Measured | Within the limit |")
        L.append("|---|---|---|")
        w = waves["figures"]
        if "test_rtl_uart_tx" in w:
            u = w["test_rtl_uart_tx"]
            L.append(f"| `test_rtl_uart_tx`, 115,200 bit/s | bit {u['bit_cycles']} cycles, rate error "
                     f"{u['rate_error_pct']:+.3f} % (limit ±1 %) | {'yes' if u['ok'] else 'no'} |")
        if "test_rtl_spi_ctl" in w:
            u = w["test_rtl_spi_ctl"]
            L.append(f"| `test_rtl_spi_ctl`, mode 0 | SCK {u['sck_mhz']:.2f} MHz (half period {u['half_period']} "
                     f"cycles), MOSI set-up {u['setup_min']} and hold {u['hold_min']} cycles | "
                     f"{'yes' if u['ok'] else 'no'} |")
        if "test_rtl_i2c" in w:
            u = w["test_rtl_i2c"]
            L.append(f"| `test_rtl_i2c`, Fast-mode | fSCL {u['f_khz']:.1f} kHz, tLOW {u['mins'].get('low')} and "
                     f"tHIGH {u['mins'].get('high')} cycles (UM10204 Table 11) | {'yes' if u['ok'] else 'no'} |")
        if "test_rtl_can" in w:
            u = w["test_rtl_can"]
            L.append(f"| `test_rtl_can`, 250 kbit/s | bit {u['bit_cycles']} cycles, ACK from the node "
                     f"{'seen' if u['ack'] else 'not seen'} | {'yes' if u['ok'] else 'no'} |")
        L.append("")

    L.append("## Timing at each corner\n")
    L.append(f"From {link(gds)}: flip-flop setup slack from the first path of group `flops` in `checks.rpt` "
             "(latch paths borrow time and report 0.000, so they are grouped apart), the rest from `metrics.csv`.\n")
    L.append("| Corner | Flip-flop setup slack | Worst hold slack | Setup / hold violations | Max slew / cap "
             "violations | Max fanout pins |")
    L.append("|---|---|---|---|---|---|")
    for short, corner, cond in CORNERS:
        fl = flops[short]
        g = lambda k: m.get(f"{k}__corner:{corner}", NA)  # noqa: E731
        L.append(f"| {short} ({cond}) | " + (f"{fl[0]:+.3f} ns" if fl else NA)
                 + f" | {num(g('timing__hold__ws'))} ns | {g('timing__setup_vio__count')} / "
                 f"{g('timing__hold_vio__count')} | {g('design__max_slew_violation__count')} / "
                 f"{g('design__max_cap_violation__count')} | {g('design__max_fanout_violation__count')} |")
    L.append("")

    L.append("## Protocols on the RTL and on the gate-level netlist\n")
    L.append(f"Each test loads the firmware through the host SPI port and runs it against an independent protocol "
             f"model, with the reference model in lockstep on every pin in every cycle. RTL: {link(test)}; gate "
             f"level: {link(gds)} (jobs `gl_protocols`, functional cell models, no delays).\n")
    L.append("| Test | Protocol | RTL | Gate level |")
    L.append("|---|---|---|---|")
    for name in sorted(set(proto_rtl) | set(glp), key=lambda n: list(PROTOCOLS).index(n) if n in PROTOCOLS else 99):
        r, g = proto_rtl.get(name), glp.get(name)
        L.append(f"| `{name}` | {PROTOCOLS.get(name, '')} | {r[1] if r else NA} | {g[1] if g else NA} |")
    L.append("")

    if fjobs:
        L.append("## Formal jobs\n")
        L.append(f"From {link(formal)}. A job passes when SymbiYosys returns 0: PASS for the proofs, all covers "
                 "reached for the cover jobs, and the expected counterexample for `host_port_mut_id` (the check of "
                 "the check).\n")
        L.append("| Job | Job result | SymbiYosys status | Process time |")
        L.append("|---|---|---|---|")
        for j in sorted(fjobs, key=lambda j: j["name"]):
            job = j["name"].replace("formal (", "").rstrip(")")
            msg = notices(formal, j["name"], f"formal {job}") or ""
            done = re.search(r"DONE \(([^)]*)\)", msg)
            secs = re.search(r"Elapsed process time[^(]*\(secs\)\]: [\d:]+ \((\d+)\)", msg)
            L.append(f"| `{job}` | {j['conclusion']} | {done.group(1) if done else NA} | "
                     f"{secs.group(1) + ' s' if secs else NA} |")
        L.append("")

    L.append("## Runs used\n")
    L.append("| Workflow | Run | Commit | Result | Started |")
    L.append("|---|---|---|---|---|")
    for key in ("gds", "test", "coverage", "formal"):
        run = runs.get(key)
        if run:
            L.append(f"| {key} | {link(run)} | [`{run['sha'][:7]}`](https://github.com/{R['repo']}/commit/{run['sha']}) "
                     f"| {run['conclusion']} | {run['created'].replace('T', ' ').replace('Z', ' UTC')} |")
        else:
            L.append(f"| {key} | none | | | |")
    L.append("")
    with open(out_md, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    summary = dict(flops=flops, rtl=c_rtl, gl=c_gl, gl_protocols=c_glp, coverage=cov_all, formal=[len(f_ok), len(fjobs)],
                   precheck=[pre_pass, len(pre_rows)], runs={k: (v["id"] if v else None) for k, v in runs.items()})
    with open(os.path.splitext(out_md)[0] + ".json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1)
    print(json.dumps(summary))


if __name__ == "__main__":
    main(*sys.argv[1:])
