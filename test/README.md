# Tests

The [cocotb](https://docs.cocotb.org/en/stable/) tests of the chip, run with Icarus Verilog. [docs/verification.md](../docs/verification.md) gives what each module checks, what it does not check, and its newest result.

| Module | Tests | What it tests |
|---|---|---|
| `test.py` | 10 | the host SPI port, the chip ID and command rules, and `fw/uart_tx.s` against a UART receiver model, at 50 MHz |
| `test_isa.py` | 21 | the assembler and the reference model against the instruction set document |
| `test_core.py` | 14 | the RTL against the reference model in lockstep, every pin and register in every cycle |
| `test_fw_full.py` | 17 | UART, SPI and I2C firmware in the model against protocol models, rate sweeps |
| `test_flex.py` | 7 | CAN, JTAG, SWD, PS/2 and timed-trigger firmware in the model |
| `test_holdout.py` | 6 | the hold-out protocols (1-Wire, AM2302, PDI) in the model |
| `test_rtl_fw.py` | 15 | every protocol program on the RTL at 60 MHz, with a real-time host on the SPI pins |

## How to run

```sh
pip install -r requirements.txt
make -B                                                              # all modules on the RTL
make -B COCOTB_TEST_MODULES=test_rtl_fw COCOTB_TEST_FILTER=test_rtl_i2c   # one test
```

The whole set takes about 40 minutes on a GitHub runner (test workflow).

For the gate-level simulation, copy the netlist of a gds run (artifact `tt_submission`, `tt_submission/*.v`) to `gate_level_netlist.v`, and set `PDK_ROOT` to a CMOS5L PDK installation.

Then run:

```sh
make -B GATES=yes
```

At gate level this runs every module except the protocol tests `test_rtl_fw.py`, which the gds workflow runs in two parallel jobs. To run them on the netlist (all 15, or one part with `GL_PART=1` or `GL_PART=2`):

```sh
GL_PART=1 make -B GATES=yes COCOTB_TEST_MODULES=test_rtl_fw
```

The RTL run writes the waveform of every signal to `tb.fst` (the long protocol tests stop the dump while they run). For a VCD of the chip pins only, during the whole test, as the waveform figures of the documentation use:

```sh
make -B PIN_VCD=1 FST= COCOTB_TEST_MODULES=test_rtl_fw COCOTB_TEST_FILTER=test_rtl_uart_tx
```

This writes `pins.vcd` (`rst_n`, `ui_in`, `uo_out`, `uio_in`, `uio_out`, `uio_oe`).

## How to view the waveform file

Using GTKWave

```sh
gtkwave tb.fst tb.gtkw
```

Using Surfer

```sh
surfer tb.fst
```
