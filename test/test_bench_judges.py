"""Bench judges (bench/judges) on results of the RTL runs, each with its negative control (C4 W8).

Inputs, written by test/test_host_prog.py in the same run (run that module first):
  output/usb_enum.json    device, configuration and string descriptors read by the USB host model from the RTL
  output/eth_line.pcapng  the 10BASE-T line of test_host_eth_ping (partner frames and chip frames, no FCS)
The OS tools are not run here: their output is rendered from these RTL results in the tools' formats
(bench/judges/usb_os.py, net.py), then parsed and judged. This shows that each judge reads its format and that its
control fails; it does not show that a real tool prints exactly this format (checked on the first bench day, W12).
The logic-analyser judge (bench/judges/capture.py) needs sigrok-cli and runs in .github/workflows/bench.yaml.
No simulation time is used.
"""

import json
import os
import sys

import cocotb

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "bench", "judges"))
import net  # noqa: E402
import pcapng  # noqa: E402
import usb_os as U  # noqa: E402

OUT = os.path.join(HERE, "output")
STATION_IP, HOST_IP = bytes([192, 168, 7, 2]), bytes([192, 168, 7, 1])
STATION_MAC = bytes([0x02, 0, 0, 0, 0, 0x01])


def _enum():
    p = os.path.join(OUT, "usb_enum.json")
    assert os.path.exists(p), "output/usb_enum.json missing: run test/test_host_prog.py (test_host_usb_keys) first"
    j = json.load(open(p, encoding="utf-8"))
    dev, cfg = bytes.fromhex(j["device"]), bytes.fromhex(j["config"])
    strings = {1: bytes.fromhex(j["string1"])[2:].decode("utf-16-le"),
               2: bytes.fromhex(j["string2"])[2:].decode("utf-16-le")}
    return dev, cfg, strings


def _check(log, name, res, want):
    ok, detail = res
    log.info("%-38s %-5s %s" % (name, "PASS" if ok else "FAIL", detail))
    assert ok == want, "%s: judge gave %s, expected %s (%s)" % (name, ok, want, detail)
    return name, ok


@cocotb.test()
async def test_judge_usb_os(dut):
    """lsusb -v, lsusb -t, pnputil, Get-PnpDevice and system_profiler judges on renderings of the descriptors read from
    the RTL: each passes; each control fails (a changed bInterval, a changed product ID, a device Status other than
    Started/OK, a full-speed link, a missing device)."""
    log = dut._log
    dev, cfg, strings = _enum()
    rows = []
    rows.append(_check(log, "lsusb -v (RTL descriptors)", U.judge_lsusb(U.render_lsusb(dev, cfg, strings)), True))
    bad = bytearray(cfg)
    bad[33] = 20                                          # endpoint bInterval 10 -> 20
    rows.append(_check(log, "control: lsusb -v, bInterval 20", U.judge_lsusb(U.render_lsusb(dev, bytes(bad), strings)),
                       False))
    rows.append(_check(log, "lsusb -t", U.judge_lsusb_tree(U.render_lsusb_tree(True)), True))
    rows.append(_check(log, "control: lsusb -t at 12M", U.judge_lsusb_tree(U.render_lsusb_tree(False)), False))
    vid, pid = dev[8] | (dev[9] << 8), dev[10] | (dev[11] << 8)
    rows.append(_check(log, "pnputil", U.judge_pnputil(U.render_pnputil(vid, pid)), True))
    rows.append(_check(log, "control: pnputil Status Problem", U.judge_pnputil(U.render_pnputil(vid, pid, "Problem")),
                       False))
    rows.append(_check(log, "control: pnputil other PID", U.judge_pnputil(U.render_pnputil(vid, pid + 1)), False))
    rows.append(_check(log, "Get-PnpDevice", U.judge_pnpdevice(U.render_pnpdevice(vid, pid)), True))
    rows.append(_check(log, "control: Get-PnpDevice Status Error", U.judge_pnpdevice(U.render_pnpdevice(vid, pid, "Error")),
                       False))
    rows.append(_check(log, "system_profiler", U.judge_system_profiler(U.render_system_profiler(dev, cfg, strings)),
                       True))
    rows.append(_check(log, "control: system_profiler full speed",
                       U.judge_system_profiler(U.render_system_profiler(dev, cfg, strings, "full_speed")), False))
    bad = bytearray(dev)
    bad[10] ^= 0x01
    rows.append(_check(log, "control: system_profiler other PID",
                       U.judge_system_profiler(U.render_system_profiler(bytes(bad), cfg, strings)), False))
    log.info("USB OS judges: %d checks (%d judges, %d controls), all as required; descriptors from the RTL run "
             "(VID %04x PID %04x, %d config bytes)" % (len(rows), sum(1 for _, ok in rows if ok),
                                                     sum(1 for _, ok in rows if not ok), vid, pid, len(cfg)))


