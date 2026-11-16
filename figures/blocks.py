"""Block diagram of the chip (SVG), drawn from the RTL hierarchy.

What it does:
  Yosys reads the RTL files of info.yaml (the files of the layout flow) and writes the module hierarchy as JSON.
  This script counts the instances of each module in that JSON and reads the sizes from it: memory words and
  width (pe_latch_ram parameters), threads (width of the FIFO thread select), FIFO depth, event slots, inputs,
  synchronizers, spike filters and the cycle-counter width. It draws the blocks with these numbers.
  It stops with an error if the RTL has a module that the drawing does not show, so the diagram cannot fall
  behind the RTL. The pin names come from info.yaml.

Usage: python figures/blocks.py <out.svg> [<hierarchy.json>]
  Without the JSON it runs Yosys (yosys on the PATH; .github/workflows/docs.yaml installs it).
"""

import json
import os
import re
import subprocess
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import svg as S  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Every module of the RTL hierarchy, and where the drawing shows it.
DRAWN = {
    "pe_reset_sync": "reset synchronizer",
    "pe_sync2": "host-pin and input synchronizers",
    "pe_host_port": "host SPI port",
    "pe_cycle_counter": "cycle counter",
    "pe_config": "configuration",
    "pe_proc": "processor container",
    "pe_core": "barrel core",
    "pe_sram": "program and data memory (SRAM macros)",
    "pe_latch_ram": "FIFO words",
    "pe_lat": "latch bits (counted in the FIFO box)",
    "pe_cg": "clock gates (counted in the FIFO box)",
    "pe_host_fifo": "host FIFOs",
    "pe_timed_out": "event slots",
    "pe_timed_in": "input unit",
    "pe_spike_filter": "spike filters (in the input box)",
    "pe_slu": "line helpers",
    "pe_slu_loop": "receive loop (in the helper boxes)",
    "pe_slu_loop_step": "loop steps (counted in the helper boxes)",
}
# Black-box macros: in the hierarchy JSON only if Yosys writes the black-box modules; read from the cell types.
MACROS = {
    "RM_IHPSG13_1P_512x16_c2_bm_bist": "program memory macro",
    "RM_IHPSG13_1P_1024x16_c2_bm_bist": "data memory macro",
}


def info_yaml():
    text = open(os.path.join(ROOT, "info.yaml"), encoding="utf-8").read()
    top = re.search(r'^\s*top_module:\s*"([^"]+)"', text, re.M).group(1)
    block = text.split("source_files:", 1)[1].split("\n\n", 1)[0]
    files = re.findall(r'^\s*-\s*"([^"]+)"', block, re.M)
    clock = int(re.search(r"^\s*clock_hz:\s*(\d+)", text, re.M).group(1))
    tiles = re.search(r'^\s*tiles:\s*"([^"]+)"', text, re.M).group(1)
    pins = dict(re.findall(r'^\s*(u[io]o?\[\d\]|uio\[\d\]):\s*"([^"]*)"', text, re.M))
    return top, files, clock, tiles, pins


def run_yosys(top, files, out_json):
    paths = " ".join(os.path.join(ROOT, "src", f) for f in files)
    # -nosynthesis: no SYNTHESIS macro, so src/pe_cells.v uses its behavioural latch and clock-gate models (the
    # library cells are not needed to read the structure).
    script = f"read_verilog -nosynthesis -sv {paths}; hierarchy -check -top {top}; proc; write_json -compat-int {out_json}"
    subprocess.run(["yosys", "-q", "-p", script], check=True)


def base(name):
    """Module name without the parameter suffix that Yosys adds ($paramod\\name\\P=... or $paramod$hash\\name)."""
    if name.startswith("$paramod"):
        return name.split("\\")[1]
    return name.lstrip("\\")


def to_int(v):
    if isinstance(v, int):
        return v
    v = str(v)
    if v and set(v) <= {"0", "1"}:
        return int(v, 2)
    return int(v)


