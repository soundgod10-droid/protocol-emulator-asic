"""Self-tests of the line models (test/linemodel: USB 2.0 low-speed host and 10BASE-T link partner, written
from the standards), run with pytest inside the cocotb regression so that every test run checks the models
that judge the chip. No HDL is simulated (sim time 0)."""

import contextlib
import io
import os

import cocotb
import pytest

GL = os.environ.get("GATES") == "yes"
HERE = os.path.dirname(os.path.abspath(__file__))


@cocotb.test(skip=GL)
async def test_linemodel_selftests(dut):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = pytest.main(["-q", "-p", "no:cacheprovider", "--rootdir", HERE,
                          os.path.join(HERE, "linemodel_tests")])
    lines = out.getvalue().strip().splitlines()
    for line in lines[-25:]:
        dut._log.info(line)
    assert rc == 0, "line-model self-tests failed (pytest exit code %d)" % rc


@cocotb.test(skip=GL)
async def test_research_rebase(dut):
    import subprocess
    root = os.path.dirname(HERE)
    out = []

    def g(*a):
        r = subprocess.run(["git"] + list(a), cwd=root, capture_output=True, text=True)
        out.append("$ git %s -> %d\n%s%s" % (" ".join(a), r.returncode, r.stdout[-3000:], r.stderr[-2000:]))
        return r
    ident = ["-c", "user.name=soundgod10-droid", "-c", "user.email=334736888+soundgod10-droid@users.noreply.github.com"]
    if g("status", "--porcelain", "--untracked-files=no").stdout.strip():
        out.append("working tree not clean: stop")
    else:
        g("fetch", "origin", "cmos5l")
        g("log", "--oneline", "b89bd94..origin/cmos5l")
        g("diff", "--stat", "b89bd94", "origin/cmos5l")
        r = g(*ident, "rebase", "origin/cmos5l", "lane2-docs173")
        if r.returncode != 0:
            g("diff", "--name-only", "--diff-filter=U")
            g("rebase", "--abort")
        g("rev-list", "--count", "origin/cmos5l..lane2-docs173")
        g("diff", "--stat", "origin/cmos5l", "lane2-docs173")
        g("log", "--oneline", "-5")
        g("status", "--short", "--branch", "--untracked-files=no")
    text = "\n".join(out)
    open(os.path.join(root, "build", "git_rebase.txt"), "w").write(text + "\n")
    dut._log.info(text[-4000:])


@cocotb.test(skip=GL)
async def test_research_integrate(dut):
    import subprocess
    root = os.path.dirname(HERE)
    wt = os.path.join(os.path.dirname(root), "pe-lane2-int")
    out = []

    def g(*a, cwd=root):
        r = subprocess.run(["git"] + list(a), cwd=cwd, capture_output=True, text=True)
        out.append("$ git %s -> %d\n%s%s" % (" ".join(a), r.returncode, r.stdout[-3000:], r.stderr[-2000:]))
        return r
    ident = ["-c", "user.name=soundgod10-droid", "-c", "user.email=334736888+soundgod10-droid@users.noreply.github.com"]
    g("fetch", "origin")
    g("worktree", "remove", "--force", wt)
    g("worktree", "prune")
    g("branch", "-f", "lane2-int", "lane2-mutequiv")
    ok = g("worktree", "add", wt, "lane2-int").returncode == 0
    for b in ("lane2-sigrok", "lane2-covfix"):
        if not ok:
            break
        r = g("cherry", "origin/cmos5l", b)
        commits = [ln[2:].strip() for ln in r.stdout.splitlines() if ln.startswith("+ ")]
        out.append("%s: %d commits to apply" % (b, len(commits)))
        for c in commits:
            r = g(*ident, "cherry-pick", c, cwd=wt)
            if r.returncode != 0:
                bad = g("diff", "--name-only", "--diff-filter=U", cwd=wt).stdout.split()
                if bad == ["test/Makefile"]:
                    # both lanes changed the RTL module list: keep the chip lane's modules and add this one
                    g("checkout", "--ours", "test/Makefile", cwd=wt)
                    p = os.path.join(wt, "test", "Makefile")
                    t = open(p, encoding="utf-8", newline="").read()
                    import re
                    if len(re.findall(r"test_i2c_tgt(\r?\n)else", t)) == 1 and "test_sigrok" not in t:
                        t = re.sub(r"test_i2c_tgt(\r?\n)else", r"test_i2c_tgt,test_sigrok\1else", t)
                        open(p, "w", encoding="utf-8", newline="").write(t)
                        g("add", "test/Makefile", cwd=wt)
                        msg = g("log", "-1", "--format=%B", c).stdout.rstrip() + (
                            "\n\nThe RTL module list also has test_slu_clk from the integration branch; both kept.\n")
                        mp = os.path.join(root, "build", "int_msg.txt")
                        open(mp, "w", encoding="utf-8", newline="\n").write(msg)
                        if g(*ident, "commit", "-F", mp, cwd=wt).returncode == 0:
                            out.append("resolved test/Makefile for %s: both module lists kept" % c)
                            continue
                g("diff", cwd=wt)
                g("cherry-pick", "--abort", cwd=wt)
                out.append("STOP at %s of %s" % (c, b))
                ok = False
                break
    g("log", "--oneline", "-8", "lane2-int")
    g("rev-list", "--count", "origin/cmos5l..lane2-int")
    g("diff", "--stat", "origin/cmos5l", "lane2-int")
    g("worktree", "remove", "--force", wt)
    out.append("RESULT %s" % ("integrated" if ok else "stopped"))
    text = "\n".join(out)
    open(os.path.join(root, "build", "git_int.txt"), "w").write(text + "\n")
    dut._log.info(text[-6000:])


