# SPDX-License-Identifier: Apache-2.0
"""Host program of the protocol emulator for the RP2350 (demo board or a Pico 2 on the FPGA bench).

core     chip commands, load with read-back check, FIFO pumps (docs/host_port.md)
pio_spi  PIO SPI transport with a set MISO sample time (host_port.md 12)
eth      10BASE-T frame pump for fw/eth_10bt.s, ARP and ICMP echo responder
kbd      USB keyboard report pump for fw/usb_dev.s
See docs/host_program.md.
"""
