# SPDX-License-Identifier: Apache-2.0
"""Trim the raw pin dumps of test/tb.v (make PIN_VCD=1) into small VCD samples for the waveform page.

Raw dump: vectors rst_n, ui_in, uio_in, uo_out, uio_out, uio_oe (timescale 1 ps, sim clock 16,668 ps).
Each sample: named 1-bit lines derived from the pins, a time window, times shifted so the window starts at 0.

Line sources (spec "src"):
  "uo:N"    uo_out[N]                        (push-pull output)
  "ui:N"    ui_in[N]                         (input from the protocol model, or the host SPI pins)
  "in:N"    uio_in[N]                        (the level the bus model returns: the bus level of open-drain lines)
  "drv:N"   uio_out[N] when uio_oe[N] = 1, else z   (push-pull uio output)
  "pull:N"  0 when the chip pulls uio[N] low (oe 1, out 0), else 1   (the chip's own open-drain drive)

Usage:
  python -I trim_vcd.py edges RAW.vcd SRC [SRC ...]      print the edge summary of the given lines
  python -I trim_vcd.py build SPEC.json                    write every sample of the spec and the manifest
  python -I trim_vcd.py same RAW_A.vcd RAW_B.vcd           compare two raw dumps value by value (order-free)
"""
import hashlib
import json
import os
import sys

CLK_PS = 16668


def read_raw(path):
    """Returns {name: [(t, value_str)]} for the top-level vectors, and the end time."""
    ids, ch, t, end = {}, {}, 0, 0
    with open(path, encoding="ascii") as f:
        head = True
        for line in f:
            s = line.split()
            if not s:
                continue
            if head:
                if s[0] == "$var":
                    ids[s[3]] = s[4]
                    ch[s[4]] = []
                elif s[0] == "$enddefinitions":
                    head = False
                continue
            w = s[0]
            if w[0] == "#":
                t = int(w[1:])
                end = max(end, t)
            elif w[0] in "bB":
                name = ids.get(s[1])
                if name:
                    ch[name].append((t, w[1:]))
            elif w[0] in "01xzXZ" and len(w) > 1:
                name = ids.get(w[1:])
                if name:
                    ch[name].append((t, w[0]))
    return ch, end


def bit_of(val, n, width=8):
    v = val.rjust(width, val[0] if val[0] in "xz" else "0")
    c = v[width - 1 - n]
    return c if c in "01" else "x"


def line(raw, src):
    """[(t, '0'|'1'|'z'|'x')] for one source."""
    kind, n = src.split(":")
    n = int(n)
    if kind in ("uo", "ui", "in"):
        vec = {"uo": "uo_out", "ui": "ui_in", "in": "uio_in"}[kind]
        out = [(t, bit_of(v, n)) for t, v in raw[vec]]
    else:
        ev = sorted([(t, "o", v) for t, v in raw["uio_out"]] + [(t, "e", v) for t, v in raw["uio_oe"]])
        o = e = "x"
        out = []
        for t, k, v in ev:
            if k == "o":
                o = bit_of(v, n)
            else:
                e = bit_of(v, n)
            if kind == "drv":
                val = o if e == "1" else ("z" if e == "0" else "x")
            else:
                val = "0" if (e == "1" and o == "0") else ("1" if e in "01" else "x")
            out.append((t, val))
    res = []
    for t, v in out:
        if res and res[-1][0] == t:
            res[-1] = (t, v)
        elif not res or res[-1][1] != v:
            res.append((t, v))
    # remove repeats created by same-time replacement
    clean = []
    for t, v in res:
        if not clean or clean[-1][1] != v:
            clean.append((t, v))
    return clean


def edges(path, srcs):
    raw, end = read_raw(path)
    print(f"end {end} ps = {end / CLK_PS:.0f} cycles")
    for s in srcs:
        L = line(raw, s)
        ts = [t for t, _ in L[1:]]
        gaps = sorted(((b - a, a) for a, b in zip(ts, ts[1:])), reverse=True)[:6]
        print(f"{s}: {len(L)} changes, first {ts[0] if ts else None}, last {ts[-1] if ts else None}; largest gaps (ps, at): "
              + ", ".join(f"{g}@{a}" for g, a in gaps))


VCD_ID = [chr(c) for c in range(33, 127)]


