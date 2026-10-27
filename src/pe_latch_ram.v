/*
 * Copyright (c) 2026 Steven Anthony
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// pe_latch_ram: memory of latches with one write port and one read port.
//
// What it does:
//   A write request (we, waddr, wdata) in clock cycle k writes the word in
//   the first half of cycle k+1. The write data goes into a flip-flop at
//   the clock edge at the end of cycle k. The clock gate of the word
//   (pe_cg) then opens the latches of the word while clk is high in cycle
//   k+1. The latches close at the falling clock edge, 10 ns before the
//   write-data flip-flop can change again. The read port is
//   combinational: rdata is the word at raddr.
//
// Why:
//   Decision D2: the 128 x 16 program and data memory is made of latches
//   (plan B, no SRAM macro). The same module also holds the host FIFO
//   words (A1 block D6: 512 latches). A latch costs 30.84 um2, a loadable
//   flip-flop 67.13 um2 (R1_uart_tx.md 9.4 item 3).
//   The data comes from a flip-flop and the latches close at the falling
//   edge, so the data is stable for half a clock period before and after
//   the latch closes. There is no hold race at the closing edge.
//
// Read timing:
//   A read of the word in cycle k+1 (while the latches are open) gives the
//   new word only near the end of the cycle. docs/isa.md 3.3 does not
//   define a read 1 to 3 cycles after a store, so the processor needs
//   nothing more. The host FIFOs read a new word only from cycle k+1; the
//   path then goes through the open latch, and STA checks it.
//
// Reset:
//   The latches have no reset (docs/isa.md 2.4: the memory does not change
//   at reset). The host must write a word before it is read.

module pe_latch_ram #(
    parameter integer WORDS = 128,
    parameter integer AW    = 7,   // address bits: 2^AW = WORDS
    parameter integer WIDTH = 16
) (
    input  wire             clk,
    input  wire             rst_n,  // active low, asynchronous assert (write-data flip-flops only)
    input  wire             we,     // 1 = write wdata to waddr
    input  wire [AW-1:0]    waddr,
    input  wire [WIDTH-1:0] wdata,
    input  wire [AW-1:0]    raddr,
    output wire [WIDTH-1:0] rdata
);

  // The write data is loaded in each cycle (no enable, so no mux2): only
  // the clock gate of the addressed word decides if it is written.
  reg [WIDTH-1:0] wdata_q;
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      wdata_q <= {WIDTH{1'b0}};
    end else begin
      wdata_q <= wdata;
    end
  end

  // All latch outputs in one vector: word w is q[w*WIDTH +: WIDTH]. A flat
  // vector (not an array of wires) is read the same way by the simulator,
  // Yosys and the linter.
  localparam integer WB = $clog2(WIDTH);  // WIDTH must be a power of 2
  wire [WORDS*WIDTH-1:0] q;
`ifdef FORMAL
  wire [WORDS-1:0] f_gclk;  // gated clock of each word, for the properties
`endif

  genvar w, b;
  generate
    for (w = 0; w < WORDS; w = w + 1) begin : g_word
      localparam [AW-1:0] ADDR = w;
      wire gclk;
      pe_cg u_cg (
          .clk (clk),
          .en  (we && (waddr == ADDR)),
          .gclk(gclk)
      );
`ifdef FORMAL
      assign f_gclk[w] = gclk;
`endif
      for (b = 0; b < WIDTH; b = b + 1) begin : g_bit
        pe_lat u_lat (
            .g(gclk),
            .d(wdata_q[b]),
            .q(q[w*WIDTH+b])
        );
      end
    end
  endgenerate

  // One read port. docs/isa.md 3.3: LD and LDX take 2 slots so that the
  // processor needs only this one 128:1 multiplexer. The index has exactly
  // the bits of the vector address (raddr, then WB zero bits), so the
  // multiplexer has no unused inputs.
  wire [AW+WB-1:0] rbase = {raddr, {WB{1'b0}}};
  assign rdata = q[rbase+:WIDTH];

endmodule
