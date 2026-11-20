# SPDX-License-Identifier: Apache-2.0
"""Jobs for "LATE is never set" of small firmware images on the processor RTL (formal/fwlate/fwlate_fv.v).

Usage: python formal/fwlate/gen.py [--out formal/fwlate/build] [--mode prove|bmc] [--depth N] [programs ...]

For each program of fw/ with at most 40 words (default: all of them, with their default .equ values) this writes
<out>/<program>/: the RTL files the job reads (copies of src/), formal/proc_stubs.v without its free SRAM model,
fwlate_fv.v, fwlate_rom.vh (a function with the word of each address of the image, HALT elsewhere), fwlate_params.vh
(OWNED: the output pins that the program's OUT instructions name) and <program>.sby. The job proves (k-induction,
--mode prove) or checks to a depth (--mode bmc) that thread 0 never sets LATE and never stores, for every host word
and every input level (free models), with the property cells of pe_core and pe_timed_out assumed (proved by core.sby
and timed_out.sby).
"""

import argparse
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "asm"))
import pe_asm  # noqa: E402

RTL = ("pe_cycle_counter.v", "pe_core.v", "pe_timed_out.v", "pe_proc.v")
MAX_WORDS = 40


def small_programs():
    out = []
    for f in sorted(os.listdir(os.path.join(ROOT, "fw"))):
        if f.endswith(".s"):
            prog = pe_asm.assemble_file(os.path.join(ROOT, "fw", f))
            if prog.size <= MAX_WORDS:
                out.append(f)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("programs", nargs="*")
    ap.add_argument("--out", default=os.path.join(HERE, "build"))
    ap.add_argument("--mode", default="prove", choices=("prove", "bmc"))
    ap.add_argument("--depth", type=int, default=24)
    args = ap.parse_args()
    stubs = open(os.path.join(ROOT, "formal", "proc_stubs.v"), encoding="utf-8").read()
    stubs = re.sub(r"module pe_sram \(.*?endmodule\n", "", stubs, flags=re.S)
    for name in args.programs or small_programs():
        prog = pe_asm.assemble_file(os.path.join(ROOT, "fw", name))
        job = re.sub(r"\W", "_", name[:-2])
        d = os.path.join(args.out, job)
        os.makedirs(d, exist_ok=True)
        for f in RTL:
            shutil.copyfile(os.path.join(ROOT, "src", f), os.path.join(d, f))
        open(os.path.join(d, "fwlate_stubs.v"), "w", encoding="utf-8").write(stubs)
        shutil.copyfile(os.path.join(HERE, "fwlate_fv.v"), os.path.join(d, "fwlate_fv.v"))
        rom = ["function [15:0] fwlate_rom(input [10:0] a);", "  case (a)"]
        rom += ["    11'd%d: fwlate_rom = 16'h%04X;" % (a, w) for a, w in sorted(prog.words.items())]
        rom += ["    default: fwlate_rom = 16'h0000;   // HALT", "  endcase", "endfunction", ""]
        open(os.path.join(d, "fwlate_rom.vh"), "w", encoding="utf-8").write("\n".join(rom))
        owned = 0
        for w in prog.words.values():
            if w >> 12 == 0xC and (w >> 3) & 7 < 6 and ((w >> 8) & 0xF) < 15:
                owned |= 1 << ((w >> 8) & 0xF)
        open(os.path.join(d, "fwlate_params.vh"), "w", encoding="utf-8").write(
            "localparam [14:0] OWNED = 15'h%04X;   // output pins of the OUT instructions of %s\n" % (owned, name))
        sby = [
            "# %s: thread 0 runs fw/%s (%d words) from a ROM; LATE is never set and the program never stores "
            "(formal/fwlate/gen.py)." % (job, name, prog.size),
            "[options]", "mode %s" % args.mode, "depth %d" % args.depth, "", "[engines]", "smtbmc z3", "", "[script]",
            "read -formal %s fwlate_stubs.v fwlate_fv.v" % " ".join(RTL),
            "prep -top fwlate_fv",
            "chformal -assert2assume pe_core *pe_timed_out*",
            "select -assert-min 2 fwlate_fv/t:$check fwlate_fv/t:$assert pe_sram/t:$check pe_sram/t:$assert",
            "", "[files]"] + list(RTL) + ["fwlate_stubs.v", "fwlate_fv.v", "fwlate_rom.vh", "fwlate_params.vh", ""]
        open(os.path.join(d, job + ".sby"), "w", encoding="utf-8").write("\n".join(sby))
        print("%s: fw/%s, %d words, owned pins 0x%04X, %s depth %d" % (job, name, prog.size, owned, args.mode,
                                                                        args.depth))
    return 0


if __name__ == "__main__":
    sys.exit(main())
