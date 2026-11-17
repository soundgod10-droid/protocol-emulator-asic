"""USB 2.0 chapter 9: standard requests and descriptor checks for a
low-speed device. References are USB 2.0 section and printed page.
"""
import struct

# bRequest codes, Table 9-4 p.251
GET_STATUS, CLEAR_FEATURE, SET_FEATURE = 0, 1, 3
SET_ADDRESS, GET_DESCRIPTOR, SET_DESCRIPTOR = 5, 6, 7
GET_CONFIGURATION, SET_CONFIGURATION = 8, 9
# Descriptor types, Table 9-5 p.251
DT_DEVICE, DT_CONFIG, DT_STRING, DT_INTERFACE, DT_ENDPOINT = 1, 2, 3, 4, 5
DT_DEVICE_QUALIFIER = 6


def setup_packet(bmRequestType, bRequest, wValue, wIndex, wLength):
    """8-byte Setup data, little-endian fields, Table 9-2 p.248; 8.1 p.195."""
    return struct.pack("<BBHHH", bmRequestType, bRequest, wValue, wIndex, wLength)


def get_descriptor(dtype, index, wLength, langid=0):
    """GET_DESCRIPTOR: bmRequestType 10000000b, wValue = type<<8 | index,
    wIndex = 0 or LANGID. 9.4.3 p.253."""
    return setup_packet(0x80, GET_DESCRIPTOR, (dtype << 8) | index, langid, wLength)


def set_address(addr):
    """SET_ADDRESS: 00000000b, wValue = address, wIndex = wLength = 0.
    9.4.6 p.256."""
    return setup_packet(0x00, SET_ADDRESS, addr, 0, 0)


def set_configuration(value):
    """SET_CONFIGURATION: 00000000b, wValue = configuration value.
    9.4.7 p.257."""
    return setup_packet(0x00, SET_CONFIGURATION, value, 0, 0)


def _v(out, ok, code, text, ref):
    if not ok:
        out.append((code, text, ref))


def _is_bcd16(v):
    return all(((v >> s) & 0xF) <= 9 for s in (0, 4, 8, 12))


def check_device_descriptor(d, low_speed=True):
    """Table 9-8 p.262-263 and 9.6.1 p.261."""
    v = []
    _v(v, len(d) >= 18, "DEV_LEN", "device descriptor %d bytes < 18" % len(d), "Table 9-8 p.262")
    if len(d) < 18:
        return v
    (bLength, bType, bcdUSB, cls, sub, proto, mps0, vid, pid, bcdDev,
     iMan, iProd, iSer, nConf) = struct.unpack("<BBHBBBBHHHBBBB", d[:18])
    _v(v, bLength == 18, "DEV_BLENGTH", "bLength %d != 18" % bLength, "Table 9-8 p.262")
    _v(v, bType == DT_DEVICE, "DEV_TYPE", "bDescriptorType %d != 1" % bType, "Table 9-8 p.262; Table 9-5 p.251")
    _v(v, _is_bcd16(bcdUSB), "DEV_BCDUSB", "bcdUSB %04x not BCD" % bcdUSB, "Table 9-8 p.262; 9.6.1 p.261")
    _v(v, _is_bcd16(bcdDev), "DEV_BCDDEVICE", "bcdDevice %04x not BCD" % bcdDev, "Table 9-8 p.263")
    if cls == 0:
        _v(v, sub == 0, "DEV_SUBCLASS", "bDeviceSubClass must be 0 when class is 0", "Table 9-8 p.262")
    _v(v, mps0 in (8, 16, 32, 64), "DEV_MPS0", "bMaxPacketSize0 %d not 8/16/32/64" % mps0, "Table 9-8 p.263")
    if low_speed:
        _v(v, mps0 == 8, "DEV_MPS0_LS", "LS bMaxPacketSize0 %d != 8" % mps0, "5.5.3 p.39")
    _v(v, nConf >= 1, "DEV_NCONF", "bNumConfigurations 0", "9.4.3 p.254 (at least one configuration)")
    return v


def parse_device(d):
    f = struct.unpack("<BBHBBBBHHHBBBB", d[:18])
    keys = ("bLength bDescriptorType bcdUSB bDeviceClass bDeviceSubClass "
            "bDeviceProtocol bMaxPacketSize0 idVendor idProduct bcdDevice "
            "iManufacturer iProduct iSerialNumber bNumConfigurations").split()
    return dict(zip(keys, f))


