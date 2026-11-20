# SPDX-License-Identifier: Apache-2.0
"""Check L3: the waveform page's decoders against sigrok-cli (libsigrokdecode) on the same recorded samples.

Use: python -I sim/tools/sigrok_compare.py SAMPLES_DIR DUMP.json OUT.json

What it does:
  DUMP.json holds the page decoders' records (sim/tools/decode_dump.mjs). For each sample and decoder of
  SAMPLES_DIR/manifest.json, sigrok-cli decodes the same VCD as a separate program (-I vcd:downsample=8334: 8,334 ps is
  half the 16,668 ps test clock and every value change of the samples is on that grid, so the samples are exact), and
  both outputs are turned into the same lists and compared element by element: UART bytes, SPI MOSI and MISO words, I2C
  events, CAN frames, JTAG scans and TAP states, SWD transfers, PS/2 device frames, 1-Wire link symbols and bytes, USB
  packets (usb_signalling and usb_packet). sigrok-cli 0.7.2 has no 10BASE-T decoder: the page's 10BASE-T decoder is
  compared with the repository's 802.3 partner model (test/linemodel/tenbaset.py decode_tx) on the same lines.
  Three differences come from sigrok-cli 0.7.2 itself; each is shown in sigrok's decoder source and applied here as a
  stated rule, and the comparison after the rule must be equal:
    CAN: a remote frame has no data field (Bosch CAN 2.0), but can/pd.py reads DLC data bytes after it;
    JTAG: IEEE 1149.1 shifts only in Shift-DR/IR, but jtag/pd.py also takes a bit at Exit1-DR to Pause-DR;
    PS/2: ps2/pd.py emits a frame at the 12th falling clock edge and uses that edge up, so it reads back-to-back frames
      one bit late (the rule is applied to the same file and must reproduce sigrok's output exactly).
  A decoder passes when its list equals sigrok's (or equals it after the rule), it is not empty, and it has the number
  of records ("expect" in the manifest) that the test's traffic gives. Any other difference fails the check.

Why:
  The page's decoders are written for the page; an independent decoder must read the same bytes from the same pins.
  sigrok runs only here in CI, so nothing of it is distributed with the page.
"""

import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SG = os.environ.get("SIGROK_CLI") or shutil.which("sigrok-cli") or "sigrok-cli"
SAMPLES = "."
DS = 8334  # ps per sigrok sample (half the 16,668 ps clock): every value change is on this grid
LINE = re.compile(r"^(\d+)-(\d+) [\w-]+: (.*)$")


def sigrok(vcd, pd, ann):
    cmd = [SG, "-i", os.path.join(SAMPLES, vcd), "-I", f"vcd:downsample={DS}", "--protocol-decoder-samplenum", "-P", pd]
    if ann:
        cmd += ["-A", ann]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=600).stdout
    rows = []
    for ln in out.splitlines():
        m = LINE.match(ln.strip())
        if m:
            rows.append((int(m.group(1)) * DS, int(m.group(2)) * DS, m.group(3)))
    return rows, " ".join(cmd[1:]).replace(SAMPLES + os.sep, "")


def pins(d, *roles):
    return ":".join(f"{sr}={d['pins'][r]}" for r, sr in roles if r in d["pins"])


def cmp_lists(a, b):
    """equal flag, number compared, first differences"""
    diffs = []
    for i in range(max(len(a), len(b))):
        x = a[i] if i < len(a) else None
        y = b[i] if i < len(b) else None
        if x != y:
            diffs.append({"index": i, "page": x, "sigrok": y})
    return not diffs, diffs[:8]