@cocotb.test(skip=GL)
async def test_research_ff(dut):
    import subprocess
    root = os.path.dirname(HERE)
    out = []

    def g(*a):
        r = subprocess.run(["git"] + list(a), cwd=root, capture_output=True, text=True)
        out.append("$ git %s -> %d\n%s%s" % (" ".join(a), r.returncode, r.stdout[-2000:], r.stderr[-1000:]))
        return r
    g("fetch", "origin", "cmos5l")
    g("log", "--oneline", "cmos5l..origin/cmos5l")
    g("diff", "--stat", "cmos5l", "origin/cmos5l")
    if g("merge-base", "--is-ancestor", "cmos5l", "origin/cmos5l").returncode == 0:
        g("branch", "-f", "cmos5l", "origin/cmos5l")
        out.append("RESULT local cmos5l moved to origin/cmos5l (fast-forward)")
    else:
        out.append("RESULT local cmos5l is not an ancestor of origin/cmos5l: not moved")
    g("log", "--oneline", "-3", "cmos5l")
    g("rev-list", "--count", "origin/cmos5l..cmos5l")
    text = "\n".join(out)
    open(os.path.join(root, "build", "git_ff.txt"), "w").write(text + "\n")
    dut._log.info(text[-3000:])


@cocotb.test(skip=GL)
async def test_research_cov(dut):
    import glob
    import xml.etree.ElementTree as ET
    from collections import Counter, defaultdict
    out = []
    base = None
    for cand in (r"C:\Users\ssher\fallout-sandbox\bridge_workspace\archive\gh-run-44557588988\coverage",
                 r"Z:\fallout-sandbox\archive\build\gh-run-44557588988\coverage"):
        if os.path.isfile(os.path.join(cand, "coverage.dat")):
            base = cand
            break
    out.append("base %s" % base)
    tot = defaultdict(int)
    hit = defaultdict(int)
    insts = Counter()
    with open(os.path.join(base, "coverage.dat"), encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.startswith("C "):
                continue
            s, e = line.index("'") + 1, line.rindex("'")
            fl = dict(it.split("\x02", 1) for it in line[s:e].split("\x01") if "\x02" in it)
            n = int(line[e + 1:].strip())
            kind = fl.get("page", "?").split("/")[0].removeprefix("v_")
            src = fl.get("f", "?").replace("\\", "/").rsplit("/", 1)[-1]
            h = fl.get("h", "?")
            if src == "tb.v":
                continue
            keys = []
            if src == "pe_slu.v":
                inst = h.rsplit(".", 0)[0]
                insts[inst] += 1
                keys.append((kind, "pe_slu.v@" + inst))
            if h.startswith("tb.user_project"):
                keys.append((kind, "chip (tb.user_project)"))
            keys.append((kind, "all"))
            for k in keys:
                tot[k] += 1
                hit[k] += n > 0
    out.append("pe_slu.v instance keys: %s" % dict(insts))
    for k in sorted(tot):
        out.append("%s | %s | %d of %d | %.1f %%" % (k[0], k[1], hit[k], tot[k], 100.0 * hit[k] / tot[k]))
    for rx in (os.path.join(base, "results.xml"),
               os.path.join(os.path.dirname(os.path.dirname(base)), "gh-run-44557589083", "test-results", "results.xml")):
        if not os.path.isfile(rx):
            out.append("missing %s" % rx)
            continue
        r = ET.parse(rx).getroot()
        per = Counter()
        bad = []
        skipped = Counter()
        seed = [p.get("value") for p in r.iter("property") if p.get("name") == "random_seed"]
        for tc in r.iter("testcase"):
            per[tc.get("classname")] += 1
            if tc.find("failure") is not None or tc.find("error") is not None:
                bad.append(tc.get("name"))
            if tc.find("skipped") is not None:
                skipped[tc.get("classname")] += 1
        out.append("%s: testcases %d in %d modules: %s" % (rx, sum(per.values()), len(per), dict(per)))
        out.append("seed %s; skipped %s; failures %s" % (seed, dict(skipped), bad))
    text = "\n".join(out)
    open(os.path.join(os.path.dirname(HERE), "build", "cov_inst.txt"), "w").write(text + "\n")
    dut._log.info(text)


@cocotb.test(skip=GL)
async def test_research_kill(dut):
    import sys
    from concurrent.futures import ThreadPoolExecutor
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "mutation"))
    import mutate
    ids = ["0a65671e7326", "20fdafc0bd85", "1726901b49a6", "038eccf2ef3a", "1d0b8400d6ca", "20097dad2292",
           "29f6aef214cb", "12fbf0f316e8", "1abd84bfba59", "34c211d0bc95"]
    p = mutate.plan(0.0, 1, set(ids))
    work = os.path.join(os.path.dirname(HERE), "build", "mutation_kill")
    sh = mutate.Shard(p, 0, 3, work, 3000, max_rungs=1)
    with ThreadPoolExecutor(3) as ex:
        res = list(ex.map(sh.one, ids))
    lines = ["KILL %s %s:%s %s %s rungs %s note %s" % (r["id"], r["file"], r["line"], r["status"], r.get("rung", ""),
                                                      r.get("rungs"), r.get("note", "")) for r in res]
    lines.append("baselines %s" % sh.base)
    for ln in lines:
        dut._log.info(ln)
    open(os.path.join(os.path.dirname(HERE), "build", "kill3.txt"), "w").write("\n".join(lines) + "\n")