def params(mods, name):
    p = {k: to_int(v) for k, v in mods[name].get("parameter_default_values", {}).items() if str(v).strip("01") == ""
         or isinstance(v, int)}
    for k, bits, dec in re.findall(r"\\(\w+)=(?:s?\d+'([01]+)|(\d+))", name):
        p[k] = int(bits, 2) if bits else int(dec)
    return p


class Hier:
    def __init__(self, data, top):
        self.mods = data["modules"]
        self.top = top
        self.total = Counter()
        self._walk(top, 1)

    def children(self, name):
        for cell in self.mods[name]["cells"].values():
            t = cell["type"]
            if t in self.mods:
                yield t

    def _walk(self, name, mult):
        for t in self.children(name):
            self.total[base(t)] += mult
            self._walk(t, mult)

    def variants(self, b):
        return [m for m in self.mods if base(m) == b]

    def count_in(self, parent, child):
        """Instances of module `child` directly inside one instance of `parent` (any parameter variant)."""
        counts = [sum(1 for t in self.children(m) if base(t) == child) for m in self.variants(parent)]
        return max(counts) if counts else 0

    def child_variant(self, parent, child):
        for m in self.variants(parent):
            for t in self.children(m):
                if base(t) == child:
                    return t
        raise KeyError(f"{child} not found in {parent}")

    def port_width(self, module, port):
        m = self.mods[self.variants(module)[0]]
        return len(m["ports"][port]["bits"])


def facts(h):
    mem = h.child_variant("pe_proc", "pe_latch_ram")
    mp = params(h.mods, mem)
    fifo_ram = h.child_variant("pe_host_fifo", "pe_latch_ram")
    fp = params(h.mods, fifo_ram)
    threads = 2 ** h.port_width("pe_host_fifo", "h_tid")
    filt = h.child_variant("pe_timed_in", "pe_spike_filter")
    counter = h.child_variant(h.top, "pe_cycle_counter")
    f = dict(
        threads=threads,
        mem_words=mp["WORDS"], mem_width=mp["WIDTH"],
        fifo_rams=h.count_in("pe_host_fifo", "pe_latch_ram"),
        fifo_words=fp["WORDS"], fifo_width=fp["WIDTH"],
        slots=h.count_in("pe_proc", "pe_timed_out"),
        inputs=h.count_in("pe_timed_in", "pe_sync2"),
        host_sync=h.count_in(h.top, "pe_sync2"),
        filters=h.count_in("pe_timed_in", "pe_spike_filter"),
        filter_k=params(h.mods, filt).get("K"),
        counter_width=params(h.mods, counter).get("WIDTH"),
        lat_total=h.total["pe_lat"], cg_total=h.total["pe_cg"], sync_total=h.total["pe_sync2"],
    )
    # The latch and clock-gate counts of one memory follow from its generate loops (one pe_cg per word, one pe_lat
    # per bit); check them against the hierarchy totals.
    want_lat = f["mem_words"] * f["mem_width"] + f["fifo_rams"] * f["fifo_words"] * f["fifo_width"]
    want_cg = f["mem_words"] + f["fifo_rams"] * f["fifo_words"]
    assert (f["lat_total"], f["cg_total"]) == (want_lat, want_cg), (f, want_lat, want_cg)
    f["fifo_depth"] = f["fifo_words"] // threads
    return f