def do_uart(vcd, d, rec):
    o = d.get("options", {})
    pd = f"uart:{pins(d, ('rx', 'rx'))}:baudrate={o.get('baud', 115200)}"
    rows, cmd = sigrok(vcd, pd, "uart=rx-data:rx-warnings")
    s_bytes = [int(t, 16) for _, _, t in rows if re.fullmatch(r"[0-9A-F]{2}", t)]
    s_warn = [t for _, _, t in rows if not re.fullmatch(r"[0-9A-F]{2}", t)]
    s_pos = [a for a, _, t in rows if re.fullmatch(r"[0-9A-F]{2}", t)]
    page = [r["value"] for r in rec]
    tb = 1e12 / o.get("baud", 115200)
    p_pos = [r["t0"] + tb for r in rec]  # start of the data bits
    dt = max((abs(a - b) for a, b in zip(s_pos, p_pos)), default=0)
    ok, diffs = cmp_lists(page, s_bytes)
    perr = sum(1 for r in rec if not r["frameOk"] or r["parityOk"] is False)
    return dict(compared="bytes", n_page=len(page), n_sigrok=len(s_bytes), equal=ok, diffs=diffs, page_errors=perr,
                sigrok_warnings=s_warn[:5], max_data_start_offset_ns=round(dt / 1000, 3), cmd=cmd, sample_values=[f"{v:02X}" for v in page[:12]])


def do_spi(vcd, d, rec):
    o = d.get("options", {})
    base = f"spi:{pins(d, ('clk', 'clk'), ('mosi', 'mosi'), ('miso', 'miso'), ('cs', 'cs'))}:cpol={o.get('cpol', 0)}:cpha={o.get('cpha', 0)}"
    res = {"compared": "MOSI and MISO words", "cmd": None}
    allok = True
    for side in ("mosi", "miso"):
        rows, cmd = sigrok(vcd, base, f"spi={side}-data")
        first = rec[0]["t0"] if rec else 0
        skipped = sum(1 for a, _, _ in rows if a < first - DS)
        s = [int(t, 16) for a, _, t in rows if a >= first - DS]
        p = [r[side] for r in rec]
        res["sigrok_words_before_first_cs"] = skipped
        ok, diffs = cmp_lists(p, s)
        allok &= ok
        res[side] = dict(n_page=len(p), n_sigrok=len(s), equal=ok, diffs=diffs, sample_values=[f"{v:02X}" if v is not None else None for v in p[:12]])
        res["cmd"] = cmd.replace(f"spi={side}-data", "spi=mosi-data | miso-data")
    res["equal"] = allok
    res["n_page"] = res["mosi"]["n_page"]
    return res


I2C_MAP = {"Start": "S", "Start repeat": "Sr", "Stop": "P", "ACK": "A", "NACK": "N"}


def do_i2c(vcd, d, rec):
    rows, cmd = sigrok(vcd, f"i2c:{pins(d, ('scl', 'scl'), ('sda', 'sda'))}", "i2c=addr-data")
    s = []
    for a, _, t in sorted(rows, key=lambda r: r[0]):
        if t in I2C_MAP:
            s.append(I2C_MAP[t])
        else:
            m = re.match(r"(Address|Data) (read|write): ([0-9A-F]+)", t)
            if m:
                s.append(f"{m.group(1)[0]}{m.group(2)[0].upper()}:{int(m.group(3), 16):02X}")
    pm = {"start": "S", "repeated start": "Sr", "stop": "P", "ack": "A", "nack": "N"}
    p = []
    for r in sorted(rec, key=lambda r: r["t"]):
        if r["type"] in pm:
            p.append(pm[r["type"]])
        else:
            kind, rw = r["type"].split()
            p.append(f"{kind[0].upper()}{rw[0].upper()}:{r['value']:02X}")
    ok, diffs = cmp_lists(p, s)
    return dict(compared="events (S, Sr, P, address, data, ACK, NACK)", n_page=len(p), n_sigrok=len(s), equal=ok, diffs=diffs,
                sample_values=p[:16], cmd=cmd)


