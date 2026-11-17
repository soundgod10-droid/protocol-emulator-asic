"""Line models written from the standards for the chip tests: USB 2.0 low-speed host (usb_ls,
usb_host, usb_ch9) and 10BASE-T link partner (tenbaset). They do not depend on the chip's RTL
interface; the cocotb glue is in the test modules. usb_refdev is a reference device for the model
self-tests (test/linemodel_tests) only.

References are to the section and printed page of the standard; no text of a standard is copied.
"""