def draw(f, top, clock, tiles, pins, commit, out):
    s = S.Svg(1160, 720)
    mhz = clock / 1e6
    s.text(20, 32, "Block diagram of the chip, drawn from the RTL hierarchy", size=19, weight="bold")
    s.text(20, 54, f"{top}  ·  one clock domain (clk), declared {mhz:g} MHz  ·  IHP SG13CMOS5L, {tiles} tiles",
           size=12, colour="muted")

    def box(x, y, w, h, title, rows, fill=S.FILL, size=11.5):
        s.rect(x, y, w, h, fill=fill)
        s.text(x + 10, y + 19, title, size=13, weight="bold", mono=True, colour="line")
        s.lines(x + 10, y + 37, rows, size=size)

    # Host and the host pins
    s.rect(20, 80, 170, 120, fill="#f6f7f9", stroke=S.MUTED)
    s.text(32, 104, "Host", size=13, weight="bold")
    s.lines(32, 124, ["RP2350 of the demo board,", "or any SPI host"], size=12)
    for i, (pin, name) in enumerate((("ui[7]", "SCK"), ("ui[6]", "CS_N"), ("ui[5]", "MOSI"))):
        y = 104 + 22 * i
        s.line(190, y, 270, y, end=True)
        s.text(196, y - 4, f"{name} {pin.replace('ui', 'ui_in')}", size=10, colour="muted", mono=True)
    box(270, 88, 110, 70, f"pe_sync2 ×{f['host_sync']}", ["2-flip-flop", "synchronizers"])
    s.line(380, 126, 440, 126, end=True)
    s.path([(440, 186), (190, 186)], end=True)
    s.text(196, 181, "MISO uo_out[7]", size=10, colour="muted", mono=True)

    box(440, 80, 270, 120, "pe_host_port", [
        "SPI mode 0 target",
        "STOP, START, WRITE_MEM, READ_MEM,",
        "WRITE_CFG, READ_ID, READ_STATUS,",
        "PUSH, PULL, CLEAR"])
    box(750, 78, 210, 62, "pe_config", [f"start address ×{f['threads']}, RUN mask,",
                                        f"owner, open drain ×{f['slots']} pins"], size=11)
    box(750, 152, 210, 62, "pe_cycle_counter", [f"{f['counter_width']} bit, START clears it",
                                               "now = bits 15:0"], size=11)
    s.line(710, 109, 750, 109, end=True)
    s.line(710, 183, 750, 183, end=True)
    s.text(716, 177, "START", size=9.5, colour="muted")

    # Reset
    s.text(22, 621, "rst_n", size=11, mono=True, colour="muted")
    s.line(62, 617, 80, 617, end=True)
    box(80, 590, 150, 56, "pe_reset_sync", ["assert at once,", "release at a clock edge"], size=10.5)

    # Processor container
    s.rect(250, 245, 700, 425, fill="#fbfcfe", stroke=S.LINE, dash="6 4", rx=10)
    s.text(262, 264, "pe_proc", size=13, weight="bold", mono=True, colour="line")
    s.text(330, 264, "held in reset while the threads are stopped (STOP)", size=11, colour="muted")

    s.rect(270, 278, 330, 172)
    s.text(280, 297, "pe_core", size=13, weight="bold", mono=True, colour="line")
    s.text(350, 297, f"{f['threads']}-thread barrel core", size=12, weight="bold")
    for n in range(f["threads"]):
        x = 285 + n * 77
        s.rect(x, 308, 70, 42, fill=S.FILL2, rx=4)
        s.text(x + 35, 325, f"thread {n}", size=11, anchor="middle", weight="bold")
        s.text(x + 35, 341, f"c mod {f['threads']} = {n}", size=10, anchor="middle", mono=True)
    s.lines(282, 370, ["one 16-bit instruction per cycle c, threads in turn;",
                       "a waiting thread keeps its slot",
                       "per thread: PC, T (16-bit time), r0 to r3,",
                       "flags Z C N E, RUN, LATE, ERR"], size=11.5)

    lat_mem = f["mem_words"] * f["mem_width"]
    box(270, 468, 330, 82, f"pe_latch_ram  {f['mem_words']} × {f['mem_width']}", [
        "programs and data of all threads",
        f"{lat_mem:,} latches (pe_lat), {f['mem_words']} clock gates (pe_cg)"])
    lat_fifo = f["fifo_rams"] * f["fifo_words"] * f["fifo_width"]
    box(270, 568, 330, 82, "pe_host_fifo", [
        f"{f['fifo_depth']}-word in-FIFO and {f['fifo_depth']}-word out-FIFO per thread",
        f"{f['fifo_rams']} × pe_latch_ram {f['fifo_words']} × {f['fifo_width']}: {lat_fifo} latches"])
    box(630, 278, 300, 152, f"pe_timed_out ×{f['slots']}", [
        "one event slot per output pin:",
        "event time (16 bit), value, OE",
        "the pin flip-flop changes in the cycle",
        "when now = event time",
        "late event: 4 cycles after OUT, LATE flag"])
    box(630, 468, 300, 182, "pe_timed_in", [
        f"{f['inputs']} inputs, pe_sync2 ×{f['inputs']}",
        f"pe_spike_filter ×{f['filters']} (K = {f['filter_k']}) on uio[6], uio[7]",
        "5-cycle input history shared",
        f"by the {f['threads']} threads: SAMPLE at a given",
        "cycle, WAIT returns the edge cycle"])

    # Connections inside
    s.line(435, 450, 435, 468, start=True, end=True)
    s.text(442, 463, "fetch, LD, ST", size=9.5, colour="muted")
    s.path([(270, 425), (258, 425), (258, 609), (270, 609)], end=True)
    s.line(262, 425, 270, 425, end=True)
    s.line(600, 330, 630, 330, end=True)
    s.path([(630, 520), (614, 520), (614, 412), (600, 412)], end=True)
    s.path([(850, 214), (850, 278)], end=True)
    s.text(856, 250, "now", size=10, colour="muted", mono=True)
    s.path([(750, 132), (730, 132), (730, 238), (500, 238), (500, 278)], end=True)
    s.text(612, 233, "configuration", size=9.5, colour="muted")
    s.path([(470, 200), (470, 224), (236, 224), (236, 631), (270, 631)], end=True)
    s.line(236, 510, 270, 510, end=True)
    s.text(300, 219, "memory load and read back; PUSH, PULL", size=9.5, colour="muted")

    # Pins
    box(980, 288, 160, 56, "uo_out[6:0]", ["outputs 8 to 14"], fill="#f6f7f9")
    box(980, 368, 160, 70, "uio[7:0]", ["pins 0 to 7:", "output with enable, input"], fill="#f6f7f9")
    box(980, 528, 160, 56, "ui_in[4:0]", ["inputs 8 to 12"], fill="#f6f7f9")
    s.line(930, 316, 980, 316, end=True)
    s.line(930, 398, 980, 398, end=True)
    s.path([(980, 422), (960, 422), (960, 545), (930, 545)], end=True)
    s.line(980, 562, 930, 562, end=True)

    s.text(20, 690, f"Counts and sizes from the Yosys hierarchy of the RTL (info.yaml source files) at commit {commit}: "
           f"{f['lat_total']:,} latches, {f['cg_total']} clock gates, {f['sync_total']} two-stage synchronizers.",
           size=11, colour="muted")
    s.text(20, 708, "Pin names from info.yaml. The host port, the threads and the pins share the one clock; "
           "every output comes from a flip-flop.", size=11, colour="muted")
    s.save(out, "Block diagram of the protocol emulator chip",
           f"{f['threads']}-thread barrel core with {f['mem_words']} x {f['mem_width']} latch memory, "
           f"{f['slots']} timed output event slots, {f['inputs']} synchronized inputs and a host SPI port.")


def main(out, hier_json=None):
    top, files, clock, tiles, pins = info_yaml()
    if hier_json is None:
        hier_json = os.path.join(os.path.dirname(os.path.abspath(out)), "hierarchy.json")
        run_yosys(top, files, hier_json)
    with open(hier_json, encoding="utf-8") as fp:
        data = json.load(fp)
    h = Hier(data, top)
    missing = sorted(set(h.total) - set(DRAWN))
    if missing:
        sys.exit(f"modules in the RTL that the block diagram does not show: {missing} (add them to figures/blocks.py)")
    gone = sorted(set(DRAWN) - set(h.total))
    if gone:
        sys.exit(f"modules in the block diagram that the RTL no longer has: {gone}")
    f = facts(h)
    commit = os.environ.get("GITHUB_SHA", "local")[:7]
    draw(f, top, clock, tiles, pins, commit, out)
    with open(os.path.splitext(out)[0] + ".json", "w", encoding="utf-8") as fp:
        json.dump(dict(facts=f, instances=dict(h.total)), fp, indent=1)
    print(f"block diagram: {out}; {dict(h.total)}")


if __name__ == "__main__":
    main(*sys.argv[1:])
