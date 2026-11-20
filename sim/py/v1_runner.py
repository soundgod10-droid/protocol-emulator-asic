# SPDX-License-Identifier: Apache-2.0
"""Runner of the browser simulator: runs an example on the unchanged chip model, assembler and test models.

What it does:
  An example is a procedure of the model tests: a test helper (for example test_fw_full.tx_run) or a
  test function, called with its parameters. The procedure builds the reference model
  (model/pe_model.py), assembles the firmware with asm/pe_asm.py and attaches the device models of the
  tests, then drives the run with the test bench (Bench.run of the test module). This runner changes
  none of that code. While the procedure runs, the test module's Bench class is replaced by a subclass
  that records the run of one bench (the "main" bench of the example): the chip pins of each cycle as
  the tests' watch functions see them, the inputs that the device models give, the host words in both
  directions, and copies of the model state and the host queues at the start of each run() call and
  every SNAP cycles. The subclass calls the test's own Bench.run for every cycle; it only splits a run
  into parts and records.

  The page shows any cycle from these copies: it copies the state before that cycle and runs the
  test's Bench.run up to it, with the device inputs of the recording (ReplayInputs) in place of the
  device model. Between two copies only Bench.run changes the state (host words are given between
  run() calls, and a copy is taken at the start of each run() call), and the inputs are those that the
  device model gave in the run, so the state shown is the state of the procedure's own run.

  trace_hash() is the SHA-256 of the recorded run (pins, inputs, host words, final model state). The
  same example gives the same hash in CPython and in the browser (Pyodide) if both run it alike.

Why:
  The page must show the verified model and the tests' own device models, not a second
  implementation of them, and anyone must be able to check that the browser run equals the test run.
"""

