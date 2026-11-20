"""Coverage build with the unit-test copies of the line helper kept apart from the chip's copies.

Verilator sums the coverage of all instances of a module that have the same parameter set ("Hierarchy
is compressed, so if a module is instantiated multiple times, coverage will be summed for that bit across
all instantiations of that module with the same parameter set", Verilator guide, Simulating, coverage
analysis). test/tb.v holds unit-test instances of pe_slu (u_slu_ut: FULL 1, SPEC 0; u_slc_ut: FULL 0,
SPEC 1) and of pe_slu_loop (u_slu_loop_ut) beside the chip's u_usb (FULL 0, SPEC 1) and u_eth (FULL 1,
SPEC 2) in src/pe_proc.v. u_slc_ut has the parameter set of u_usb, and every pe_slu_loop instance has the
same (none), so the unit tests' hits are added to the chip's and a chip-only figure cannot be read from
coverage.dat.

This script edits the coverage job's checkout only (nothing is committed): the three unit-test instances
of test/tb.v use pe_slu_ut and pe_slu_loop_ut, copies of src/pe_slu.v and src/pe_slu_loop.v that differ
only in the module names (and the loop instance inside pe_slu_ut), appended to test/tb.v behind `line
directives, so Verilator reports their points with the file name and the line numbers of src/. The logic
is unchanged, and each point of a unit copy has the file, line, column and text of the chip copy's point.
coverage/scope.py adds the copies again for the raw figure, which is then the figure of the build without
this script.

Usage: python coverage/split.py [repository root]
"""

import pathlib
import re
import sys


def sub_once(text, pattern, repl, what):
    new, n = re.subn(pattern, repl, text)
    if n != 1:
        sys.exit("split.py: %s: %d matches of %r, expected 1 (test/tb.v or src/ changed?)" % (what, n, pattern))
    return new


def main(root):
    root = pathlib.Path(root).resolve()
    tb_path = root / "test" / "tb.v"
    slu_path = root / "src" / "pe_slu.v"
    loop_path = root / "src" / "pe_slu_loop.v"
    tb = tb_path.read_text(encoding="utf-8")
    slu = slu_path.read_text(encoding="utf-8")
    loop = loop_path.read_text(encoding="utf-8")

    tb = sub_once(tb, r"\bpe_slu(\s*#\(\s*\.FULL\(1\)\s*\)\s*u_slu_ut\b)", r"pe_slu_ut\1", "tb.v u_slu_ut")
    tb = sub_once(tb, r"\bpe_slu(\s*#\(\s*\.FULL\(0\),\s*\.SPEC\(1\)\s*\)\s*u_slc_ut\b)", r"pe_slu_ut\1",
                  "tb.v u_slc_ut")
    tb = sub_once(tb, r"\bpe_slu_loop(\s+u_slu_loop_ut\b)", r"pe_slu_loop_ut\1", "tb.v u_slu_loop_ut")
    slu_ut = sub_once(slu, r"\bmodule pe_slu\b", "module pe_slu_ut", "pe_slu.v module name")
    slu_ut = sub_once(slu_ut, r"\bpe_slu_loop(\s+u_loop\b)", r"pe_slu_loop_ut\1", "pe_slu.v loop instance")
    loop_ut = sub_once(loop, r"\bmodule pe_slu_loop\b", "module pe_slu_loop_ut", "pe_slu_loop.v module name")
    if slu_ut.count("\n") != slu.count("\n") or loop_ut.count("\n") != loop.count("\n"):
        sys.exit("split.py: the renamed copies must keep every line number")

    if not tb.endswith("\n"):
        tb += "\n"
    for path, text in ((slu_path, slu_ut), (loop_path, loop_ut)):
        tb += "`line 1 \"%s\" 0\n%s" % (path.as_posix(), text if text.endswith("\n") else text + "\n")
    tb_path.write_text(tb, encoding="utf-8")
    print("split.py: test/tb.v now uses pe_slu_ut (u_slu_ut, u_slc_ut) and pe_slu_loop_ut (u_slu_loop_ut, "
          "and the loops inside pe_slu_ut); copies appended with `line directives to src/")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
