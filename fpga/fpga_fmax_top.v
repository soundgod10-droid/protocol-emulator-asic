/*
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// fpga_fmax_top: top for the post-route fmax builds of fpga.yaml only (not for the bench). The chip's inputs and
// reset come from a shift register fed by one pin, and all its outputs and output enables are registered and
// XOR-folded to one pin, so the whole design stays observable and the place-and-route needs three pins (clock,
// serial in, serial out) on every board. The fmax of the chip's register-to-register paths is then the same as with
// the bench top (fpga_top.v); input and output pin timing is not part of this number.

module fpga_fmax_top (
    input  wire clk,
    input  wire sin,
    output wire sout
);
  reg [16:0] sh;
  always @(posedge clk) sh <= {sh[15:0], sin};

  wire [7:0] uo, uio_out, uio_oe;
  tt_um_soundgod10_droid_protocol_emulator u_chip (
      .ui_in  (sh[7:0]),
      .uo_out (uo),
      .uio_in (sh[15:8]),
      .uio_out(uio_out),
      .uio_oe (uio_oe),
      .ena    (1'b1),
      .clk    (clk),
      .rst_n  (sh[16])
  );

  reg [23:0] cap;
  reg        o;
  always @(posedge clk) begin
    cap <= {uo, uio_out, uio_oe};
    o   <= ^cap;
  end
  assign sout = o;
endmodule