def do_can(vcd, d, rec):
    o = d.get("options", {})
    pd = f"can:{pins(d, ('rx', 'can_rx'))}:nominal_bitrate={o.get('bitrate', 250000)}:sample_point={round(o.get('samplePoint', 0.75) * 100)}"
    rows, cmd = sigrok(vcd, pd, "can=fields:warnings")
    frames, cur, warns = [], None, []
    for a, _, t in sorted(rows, key=lambda r: r[0]):
        if t == "Start of frame":
            cur = {"id": None, "ext": False, "rtr": False, "dlc": None, "data": [], "crc": None, "ack": None}
            frames.append(cur)
            continue
        if cur is None:
            continue
        m = re.match(r"Identifier: (\d+)", t)
        if m and not cur["ext"]:
            cur["id"] = int(m.group(1))
        m = re.match(r"Full Identifier: (\d+)", t)
        if m:
            cur["id"], cur["ext"] = int(m.group(1)), True
        if t.startswith("Remote transmission request:"):
            cur["rtr"] = "remote" in t
        m = re.match(r"Data length code: (\d+)$", t)
        if m:
            cur["dlc"] = int(m.group(1))
        m = re.match(r"Data byte \d+: 0x([0-9a-fA-F]+)", t)
        if m:
            cur["data"].append(int(m.group(1), 16))
        m = re.match(r"CRC-15 sequence: 0x([0-9a-fA-F]+)", t)
        if m:
            cur["crc"] = int(m.group(1), 16)
        if t.startswith("ACK slot:"):
            cur["ack"] = t.endswith(": ACK")
        if "must" in t or "error" in t.lower() or "not allowed" in t:
            warns.append(t)
    s = [(f["id"], f["ext"], f["rtr"], f["dlc"], f["data"], f["crc"], f["ack"]) for f in frames]
    p = [(r.get("id"), r.get("ext"), r.get("rtr"), r.get("dlc"), r.get("data"), r.get("crc"), r.get("ack")) if "error" not in r
         else ("error", r["error"]) for r in rec]
    p = [list(x) for x in p]
    s = [list(x) for x in s]
    ok, diffs = cmp_lists(p, s)
    n_eq = sum(1 for a, b in zip(p, s) if a == b)
    rtr_diff = [i for i, (a, b) in enumerate(zip(p, s)) if a != b and a[2] is True]
    return dict(equal_frames=n_eq, differing_remote_frames=rtr_diff,
                sigrok_rule="sigrok 0.7.2 reads DLC data bytes after a remote frame too (can/pd.py lines 267-293), so the CRC and ACK of a remote frame with DLC > 0 are read from the wrong bits",
                equal_after_rule=all(a == b for i, (a, b) in enumerate(zip(p, s)) if i not in rtr_diff) and len(p) == len(s),
                compared="frames (identifier, IDE, RTR, DLC, data bytes, CRC-15, ACK slot)", n_page=len(p), n_sigrok=len(s), equal=ok,
                diffs=diffs, sigrok_warnings=warns[:5], page_crc_ok=[r.get("crcOk") for r in rec],
                sample_values=[f"id 0x{x[0]:03X} dlc {x[3]} data {' '.join(f'{b:02X}' for b in x[4])}" for x in p if x[0] != "error"][:8], cmd=cmd)


