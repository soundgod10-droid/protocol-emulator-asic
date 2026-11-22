# SPDX-License-Identifier: Apache-2.0
"""Bench judges for the USB rows (docs/bench.md U1, U2; demo-board rows): what the operating systems report for the
device, compared with the descriptors the firmware serves (fw/usb_dev_data.py).

  judge_lsusb(text)        Linux `lsusb -v -d 1209:0001`: every field of the device, configuration, interface, HID
                           and endpoint descriptors and the two strings equal to fw/usb_dev_data.py
  judge_lsusb_tree(text)   Linux `lsusb -t`: the device at 1.5M (low speed) with driver usbhid
  judge_pnputil(text)      Windows `pnputil /enum-devices /connected`: USB\\VID_1209&PID_0001 and its HID keyboard
                           child, both Status Started
  judge_pnpdevice(text)    Windows `Get-PnpDevice -PresentOnly | ? InstanceId -like '*VID_1209&PID_0001*' |
                           ConvertTo-Json`: every entry Status OK, a Keyboard class entry present
  judge_system_profiler(t) macOS `system_profiler SPUSBDataType -json`: an item with vendor_id 0x1209, product_id
                           0x0001, the product and manufacturer strings, low speed
The expected values are decoded here from the descriptor bytes of fw/usb_dev_data.py (USB 2.0 9.6, HID 1.11 6.2.1),
not copied by hand. The text formats are those of usbutils 017, Windows 10/11 (English) and macOS 14; the parsers are
checked against renderings of descriptors read from the RTL in CI, and must be checked against the first real output
on the bench (W12).
"""

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "fw"))
import usb_dev_data as D  # noqa: E402


def le16(b, i):
    return b[i] | (b[i + 1] << 8)


def bcd(v):
    return "%x.%02x" % (v >> 8, v & 0xFF)


def expected(device=None, config=None, strings=None):
    """Field values that each OS shows, from descriptor bytes (default: the firmware's own)."""
    d = bytes(device if device is not None else D.DEVICE)
    c = bytes(config if config is not None else D.CONFIG)
    s = strings if strings is not None else {1: D.MANUFACTURER, 2: D.PRODUCT}
    itf, hid, ep = c[9:18], c[18:27], c[27:34]
    return {
        "bcdUSB": bcd(le16(d, 2)), "bDeviceClass": d[4], "bMaxPacketSize0": d[7],
        "idVendor": "0x%04x" % le16(d, 8), "idProduct": "0x%04x" % le16(d, 10), "bcdDevice": bcd(le16(d, 12)),
        "iManufacturer": s.get(d[14], ""), "iProduct": s.get(d[15], ""), "bNumConfigurations": d[17],
        "wTotalLength": "0x%04x" % le16(c, 2), "bNumInterfaces": c[4], "bConfigurationValue": c[5],
        "bmAttributes": "0x%02x" % c[7], "MaxPower": "%dmA" % (2 * c[8]),
        "bInterfaceClass": itf[5], "bInterfaceSubClass": itf[6], "bInterfaceProtocol": itf[7],
        "bNumEndpoints": itf[4], "bcdHID": bcd(le16(hid, 2)), "wDescriptorLength": le16(hid, 7),
        "bEndpointAddress": "0x%02x" % ep[2], "epAttributes": ep[3], "wMaxPacketSize": "0x%04x" % le16(ep, 4),
        "bInterval": ep[6],
    }


SECTIONS = ("Device Descriptor:", "Configuration Descriptor:", "Interface Descriptor:", "HID Device Descriptor:",
            "Endpoint Descriptor:")


def parse_lsusb(text):
    """Fields of lsusb -v: {section: {field: (value, rest)}} for the first device."""
    out, sec = {}, None
    for ln in text.splitlines():
        s = ln.strip()
        if s in SECTIONS:
            sec = s[:-1]
            out.setdefault(sec, {})
            continue
        m = re.match(r"^(\w+)\s+(\S+)\s*(.*)$", s)
        if sec and m and not s.endswith(":"):
            out[sec].setdefault(m.group(1), (m.group(2), m.group(3).strip()))
    return out


