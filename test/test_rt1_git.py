"""Research helper on branch rt1-g1 (not part of the chip's tests, not for merging): writes git facts of this
working copy into the engineering workspace, because the workstation bridge has no git tool. It dumps the logs of
the research branches with their commit times (for the time rule of the stage note), the reflog, and the CAN node
test and block of branch rt1-can (test/test_can_fd.py, src/pe_can.v), whose node model the general-unit CAN test
reuses. It changes nothing in the repository.
"""

import os
import subprocess

import cocotb

OUT = r"C:\Users\ssher\fallout-sandbox\bridge_workspace\build\rt1"
REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def git(*args):
    r = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode, r.stdout, r.stderr


@cocotb.test()
async def test_rt1_git_dump(dut):
    os.makedirs(OUT, exist_ok=True)
    items = {
        "git_log_rt1_g1.txt": ["log", "--format=%H %cI %s", "-n", "90", "rt1-g1"],
        "git_log_rt1_can.txt": ["log", "--format=%H %cI %s", "-n", "60", "rt1-can"],
        "git_log_rt1_research.txt": ["log", "--format=%H %cI %s", "-n", "20", "rt1-research"],
        "git_log_cmos5l.txt": ["log", "--format=%H %cI %s", "-n", "10", "cmos5l"],
        "git_reflog.txt": ["reflog", "--date=iso", "-n", "200"],
        "git_branches.txt": ["branch", "-vv"],
        "git_status.txt": ["status", "--porcelain", "--untracked-files=no"],
        "test_can_fd_rt1can.py": ["show", "rt1-can:test/test_can_fd.py"],
        "pe_can_rt1can.v": ["show", "rt1-can:src/pe_can.v"],
        "diff_rt1g1_cmos5l_stat.txt": ["diff", "--stat", "cmos5l...rt1-g1"],
    }
    log = []
    for name, args in items.items():
        rc, out, err = git(*args)
        with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
            f.write(out if rc == 0 else f"ERROR rc={rc}\n{err}")
        log.append(f"{name}: rc {rc}, {len(out)} chars")
    dut._log.info("git dump: " + "; ".join(log))