def write_sample(raw, end, spec, out_path):
    t0 = int(spec["window_ps"][0])
    t1 = int(spec["window_ps"][1]) if spec["window_ps"][1] is not None else end
    t0 -= t0 % CLK_PS  # start on a clock edge, so the cycle count of the page is exact
    lines = []
    for group in spec["groups"]:
        for name, src in group["lines"]:
            L = line(raw, src)
            lines.append((group["name"], name, src, L))
    out = ["$date", "\t(trimmed sample)", "$end", "$version", "\tIcarus Verilog pin dump, trimmed by sim/tools/trim_vcd.py", "$end"]
    for c in spec["comments"]:
        out.append(f"$comment {c} $end")
    out += ["$timescale", "\t1ps", "$end", "$scope module chip $end"]
    ids = {}
    cur = None
    for k, (g, name, src, L) in enumerate(lines):
        if g != cur:
            if cur is not None:
                out.append("$upscope $end")
            out.append(f"$scope module {g} $end")
            cur = g
        ids[k] = VCD_ID[k]
        out.append(f"$var wire 1 {ids[k]} {name} $end")
    out += ["$upscope $end", "$upscope $end", "$enddefinitions $end"]
    # initial values at t0, then changes inside the window
    ev = {}
    init = []
    for k, (_, _, _, L) in enumerate(lines):
        v0 = "x"
        for t, v in L:
            if t <= t0:
                v0 = v
            elif t <= t1:
                ev.setdefault(t - t0, []).append(f"{v}{ids[k]}")
        init.append(f"{v0}{ids[k]}")
    out += ["#0", "$dumpvars"] + init + ["$end"]
    for t in sorted(ev):
        if t == 0:
            continue
        out.append(f"#{t}")
        out += ev[t]
    out.append(f"#{t1 - t0}")
    text = "\n".join(out) + "\n"
    with open(out_path, "w", encoding="ascii", newline="\n") as f:
        f.write(text)
    return t0, t1, len(text.encode())


def same(a, b):
    """Value-by-value comparison of two raw dumps: per timestamp, the set of (signal, value) changes."""
    ra, ea = read_raw(a)
    rb, eb = read_raw(b)
    diffs = []
    for name in ra:
        if ra[name] != rb.get(name):
            diffs.append(name)
    return ea == eb and not diffs, diffs, ea, eb


def build(spec_path):
    spec = json.load(open(spec_path, encoding="utf-8"))
    base = os.path.dirname(os.path.abspath(spec_path))
    out_dir = os.path.normpath(os.path.join(base, spec["out_dir"]))
    os.makedirs(out_dir, exist_ok=True)
    cache = {}
    manifest = {"commit": spec["commit"], "clock_ps": CLK_PS, "ns_per_cycle": CLK_PS / 1000, "samples": []}
    for s in spec["samples"]:
        rawp = os.path.normpath(os.path.join(base, s["raw"]))
        if rawp not in cache:
            cache[rawp] = read_raw(rawp)
        raw, end = cache[rawp]
        comments = [f"test {s['test']} (test/test_rtl_fw.py), commit {spec['commit']}",
                    f"{s['level']}, {s['simulator']}", f"window {s['window_note']}"]
        t0, t1, size = write_sample(raw, end, {**s, "comments": comments}, os.path.join(out_dir, s["file"]))
        raw_sha = hashlib.sha256(open(rawp, "rb").read()).hexdigest()
        m = {k: s[k] for k in ("file", "title", "test", "level", "simulator", "decoders", "timing", "note") if k in s}
        m.update(window_ps=[t0, t1], window_cycles=[t0 // CLK_PS, t1 // CLK_PS], window_note=s["window_note"],
                 bytes=size, raw_sha256=raw_sha, commit=spec["commit"])
        if "gl_equal" in s:
            m["gl_equal_to_rtl"] = s["gl_equal"]
        manifest["samples"].append(m)
        print(f"{s['file']}: {size} bytes, window {t0}..{t1} ps ({(t1 - t0) / CLK_PS:.0f} cycles)")
        assert size < 1_000_000, f"{s['file']} is {size} bytes"
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, indent=1)
        f.write("\n")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "edges":
        edges(sys.argv[2], sys.argv[3:])
    elif cmd == "build":
        build(sys.argv[2])
    elif cmd == "same":
        ok, diffs, ea, eb = same(sys.argv[2], sys.argv[3])
        print("SAME" if ok else f"DIFFERENT {diffs} end {ea} {eb}")