def check_config_bundle(c, low_speed=True):
    """Configuration descriptor plus its interface and endpoint descriptors
    (9.4.3 p.253-254; Table 9-10 p.265-266; Table 9-12 p.268-269;
    Table 9-13 p.269-271). Returns (violations, info)."""
    v, info = [], {"interfaces": [], "endpoints": [], "strings": []}
    if len(c) < 9:
        v.append(("CFG_LEN", "configuration data %d bytes < 9" % len(c), "Table 9-10 p.265"))
        return v, info
    bLength, bType, wTotal, nIf, cfgVal, iCfg, attr, maxPow = struct.unpack("<BBHBBBBB", c[:9])
    info.update(bConfigurationValue=cfgVal, wTotalLength=wTotal, bNumInterfaces=nIf)
    if iCfg:
        info["strings"].append(iCfg)
    _v(v, bLength == 9, "CFG_BLENGTH", "bLength %d != 9" % bLength, "Table 9-10 p.265")
    _v(v, bType == DT_CONFIG, "CFG_TYPE", "bDescriptorType %d != 2" % bType, "Table 9-10 p.265")
    _v(v, wTotal == len(c), "CFG_TOTAL", "wTotalLength %d != returned %d" % (wTotal, len(c)), "Table 9-10 p.265")
    _v(v, attr & 0x80, "CFG_ATTR_D7", "bmAttributes D7 not set", "Table 9-10 p.266")
    _v(v, (attr & 0x1F) == 0, "CFG_ATTR_RSVD", "bmAttributes D4..0 not zero", "Table 9-10 p.266")
    off = bLength if bLength >= 2 else 9
    cur_if, eps_left, if0 = None, 0, set()
    ep_numbers = set()
    while off + 2 <= len(c):
        L, T = c[off], c[off + 1]
        if L < 2 or off + L > len(c):
            v.append(("DESC_CHAIN", "bad descriptor length %d at offset %d" % (L, off), "9.5 p.260"))
            break
        body = c[off:off + L]
        if T == DT_INTERFACE:
            _v(v, eps_left == 0, "IF_NEP", "previous interface has %d endpoint descriptors missing" % eps_left,
               "Table 9-12 p.268")
            _v(v, L == 9, "IF_BLENGTH", "interface bLength %d != 9" % L, "Table 9-12 p.268")
            if L >= 9:
                num, alt, nEp, icl, isub, iproto, iIf = body[2:9]
                cur_if, eps_left = (num, alt), nEp
                info["interfaces"].append(cur_if)
                if alt == 0:
                    if0.add(num)
                if icl == 0:
                    _v(v, isub == 0, "IF_SUBCLASS", "bInterfaceSubClass must be 0 when class is 0", "Table 9-12 p.268")
                if iIf:
                    info["strings"].append(iIf)
        elif T == DT_ENDPOINT:
            _v(v, cur_if is not None, "EP_ORPHAN", "endpoint before any interface", "9.4.3 p.254")
            _v(v, L == 7, "EP_BLENGTH", "endpoint bLength %d != 7" % L, "Table 9-13 p.269")
            if L >= 7:
                addr, eattr, mps, ival = struct.unpack("<BBHB", body[2:7])
                num, ttype = addr & 0x0F, eattr & 0x03
                info["endpoints"].append((addr, ttype, mps, ival))
                eps_left -= 1
                _v(v, num != 0, "EP_ZERO", "endpoint descriptor for endpoint 0", "9.6.6 p.269")
                _v(v, (addr & 0x70) == 0, "EP_ADDR_RSVD", "bEndpointAddress bits 6..4 not zero", "Table 9-13 p.269")
                if ttype != 1:
                    _v(v, (eattr & 0xFC) == 0, "EP_ATTR_RSVD", "bmAttributes bits 7..2 not zero", "Table 9-13 p.270")
                _v(v, (mps >> 13) == 0, "EP_MPS_RSVD", "wMaxPacketSize bits 15..13 not zero", "Table 9-13 p.271")
                if low_speed:
                    _v(v, ttype in (0, 3), "EP_LS_TYPE",
                       "LS endpoint transfer type %d (only control or interrupt)" % ttype, "8.3.2.2 p.197")
                    _v(v, (mps & 0x7FF) <= 8 and (mps >> 11) == 0, "EP_LS_MPS",
                       "LS wMaxPacketSize %d > 8" % mps, "5.7.3 p.48; 5.5.3 p.39")
                    if ttype == 3:
                        _v(v, 10 <= ival <= 255, "EP_LS_INTERVAL",
                           "LS interrupt bInterval %d not 10..255" % ival, "5.7.4 p.51")
                ep_numbers.add(num)
        off += L
    _v(v, eps_left == 0, "IF_NEP", "last interface has %d endpoint descriptors missing" % eps_left,
       "Table 9-12 p.268")
    _v(v, off == len(c), "DESC_CHAIN", "descriptor chain ends at %d of %d" % (off, len(c)), "9.5 p.260")
    _v(v, len(if0) == nIf, "CFG_NIF", "bNumInterfaces %d != %d interfaces found" % (nIf, len(if0)),
       "Table 9-10 p.265")
    if low_speed:
        _v(v, len(ep_numbers) <= 2, "LS_PIPES",
           "LS device uses %d endpoints besides EP0 (max 2)" % len(ep_numbers), "8.3.2.2 p.197")
    return v, info


def check_string0(s):
    """String descriptor zero: LANGID array. Table 9-15 p.273."""
    v = []
    if len(s) < 4:
        return [("STR0_LEN", "string 0 shorter than 4 bytes", "Table 9-15 p.273")]
    _v(v, s[0] == len(s), "STR_BLENGTH", "bLength %d != %d" % (s[0], len(s)), "Table 9-15 p.273")
    _v(v, s[1] == DT_STRING, "STR_TYPE", "bDescriptorType %d != 3" % s[1], "Table 9-15 p.273")
    _v(v, (s[0] - 2) % 2 == 0, "STR0_ODD", "LANGID array length odd", "9.6.7 p.273")
    return v


def langids(s):
    return [struct.unpack("<H", s[i:i + 2])[0] for i in range(2, len(s) - 1, 2)]


def check_string(s):
    """UNICODE string descriptor, Table 9-16 p.274."""
    v = []
    if len(s) < 2:
        return [("STR_LEN", "string shorter than 2 bytes", "Table 9-16 p.274")]
    _v(v, s[0] == len(s), "STR_BLENGTH", "bLength %d != %d" % (s[0], len(s)), "Table 9-16 p.274")
    _v(v, s[1] == DT_STRING, "STR_TYPE", "bDescriptorType %d != 3" % s[1], "Table 9-16 p.274")
    _v(v, len(s) % 2 == 0, "STR_ODD", "string length odd (UTF-16 needs 2-byte units)", "9.6.7 p.273")
    if len(s) % 2 == 0:
        try:
            s[2:].decode("utf-16-le")
        except UnicodeDecodeError:
            v.append(("STR_UTF16", "string not valid UTF-16LE", "9.6.7 p.273"))
    return v