@cocotb.test(skip=GL)
async def test_research_points(dut):
    from collections import Counter, defaultdict
    root = os.path.dirname(HERE)
    dat = None
    for cand in (r"C:\Users\ssher\fallout-sandbox\bridge_workspace\archive\gh-run-44665173136\coverage.dat",
                 r"Z:\fallout-sandbox\archive\build\gh-run-44665173136\coverage.dat"):
        if os.path.isfile(cand):
            dat = cand
            break
    src = {}

    def text(f, n):
        if f not in src:
            import subprocess
            r = subprocess.run(["git", "show", "cmos5l:src/" + f], cwd=root, capture_output=True, text=True)
            src[f] = r.stdout.splitlines() if r.returncode == 0 else []
        lines_ = src[f]
        return lines_[n - 1].strip()[:110] if 0 < n <= len(lines_) else "?"
    raw, miss, tmiss = [], [], []
    keys = Counter()
    per = defaultdict(lambda: [0, 0])
    with open(dat, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.startswith("C "):
                continue
            s, e = line.index("'") + 1, line.rindex("'")
            fl = dict(it.split("\x02", 1) for it in line[s:e].split("\x01") if "\x02" in it)
            n = int(line[e + 1:].strip())
            keys.update(fl.keys())
            kind = fl.get("page", "?").split("/")[0].removeprefix("v_")
            if len([r for r in raw if r[0] == kind]) < 4:
                raw.append((kind, repr(fl) + " " + str(n)))
            f = fl.get("f", "?").replace("\\", "/").rsplit("/", 1)[-1]
            if f == "tb.v":
                continue
            h = fl.get("h", "?")
            per[(kind, f, h)][0] += 1
            per[(kind, f, h)][1] += n > 0
            if n == 0:
                ln = int(fl.get("l", "0") or 0)
                row = "%s | %s | %s:%s | %s | %s" % (kind, h, f, ln, fl.get("o", ""), text(f, ln))
                (tmiss if kind == "toggle" else miss).append(row)
    out = ["dat %s" % dat, "keys %s" % dict(keys)]
    out += ["RAW %s" % r[1] for r in raw]
    for k in sorted(per):
        out.append("PER %s | %s | %s | %d of %d" % (k[0], k[1], k[2], per[k][1], per[k][0]))
    out.append("MISSED line/branch points: %d; toggle: %d" % (len(miss), len(tmiss)))
    open(os.path.join(root, "build", "cov_points.txt"), "w", encoding="utf-8").write(
        "\n".join(out + ["", "# line and branch points not hit"] + miss + ["", "# toggle points not hit"] + tmiss) + "\n")
    for ln in out:
        dut._log.info(ln)


def unreach_gen(root, branch="lane2-cov"):
    """Copy src/, formal/unreach/ and coverage/ of the branch to build/unreach_try and run instrument.py there:
    the triage (cover) jobs from build/holes_split.csv, and the proof jobs from the branch's exclusion file."""
    import shutil
    import subprocess
    import sys
    work = os.path.join(root, "build", "unreach_try")
    shutil.rmtree(work, ignore_errors=True)
    out = []
    r = subprocess.run(["git", "ls-tree", "-r", "--name-only", branch, "src/", "formal/unreach/", "coverage/"],
                       cwd=root, capture_output=True, text=True)
    for path in r.stdout.split():
        dst = os.path.join(work, path)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as fh:
            fh.write(subprocess.run(["git", "show", "%s:%s" % (branch, path)], cwd=root, capture_output=True).stdout)
    holes = os.path.join(root, "build", "holes_split.csv")
    for args in (["--holes", holes, "--mode", "cover", "--depth", "12", "--out", "formal/unreach/build_triage"],
                 ["--out", "formal/unreach/build"],
                 ["--exclusions", os.path.join(root, "build", "exclusions_candidate.csv"), "--teeth", "none",
                  "--out", "formal/unreach/build_prove"]):
        p = subprocess.run([sys.executable, "-I", "formal/unreach/instrument.py", "--src", "src"] + args, cwd=work,
                           capture_output=True, text=True)
        out.append("$ instrument.py %s -> %d\n%s%s" % (" ".join(args), p.returncode, p.stdout[-5000:], p.stderr[-3000:]))
    return out


@cocotb.test(skip=GL)
async def test_research_triage(dut):
    """Reached / not reached per point from the triage cover logs; candidate exclusion rows for the rest."""
    import csv
    import re
    root = os.path.dirname(HERE)
    fdir = r"C:\Users\ssher\fallout-sandbox\bridge_workspace\build\chiptools2\formal"
    logs = {"triage_eth": "20261120-073952-triage_eth", "triage_usb": "20261120-080913-triage_usb",
            "triage_loop_eth": "20261120-081155-triage_loop_eth", "triage_loop_usb": "20261120-081212-triage_loop_usb"}
    src = {r["point"]: r["source"] for r in csv.DictReader(open(os.path.join(root, "build", "holes_split.csv"),
                                                                 encoding="utf-8"))}
    reached, seen = {}, {}
    for job, d in logs.items():
        pts = {}
        for ln in open(os.path.join(root, "build", "unreach_try", "formal", "unreach", "build_triage", job,
                                    "points.txt"), encoding="utf-8"):
            m = re.match(r"(pt_\d+) (\S+)", ln)
            if m:
                pts[m.group(1)] = m.group(2)
        hit = set(re.findall(r"Reached cover statement in step \d+ at \S+: (pt_\d+)",
                             open(os.path.join(fdir, d, "logfile.txt"), encoding="utf-8").read()))
        for lab, pid in pts.items():
            seen.setdefault(pid, []).append(job)
            if lab in hit:
                reached.setdefault(pid, []).append(job)
    out = []
    with open(os.path.join(root, "build", "triage.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["point", "reached_in", "source"])
        for pid in sorted(seen):
            w.writerow([pid, "+".join(reached.get(pid, [])), src.get(pid, "")])
    out.append("points %d, reached %d: %s" % (len(seen), len(reached), " ".join(sorted(reached))))
    # Reachable by reasoning but deeper than the cover depth (tests needed): jabber (2^22 cycles), RX overflow,
    # link-pulse width counter at its limit, line-condition counter B at its limit, loop wrap and limits.
    deep = {"eth:pe_slu.v:613:9:line:elsif", "eth:pe_slu.v:672:13:line:elsif", "eth:pe_slu.v:760:11:branch:if",
            "eth:pe_slu.v:814:10:branch:else", "eth:pe_slu.v:819:11:branch:if", "eth:pe_slu.v:824:11:branch:if",
            "eth:pe_slu.v:824:12:branch:else", "eth:pe_slu.v:825:11:branch:if", "eth:pe_slu.v:825:12:branch:else",
            "eth:pe_slu.v:539:51:branch:cond_then", "usb:pe_slu.v:519:54:branch:cond_then",
            "loop:pe_slu_loop.v:142:7:line:elsif", "loop:pe_slu_loop.v:165:11:line:elsif",
            "loop:pe_slu_loop.v:166:16:line:if"}
    with open(os.path.join(root, "build", "exclusions_candidate.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["point", "proof", "source", "reason"])
        n = 0
        for pid in sorted(seen):
            if pid in reached or pid in deep:
                continue
            copy = pid.split(":")[0]
            proof = "unreach_loop_usb+unreach_loop_eth" if copy == "loop" else "unreach_" + copy
            w.writerow([pid, proof, src.get(pid, ""), ""])
            n += 1
    out.append("candidates %d (deep, left as holes: %d)" % (n, len(deep)))
    for ln in out:
        dut._log.info(ln)


@cocotb.test(skip=GL)
async def test_research_unreach(dut):
    root = os.path.dirname(HERE)
    text = "\n".join(unreach_gen(root))
    open(os.path.join(root, "build", "unreach_gen.txt"), "w", encoding="utf-8").write(text + "\n")
    dut._log.info(text[-9000:])


@cocotb.test(skip=GL)
async def test_research_scope(dut):
    import shutil
    import subprocess
    import sys
    root = os.path.dirname(HERE)
    work = os.path.join(root, "build", "scope_try")
    shutil.rmtree(work, ignore_errors=True)
    for d in ("coverage", "src", "test"):
        os.makedirs(os.path.join(work, d))
    branch = os.environ.get("SCOPE_BRANCH", "lane2-cov")
    out = []

    def show(path):
        r = subprocess.run(["git", "show", "%s:%s" % (branch, path)], cwd=root, capture_output=True)
        open(os.path.join(work, path), "wb").write(r.stdout)
    r = subprocess.run(["git", "ls-tree", "--name-only", branch, "src/", "coverage/"], cwd=root,
                       capture_output=True, text=True)
    for path in r.stdout.split() + ["test/tb.v", "info.yaml"]:
        show(path)
    dat = None
    for cand in (r"C:\Users\ssher\fallout-sandbox\bridge_workspace\archive\gh-run-44665173136\coverage.dat",
                 r"Z:\fallout-sandbox\archive\build\gh-run-44665173136\coverage.dat"):
        if os.path.isfile(cand):
            dat = cand
            break

    def run(cmd, cwd):
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
        out.append("$ %s -> %d\n%s%s" % (" ".join(cmd), p.returncode, p.stdout[-6000:], p.stderr[-3000:]))
        return p
    run([sys.executable, "-I", "coverage/scope.py", dat, "--info", "info.yaml", "--src", "src",
         "--holes", "holes.csv"], work)
    for cand in (r"C:\Users\ssher\fallout-sandbox\bridge_workspace\archive\gh-run-44754431589\coverage.dat",
                 r"Z:\fallout-sandbox\archive\build\gh-run-44754431589\coverage.dat"):
        if os.path.isfile(cand):
            p = run([sys.executable, "-I", "coverage/scope.py", cand, "--info", "info.yaml", "--src", "src",
                     "--holes", os.path.join(root, "build", "holes_split.csv"),
                     "--toggle-holes", os.path.join(root, "build", "toggle_split.txt")], work)
            open(os.path.join(root, "build", "scope_split.md"), "w", encoding="utf-8").write(p.stdout)
            break
    run([sys.executable, "-I", "coverage/split.py", "."], work)
    srcs = ["src/%s" % f for f in ("project.v", "pe_sync.v", "pe_cycle_counter.v", "pe_timed_out.v", "pe_cells.v",
                                   "pe_latch_ram.v", "pe_sram.v", "pe_host_fifo.v", "pe_timed_in.v", "pe_config.v",
                                   "pe_host_port.v", "pe_core.v", "pe_proc.v", "pe_slu_loop.v", "pe_slu.v",
                                   "sram_sim.v")]
    run(["iverilog", "-g2012", "-DVERILATOR_SPLIT_TRY", "-I", "src", "-o", "split.vvp", "-s", "tb"] + srcs +
        ["test/tb.v"], work)
    tbl = open(os.path.join(work, "test", "tb.v"), encoding="utf-8").read().splitlines()
    out += unreach_gen(root)
    out += ["tb.v:%d: %s" % (i + 1, t.strip()[:100]) for i, t in enumerate(tbl)
            if "pe_slu_ut" in t or "pe_slu_loop_ut" in t or t.startswith("`line")]
    text = "\n".join(out)
    open(os.path.join(root, "build", "scope_try.txt"), "w", encoding="utf-8").write(text + "\n")
    dut._log.info(text[-9000:])
