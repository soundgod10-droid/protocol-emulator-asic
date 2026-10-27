/*
 * Copyright (c) 2026 Steven Anthony
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// pe_rf_alt: register-file storage alternatives for the area comparison
// of milestone M2b (design note M2b_core.md section 2). NOT part of the
// chip: the top level does not use these modules.
//
// What it does:
//   Each module stores the data registers r0 to r3 (16 bits) of the 4
//   threads with the access of the barrel core (pe_core): in each cycle
//   the slot thread (tid = now[1:0]) reads 2 registers (ports A and B)
//   and writes 0 or 1 register. A value written in the slot at cycle c
//   is read in the next slot of the thread at cycle c + 4.
//
// Why:
//   R1_uart_tx.md 9.4 item 3: the library has no flip-flop with enable,
//   so each loadable bit costs dfrbpq_1 + mux2_1. The three storage
//   methods differ in this cost and in the read multiplexers:
//   - pe_rf_ring:  4 entries that turn in each cycle (the method of
//                  pe_core). No enable; read 1 of 4 registers.
//   - pe_rf_flop:  16 registers addressed by {tid, register}, written
//                  with an enable; read 1 of 16 registers.
//   - pe_rf_latch: 16 latch words with one clock gate each (as
//                  pe_latch_ram), write data in a flip-flop; read 1 of 16.

module pe_rf_ring (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [1:0]  tid,   // not used: the ring position is the thread
    input  wire [1:0]  sa,
    input  wire [1:0]  sb,
    input  wire        we,
    input  wire [1:0]  wsel,
    input  wire [15:0] wval,
    output wire [15:0] ra,
    output wire [15:0] rb
);
  reg [63:0] st0, st1, st2, st3;
  wire [63:0] nc;
  genvar i;
  generate
    for (i = 0; i < 4; i = i + 1) begin : g_w
      localparam [1:0] R = i;
      assign nc[16*i+:16] = (we && (wsel == R)) ? wval : st0[16*i+:16];
    end
  endgenerate
  assign ra = st0[16*sa+:16];
  assign rb = st0[16*sb+:16];
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      st0 <= 64'd0;
      st1 <= 64'd0;
      st2 <= 64'd0;
      st3 <= 64'd0;
    end else begin
      st3 <= nc;
      st2 <= st3;
      st1 <= st2;
      st0 <= st1;
    end
  end
  wire _unused = &{tid, 1'b0};
endmodule

module pe_rf_flop (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [1:0]  tid,
    input  wire [1:0]  sa,
    input  wire [1:0]  sb,
    input  wire        we,
    input  wire [1:0]  wsel,
    input  wire [15:0] wval,
    output wire [15:0] ra,
    output wire [15:0] rb
);
  reg [255:0] regs;  // register {thread, r} at [16*{thread, r} +: 16]
  wire [3:0] ia = {tid, sa};
  wire [3:0] ib = {tid, sb};
  wire [3:0] iw = {tid, wsel};
  assign ra = regs[16*ia+:16];
  assign rb = regs[16*ib+:16];
  genvar i;
  generate
    for (i = 0; i < 16; i = i + 1) begin : g_r
      localparam [3:0] A = i;
      always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
          regs[16*i+:16] <= 16'd0;
        end else if (we && (iw == A)) begin
          regs[16*i+:16] <= wval;
        end
      end
    end
  endgenerate
endmodule

module pe_rf_latch (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [1:0]  tid,
    input  wire [1:0]  sa,
    input  wire [1:0]  sb,
    input  wire        we,
    input  wire [1:0]  wsel,
    input  wire [15:0] wval,
    output wire [15:0] ra,
    output wire [15:0] rb
);
  reg [15:0] wval_q;
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      wval_q <= 16'd0;
    end else begin
      wval_q <= wval;
    end
  end
  wire [255:0] q;
  wire [3:0] iw = {tid, wsel};
  genvar w, b;
  generate
    for (w = 0; w < 16; w = w + 1) begin : g_word
      localparam [3:0] A = w;
      wire gclk;
      pe_cg u_cg (
          .clk (clk),
          .en  (we && (iw == A)),
          .gclk(gclk)
      );
      for (b = 0; b < 16; b = b + 1) begin : g_bit
        pe_lat u_lat (
            .g(gclk),
            .d(wval_q[b]),
            .q(q[16*w+b])
        );
      end
    end
  endgenerate
  wire [3:0] ia = {tid, sa};
  wire [3:0] ib = {tid, sb};
  assign ra = q[16*ia+:16];
  assign rb = q[16*ib+:16];
endmodule