def judge_lsusb(text, want=None):
    w = want or expected()
    p = parse_lsusb(text)
    dev, cfg = p.get("Device Descriptor", {}), p.get("Configuration Descriptor", {})
    itf, hid, ep = (p.get("Interface Descriptor", {}), p.get("HID Device Descriptor", {}),
                    p.get("Endpoint Descriptor", {}))
    got = {
        "bcdUSB": dev.get("bcdUSB", ("",))[0], "bDeviceClass": int(dev.get("bDeviceClass", ("-1",))[0]),
        "bMaxPacketSize0": int(dev.get("bMaxPacketSize0", ("-1",))[0]), "idVendor": dev.get("idVendor", ("",))[0],
        "idProduct": dev.get("idProduct", ("",))[0], "bcdDevice": dev.get("bcdDevice", ("",))[0],
        "iManufacturer": dev.get("iManufacturer", ("", ""))[1], "iProduct": dev.get("iProduct", ("", ""))[1],
        "bNumConfigurations": int(dev.get("bNumConfigurations", ("-1",))[0]),
        "wTotalLength": cfg.get("wTotalLength", ("",))[0], "bNumInterfaces": int(cfg.get("bNumInterfaces", ("-1",))[0]),
        "bConfigurationValue": int(cfg.get("bConfigurationValue", ("-1",))[0]),
        "bmAttributes": cfg.get("bmAttributes", ("",))[0], "MaxPower": cfg.get("MaxPower", ("",))[0],
        "bInterfaceClass": int(itf.get("bInterfaceClass", ("-1",))[0]),
        "bInterfaceSubClass": int(itf.get("bInterfaceSubClass", ("-1",))[0]),
        "bInterfaceProtocol": int(itf.get("bInterfaceProtocol", ("-1",))[0]),
        "bNumEndpoints": int(itf.get("bNumEndpoints", ("-1",))[0]), "bcdHID": hid.get("bcdHID", ("",))[0],
        "wDescriptorLength": int(hid.get("wDescriptorLength", ("-1",))[0]),
        "bEndpointAddress": ep.get("bEndpointAddress", ("",))[0], "epAttributes": int(ep.get("bmAttributes", ("-1",))[0]),
        "wMaxPacketSize": ep.get("wMaxPacketSize", ("",))[0], "bInterval": int(ep.get("bInterval", ("-1",))[0]),
    }
    diff = {k: (got[k], w[k]) for k in w if got[k] != w[k]}
    return not diff, {"fields": len(w), "differences": diff}


def judge_lsusb_tree(text):
    m = re.search(r"Class=Human Interface Device, Driver=usbhid, (\S+)", text)
    return bool(m) and m.group(1) == "1.5M", {"speed": m.group(1) if m else None}


def _blocks(text):
    out, cur = [], {}
    for ln in text.splitlines():
        m = re.match(r"^([A-Za-z ]+):\s+(.*)$", ln)
        if m:
            if m.group(1) == "Instance ID" and cur:
                out.append(cur)
                cur = {}
            cur[m.group(1)] = m.group(2).strip()
        elif not ln.strip() and cur:
            out.append(cur)
            cur = {}
    if cur:
        out.append(cur)
    return out


def judge_pnputil(text, vid=D.VID, pid=D.PID):
    tag = "VID_%04X&PID_%04X" % (vid, pid)
    devs = [b for b in _blocks(text) if tag in b.get("Instance ID", "").upper()]
    usb = [b for b in devs if b["Instance ID"].upper().startswith("USB\\")]
    kbd = [b for b in devs if b.get("Class Name") == "Keyboard"]
    ok = bool(usb) and bool(kbd) and all(b.get("Status") == "Started" for b in devs)
    return ok, {"entries": len(devs), "usb": len(usb), "keyboard": len(kbd),
                "status": sorted({b.get("Status") for b in devs})}


def judge_pnpdevice(text, vid=D.VID, pid=D.PID):
    tag = "VID_%04X&PID_%04X" % (vid, pid)
    j = json.loads(text)
    items = j if isinstance(j, list) else [j]
    devs = [d for d in items if tag in str(d.get("InstanceId", "")).upper()]
    ok = bool(devs) and all(d.get("Status") == "OK" for d in devs) and any(d.get("Class") == "Keyboard" for d in devs)
    return ok, {"entries": len(devs), "status": sorted({d.get("Status") for d in devs})}


def _walk(x):
    if isinstance(x, dict):
        yield x
        for v in x.values():
            yield from _walk(v)
    elif isinstance(x, list):
        for v in x:
            yield from _walk(v)


def judge_system_profiler(text, want=None):
    w = want or expected()
    hits = [d for d in _walk(json.loads(text)) if str(d.get("vendor_id", "")).lower().startswith(w["idVendor"])
            and str(d.get("product_id", "")).lower().startswith(w["idProduct"])]
    if not hits:
        return False, {"error": "no item with vendor %s product %s" % (w["idVendor"], w["idProduct"])}
    d = hits[0]
    got = {"product": d.get("_name"), "manufacturer": d.get("manufacturer"), "speed": d.get("device_speed")}
    exp = {"product": w["iProduct"], "manufacturer": w["iManufacturer"], "speed": "low_speed"}
    diff = {k: (got[k], exp[k]) for k in exp if got[k] != exp[k]}
    return not diff, {"differences": diff}


# ----- renderers for the CI proof: what each tool prints for given descriptor bytes (formats above) -----

