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

`ifndef GL_TEST
  // Serial line unit: the receive loop alone, driven by test/test_slu.py (RTL runs only).
  reg        slu_ut_rst_n = 1'b0;
  reg        slu_ut_en = 1'b0;
  reg        slu_ut_ddr = 1'b0;
  reg        slu_ut_x0 = 1'b0;
  reg        slu_ut_x1 = 1'b0;
  reg  [5:0] slu_ut_n = 6'd12;
  reg        slu_ut_manch = 1'b1;
  reg  [2:0] slu_ut_kp0 = 3'd0;
  reg  [2:0] slu_ut_kp1 = 3'd0;
  reg  [2:0] slu_ut_kp2 = 3'd0;
  reg  [5:0] slu_ut_gn0 = 6'd0;
  reg  [5:0] slu_ut_gn1 = 6'd0;
  reg        slu_ut_ki_en = 1'b0;
  reg  [3:0] slu_ut_ki_sh = 4'd0;
  reg  [5:0] slu_ut_win = 6'd3;
  reg  [5:0] slu_ut_wina = 6'd3;
  reg  [5:0] slu_ut_acq_n = 6'd0;
  reg  [1:0] slu_ut_init_edge = 2'd2;
  wire       slu_ut_bit_v;
  wire       slu_ut_bit_d;
  wire [5:0] slu_ut_nacc;
  wire       slu_ut_init;

  pe_slu_loop u_slu_loop_ut (
      .clk(clk), .rst_n(slu_ut_rst_n), .en(slu_ut_en), .ddr(slu_ut_ddr), .x0(slu_ut_x0),
      .x1(slu_ut_x1), .n(slu_ut_n), .manch(slu_ut_manch), .kp0(slu_ut_kp0), .kp1(slu_ut_kp1),
      .kp2(slu_ut_kp2), .gn0(slu_ut_gn0), .gn1(slu_ut_gn1), .ki_en(slu_ut_ki_en),
      .ki_sh(slu_ut_ki_sh), .win(slu_ut_win), .wina(slu_ut_wina), .acq_n(slu_ut_acq_n),
      .init_edge(slu_ut_init_edge), .bit_v(slu_ut_bit_v), .bit_d(slu_ut_bit_d),
      .nacc(slu_ut_nacc), .locked_init(slu_ut_init)
  );

  // Serial line unit (FULL build) alone: port bus, pins and now driven by test/test_slu.py.
  reg  [15:0] slu_now = 16'd0;
  reg         slu_we = 1'b0;
  reg         slu_re = 1'b0;
  reg  [3:0]  slu_addr = 4'd0;
  reg  [15:0] slu_wdata = 16'd0;
  wire [15:0] slu_rdata;
  reg  [12:0] slu_pins = 13'h0000;
  reg  [12:0] slu_ps1 = 13'h0000, slu_ps2 = 13'h0000;   // 2-stage synchronizer, as pe_sync2
  wire [3:0]  slu_txv;
  wire        slu_txoe;
  wire        slu_ev;
  always @(posedge clk) begin
    slu_now <= slu_now + 16'd1;
    slu_ps1 <= slu_pins;
    slu_ps2 <= slu_ps1;
  end

  pe_slu #(.FULL(1)) u_slu_ut (
      .clk(clk), .rst_n(slu_ut_rst_n), .now(slu_now), .p_we(slu_we), .p_re(slu_re),
      .p_addr(slu_addr), .p_wdata(slu_wdata), .p_rdata(slu_rdata), .pin_raw(slu_pins),
      .pin_sync(slu_ps2), .tx_v(slu_txv), .tx_oe(slu_txoe), .ev(slu_ev)
  );

  // The USB helper form (SPEC 1, as in the chip) alone, for its clocked and NRZ modes, driven by
  // test/test_slu_clk.py through its own port bus and pins; now as for the unit above.
  reg         slc_rst_n = 1'b0;
  reg         slc_we = 1'b0;
  reg         slc_re = 1'b0;
  reg  [3:0]  slc_addr = 4'd0;
  reg  [15:0] slc_wdata = 16'd0;
  wire [15:0] slc_rdata;
  reg  [12:0] slc_pins = 13'h0000;
  reg  [12:0] slc_ps1 = 13'h0000, slc_ps2 = 13'h0000;   // 2-stage synchronizer, as pe_sync2
  wire [3:0]  slc_txv;
  wire        slc_txoe;
  wire        slc_ev;
  always @(posedge clk) begin
    slc_ps1 <= slc_pins;
    slc_ps2 <= slc_ps1;
  end

  pe_slu #(.FULL(0), .SPEC(1)) u_slc_ut (
      .clk(clk), .rst_n(slc_rst_n), .now(slu_now), .p_we(slc_we), .p_re(slc_re),
      .p_addr(slc_addr), .p_wdata(slc_wdata), .p_rdata(slc_rdata), .pin_raw(slc_pins),
      .pin_sync(slc_ps2), .tx_v(slc_txv), .tx_oe(slc_txoe), .ev(slc_ev)
  );
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
