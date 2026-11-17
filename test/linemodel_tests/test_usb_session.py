"""USB LS host session against the reference device: enumeration,
chapter 9 checks, transaction rules and device faults."""
import pytest

from linemodel import usb_host as H
from linemodel import usb_refdev as R
from linemodel import usb_ls as L
from linemodel import usb_ch9 as C

FAST = dict(reset_ps=20e6, recovery_ps=50e6)    # short reset: RefDevice detects at 2.5 us


def enum(dev, **kw):
    s = H.UsbHostSession(**{k: v for k, v in kw.items() if k in ("ppm", "stress_seed", "host_reply_bits")})
    try:
        rep = R.run(s.enumerate(addr=5, **FAST), dev)
    except H.EnumerationFailed as e:
        rep = {"failed": str(e)}
    return s, rep


def vcodes(s):
    return {v[1] for v in s.violations}


@pytest.mark.parametrize("ppm,stress", [(0.0, None), (-500.0, 11), (500.0, 12)])
def test_enumeration_clean(ppm, stress):
    s, rep = enum(R.RefDevice(), ppm=ppm, stress_seed=stress)
    assert "failed" not in rep and s.violations == []
    assert rep["strings"] == {1: "Model test", 2: "LS reference device"}
    assert rep["config"]["bConfigurationValue"] == 1


@pytest.mark.parametrize("ppm,reply", [(-15000 * 0.999, 7.4), (15000 * 0.999, 2.1)])
def test_enumeration_device_at_rate_and_reply_limits(ppm, reply):
    s, rep = enum(R.RefDevice(ppm=ppm, reply_bits=reply))
    assert "failed" not in rep and s.violations == []


def test_enumeration_with_naks_retries():
    s0, _ = enum(R.RefDevice())
    s, rep = enum(R.RefDevice(nak_first=3))
    assert "failed" not in rep and s.violations == []
    n = lambda ss: sum(1 for e in ss.log if e.what == "IN reply")
    # 3 NAKs before each of the 9 control transfers that answer an IN
    # (the device_qualifier request STALLs at once)
    assert n(s) - n(s0) == 3 * 9


DEVICE_FAULTS = [
    ("bad CRC16 from device", dict(faults={"bad_crc16"}), "CRC16"),
    ("no data toggle", dict(faults={"no_toggle"}), "TOGGLE"),
    ("answers old address", dict(faults={"answer_old"}), "UNEXPECTED_RESPONSE"),
    ("EOP SE0 1.0 us", dict(faults={"bad_eop"}), "EOP_WIDTH"),
    ("reply after 7.6 bits", dict(reply_bits=7.6), "IPD_MAX"),
    ("reply after 1.5 bits", dict(reply_bits=1.5), "IPD_MIN"),
    ("device rate -1.6 %", dict(ppm=-16000), "RATE"),
    ("device edge jitter 30 ns", dict(jitter_ps=30000), "SRC_PAIRED"),
]


@pytest.mark.parametrize("name,kw,code", DEVICE_FAULTS, ids=[f[0] for f in DEVICE_FAULTS])
def test_device_fault_detected(name, kw, code):
    s, rep = enum(R.RefDevice(**kw))
    assert code in vcodes(s), (name, s.violations[:3], rep.get("failed"))


def _with(dev_desc_key, off, val):
    d = R.default_descriptors()
    b = bytearray(d[dev_desc_key])
    b[off:off + len(val)] = val
    d[dev_desc_key] = bytes(b)
    return d


DESC_FAULTS = [
    ("bMaxPacketSize0 16", _with((1, 0), 7, b"\x10"), "DEV_MPS0_LS"),
    ("bDescriptorType wrong", _with((1, 0), 1, b"\x02"), "DEV_TYPE"),
    ("bcdUSB not BCD", _with((1, 0), 2, b"\x0a\x02"), "DEV_BCDUSB"),
    ("wTotalLength wrong", _with((2, 0), 2, b"\x20\x00"), "CFG_TOTAL"),
    ("bmAttributes D7 clear", _with((2, 0), 7, b"\x00"), "CFG_ATTR_D7"),
    ("bulk endpoint on LS", _with((2, 0), 18 + 3, b"\x02"), "EP_LS_TYPE"),
    ("LS interrupt bInterval 5", _with((2, 0), 18 + 6, b"\x05"), "EP_LS_INTERVAL"),
    ("LS endpoint MPS 16", _with((2, 0), 18 + 4, b"\x10"), "EP_LS_MPS"),
    ("bNumEndpoints 2, one given", _with((2, 0), 9 + 4, b"\x02"), "IF_NEP"),
    ("odd string length", {**R.default_descriptors(), (3, 1): bytes([5, 3, 0x53, 0x00, 0x33])}, "STR_ODD"),
    ("string index without string", {k: v for k, v in R.default_descriptors().items() if k != (3, 2)},
     "STR_MISSING"),
    ("device_qualifier answered", {**R.default_descriptors(), (6, 0): bytes([10, 6, 0, 2, 0, 0, 0, 8, 0, 0])},
     "QUALIFIER"),
]


