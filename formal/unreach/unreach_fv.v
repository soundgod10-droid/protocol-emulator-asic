/*
 * Copyright (c) 2026 Steven Anthony
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// unreach_fv: formal wrapper of one line-helper copy for the unreachability proofs of coverage points
// (coverage/exclusions.csv, formal/unreach/instrument.py).
//
// What it does:
//   It instantiates an instrumented copy of src/pe_slu.v (written by instrument.py: an assertion that fails
//   whenever it is reached sits in the statement of each excluded coverage point) with the parameters of one
//   chip copy: SPEC 1, FULL 0 (u_usb) or SPEC 2, FULL 1 (u_eth). Every port write and read, every written word
//   and every pin input is free in every cycle; now counts up by 1 from a free value, as the chip's cycle
//   counter while the helper is out of reset (as formal/slu_fv.v); reset is free after step 0. So the proof
//   covers every behaviour of the chip's helper and more: a point proved unreachable here cannot be reached
//   in the chip.

module unreach_fv #(
    parameter SPEC = 2
) (
    input wire        clk,
    input wire        rst_n,
    input wire        p_we,
    input wire        p_re,
    input wire [3:0]  p_addr,
    input wire [15:0] p_wdata,
    input wire [12:0] pin_raw,
    input wire [12:0] pin_sync
);
  localparam FULL = (SPEC == 2) ? 1 : 0;

  initial assume (!rst_n);

  reg [15:0] now;
  always @(posedge clk) now <= now + 16'd1;

  wire [15:0] p_rdata;
  wire [3:0]  tx_v;
  wire        tx_oe, ev;
  pe_slu #(
      .FULL(FULL),
      .SPEC(SPEC)
  ) u_slu (
      .clk     (clk),
      .rst_n   (rst_n),
      .now     (now),
      .p_we    (p_we),
      .p_re    (p_re),
      .p_addr  (p_addr),
      .p_wdata (p_wdata),
      .p_rdata (p_rdata),
      .pin_raw (pin_raw),
      .pin_sync(pin_sync),
      .tx_v    (tx_v),
      .tx_oe   (tx_oe),
      .ev      (ev)
  );

endmodule