def do_jtag(vcd, d, rec, meta):
    pd = f"jtag:{pins(d, ('tdi', 'tdi'), ('tdo', 'tdo'), ('tck', 'tck'), ('tms', 'tms'))}"
    rows, cmd = sigrok(vcd, pd, "jtag=bitstrings-tdi:bitstrings-tdo:states")
    s_scan, s_states = [], []
    for a, _, t in sorted(rows, key=lambda r: r[0]):
        m = re.match(r"(IR|DR) (TDI|TDO): ([01]+) \(", t)
        if m:
            if m.group(2) == "TDI":
                s_scan.append([m.group(1), m.group(3)[::-1], None])
            else:
                s_scan[-1][2] = m.group(3)[::-1]
        elif re.fullmatch(r"[A-Z0-9/-]+", t):
            s_states.append(t)
    # The page gives one record per Shift segment; a scan through Pause-DR has two. Merge them per scan (Capture to Update).
    merged = []
    for r in rec:
        if merged and merged[-1]["scan"] == r["scan"]:
            m = merged[-1]
            m["cuts"].append(len(m["tdi"]))
            m["tdi"] += r["tdi"]; m["tdo"] += r["tdo"]
        else:
            merged.append({"scan": r["scan"], "reg": r["reg"], "tdi": r["tdi"], "tdo": r["tdo"], "cuts": []})
    p_scan = [[m["reg"], m["tdi"], m["tdo"]] for m in merged]
    ok1, d1 = cmp_lists(p_scan, s_scan)
    # sigrok 0.7.2 also collects a bit when the old state is Exit1 (jtag/pd.py lines 182-195): one extra bit at each
    # Exit1 to Pause step. Remove it at the cut positions of the page, then compare again.
    adj = []
    for m, sv in zip(merged, s_scan):
        a, b = sv[1], sv[2]
        for k, c in enumerate(m["cuts"]):
            a = a[:c] + a[c + 1:]
            b = b[:c] + b[c + 1:] if b else b
        adj.append([sv[0], a, b])
    ok1a, d1a = cmp_lists(p_scan, adj + s_scan[len(adj):])
    # states: both from the first Run-Test/Idle (the decoders assume different start states before the first reset)
    p_states = [st.upper() for _, st in meta["states"]]
    def from_rti(xs):
        return xs[xs.index("RUN-TEST/IDLE"):] if "RUN-TEST/IDLE" in xs else xs
    ps, ss = from_rti(p_states), from_rti(s_states)
    # sigrok annotates a state from its edge to the next edge, so the state entered at the last edge has no annotation
    last_extra = len(ps) == len(ss) + 1
    ok2, d2 = cmp_lists(ps[:len(ss)] if last_extra else ps, ss)
    pause_bits = sum(len(m["cuts"]) for m in merged)
    return dict(compared="scans (IR or DR, every TDI and TDO bit) and the TAP state at every TCK edge", n_page=len(p_scan),
                n_sigrok=len(s_scan), equal=ok1 and ok2, equal_after_rule=ok1a and ok2, scans_equal=ok1, scans_equal_after_rule=ok1a,
                sigrok_rule=f"sigrok 0.7.2 adds one bit per Exit1-DR to Pause-DR step (jtag/pd.py lines 182-195); {pause_bits} such step(s) here",
                states_equal=ok2, page_extra_last_state=last_extra, n_states_page=len(ps), n_states_sigrok=len(ss), n_bits_page=sum(len(r[1]) for r in p_scan),
                diffs=d1[:3] + d1a + d2, sample_values=[f"{r[0]} {len(r[1])} bits" for r in p_scan[:8]], cmd=cmd)


SWD_REQ = {"IDCODE": (0, 1, 0), "W ABORT": (0, 0, 0), "R CTRL/STAT": (0, 1, 4), "W CTRL/STAT": (0, 0, 4), "W SELECT": (0, 0, 8),
           "R RESEND": (0, 1, 8), "RDBUFF": (0, 1, 12), "R RDBUFF": (0, 1, 12)}
SWD_ACK = {"OK": 1, "WAIT": 2, "FAULT": 4}


def do_swd(vcd, d, rec):
    rows, cmd = sigrok(vcd, f"swd:{pins(d, ('swclk', 'swclk'), ('swdio', 'swdio'))}", None)
    s, unknown = [], set()
    for a, _, t in sorted(rows, key=lambda r: r[0]):
        if t == "LINERESET":
            s.append(["line reset"])
        elif t == "JTAG->SWD":
            s.append(["jtag-to-swd"])
        elif t in SWD_ACK:
            if s and s[-1][0] == "transfer":
                s[-1][4] = SWD_ACK[t]
        elif re.fullmatch(r"0x[0-9a-f]{8}", t):
            if s and s[-1][0] == "transfer":
                s[-1][5] = int(t, 16)
        else:
            m = re.fullmatch(r"([RW]) AP([0-9A-F]+)", t)
            if m:
                s.append(["transfer", 1, 1 if m.group(1) == "R" else 0, int(m.group(2), 16), None, None])
            elif t in SWD_REQ:
                a2, r2, ad = SWD_REQ[t]
                s.append(["transfer", a2, r2, ad, None, None])
            else:
                unknown.add(t)
    p = []
    for r in rec:
        if r["type"] == "transfer":
            p.append(["transfer", r["apndp"], r["rnw"], r["addr"], r["ack"] if r["ack"] in (1, 2, 4) else None, r["data"]])
        else:
            p.append([r["type"]])
    ok, diffs = cmp_lists(p, s)
    return dict(compared="line resets, select sequence, requests (APnDP, RnW, A), ACK, data", n_page=len(p), n_sigrok=len(s), equal=ok,
                diffs=diffs, sigrok_other=sorted(unknown)[:10], cmd=cmd)


