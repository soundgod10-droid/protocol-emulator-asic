# SPDX-License-Identifier: Apache-2.0
"""Bench judges for the 10BASE-T rows (docs/bench.md, demo-board rows E1-E4): a capture of the laptop's NIC
(pcapng from Wireshark/tshark or `pktmon etl2pcap`), the laptop's `ping` and `arp -a` output, and its NIC counters
(PowerShell Get-NetAdapterStatistics | ConvertTo-Json). Each judge returns (pass, details); each has a negative
control in bench/tests and in test/test_bench_judges.py.

References: RFC 826 (ARP), RFC 791 (IPv4 header checksum), RFC 792 (echo), RFC 1071 (Internet checksum). The text
formats of Windows ping and arp -a are those of Windows 10/11 in English; a localised Windows prints other words.
"""

import json
import re

ETH_ARP, ETH_IP = 0x0806, 0x0800


def ones(b):
    if len(b) % 2:
        b = bytes(b) + b"\0"
    s = sum((b[i] << 8) | b[i + 1] for i in range(0, len(b), 2))
    while s > 0xFFFF:
        s = (s & 0xFFFF) + (s >> 16)
    return s


def mac_text(m, sep=":"):
    return sep.join("%02x" % x for x in m)


def _et(f):
    return (f[12] << 8) | f[13] if len(f) >= 14 else -1


def echo_pairs(packets, station_ip, host_ip):
    """Echo requests host -> station and their replies station -> host (same identifier, sequence and data, valid
    IPv4 and ICMP checksums). Returns (requests, answered, bad replies)."""
    reqs, reps = {}, {}
    bad = []
    for _, f in packets:
        if _et(f) != ETH_IP:
            continue
        ip = f[14:]
        ihl = (ip[0] & 15) * 4
        total = (ip[2] << 8) | ip[3]
        icmp = ip[ihl:total]
        if ip[9] != 1 or len(icmp) < 8:
            continue
        key = (icmp[4:8], bytes(icmp[8:]))
        if icmp[0] == 8 and ip[12:16] == host_ip and ip[16:20] == station_ip:
            reqs[key] = f
        elif icmp[0] == 0 and ip[12:16] == station_ip and ip[16:20] == host_ip:
            if ones(ip[:ihl]) != 0xFFFF or ones(icmp) != 0xFFFF:
                bad.append(key)
            else:
                reps[key] = f
    answered = [k for k in reqs if k in reps]
    return len(reqs), len(answered), bad


def arp_reply(packets, station_ip, station_mac, host_ip):
    for _, f in packets:
        if _et(f) == ETH_ARP and len(f) >= 42:
            a = f[14:42]
            if a[6:8] == b"\x00\x02" and a[8:14] == station_mac and a[14:18] == station_ip and a[24:28] == host_ip:
                return True
    return False


def judge_ping_capture(packets, station_ip, station_mac, host_ip, n):
    """Row E4 on a capture: an ARP reply from the station and n answered echo requests, no bad reply."""
    arp = arp_reply(packets, station_ip, station_mac, host_ip)
    nreq, nans, bad = echo_pairs(packets, station_ip, host_ip)
    ok = arp and nans >= n and nans == nreq and not bad
    return ok, {"arp_reply": arp, "requests": nreq, "answered": nans, "bad_replies": len(bad), "need": n}


def judge_frames(packets, src_mac, n, min_len=60):
    """Row E2 on a capture: n frames from the chip's MAC, each at least min_len octets (FCS not in the capture)."""
    got = [f for _, f in packets if f[6:12] == src_mac]
    short = [len(f) for f in got if len(f) < min_len]
    return len(got) >= n and not short, {"frames": len(got), "need": n, "short": short[:4]}


PING_SUM = re.compile(r"Packets: Sent = (\d+), Received = (\d+), Lost = (\d+)")
PING_REPLY = re.compile(r"Reply from ([\d.]+): bytes=(\d+) time[<=](\d+)ms TTL=(\d+)")


def parse_ping(text):
    m = PING_SUM.search(text)
    replies = [(r.group(1), int(r.group(2)), int(r.group(4))) for r in PING_REPLY.finditer(text)]
    if not m:
        return None
    return {"sent": int(m.group(1)), "received": int(m.group(2)), "lost": int(m.group(3)), "replies": replies}


def judge_ping(text, station_ip, n):
    p = parse_ping(text)
    if p is None:
        return False, {"error": "no ping summary line"}
    ip = ".".join(str(x) for x in station_ip)
    good = [r for r in p["replies"] if r[0] == ip]
    ok = p["sent"] == n and p["received"] == n and p["lost"] == 0 and len(good) == n
    return ok, p | {"replies": len(good)}


ARP_LINE = re.compile(r"^\s*([\d.]+)\s+([0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5})\s+(\w+)", re.M)


def judge_arp_table(text, station_ip, station_mac):
    ip = ".".join(str(x) for x in station_ip)
    table = {m.group(1): m.group(2).lower() for m in ARP_LINE.finditer(text)}
    want = mac_text(station_mac, "-")
    return table.get(ip) == want, {"entry": table.get(ip), "want": want}


def judge_nic_stats(before, after, n_rx):
    """Get-NetAdapterStatistics as JSON, before and after the row: at least n_rx more received unicast packets and
    no new receive error or discard."""
    b, a = json.loads(before), json.loads(after)
    d = {k: a.get(k, 0) - b.get(k, 0) for k in ("ReceivedUnicastPackets", "ReceivedPacketErrors",
                                                 "ReceivedDiscardedPackets", "OutboundPacketErrors")}
    ok = d["ReceivedUnicastPackets"] >= n_rx and d["ReceivedPacketErrors"] == 0 and d["ReceivedDiscardedPackets"] == 0
    return ok, d


# ----- renderers for the CI proof: what the laptop tools print for a given set of frames (formats above) -----

def render_ping(packets, station_ip, host_ip, sent):
    ip = ".".join(str(x) for x in station_ip)
    lines = ["", "Pinging %s with 32 bytes of data:" % ip]
    nreq, nans, bad = echo_pairs(packets, station_ip, host_ip)
    got = 0
    for _, f in packets:
        if _et(f) == ETH_IP and f[23] == 1 and f[34] == 0 and f[26:30] == station_ip and f[30:34] == host_ip:
            ip_total = (f[16] << 8) | f[17]
            lines.append("Reply from %s: bytes=%d time<1ms TTL=%d" % (ip, ip_total - 28, f[22]))
            got += 1
    lines += ["Request timed out."] * max(0, sent - got)
    lost = max(0, sent - got)
    lines += ["", "Ping statistics for %s:" % ip,
              "    Packets: Sent = %d, Received = %d, Lost = %d (%d%% loss)," % (sent, got, lost, 100 * lost // max(1, sent))]
    return "\n".join(lines) + "\n"


def render_arp(packets, station_ip, station_mac):
    ip = ".".join(str(x) for x in station_ip)
    rows = ["", "Interface: 192.168.7.1 --- 0x5", "  Internet Address      Physical Address      Type"]
    if arp_reply(packets, station_ip, station_mac, None) or any(
            _et(f) == ETH_ARP and f[22:28] == station_mac for _, f in packets):
        rows.append("  %-21s %-21s dynamic" % (ip, mac_text(station_mac, "-")))
    return "\n".join(rows) + "\n"