@cocotb.test()
async def test_judge_net(dut):
    """Capture, ping, arp -a and NIC-counter judges on the 10BASE-T line of the RTL run (ARP + 10 echoes): each
    passes; each control fails (the last echo reply removed from the capture, one ICMP checksum broken, a lost ping, a
    wrong MAC in the ARP table, a receive error in the NIC counters)."""
    log = dut._log
    p = os.path.join(OUT, "eth_line.pcapng")
    assert os.path.exists(p), "output/eth_line.pcapng missing: run test/test_host_prog.py (test_host_eth_ping) first"
    pk = pcapng.read(p)
    rows = []
    rows.append(_check(log, "capture: ARP + 10 echoes", net.judge_ping_capture(pk, STATION_IP, STATION_MAC, HOST_IP, 10),
                       True))
    replies = [i for i, (_, f) in enumerate(pk) if f[6:12] == STATION_MAC and f[12:14] == b"\x08\x00"]
    cut = [x for i, x in enumerate(pk) if i != replies[-1]]
    rows.append(_check(log, "control: last echo reply removed",
                       net.judge_ping_capture(cut, STATION_IP, STATION_MAC, HOST_IP, 10), False))
    broken = list(pk)
    t, f = broken[replies[0]]
    f = bytearray(f)
    f[36] ^= 0x55                                         # ICMP checksum byte
    broken[replies[0]] = (t, bytes(f))
    rows.append(_check(log, "control: ICMP checksum broken",
                       net.judge_ping_capture(broken, STATION_IP, STATION_MAC, HOST_IP, 10), False))
    rows.append(_check(log, "capture: 11 chip frames", net.judge_frames(pk, STATION_MAC, 11), True))
    rows.append(_check(log, "control: 12 chip frames needed", net.judge_frames(pk, STATION_MAC, 12), False))
    rows.append(_check(log, "ping", net.judge_ping(net.render_ping(pk, STATION_IP, HOST_IP, 10), STATION_IP, 10), True))
    rows.append(_check(log, "control: ping with one reply lost",
                       net.judge_ping(net.render_ping(cut, STATION_IP, HOST_IP, 10), STATION_IP, 10), False))
    rows.append(_check(log, "arp -a", net.judge_arp_table(net.render_arp(pk, STATION_IP, STATION_MAC), STATION_IP,
                                                          STATION_MAC), True))
    rows.append(_check(log, "control: arp -a other MAC",
                       net.judge_arp_table(net.render_arp(pk, STATION_IP, STATION_MAC), STATION_IP,
                                           bytes([2, 0, 0, 0, 0, 9])), False))
    n_rx = len([1 for _, f in pk if f[6:12] == STATION_MAC])
    before = json.dumps({"ReceivedUnicastPackets": 1000, "ReceivedPacketErrors": 0, "ReceivedDiscardedPackets": 0})
    after = json.dumps({"ReceivedUnicastPackets": 1000 + n_rx, "ReceivedPacketErrors": 0, "ReceivedDiscardedPackets": 0})
    after_err = json.dumps({"ReceivedUnicastPackets": 1000 + n_rx, "ReceivedPacketErrors": 1,
                            "ReceivedDiscardedPackets": 0})
    rows.append(_check(log, "NIC counters", net.judge_nic_stats(before, after, n_rx), True))
    rows.append(_check(log, "control: NIC receive error", net.judge_nic_stats(before, after_err, n_rx), False))
    rt = os.path.join(OUT, "eth_line_copy.pcapng")
    pcapng.write(rt, pk)
    assert pcapng.read(rt) == pk, "pcapng write/read round trip"
    log.info("network judges: %d checks (%d judges, %d controls), all as required; %d frames from the RTL run" % (
        len(rows), sum(1 for _, ok in rows if ok), sum(1 for _, ok in rows if not ok), len(pk)))
