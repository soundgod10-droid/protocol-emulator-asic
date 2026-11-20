# SPDX-License-Identifier: Apache-2.0
"""Host calls for the example programs of fw/helper/ (the USB helper in its clocked and NRZ modes).

What it does:
  Each program takes its job from the data memory and leaves its result there (docs/helpers.md section 6). These
  calls make the words that a host writes before START and read the result words that the host reads after STOP,
  so a host program (the test benches here, or a board host) needs no knowledge of the helper's bit order:
    spi_flash.s     spi_flash_read_frame(addr, n) -> {512: words}; spi_flash_read_bytes(words, n) -> n bytes
    uart_tx.s       uart_tx_frame(data) -> {512: words} (8N1, back to back)
    uart_rx.s       uart_rx_buffer(n) -> {768: words}; uart_rx_bytes(words) -> (bytes, framing errors)
    jtag_idcode.s   jtag_idcode_values(words, n_devices) -> IDCODEs, first device on TDO first
  Bit order of a helper frame: first line bit in bit 0 of the first word; a frame word at 512 gives the bit count.

Why:
  The examples are the firmware of the chip tests (test/test_slu.py, RTL and gate level), so these calls are
  checked by the same runs. Plain Python with no import from the chip's model or assembler.
"""

FRAME_ADDR = 512        # bit count, then the frame words (spi_flash.s, uart_tx.s)
RESULT_ADDR = 768       # received words (spi_flash.s, uart_rx.s, jtag_idcode.s)
END_ADDR = 500          # next free address after the received words (spi_flash.s, jtag_idcode.s)
EMPTY = 0xFFFF          # uart_rx.s: a word not yet written


def words_of(bits):
    """Frame words of a bit list, first bit in bit 0 of the first word."""
    return [sum(b << i for i, b in enumerate(bits[k:k + 16])) for k in range(0, len(bits), 16)]


def bits_of(words, n):
    """The first n frame bits of the words (inverse of words_of)."""
    out = []
    for w in words:
        out += [(w >> i) & 1 for i in range(16)]
    return out[:n]


def msb_bits(data):
    """Bits of bytes, most significant bit first (SPI)."""
    return [(b >> (7 - i)) & 1 for b in data for i in range(8)]


# ----- SPI flash (spi_flash.s) -----

def spi_flash_read_frame(addr, n):
    """Data-memory words for a READ (03h) of n bytes from the 24-bit address addr: {address: words}."""
    bits = msb_bits([0x03, (addr >> 16) & 0xFF, (addr >> 8) & 0xFF, addr & 0xFF]) + [0] * (8 * n)
    return {FRAME_ADDR: [len(bits)] + words_of(bits)}


def spi_flash_read_bytes(words, n):
    """The n data bytes from the received words (read from RESULT_ADDR): MISO bits after the 32 command bits."""
    bits = bits_of(words, 32 + 8 * n)[32:]
    return [int("".join(map(str, bits[k:k + 8])), 2) for k in range(0, 8 * n, 8)]


# ----- UART (uart_tx.s, uart_rx.s) -----

def uart_line_bits(data):
    """Line bits of back-to-back 8N1 characters: start 0, data LSB first, stop 1."""
    out = []
    for b in data:
        out += [0] + [(b >> i) & 1 for i in range(8)] + [1]
    return out


def uart_tx_frame(data):
    """Data-memory words for uart_tx.s: the bytes as one frame of 8N1 characters."""
    bits = uart_line_bits(data)
    return {FRAME_ADDR: [len(bits)] + words_of(bits)}


def uart_rx_buffer(n):
    """Data-memory words for uart_rx.s: n + 8 empty words at RESULT_ADDR (the program writes one word a character)."""
    return {RESULT_ADDR: [EMPTY] * (n + 8)}


def uart_rx_bytes(words):
    """Characters from the words of uart_rx.s (10 samples a frame: start in bit 6, data in bits 7 to 14, stop in bit
    15), up to the first empty word. Returns (bytes, indices of characters with a framing error)."""
    out, bad = [], []
    for i, w in enumerate(words):
        if w == EMPTY:
            break
        out.append((w >> 7) & 0xFF)
        if (w >> 6) & 1 or not (w >> 15) & 1:
            bad.append(i)
    return out, bad


# ----- JTAG IDCODE (jtag_idcode.s) -----

def jtag_idcode_values(words, n_devices=2):
    """IDCODEs from the received words of jtag_idcode.s: word 0 holds the TDO samples of the navigation frame, the
    scan frame follows (first device on TDO first, each IDCODE LSB first)."""
    bits = bits_of(words[1:], 32 * n_devices)
    return [sum(b << i for i, b in enumerate(bits[32 * k:32 * k + 32])) for k in range(n_devices)]