def do_ps2(vcd, d, rec):
    rows, cmd = sigrok(vcd, f"ps2:{pins(d, ('clk', 'clk'), ('data', 'data'))}", "ps2=fields")
    s = []
    for a, b, t in sorted(rows, key=lambda r: r[0]):
        m = re.match(r"Data: ([0-9a-f]{2})", t)
        if m:
            s.append({"t": a, "value": int(m.group(1), 16), "parityOk": None})
        elif t.startswith("Parity") and s:
            s[-1]["parityOk"] = t == "Parity OK"
    host = [(r["t0"], r["t0"] + 13 * 80e6) for r in rec if r["dir"] == "host"]
    s_dev = [x for x in s if not any(h0 - 1e6 <= x["t"] <= h1 for h0, h1 in host)]
    p = [[r["value"], r["parityOk"]] for r in rec if r["dir"] == "device" and r.get("value") is not None]
    sv = [[x["value"], x["parityOk"]] for x in s_dev]
    ok, diffs = cmp_lists(p, sv)
    # Check of the explanation: the frame rule of sigrok 0.7.2 as its source states it (bits on falling edges, a 1 before
    # the start is skipped, a frame is emitted at the 12th falling edge and that edge is used up), applied to the
    # falling-edge DATA bits of the same file.
    sys.path.insert(0, HERE)
    import trim_vcd
    raw, _ = trim_vcd.read_raw(os.path.join(SAMPLES, vcd))
    clk, dat = raw[d["pins"]["clk"]], raw[d["pins"]["data"]]
    def val_before(ch, t):
        v = "x"
        for tt, vv in ch:
            if tt < t:
                v = vv
            else:
                break
        return v
    bits, model = [], []
    for k in range(1, len(clk)):
        if clk[k][1] == "0" and clk[k - 1][1] == "1":
            b = 1 if val_before(dat, clk[k][0] + 1) == "1" else 0
            if not bits and b == 1:
                continue
            bits.append(b)
            if len(bits) == 12:
                w = sum(bits[1 + i] << i for i in range(8))
                model.append([w, (bin(w).count("1") + bits[9]) % 2 == 1])
                bits = []
    rule_reproduces = model == [[x["value"], x["parityOk"]] for x in s]
    return dict(sigrok_rule_model=[[f"{a:02X}", b] for a, b in model], rule_reproduces_sigrok=rule_reproduces,
                equal_after_rule=rule_reproduces and bool(p) and p[:3] == [[0xAA, True], [0x1C, True], [0xF0, True]],
                sigrok_rule="sigrok 0.7.2 emits a PS/2 frame at the 12th falling clock edge and uses that edge up (ps2/pd.py lines 73-93): "
                "when the next frame follows with no extra clock, its start bit is lost and the frame is read one bit late",
                equal_first=bool(p and sv and p[0] == sv[0]), compared="device-to-host frames (byte, parity); sigrok's ps2 decoder reads device-to-host only, so the host-to-device "
                "frame and the device answer within 13 clocks after it are left out of the sigrok list", n_page=len(p), n_sigrok=len(sv),
                equal=ok, diffs=diffs, page_host_frames=[[f"{r['value']:02X}", r["ack"]] for r in rec if r["dir"] == "host"],
                sigrok_all=[[f"{x['value']:02X}", x["parityOk"]] for x in s], cmd=cmd)


def do_onewire(vcd, d, rec):
    rows, cmd = sigrok(vcd, f"onewire_link:{pins(d, ('owr', 'owr'))},onewire_network", None)
    link, net = [], []
    for a, _, t in sorted(rows, key=lambda r: r[0]):
        if t == "Reset":
            link.append("R")
        elif t.startswith("Presence: "):
            link.append("P1" if t.endswith("true") else "P0")
        else:
            m = re.fullmatch(r"Bit: ([01])", t)
            if m:
                link.append(m.group(1))
            m = re.match(r"(ROM command|Data): 0x([0-9a-fA-F]+)", t)
            if m:
                net.append(int(m.group(2), 16))
            m = re.match(r"ROM: 0x([0-9a-fA-F]{16})", t)
            if m:
                v = int(m.group(1), 16)
                net += [(v >> (8 * k)) & 0xFF for k in range(8)]
    p_link = []
    for r in rec:
        if r["type"] == "reset":
            p_link.append("R")
        elif r["type"] == "presence":
            p_link.append("P1")
        elif r["type"] == "bit":
            p_link.append(str(r["value"]))
    p_bytes = [r["value"] for r in rec if r["type"] == "byte"]
    ok1, d1 = cmp_lists(p_link, link)
    ok2, d2 = cmp_lists(p_bytes, net)
    return dict(compared="link layer (reset, presence, every bit) and bytes (ROM command, ROM, data)", n_page=len(p_link), n_sigrok=len(link),
                equal=ok1 and ok2, link_equal=ok1, bytes_equal=ok2, n_bytes_page=len(p_bytes), n_bytes_sigrok=len(net), diffs=d1 + d2,
                sample_values=[f"{b:02X}" for b in p_bytes[:14]], cmd=cmd)