def render_lsusb(device, config, strings, bus=1, addr=5):
    w = expected(device, config, strings)
    hid_len = w["wDescriptorLength"]
    return "\n".join([
        "Bus %03d Device %03d: ID %s:%s Generic" % (bus, addr, w["idVendor"][2:], w["idProduct"][2:]),
        "Device Descriptor:", "  bLength                18", "  bDescriptorType         1",
        "  bcdUSB               %s" % w["bcdUSB"], "  bDeviceClass            %d " % w["bDeviceClass"],
        "  bDeviceSubClass         0 ", "  bDeviceProtocol         0 ",
        "  bMaxPacketSize0         %d" % w["bMaxPacketSize0"], "  idVendor           %s Generic" % w["idVendor"],
        "  idProduct          %s" % w["idProduct"], "  bcdDevice            %s" % w["bcdDevice"],
        "  iManufacturer           1 %s" % w["iManufacturer"], "  iProduct                2 %s" % w["iProduct"],
        "  iSerial                 0 ", "  bNumConfigurations      %d" % w["bNumConfigurations"],
        "  Configuration Descriptor:", "    bLength                 9", "    bDescriptorType         2",
        "    wTotalLength       %s" % w["wTotalLength"], "    bNumInterfaces          %d" % w["bNumInterfaces"],
        "    bConfigurationValue     %d" % w["bConfigurationValue"], "    iConfiguration          0 ",
        "    bmAttributes         %s" % w["bmAttributes"], "      Self Powered",
        "    MaxPower              %s" % w["MaxPower"],
        "    Interface Descriptor:", "      bLength                 9", "      bDescriptorType         4",
        "      bInterfaceNumber        0", "      bAlternateSetting       0",
        "      bNumEndpoints           %d" % w["bNumEndpoints"],
        "      bInterfaceClass         %d Human Interface Device" % w["bInterfaceClass"],
        "      bInterfaceSubClass      %d Boot Interface Subclass" % w["bInterfaceSubClass"],
        "      bInterfaceProtocol      %d Keyboard" % w["bInterfaceProtocol"], "      iInterface              0 ",
        "        HID Device Descriptor:", "          bLength                 9", "          bDescriptorType        33",
        "          bcdHID               %s" % w["bcdHID"], "          bCountryCode            0 Not supported",
        "          bNumDescriptors         1", "          bDescriptorType        34 Report",
        "          wDescriptorLength      %d" % hid_len, "         Report Descriptors: ",
        "           ** UNAVAILABLE **", "      Endpoint Descriptor:", "        bLength                 7",
        "        bDescriptorType         5", "        bEndpointAddress     %s  EP 1 IN" % w["bEndpointAddress"],
        "        bmAttributes            %d" % w["epAttributes"], "          Transfer Type            Interrupt",
        "          Synch Type               None", "          Usage Type               Data",
        "        wMaxPacketSize     %s  1x 8 bytes" % w["wMaxPacketSize"], "        bInterval              %d" % w["bInterval"],
        "Device Status:     0x0001", "  Self Powered", ""])


def render_lsusb_tree(low_speed=True):
    return ("/:  Bus 001.Port 001: Dev 001, Class=root_hub, Driver=xhci_hcd/12p, 480M\n"
            "    |__ Port 003: Dev 005, If 0, Class=Human Interface Device, Driver=usbhid, %s\n"
            % ("1.5M" if low_speed else "12M"))


def render_pnputil(vid, pid, status="Started"):
    tag = "VID_%04X&PID_%04X" % (vid, pid)
    return ("Microsoft PnP Utility\n\n"
            "Instance ID:                USB\\%s\\5&2bd6b4b4&0&3\n" % tag +
            "Device Description:         USB Input Device\nClass Name:                 HIDClass\n"
            "Class GUID:                 {745a17a0-74d3-11d0-b6fe-00a0c90f57da}\n"
            "Manufacturer Name:          (Standard system devices)\nStatus:                     %s\n" % status +
            "Driver Name:                input.inf\n\n"
            "Instance ID:                HID\\%s\\6&1c0f2a7&0&0000\n" % tag +
            "Device Description:         HID Keyboard Device\nClass Name:                 Keyboard\n"
            "Class GUID:                 {4d36e96b-e325-11ce-bfc1-08002be10318}\n"
            "Manufacturer Name:          (Standard keyboards)\nStatus:                     %s\n" % status +
            "Driver Name:                keyboard.inf\n")


def render_pnpdevice(vid, pid, status="OK"):
    tag = "VID_%04X&PID_%04X" % (vid, pid)
    return json.dumps([{"Status": status, "Class": "HIDClass", "FriendlyName": "USB Input Device",
                        "InstanceId": "USB\\%s\\5&2BD6B4B4&0&3" % tag},
                       {"Status": status, "Class": "Keyboard", "FriendlyName": "HID Keyboard Device",
                        "InstanceId": "HID\\%s\\6&1C0F2A7&0&0000" % tag}], indent=1)


def render_system_profiler(device, config, strings, speed="low_speed"):
    w = expected(device, config, strings)
    return json.dumps({"SPUSBDataType": [{"_name": "USB31Bus", "_items": [{
        "_name": w["iProduct"], "bcd_device": w["bcdDevice"], "device_speed": speed, "location_id": "0x01100000",
        "manufacturer": w["iManufacturer"], "product_id": w["idProduct"],
        "vendor_id": w["idVendor"] + "  (pid.codes)"}]}]}, indent=1)
