/*
 * Copyright (c) 2026 Steven Anthony
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// REMOVED IN M2c. This file is not a source of the chip any more: info.yaml
// and test/Makefile do not list it, and no module instantiates it. The host
// SPI port (src/pe_host_port.v, docs/host_port.md) replaces it. The text
// below is the last version (chip commit 634b990 and earlier), kept only
// as a record until the file is deleted with git rm.
//
// pe_test_port: TEMPORARY serial test port of milestone M2b.
//
// What it does:
//   The test bench loads the memory and the configuration, starts the
//   threads and uses the host FIFOs through 3 input pins and 1 output pin.
//   Each level change of TP_CLK (after the synchronizer) is one bit; the
//   bit is the TP_DAT level at the same time. 26 bits make one frame,
//   most significant bit first:
//     frame[25:23] command, frame[22:16] argument, frame[15:0] data
//   Commands:
//     1 MEMW   memory word at address argument = data
//     2 CFG    configuration register at address argument = data (pe_config)
//     3 START  clear the cycle counter and start the threads (pe_core)
//     4 PUSH   write data into the host-to-thread FIFO of thread argument[1:0]
//     5 POP    take the head word of the thread-to-host FIFO of thread
//              argument[1:0] into the readout register
//     0, 6, 7  no operation
//   The readout register (17 bits) is {valid, word} after a POP. Its bit
//   16 is on uo_out[7]. Each received bit after that shifts it one place
//   to the left, so the bits of the next frame read it out (valid first,
//   then the word MSB first).
//   The command occurs at the clock edge at the end of the cycle in which
//   the last bit of the frame arrives. When CORE_SEL is 0 the port is off
//   and the bit counter goes to 0, so each frame starts after CORE_SEL
//   goes to 1.
//
// Why it is TEMPORARY:
//   Milestone M2c replaces it with the host SPI port (A1 block D7). The
//   port lets the cocotb tests (also at gate level) use only chip pins.
//   The bits are synchronized and counted at the chip clock, so the test
//   bench knows the exact cycle of each command (lockstep test).
//
// Rules for the user (the test bench):
//   - Apply the chip reset before a new load: START does not clear the
//     registers of the threads.
//   - MEMW only while the threads stop (before START).
//   - Change TP_CLK at most once in each clock cycle.

module pe_test_port (
    input  wire        clk,
    input  wire        rst_n,            // active low, asynchronous assert
    input  wire        active,           // CORE_SEL after its synchronizer
    input  wire        tp_clk,           // TP_CLK after its synchronizer
    input  wire        tp_dat,           // TP_DAT after its synchronizer
    output wire        ro_bit,           // readout bit for uo_out[7]
    // Commands (1 for one cycle)
    output wire        mem_we,
    output wire        cfg_we,
    output wire        start,
    output wire        fifo_push,
    output wire        fifo_pop,
    output wire [6:0]  arg,
    output wire [15:0] data,
    // Head word of the thread-to-host FIFO of thread arg[1:0]
    input  wire        fifo_out_valid,
    input  wire [15:0] fifo_out_data
);

  reg        tp_clk_q;  // TP_CLK level of the last cycle
  reg [24:0] sr;        // the first 25 bits of the frame
  reg [4:0]  nbits;     // bits of the frame received so far, 0 to 25
  reg [16:0] ro;

  // tp_clk_q follows TP_CLK also while the port is off. Thus, a level
  // change before CORE_SEL goes to 1 does not count as a bit.
  wire strobe = active && (tp_clk != tp_clk_q);
  wire last   = strobe && (nbits == 5'd25);

  wire [25:0] frame = {sr, tp_dat};
  wire [2:0]  cmd = frame[25:23];
  assign arg  = frame[22:16];
  assign data = frame[15:0];

  assign mem_we    = last && (cmd == 3'd1);
  assign cfg_we    = last && (cmd == 3'd2);
  assign start     = last && (cmd == 3'd3);
  assign fifo_push = last && (cmd == 3'd4);
  assign fifo_pop  = last && (cmd == 3'd5);

  assign ro_bit = ro[16];

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      tp_clk_q <= 1'b0;
      sr       <= 25'd0;
      nbits    <= 5'd0;
      ro       <= 17'd0;
    end else begin
      tp_clk_q <= tp_clk;
      if (!active) begin
        nbits <= 5'd0;
      end else if (strobe) begin
        sr    <= {sr[23:0], tp_dat};
        nbits <= last ? 5'd0 : nbits + 5'd1;
      end
      if (fifo_pop) begin
        ro <= {fifo_out_valid, fifo_out_data};
      end else if (strobe) begin
        ro <= {ro[15:0], 1'b0};
      end
    end
  end

endmodule