import bisect
import copy
import hashlib
import json
import os
import sys
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
PE = os.path.join(HERE, "pe")
for _p in (os.path.join(PE, "asm"), os.path.join(PE, "model"), os.path.join(PE, "test"), os.path.join(HERE, "stub")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pe_asm  # noqa: E402
import pe_model  # noqa: E402
import test_fw_full  # noqa: E402
import test_flex  # noqa: E402
import test_holdout  # noqa: E402
import test_isa  # noqa: E402

SNAP = 8192  # cycles between state copies inside a run() call
POST_S = 0.06  # seconds between two progress messages to the page
LIMIT = 4_000_000  # most cycles of one main bench (a firmware edit can make a procedure wait for ever)
TEST_MODULES = (test_fw_full, test_flex, test_holdout, test_isa)
ORIG_ASSEMBLE = pe_asm.assemble


class RunLimit(Exception):
    """The main bench reached LIMIT cycles."""


class Log:
    """Stands for dut._log of a test function: keeps the lines."""

    def __init__(self):
        self.lines = []

    def info(self, text):
        self.lines.append(str(text))

    warning = error = debug = info


class FakeDut:
    """The test functions of the model tests use only dut._log."""

    def __init__(self):
        self._log = Log()


def run_test_function(fn):
    """Runs an async model test function to its end (model tests await nothing). Returns its log lines."""
    dut = FakeDut()
    coro = fn(dut)
    try:
        coro.send(None)
    except StopIteration:
        return dut._log.lines
    coro.close()
    raise RuntimeError(f"{fn.__name__} waits for a simulator event; it is not a model-only test")


class InputRecorder:
    """Calls the device callable of the bench and records the inputs that the model gets."""

    def __init__(self, f):
        self.f = f
        self.v1_changes = []
        self.ui = 0
        self.uio = 0

    def __call__(self, c, uo, uio, oe):
        ui_in, uio_in = self.f(c, uo, uio, oe)
        ui = self.ui if ui_in is None else ui_in & 0xFF
        uiov = self.uio if uio_in is None else uio_in & 0xFF
        if not self.v1_changes or ui != self.ui or uiov != self.uio:
            self.v1_changes.append((c, ui, uiov))
        self.ui, self.uio = ui, uiov
        return ui_in, uio_in

    def __getattr__(self, name):  # a procedure may read attributes of its device (bench.inputs.changes)
        return getattr(self.__dict__["f"], name)


class ReplayInputs:
    """The chip inputs that the device model gave in the recorded run, cycle by cycle (InputRecorder.v1_changes)."""

    def __init__(self, changes):
        self.c = [x[0] for x in changes]
        self.v = [(x[1], x[2]) for x in changes]

    def __call__(self, c, uo, uio, oe):
        k = bisect.bisect_right(self.c, c) - 1
        return self.v[k] if k >= 0 else (None, None)


def _copy_function(f, memo):
    """Deep copy of a function with its closure cells (copy.deepcopy keeps functions as they are)."""
    if f.__closure__ is None:
        return f
    cells = tuple(types.CellType() for _ in f.__closure__)
    g = types.FunctionType(f.__code__, f.__globals__, f.__name__, f.__defaults__, cells)
    memo[id(f)] = g
    for new, old in zip(cells, f.__closure__):
        try:
            new.cell_contents = copy.deepcopy(old.cell_contents, memo)
        except ValueError:  # empty cell
            pass
    g.__kwdefaults__ = f.__kwdefaults__
    return g


def deep_copy(obj):
    saved = copy._deepcopy_dispatch.get(types.FunctionType)
    copy._deepcopy_dispatch[types.FunctionType] = lambda f, memo: _copy_function(f, memo)
    try:
        return copy.deepcopy(obj)
    finally:
        if saved is None:
            del copy._deepcopy_dispatch[types.FunctionType]
        else:
            copy._deepcopy_dispatch[types.FunctionType] = saved


SESSION = None  # the Session that is running a procedure


def _make_recording(base):
    class RecordingBench(base):
        __doc__ = f"{base.__module__}.Bench with recording of the main bench (see v1_runner)."
        v1_base = base

        def __init__(self, model, inputs=None, *args, **kw):
            base.__init__(self, model, inputs, *args, **kw)
            self.v1 = None
            s = SESSION
            if s is not None and s.armed and s.main is None:
                where = s.armed_where
                if where is None or sys._getframe(1).f_code.co_name == where:
                    s.attach(self)

        def send(self, n, *words):
            if self.v1 is not None:
                self.v1.on_send(self, n, words)
            base.send(self, n, *words)

        def run(self, cycles):
            if self.v1 is not None:
                self.v1.run(self, cycles)
            else:
                if SESSION is not None:
                    SESSION.cycles_other += cycles
                base.run(self, cycles)

    RecordingBench.__name__ = "Bench"
    return RecordingBench


def pin_name(p):
    """Output pin number (isa 5.1) to its name."""
    return f"uio{p}" if p < 8 else f"uo{p - 8}"


def in_name(p):
    """Input pin number (isa 5.1, 6.1) to its name; 13 and 14 are the line helpers' event inputs."""
    if p == 13:
        return "EV13 (USB helper event)"
    if p == 14:
        return "EV14 (10BASE-Te helper event)"
    return f"uio{p}" if p < 8 else f"ui{p - 8}"


HELPER_SIGNAL = ("TXA", "TXB", "TXC", "TXD", "OE", "not OE")
HELPER_TX = ("idle", "armed", "data", "CRC", "tail 1", "tail 2", "link pulse", "jam")
HELPER_RX = ("off", "hunt", "data", "EOP", "reset")
HELPER_FLAGS = ("TXDONE", "TXLATE", "TXUNDER", "RXWORD", "RXSOP", "RXEOP", "CRCA_OK", "CRCB_OK", "STUFFERR", "RXOVER",
                "LINEA", "LINEB", "CARRIER", "LINKPULSE", "COLLISION", "JABBER")


def helper_state(m):
    """State of the line helpers of a run with the helper model (test/slu_model.py HelperChip), for the page."""
    out = {}
    for name, h in getattr(m, "helpers", {}).items():
        fl = h.flags()
        out[name] = {
            "tx": HELPER_TX[h.tx_st] if h.tx_st < len(HELPER_TX) else h.tx_st,
            "rx": HELPER_RX[h.rx_st] if h.rx_st < len(HELPER_RX) else h.rx_st,
            "flags": fl, "flag_names": [HELPER_FLAGS[k] for k in range(16) if (fl >> k) & 1],
            "evmask": h.evmask, "ev": h.ev(), "tx_words": h.tx_cnt, "rx_words": h.rx_cnt,
            "tx_time": h.tx_time, "tstamp": h.tstamp,
        }
    return out


EDGE = {0: "FALL", 1: "RISE", 2: "ANY"}


def waits_for(m, th):
    """Text: what the thread waits for at this cycle, or ''."""
    if not th.run:
        return f"stopped: {th.stop_reason}" if th.stop_reason else "stopped"
    p = th.pend
    if p is not None:
        op = p["op"]
        if op == "sample":
            return f"SAMPLE {in_name(p['pin'])} at cycle {p['at']}"
        if op == "wait":
            text = f"WAIT {in_name(p['pin'])} {EDGE.get(p['kind'], p['kind'])}"
            if p["edge"] is not None:
                text += f": edge at cycle {p['edge']}"
            elif p["until"] is not None:
                text += f", timeout at cycle {p['until']}"
            return text
        if op == "twait":
            return f"TWAIT until cycle {p['at']}"
        if op == "ld":
            return f"LD address {p['addr']}"
    w = m.mem[th.pc]
    op = w >> 12
    if op == 0xE:
        fn, nb = (w >> 10) & 3, (w >> 7) & 1
        if fn == 0 and not nb and not m.fifo_in[th.number]:
            return "PULL: in-FIFO empty"
        if fn == 1 and not nb and len(m.fifo_out[th.number]) >= pe_model.FIFO_DEPTH:
            return "PUSH: out-FIFO full"
    if op == 0xC and ((w >> 3) & 7) < 6:
        pin = (w >> 8) & 0xF
        if pin < pe_model.N_OUT and m.owner[pin] == th.number and m.slot[pin] is not None:
            return f"OUT {pin_name(pin)}: slot full until cycle {m.slot[pin][0]}"
    return ""


def thread_state(m, th):
    p = th.pend
    return {
        "pc": th.pc, "r": list(th.r), "t": th.t, "z": th.z, "c": th.c, "n": th.n, "e": th.e,
        "status": th.status, "run": bool(th.run),
        "pend": None if p is None else {k: p[k] for k in sorted(p)},
        "stop": th.stop_reason, "waits": waits_for(m, th),
    }


def model_state(m, bench=None):
    """State of the model before cycle m.cycle (and the host queues of the bench)."""
    st = {
        "cycle": m.cycle,
        "threads": [thread_state(m, th) for th in m.threads],
        "pin_v": m.pin_v, "pin_oe": m.pin_oe, "ui_in": m.ui_in, "uio_in": m.uio_in,
        "level": m._input_level(),
        "slot": [None if s is None else list(s) for s in m.slot],
        "owner": list(m.owner), "od": [bool(x) for x in m.od],
        "filt": m.filt, "fcnt": [m.fcnt[p] for p in pe_model.FILTER_PINS],
        "sync1": m.sync1, "sync2": m.sync2, "s_prev": m.s_prev,
        "samples": {str(k): list(v) for k, v in sorted(m.samples.items())},
        "fifo_in": [list(q) for q in m.fifo_in], "fifo_out": [list(q) for q in m.fifo_out],
        "mem": list(m.mem),
    }
    if hasattr(m, "helpers"):
        st["helpers"] = helper_state(m)
    if bench is not None:
        st["host_queue"] = [len(q) for q in bench.to_thread]
    return st


class Main:
    """Recording of the main bench of a session."""

    def __init__(self, session, bench):
        self.s = session
        self.bench = bench
        m = bench.m
        self.mem0 = list(m.mem)
        self.config = {
            "start_pc": [th.pc for th in m.threads], "run": [bool(th.run) for th in m.threads],
            "owner": list(m.owner), "od": [bool(x) for x in m.od], "cycle0": m.cycle,
        }
        if hasattr(m, "helper_cfg"):  # pins that a line helper drives: helper and signal
            self.config["helper"] = [None if hc is None else ("ETH " if hc[0] else "USB ") + HELPER_SIGNAL[min(hc[1], 5)]
                                     for hc in m.helper_cfg]  # signals 5 to 7 are not OE (pe_proc)
        if bench.inputs is not None:
            bench.inputs = InputRecorder(bench.inputs)
        self.pins = bench.watches.setdefault("__v1_pins", [lambda uo, uio, oe: (uo, uio, oe), []])[1]
        self.sends = []  # (cycle, thread, words)
        self.snaps = []  # (cycle, copied (model, device callable, host queues))
        self.posted = {"pins": 0, "inputs": 0, "words": [0] * pe_model.N_THREADS, "sends": 0}
        self.last_post = 0.0
        self.cursor = None  # [snapshot index, bench] of the last state request

    # ----- recording -----

    def on_send(self, bench, n, words):
        self.sends.append((bench.m.cycle, n, [w & 0xFFFF for w in words]))

    def device(self):
        inp = self.bench.inputs
        return inp.f if isinstance(inp, InputRecorder) else inp

    def snapshot(self):
        b = self.bench
        c = b.m.cycle
        # The model and the host queues; the device inputs come back from the recording (ReplayInputs), so a device
        # model that cannot be copied (a generator-driven host session) needs no copy.
        snap = deep_copy((b.m, b.to_thread))
        if self.snaps and self.snaps[-1][0] == c:
            self.snaps[-1] = (c, snap)
        else:
            self.snaps.append((c, snap))

    def run(self, bench, cycles):
        base = type(bench).v1_base
        m = bench.m
        end = m.cycle + cycles
        self.snapshot()  # the procedure may have changed a device or sent words since the last part
        while m.cycle < end:
            c = m.cycle
            if c >= self.s.limit:
                raise RunLimit(f"the run reached {self.s.limit:,} cycles")
            k = min(end - c, SNAP - c % SNAP, self.s.limit - c)
            base.run(bench, k)
            self.s.cycles_main += k
            if m.cycle % SNAP == 0:
                self.snapshot()
            if self.s.post is not None and time.perf_counter() - self.last_post >= POST_S:
                self.post("chunk")

    # ----- messages to the page -----

    def inputs_list(self):
        inp = self.bench.inputs
        return inp.v1_changes if isinstance(inp, InputRecorder) else []

    def delta(self):
        b, p = self.bench, self.posted
        inputs = self.inputs_list()
        d = {
            "cycle": b.m.cycle,
            "pins": [[c, *v] for c, v in self.pins[p["pins"]:]],
            "inputs": [list(x) for x in inputs[p["inputs"]:]],
            "words": [[[c, w] for c, w in b.from_thread[n][p["words"][n]:]] for n in range(pe_model.N_THREADS)],
            "sends": [[c, n, ws] for c, n, ws in self.sends[p["sends"]:]],
        }
        p["pins"], p["inputs"], p["sends"] = len(self.pins), len(inputs), len(self.sends)
        p["words"] = [len(b.from_thread[n]) for n in range(pe_model.N_THREADS)]
        return d

    def post(self, kind, **extra):
        msg = {"kind": kind, "delta": self.delta(), "state": model_state(self.bench.m, self.bench)}
        msg.update(extra)
        self.last_post = time.perf_counter()
        self.s.post(msg)

    # ----- state at any cycle -----

    def state_at(self, cycle):
        """Model state before `cycle` (0 <= cycle <= frontier), from the nearest copy before it."""
        cycle = max(self.config["cycle0"], min(cycle, self.bench.m.cycle))
        j = 0
        lo, hi = 0, len(self.snaps) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if self.snaps[mid][0] <= cycle:
                j, lo = mid, mid + 1
            else:
                hi = mid - 1
        cur = self.cursor
        if cur is None or cur[0] != j or cur[1].m.cycle > cycle:
            m, queues = deep_copy(self.snaps[j][1])
            dev = ReplayInputs(self.inputs_list()) if self.bench.inputs is not None else None
            base = type(self.bench).v1_base
            rb = base.__new__(base)
            rb.m, rb.inputs, rb.to_thread = m, dev, queues
            rb.from_thread = [[] for _ in range(pe_model.N_THREADS)]
            rb.watches = {}
            cur = self.cursor = [j, rb]
        rb = cur[1]
        type(self.bench).v1_base.run(rb, cycle - rb.m.cycle)
        return model_state(rb.m, rb)

    def step_instruction(self, cycle, n, most=1 << 16):
        """First cycle after `cycle` at which thread n has completed an instruction (its PC changed or its
        blocking operation ended), at most `most` cycles later and not after the frontier."""
        st = self.state_at(cycle)
        th = st["threads"][n]
        pc, pend, run = th["pc"], th["pend"] is not None, th["run"]
        c = st["cycle"]
        front = self.bench.m.cycle
        while c < front and c - st["cycle"] < most:
            c += 1
            s2 = self.state_at(c)
            t2 = s2["threads"][n]
            if t2["pc"] != pc or (pend and t2["pend"] is None) or t2["run"] != run:
                return s2, ""
        return self.state_at(c), ("the run has no more cycles" if c >= front else
                                  f"thread {n} did not complete an instruction in {most:,} cycles")


class Session:
    """One run of an example procedure."""

    def __init__(self, example, post=None, limit=LIMIT):
        self.example = example
        self.post = post
        self.limit = limit
        self.armed = False
        self.armed_where = None
        self.main = None
        self.asm_log = []  # (name, base, text, Program or None, errors)
        self.cycles_main = 0
        self.cycles_other = 0
        self.result = None
        self.wall = None

    # called by example procedures
    def arm(self, where=None):
        """The next Bench made (in a function named `where`, if given) is the main bench."""
        self.armed, self.armed_where = True, where

    def attach(self, bench):
        self.main = Main(self, bench)
        bench.v1 = self.main
        self.armed = False
        if self.post is not None:
            self.post({"kind": "begin", "programs": self.programs(), "config": self.main.config})

    def _assemble(self, text, name="<text>", base=0):
        try:
            prog = ORIG_ASSEMBLE(text, name, base)
        except pe_asm.AsmError as err:
            self.asm_log.append((name, base, text, None, list(err.errors)))
            raise
        self.asm_log.append((name, base, text, prog, []))
        return prog

    def programs(self):
        """The assembled files that make up the main bench's memory image, with the address map."""
        mem0 = self.main.mem0 if self.main else None
        chosen, used = [], set()
        for name, base, text, prog, errors in reversed(self.asm_log):
            if prog is None or mem0 is None:
                continue
            addrs = set(prog.words)
            if addrs & used or any(mem0[a] != w for a, w in prog.words.items()):
                continue
            chosen.append((name, base, text, prog))
            used |= addrs
        chosen.sort(key=lambda x: x[1])
        files = []
        for name, base, text, prog in chosen:
            files.append({
                "name": name, "base": base, "text": text, "size": prog.size,
                "lines": {str(a): ln for a, (ln, _) in sorted(prog.source.items())},
            })
        return files

    def run(self):
        """Runs the example procedure with the recording benches in place. Returns the result dict."""
        global SESSION
        saved = [(mod, mod.Bench) for mod in TEST_MODULES if hasattr(mod, "Bench")]
        bases = {}
        for mod, cls in saved:
            if cls not in bases:
                bases[cls] = _make_recording(cls)
            mod.Bench = bases[cls]
        pe_asm.assemble = self._assemble
        SESSION = self
        t0 = time.perf_counter()
        ok, text, lines, errors = False, "", [], []
        try:
            out = self.example["proc"](self)
            ok, text, lines = out if len(out) == 3 else (*out, [])
        except pe_asm.AsmError as err:
            errors = list(err.errors)
            text = "assembler errors"
        except RunLimit as err:
            text = str(err)
        except AssertionError as err:
            text = f"test check failed: {err}"
        except Exception as err:  # noqa: BLE001 - the page shows any fault of an edited setup
            text = f"{type(err).__name__}: {err}"
        finally:
            self.wall = time.perf_counter() - t0
            SESSION = None
            pe_asm.assemble = ORIG_ASSEMBLE
            for mod, cls in saved:
                mod.Bench = cls
        failed = [{"name": n, "base": b, "text": t, "errors": e} for n, b, t, p, e in self.asm_log if p is None]
        self.result = {
            "ok": bool(ok), "text": text, "log": lines, "asm_errors": errors, "failed_files": failed[-1:],
            "wall_s": self.wall, "cycles_main": self.cycles_main, "cycles_other": self.cycles_other,
            "has_main": self.main is not None,
        }
        if self.main is not None:
            self.result["hash"] = self.trace_hash()
            self.result["cycles"] = self.main.bench.m.cycle
        return self.result

    def extend(self, cycles):
        """Runs the main bench on after the procedure (the host sends nothing more)."""
        global SESSION
        SESSION = self
        try:
            self.main.run(self.main.bench, cycles)
        except RunLimit:
            pass
        finally:
            SESSION = None

    def trace(self):
        """The recorded run as one JSON-ready object (the input of trace_hash)."""
        mn = self.main
        b = mn.bench
        return {
            "config": mn.config,
            "mem0": mn.mem0,
            "pins": [[c, *v] for c, v in mn.pins],
            "inputs": [list(x) for x in mn.inputs_list()],
            "words": [[[c, w] for c, w in b.from_thread[n]] for n in range(pe_model.N_THREADS)],
            "sends": [[c, n, ws] for c, n, ws in mn.sends],
            "final": model_state(b.m, b),
        }

    def trace_hash(self):
        text = json.dumps(self.trace(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode()).hexdigest()


def run_example(example, post=None, limit=LIMIT):
    s = Session(example, post=post, limit=limit)
    s.run()
    return s
