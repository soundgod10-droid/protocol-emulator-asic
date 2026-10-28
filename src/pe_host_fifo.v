/*
 * Copyright (c) 2026 Steven Anthony
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// pe_host_fifo: host FIFOs of the four threads (A1 block D6).
//
// What it does:
//   Each thread n has a host-to-thread FIFO ("in") and a thread-to-host
//   FIFO ("out") of 4 words of 16 bits (docs/isa.md 2.2). The thread side
//   is used by the thread that has the slot (t_tid): PULL takes the head
//   word of its in-FIFO, PUSH writes a word into its out-FIFO. The host
//   side writes into the in-FIFO and reads from the out-FIFO of one
//   thread. A change occurs at the clock edge at the end of the cycle. A
//   word that is written in cycle k can be read from cycle k+1.
//
// Why:
//   docs/isa.md 8.5 (PULL, PULL.NB, PUSH, PUSH.NB) and the firmware in fw/
//   get their data from the host through these FIFOs. The words are in
//   two 16 x 16 latch memories (pe_latch_ram), as A1 section 7 block D6
//   plans (512 latches). Only one thread has the slot in a cycle and the
//   host does one write and one read in a cycle, so each memory needs one
//   write port and one read port.
//
// Rules for the users:
//   - The core gives t_pull only when t_in_valid is 1, and t_push only
//     when t_out_full is 0.
//   - A host write to a full in-FIFO and a host read from an empty
//     out-FIFO have no effect (h_in_full and h_out_valid tell it).

module pe_host_fifo (
    input  wire        clk,
    input  wire        rst_n,       // active low, asynchronous assert
    // Thread side (the thread that has the slot)
    input  wire [1:0]  t_tid,
    input  wire        t_pull,      // take the head word of the in-FIFO of t_tid
    input  wire        t_push,      // write t_wdata into the out-FIFO of t_tid
    input  wire [15:0] t_wdata,
    output wire        t_in_valid,  // the in-FIFO of t_tid has a word
    output wire [15:0] t_in_data,   // its head word
    output wire        t_out_full,  // the out-FIFO of t_tid has 4 words
    // Host side
    input  wire [1:0]  h_tid,       // thread for h_push and h_pop
    input  wire        h_push,      // write h_wdata into the in-FIFO of h_tid
    input  wire [15:0] h_wdata,
    output wire        h_in_full,   // the in-FIFO of h_tid has 4 words
    input  wire        h_pop,       // take the head word of the out-FIFO of h_tid
    output wire        h_out_valid, // the out-FIFO of h_tid has a word
    output wire [15:0] h_out_data,  // its head word
    // Word counts of all FIFOs, for the host status (M2c host port)
    output wire [11:0] cnt_in,      // in-FIFO of thread n at [3n+2:3n]
    output wire [11:0] cnt_out      // out-FIFO of thread n at [3n+2:3n]
);

  // Read pointer, write pointer and word count of each FIFO. FIFO n uses
  // bits [2n+1:2n] of a pointer and [3n+2:3n] of a count. The word of
  // pointer value p of FIFO n is at memory address {n, p}.
  reg [7:0]  in_rp, in_wp, out_rp, out_wp;
  reg [11:0] in_cnt, out_cnt;

  wire [1:0] in_rp_t   = in_rp[2*t_tid+:2];
  wire [1:0] in_wp_h   = in_wp[2*h_tid+:2];
  wire [1:0] out_rp_h  = out_rp[2*h_tid+:2];
  wire [1:0] out_wp_t  = out_wp[2*t_tid+:2];
  wire [2:0] in_cnt_t  = in_cnt[3*t_tid+:3];
  wire [2:0] in_cnt_h  = in_cnt[3*h_tid+:3];
  wire [2:0] out_cnt_t = out_cnt[3*t_tid+:3];
  wire [2:0] out_cnt_h = out_cnt[3*h_tid+:3];

  assign cnt_in  = in_cnt;
  assign cnt_out = out_cnt;

  assign t_in_valid  = (in_cnt_t != 3'd0);
  assign t_out_full  = (out_cnt_t == 3'd4);
  assign h_in_full   = (in_cnt_h == 3'd4);
  assign h_out_valid = (out_cnt_h != 3'd0);

  wire in_we  = h_push && !h_in_full;
  wire out_re = h_pop && h_out_valid;

  pe_latch_ram #(
      .WORDS(16),
      .AW   (4),
      .WIDTH(16)
  ) u_in_ram (
      .clk  (clk),
      .rst_n(rst_n),
      .we   (in_we),
      .waddr({h_tid, in_wp_h}),
      .wdata(h_wdata),
      .raddr({t_tid, in_rp_t}),
      .rdata(t_in_data)
  );

  pe_latch_ram #(
      .WORDS(16),
      .AW   (4),
      .WIDTH(16)
  ) u_out_ram (
      .clk  (clk),
      .rst_n(rst_n),
      .we   (t_push),
      .waddr({t_tid, out_wp_t}),
      .wdata(t_wdata),
      .raddr({h_tid, out_rp_h}),
      .rdata(h_out_data)
  );

  integer n;
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      in_rp   <= 8'd0;
      in_wp   <= 8'd0;
      out_rp  <= 8'd0;
      out_wp  <= 8'd0;
      in_cnt  <= 12'd0;
      out_cnt <= 12'd0;
    end else begin
      for (n = 0; n < 4; n = n + 1) begin
        // The host and the thread can use the same FIFO in one cycle (one
        // write and one read); the count then does not change.
        if (in_we && (h_tid == n[1:0])) in_wp[2*n+:2] <= in_wp[2*n+:2] + 2'd1;
        if (t_pull && (t_tid == n[1:0])) in_rp[2*n+:2] <= in_rp[2*n+:2] + 2'd1;
        if ((in_we && (h_tid == n[1:0])) != (t_pull && (t_tid == n[1:0])))
          in_cnt[3*n+:3] <= (in_we && (h_tid == n[1:0])) ? in_cnt[3*n+:3] + 3'd1 : in_cnt[3*n+:3] - 3'd1;
        if (t_push && (t_tid == n[1:0])) out_wp[2*n+:2] <= out_wp[2*n+:2] + 2'd1;
        if (out_re && (h_tid == n[1:0])) out_rp[2*n+:2] <= out_rp[2*n+:2] + 2'd1;
        if ((t_push && (t_tid == n[1:0])) != (out_re && (h_tid == n[1:0])))
          out_cnt[3*n+:3] <= (t_push && (t_tid == n[1:0])) ? out_cnt[3*n+:3] + 3'd1 : out_cnt[3*n+:3] - 3'd1;
      end
    end
  end

endmodule
