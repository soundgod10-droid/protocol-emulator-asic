`default_nettype none
`timescale 1ns / 1ps

/* This testbench just instantiates the module and makes some convenient wires
   that can be driven / tested by the cocotb test.py.
*/
module tb ();

  // Dump the signals to a FST file. You can view it with gtkwave or surfer.
  // Not in the Verilator coverage simulation (no trace support built in).
`ifndef VERILATOR
`ifndef PIN_VCD
  initial begin
    $dumpfile("tb.fst");
    $dumpvars(0, tb);
    #1;
  end
  // A long test can stop the dump while it runs (dump_off = 1) and start it again (test/test_rtl_fw.py).
  reg dump_off = 1'b0;
  always @(dump_off) if (dump_off) $dumpoff; else $dumpon;
`endif
`endif

  // SDF job only (test/Makefile SDF_FILE): annotate the cell delays of the layout.
`ifdef SDF_FILE
  initial $sdf_annotate(`SDF_FILE, user_project);
`endif

  // Wire up the inputs and outputs:
  reg clk;
  reg rst_n;
  reg ena;
  reg [7:0] ui_in;
  reg [7:0] uio_in;
  wire [7:0] uo_out;
  wire [7:0] uio_out;
  wire [7:0] uio_oe;

  // Names for chip pins (not internal signals), so the cocotb tests can use
  // them to decide pass or fail and the waveform shows them by name.
  wire uart_tx   = uo_out[4];  // UART_TX of fw/uart_tx.s (output pin 12)
  wire host_sck  = ui_in[7];   // host SPI port (docs/host_port.md)
  wire host_cs_n = ui_in[6];
  wire host_mosi = ui_in[5];
  wire host_miso = uo_out[7];

  // All chip outputs in one vector, so a test that reads them in every clock cycle needs one read
  // (test/test_rtl_fw.py).
  wire [23:0] out_pins = {uo_out, uio_out, uio_oe};

`ifndef VERILATOR
`ifdef PIN_VCD
  // Waveform figures and the simulator's waveform samples (make PIN_VCD=1 FST=, .github/workflows/docs.yaml and
  // sim.yaml): only the chip pins, as VCD, for the whole test. dump_off has no effect in this mode. The block comes after
  // the declarations of the pins: Icarus Verilog 13 (gate-level runs) does not bind names declared later in the module.
  initial begin
    $dumpfile("pins.vcd");
    $dumpvars(0, rst_n, ui_in, uo_out, uio_in, uio_out, uio_oe);
  end
  reg dump_off = 1'b0;
`endif
`endif

  tt_um_soundgod10_droid_protocol_emulator user_project (
      .ui_in  (ui_in),    // Dedicated inputs
      .uo_out (uo_out),   // Dedicated outputs
      .uio_in (uio_in),   // IOs: Input path
      .uio_out(uio_out),  // IOs: Output path
      .uio_oe (uio_oe),   // IOs: Enable path (active high: 0=input, 1=output)
      .ena    (ena),      // enable - goes high when design is selected
      .clk    (clk),      // clock
      .rst_n  (rst_n)     // not reset
  );

endmodule
