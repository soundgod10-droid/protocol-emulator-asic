/*
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// fpga_top: the chip's top module on an FPGA board. clk is the board clock input (the bench gives 60 MHz from the
// host, C4 3.3); ena is 1 as on the shuttle; each uio pin gets a tristate buffer from uio_out and uio_oe, as the
// pad cell does in the chip. No other logic: the bench must see the chip's own pins.

module fpga_top (
    input  wire       clk,
    input  wire       rst_n,
    input  wire [7:0] ui,
    output wire [7:0] uo,
    inout  wire [7:0] uio
);
  wire [7:0] uio_out, uio_oe;
  tt_um_soundgod10_droid_protocol_emulator u_chip (
      .ui_in  (ui),
      .uo_out (uo),
      .uio_in (uio),
      .uio_out(uio_out),
      .uio_oe (uio_oe),
      .ena    (1'b1),
      .clk    (clk),
      .rst_n  (rst_n)
  );
  genvar i;
  generate
    for (i = 0; i < 8; i = i + 1) begin : g_uio
      assign uio[i] = uio_oe[i] ? uio_out[i] : 1'bz;
    end
  endgenerate
endmodule
