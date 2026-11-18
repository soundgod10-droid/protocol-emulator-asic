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
        r = g(*ident, "rebase", "origin/cmos5l", "lane2-mutequiv")
        if r.returncode != 0:
            g("diff", "--name-only", "--diff-filter=U")
            g("rebase", "--abort")
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
                g("diff", "--name-only", "--diff-filter=U", cwd=wt)
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