USB_TOKEN = re.compile(r"^(OUT|IN|SETUP|PING) ADDR (\d+) EP (\d+)$")
USB_DATA = re.compile(r"^(DATA0|DATA1|DATA2|MDATA) \[((?: [0-9A-F]{2})*) \]$")
USB_HS = re.compile(r"^(ACK|NAK|STALL|NYET|PRE|ERR)$")


def do_usb(vcd, d, rec):
    o = d.get("options", {})
    sp = "low-speed" if o.get("speed", "low") == "low" else "full-speed"
    pd = f"usb_signalling:{pins(d, ('dp', 'dp'), ('dm', 'dm'))}:signalling={sp},usb_packet:signalling={sp}"
    rows, cmd = sigrok(vcd, pd, "usb_packet=packet:fields")
    rows.sort(key=lambda r: r[0])
    packets, crcs = [], []
    for a, b, t in rows:
        m = USB_TOKEN.match(t)
        if m:
            packets.append([a, b, [m.group(1), int(m.group(2)), int(m.group(3)), None, None]])
            continue
        m = USB_DATA.match(t)
        if m:
            data = [int(x, 16) for x in m.group(2).split()]
            packets.append([a, b, [m.group(1), None, None, data, None]])
            continue
        if USB_HS.match(t):
            packets.append([a, b, [t, None, None, None, None]])
            continue
        m = re.match(r"(CRC5|CRC16)( ERROR)?: 0x", t)
        if m:
            crcs.append((a, m.group(2) is None))
    for a, b, p in packets:   # the CRC field of a packet lies inside it
        for t, ok in crcs:
            if a <= t <= b:
                p[4] = ok
    s = [p for _, _, p in packets]
    page = [[r["name"], r["addr"], r["endp"], r["data"], r["crcOk"]] for r in rec]
    ok, diffs = cmp_lists(page, s)
    bad = sum(1 for r in rec if not r["syncOk"] or not r["pidOk"] or r["crcOk"] is False or r["stuffErr"] or not r["eopOk"])
    return dict(compared="packets (PID, address, endpoint, data bytes, CRC5 or CRC16 valid)", n_page=len(page),
                n_sigrok=len(s), equal=ok, diffs=diffs, page_errors=bad, cmd=cmd,
                sample_values=[" ".join(str(x) for x in p[:3] if x is not None) +
                               (f" {len(p[3])} bytes" if p[3] is not None else "") for p in page[:12]])