@pytest.mark.parametrize("name,desc,code", DESC_FAULTS, ids=[f[0] for f in DESC_FAULTS])
def test_descriptor_fault_detected(name, desc, code):
    s, rep = enum(R.RefDevice(descriptors=desc))
    assert code in vcodes(s), (name, s.violations[:3], rep.get("failed"))


def _run_steps(dev, steps):
    s = H.UsbHostSession()

    def gen():
        out = []
        for st in steps:
            r = yield from st(s)
            out.append(r)
            yield from s.gap()
        return out
    return s, R.run(gen(), dev)


HOST_INJ = [
    ("SETUP token bad CRC5", dict(tok_flags={"bad_crc": True})),
    ("SETUP token bad PID check", dict(tok_flags={"bad_pid": True})),
    ("SETUP data bad CRC16", dict(data_flags={"bad_crc": True})),
    ("SETUP data stuffing violation", dict(data_inj=L.Inject(stuff_violation=True))),
    ("SETUP data missing EOP", dict(data_inj=L.Inject(eop="none"))),
    ("SETUP data short EOP", dict(data_inj=L.Inject(eop_se0_ps=200e3))),
    ("SETUP data truncated", dict(data_inj=L.Inject(truncate_bits=40))),
]


@pytest.mark.parametrize("name,kw", HOST_INJ, ids=[h[0] for h in HOST_INJ])
def test_host_injection_device_silent(name, kw):
    """A compliant device ignores a corrupted SETUP (8.3.1 p.196; 8.5.3
    p.226); the session records no violation when it stays silent."""
    setup8 = C.get_descriptor(C.DT_DEVICE, 0, 18)
    s, _ = _run_steps(R.RefDevice(), [lambda s: s.setup(0, 0, setup8, expect_ack=False, **kw)])
    assert s.violations == []
    assert any(e.what == "SETUP no reply" for e in s.log)


def test_host_injection_device_answers_is_flagged():
    setup8 = C.get_descriptor(C.DT_DEVICE, 0, 18)
    s, _ = _run_steps(R.RefDevice(faults={"ignore_setup_crc"}),
                      [lambda s: s.setup(0, 0, setup8, data_flags={"bad_crc": True}, expect_ack=False)])
    assert "UNEXPECTED_RESPONSE" in vcodes(s)


def test_dropped_ack_gives_same_data_again():
    """Host does not ACK: the device must send the same data and PID again
    (8.6.4 p.234)."""
    setup8 = C.get_descriptor(C.DT_DEVICE, 0, 18)
    s, out = _run_steps(R.RefDevice(), [
        lambda s: s.setup(0, 0, setup8),
        lambda s: s.in_(0, 0, drop_ack=True),
        lambda s: s.in_(0, 0),
        lambda s: s.in_(0, 0),
    ])
    assert s.violations == []
    assert out[1] == out[2] and out[1][0] == "DATA1"
    assert out[3][0] == "DATA0"


def test_keepalive_once_per_frame():
    s = H.UsbHostSession()
    acts = []

    def gen():
        s.t_frame0 = 0.0
        yield from s.wait(3.5e9)
    g = gen()
    r = None
    t = 0.0
    while True:
        try:
            a = g.send(r)
        except StopIteration:
            break
        acts.append((t, a))
        d = a.wave.end if isinstance(a, H.Drive) else a.duration_ps
        r = H.Result(t)
        t += d
    ka = [(t, a) for t, a in acts if isinstance(a, H.Drive)]
    assert len(ka) == 3
    for (t, a) in ka:
        runs = [(x, y, l) for x, y, l in a.wave.runs() if l == "SE0"]
        assert len(runs) == 1 and abs((runs[0][1] - runs[0][0]) - 2 * L.BIT_PS) < 1
        assert abs(t % 1e9) < 1 or abs(t % 1e9 - 1e9) < 1


def test_reset_wave_meets_tdrst():
    w = L.se0_wave(L.LIM["TDRST_PS"])
    (a, b, l), = [r for r in w.runs() if r[2] == "SE0"]
    assert b - a >= 10e9


def test_ch9_setup_packets():
    # Table 9-2 p.248 layout, 9.4.3 p.253, 9.4.6 p.256, 9.4.7 p.257
    assert C.get_descriptor(C.DT_DEVICE, 0, 18) == bytes([0x80, 6, 0, 1, 0, 0, 18, 0])
    assert C.get_descriptor(C.DT_STRING, 2, 255, 0x0409) == bytes([0x80, 6, 2, 3, 0x09, 0x04, 255, 0])
    assert C.set_address(5) == bytes([0, 5, 5, 0, 0, 0, 0, 0])
    assert C.set_configuration(1) == bytes([0, 9, 1, 0, 0, 0, 0, 0])
