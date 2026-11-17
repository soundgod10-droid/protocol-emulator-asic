"""Self-tests of the per-cycle device adapters (linemodel/cycle_devices.py), without a simulator.

USB: the adapter runs a SETUP transaction; a scripted device on the uio pins answers ACK 4.0 bit times
after the host EOP. The host model must see the ACK with no violation, the reply delay it measures must
be 4.0 bit times within the clock quantization, and the host's line levels on the ui pins must decode
to the packets it sent.
10BASE-T: a partner frame driven on the ui pins and read back per cycle must decode to the same frame
with a good FCS; a chip transmit wave given on the uio pins must come back from tx_wave() unchanged.
"""
from linemodel import cycle_devices as CD
from linemodel import tenbaset as T
from linemodel import usb_ch9 as C
from linemodel import usb_host as H
from linemodel import usb_ls as L
from linemodel.wave import Wave

CLK = 16666


def _bus_wave_from_ui(samples):
    """samples: list of (ps, ui_in); VP bit 1, VM bit 2 -> J/K/SE0 levels."""
    lv = {(0, 1): "J", (1, 0): "K", (0, 0): "SE0", (1, 1): "SE1"}
    edges, prev = [], None
    for t, ui in samples:
        x = lv[((ui >> 1) & 1, (ui >> 2) & 1)]
        if x != prev:
            edges.append((t, x))
            prev = x
    return Wave(edges[0][1], edges[1:], end=samples[-1][0])


def test_usb_host_port_setup_ack():
    s = H.UsbHostSession()
    port = CD.UsbHostPort(s.setup(0, 0, C.get_descriptor(C.DT_DEVICE, 0, 18)), clk_ps=CLK)
    reply = None                      # (start ps, wave) of the scripted ACK
    seen_eops = 0
    samples = []
    for cyc in range(40000):
        t = cyc * CLK
        uio_out, uio_oe = 0b110, 0b111            # OE# high: device not driving (VMO = 1)
        if reply is not None:
            r0, w = reply
            if r0 <= t < r0 + w.end:
                lvl = L._norm(w.level_at(t - r0))
                if lvl != "IDLE":
                    vpo, vmo = {"J": (0, 1), "K": (1, 0), "SE0": (0, 0)}[lvl]
                    uio_out = vpo | (vmo << 1)
        ui, _ = port(cyc, 0, uio_out, uio_oe)
        samples.append((t, ui))
        if len(port.host_eops) > seen_eops:
            seen_eops = len(port.host_eops)
            if seen_eops == 2:                       # after the DATA0 of the setup stage
                w = L.packet_wave(L.handshake_bits("ACK"))
                reply = (port.host_eops[-1] + 4.0 * L.BIT_PS, w)
        if port.done:
            break
    assert port.done and port.report == "ACK", port.report
    assert not s.violations, s.violations
    ipd = [e.measures["ipd_bits"] for e in s.log if "ipd_bits" in e.measures]
    assert len(ipd) == 1 and abs(ipd[0] - 4.0) <= CLK / L.BIT_PS + 1e-9, ipd
    bus = _bus_wave_from_ui(samples)
    d0 = L.decode_packet(bus)
    assert d0.ok and d0.pid_name == "SETUP" and d0.fields == {"addr": 0, "endp": 0}, (d0.errors, d0.pid_name)
    d1 = L.decode_packet(bus, t_from=d0.eop_end + 1)
    assert d1.ok and d1.pid_name == "DATA0" and d1.payload == C.get_descriptor(C.DT_DEVICE, 0, 18)


def test_tenbaset_port_drive_and_capture():
    frame = T.build_frame(bytes(6), bytes([2, 0, 0, 0, 0, 2]), 0x88B5, bytes(range(46)))
    w = T.manchester_wave(T.packet_bits(frame))
    port = CD.TenBaseTPort(clk_ps=CLK)
    port.send(w, at_cycle=10)
    rx, prev = [], None
    tx = T.manchester_wave(T.packet_bits(frame))      # also played as the chip's TD pair
    cur = CD._Cursor(tx)
    n = int(w.end / CLK) + 40
    for cyc in range(n):
        t = cyc * CLK
        lv = cur.at(t)
        uio_out = (1 if lv > 0 else 0) | ((1 if lv < 0 else 0) << 1) | ((1 if lv != 0 else 0) << 2)
        ui, _ = port(cyc, 0, uio_out, 0b111)
        x = (1 if ui & 1 else -1) if (ui >> 1) & 1 else 0
        if x != prev:
            rx.append((t, x))
            prev = x
    back = Wave(0, rx, end=n * CLK)
    evs = T.decode_tx(back, jitter_lim_ps=CLK, b433_lim_ps=CLK)
    pk = [e for e in evs if e.kind == "packet"]
    assert len(pk) == 1 and pk[0].frame == frame and not pk[0].violations, [e.violations for e in evs]
    cap = T.decode_tx(port.tx_wave(n), jitter_lim_ps=CLK, b433_lim_ps=CLK)
    pc = [e for e in cap if e.kind == "packet"]
    assert len(pc) == 1 and pc[0].frame == frame and not pc[0].violations, [e.violations for e in cap]