def do_eth10(vcd, d, rec):
    """sigrok-cli 0.7.2 has no 10BASE-T decoder: the comparison is with the repository's 802.3 partner model
    (test/linemodel/tenbaset.py decode_tx, written from IEEE 802.3 clauses 3, 7 and 14, not from the page)."""
    root = os.path.dirname(os.path.dirname(HERE))
    for p in (HERE, os.path.join(root, "test")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import trim_vcd
    from linemodel import tenbaset
    from linemodel.wave import Wave
    raw, end = trim_vcd.read_raw(os.path.join(SAMPLES, vcd))
    roles = {r: raw[n] for r, n in d["pins"].items()}
    times = sorted({t for ch in roles.values() for t, _ in ch})
    cur = {r: "x" for r in roles}
    idx = {r: 0 for r in roles}

    def level():
        if "p" in roles:
            if "oe" in roles and cur["oe"] == "0":
                return 0
            p, n = cur["p"] == "1", cur["n"] == "1"
            return 1 if p and not n else (-1 if n and not p else 0)
        return (1 if cur["rx"] == "1" else -1) if cur["sq"] == "1" else 0
    edges, init = [], None
    for t in times:
        for r, ch in roles.items():
            while idx[r] < len(ch) and ch[idx[r]][0] <= t:
                cur[r] = ch[idx[r]][1]
                idx[r] += 1
        lv = level()
        if init is None:
            init = lv
        elif lv != (edges[-1][1] if edges else init):
            edges.append((t, lv))
    evs = tenbaset.decode_tx(Wave(init or 0, edges, end=end))
    s = [["pulse"] if e.kind == "pulse" else ["packet", list(e.frame), tenbaset.eth_fcs_ok(e.frame)] for e in evs]
    page = [["pulse"] if r["kind"] == "pulse" else ["packet", r["bytes"], r["fcsOk"]] for r in rec]
    ok, diffs = cmp_lists(page, s)
    return dict(compared="packets (every octet with the FCS, FCS valid) and link test pulses, against the repository's "
                "802.3 partner model decoder (sigrok-cli 0.7.2 has no 10BASE-T decoder)", n_page=len(page), n_sigrok=len(s),
                equal=ok, diffs=diffs[:3], cmd="test/linemodel/tenbaset.py decode_tx",
                sample_values=[f"{r['kind']} {len(r.get('bytes', []))} octets" for r in rec[:6]])


def main(samples, dump_path, out_path):
    global SAMPLES
    SAMPLES = os.path.abspath(samples)
    dump = json.load(open(dump_path, encoding="utf-8"))
    manifest = json.load(open(os.path.join(SAMPLES, "manifest.json"), encoding="utf-8"))
    version = subprocess.run([SG, "--version"], capture_output=True, text=True).stdout.strip().splitlines()
    results = []
    for s in manifest["samples"]:
        for d in s["decoders"]:
            got = dump[s["file"]]["decoders"][d["name"]]
            rec = got["records"]
            fn = d["protocol"]
            if got["error"]:
                r = dict(compared="nothing", n_page=0, equal=False, page_error=got["error"])
            elif fn == "uart":
                r = do_uart(s["file"], d, rec)
            elif fn == "spi":
                r = do_spi(s["file"], d, rec)
            elif fn == "i2c":
                r = do_i2c(s["file"], d, rec)
            elif fn == "can":
                r = do_can(s["file"], d, rec)
            elif fn == "jtag":
                r = do_jtag(s["file"], d, rec, got["meta"])
            elif fn == "swd":
                r = do_swd(s["file"], d, rec)
            elif fn == "ps2":
                r = do_ps2(s["file"], d, rec)
            elif fn == "onewire":
                r = do_onewire(s["file"], d, rec)
            elif fn == "usb":
                r = do_usb(s["file"], d, rec)
            elif fn == "eth10":
                r = do_eth10(s["file"], d, rec)
            r.update(sample=s["file"], decoder=d["name"], protocol=fn, page_decode_ms=got["ms"])
            n = r.get("n_page", 0)
            r["expect"] = d.get("expect")
            r["pass"] = bool((r["equal"] or r.get("equal_after_rule") is True) and n > 0
                             and (d.get("expect") is None or n == d["expect"]))
            results.append(r)
            how = "EQUAL" if r["equal"] else ("EQUAL AFTER RULE" if r.get("equal_after_rule") else "DIFFERENT")
            print(f"{'PASS' if r['pass'] else 'FAIL'} {s['file']:28s} {d['name']:10s} {how:16s} page {n} "
                  f"sigrok {r.get('n_sigrok', r.get('mosi', {}).get('n_sigrok'))} expect {d.get('expect')}")
    ok = bool(results) and all(r["pass"] for r in results)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"sigrok": version, "downsample_ps": DS, "page_total_ms": dump.get("_total_ms"), "pass": ok,
                   "results": results}, f, indent=1)
    print(f"{sum(1 for r in results if r['equal'])} equal, "
          f"{sum(1 for r in results if not r['equal'] and r.get('equal_after_rule'))} equal after a sigrok 0.7.2 rule, "
          f"{sum(1 for r in results if not r['pass'])} failing, of {len(results)} decoder runs; {' '.join(version[:1])}")
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(*sys.argv[1:]))
