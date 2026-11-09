# SPDX-License-Identifier: Apache-2.0
"""R1-07: is the SDF annotation in effect, and does the host port read the chip ID with the delays of the layout?

What it does:
  It reads the chip ID through the host port, writes and reads the whole memory, and
  measures the time from each rising clock edge to the change of MISO on uo_out[7].

Why:
  A gate-level run with no annotated delay gives 0 ns here, so this test fails if the
  annotation did not work, and the other tests of the run would then say nothing about timing.
  Only the SDF job runs this module (.github/workflows/sdf.yaml); it is not in the default
  COCOTB_TEST_MODULES, because RTL and the functional gate-level test have no delays.
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer
from cocotb.utils import get_sim_time

from host_spi import CHIP_ID, SpiTimed


@cocotb.test()
async def test_r3_sdf_delay(dut):
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    dut.ena.value = 1
    dut.uio_in.value = 0
    host = SpiTimed(dut)
    dut.rst_n.value = 0
    await Timer(200, unit="ns")
    dut.rst_n.value = 1
    await Timer(200, unit="ns")

    last_rise = [0.0]
    delays = []

    async def clock_edges():
        while True:
            await RisingEdge(dut.clk)
            last_rise[0] = get_sim_time("ns")

    async def miso_edges():
        while True:
            await dut.host_miso.value_change
            delays.append(get_sim_time("ns") - last_rise[0])

    cocotb.start_soon(clock_edges())
    cocotb.start_soon(miso_edges())

    got = await host.read_id()
    assert got == CHIP_ID, f"chip ID {got}"
    assert delays and min(delays) > 0.2, f"clock to MISO {delays[:8]}: no delays in this run"
    dut._log.info(f"clock to uo_out[7] at the port: {min(delays):.3f} to {max(delays):.3f} ns, {len(delays)} changes")

    words = [(0x5A5A ^ (a * 0x0101)) & 0xFFFF for a in range(128)]
    await host.write_mem(0, words)
    back = await host.read_mem(0, 128)
    assert back == words, "memory read back differs"
    dut._log.info("128 words written and read back through the host port with the delays of the layout")
