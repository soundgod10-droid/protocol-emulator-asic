/*
 * Copyright (c) 2026 Steven Anthony
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// iso_fv: thread isolation of the barrel core (formal/iso.sby), a two-copy miter.
//
// What it does:
//   Two copies u_a and u_b of src/pe_core.v share the clock, reset, cycle counter, start sequence (as
//   project.v), configuration (start PCs, RUN mask, pin owners), the event-slot state and the input pins.
//   The words that reach the threads may differ between the copies, except those that reach thread 0:
//     - the memory word on mem_rdata in the cycle before thread 0's slot (tid = 3, or the START cycle; it
//       goes into ir for thread 0's slot: an instruction or the data word of its LD/LDX),
//     - the helper port word, the in-FIFO word and valid flag and the out-FIFO full flag in a cycle with
//       tid = 0 (thread 0's slot; the FIFOs are per thread).
//   So the other three threads may run any different programs with any different data in the two copies.
//   Proved: thread 0's state (r0 to r3, T, PC, flags, LATE, ERR, RUN, pending state) is the same in both
//   copies in every cycle (it is in ring entry st0, st3, st2, st1 at tid 0, 1, 2, 3), its instruction and
//   register selects in its slot, every side effect of its slot (event write with time, value and output
//   enable, helper port access, FIFO push and pull with the pushed word), its store request (in the store
//   register at tid 1 and in the memory request at tid 2, also its next fetch address) and its status bits
//   for the host port. Thread 0 stands for any thread: the core is symmetric in the thread number except
//   for the START order, and the proof holds from any reset.
//   Not covered: the shared data memory (a thread can read what another thread stored, by design, docs/isa.md
//   2.2) and the shared input pins and event slots (only the owner thread writes a slot: formal/proc.sby C3).
//   The monitor wires are connected to the copies' internal nets by the .sby after flattening.

module iso_fv (
    input wire        clk,
    input wire        rst_n,
    input wire [35:0] start_pc,
    input wire [3:0]  run_mask,
    input wire [29:0] pin_owner,
    input wire [14:0] pin_en,
    input wire [14:0] pin_od,
    input wire [14:0] pin_hdrv,
    input wire [14:0] ev_ready,
    input wire [14:0] s_now,
    input wire [74:0] s_hist,
    input wire [15:0] mem_rdata_a,
    input wire [15:0] mem_rdata_b,
    input wire [15:0] io_rdata_a,
    input wire [15:0] io_rdata_b,
    input wire        fifo_in_valid_a,
    input wire        fifo_in_valid_b,
    input wire [15:0] fifo_in_data_a,
    input wire [15:0] fifo_in_data_b,
    input wire        fifo_out_full_a,
    input wire        fifo_out_full_b
);

  initial assume (!rst_n);

  // Start sequence of project.v (as core_fv.v and proc_fv.v).
  reg  rst_q;
  reg  pre1, pre2;
  wire h_start = rst_n && !rst_q;
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      rst_q <= 1'b0;
      pre1  <= 1'b0;
      pre2  <= 1'b0;
    end else begin
      rst_q <= 1'b1;
      pre1  <= h_start;
      pre2  <= pre1;
    end
  end
  wire [1:0] prestart = h_start ? 2'd1 : (pre1 ? 2'd2 : 2'd0);
  wire       start    = pre2;

  wire [15:0] now;
  pe_cycle_counter #(
      .WIDTH(16)
  ) u_cnt (
      .clk  (clk),
      .rst_n(rst_n),
      .clear(start),
      .count(now)
  );
  wire [1:0] tid = now[1:0];

  // Thread 0 gets the same words in both copies.
  always @* begin
    // the next cycle is thread 0's slot: tid = 3, or START (the counter is 0 in the next cycle)
    if ((tid == 2'd3) || start) assume (mem_rdata_a == mem_rdata_b);
    if (tid == 2'd0) begin
`ifndef ISO_MUT_IO
      // (iso_mut_io.sby leaves this one out: a different helper port word then reaches thread 0)
      assume (io_rdata_a == io_rdata_b);
`endif
      assume (fifo_in_valid_a == fifo_in_valid_b);
      assume (fifo_in_data_a == fifo_in_data_b);
      assume (fifo_out_full_a == fifo_out_full_b);
    end
  end

  wire [10:0] a_mem_addr, b_mem_addr;
  wire        a_mem_we, b_mem_we;
  wire [15:0] a_mem_wdata, b_mem_wdata;
  wire        a_io_we, b_io_we, a_io_re, b_io_re;
  wire [5:0]  a_io_addr, b_io_addr;
  wire [15:0] a_io_wdata, b_io_wdata;
  wire [14:0] a_ev_load, b_ev_load;
  wire [15:0] a_ev_time, b_ev_time;
  wire        a_ev_value, b_ev_value, a_ev_oe, b_ev_oe;
  wire        a_pull, b_pull, a_push, b_push;
  wire [15:0] a_fwd, b_fwd;
  wire [3:0]  a_run, b_run, a_late, b_late, a_err, b_err;

  pe_core u_a (
      .clk(clk), .rst_n(rst_n), .now(now), .start(start), .prestart(prestart), .start_pc(start_pc),
      .run_mask(run_mask), .pin_owner(pin_owner), .pin_en(pin_en), .pin_od(pin_od), .pin_hdrv(pin_hdrv),
      .mem_addr(a_mem_addr), .mem_we(a_mem_we), .mem_wdata(a_mem_wdata), .mem_rdata(mem_rdata_a),
      .io_we(a_io_we), .io_re(a_io_re), .io_addr(a_io_addr), .io_wdata(a_io_wdata), .io_rdata(io_rdata_a),
      .ev_ready(ev_ready), .ev_load(a_ev_load), .ev_time(a_ev_time), .ev_value(a_ev_value), .ev_oe(a_ev_oe),
      .s_now(s_now), .s_hist(s_hist), .fifo_pull(a_pull), .fifo_push(a_push), .fifo_wdata(a_fwd),
      .fifo_in_valid(fifo_in_valid_a), .fifo_in_data(fifo_in_data_a), .fifo_out_full(fifo_out_full_a),
      .th_run(a_run), .th_late(a_late), .th_err(a_err)
  );

  pe_core u_b (
      .clk(clk), .rst_n(rst_n), .now(now), .start(start), .prestart(prestart), .start_pc(start_pc),
      .run_mask(run_mask), .pin_owner(pin_owner), .pin_en(pin_en), .pin_od(pin_od), .pin_hdrv(pin_hdrv),
      .mem_addr(b_mem_addr), .mem_we(b_mem_we), .mem_wdata(b_mem_wdata), .mem_rdata(mem_rdata_b),
      .io_we(b_io_we), .io_re(b_io_re), .io_addr(b_io_addr), .io_wdata(b_io_wdata), .io_rdata(io_rdata_b),
      .ev_ready(ev_ready), .ev_load(b_ev_load), .ev_time(b_ev_time), .ev_value(b_ev_value), .ev_oe(b_ev_oe),
      .s_now(s_now), .s_hist(s_hist), .fifo_pull(b_pull), .fifo_push(b_push), .fifo_wdata(b_fwd),
      .fifo_in_valid(fifo_in_valid_b), .fifo_in_data(fifo_in_data_b), .fifo_out_full(fifo_out_full_b),
      .th_run(b_run), .th_late(b_late), .th_err(b_err)
  );

  // Internal nets of the copies, connected by formal/iso.sby (not driven here).
  (* keep *) wire [112:0] a_st0, a_st1, a_st2, a_st3, b_st0, b_st1, b_st2, b_st3;
  (* keep *) wire [15:0]  a_ir, b_ir, a_stdata, b_stdata;
  (* keep *) wire [1:0]   a_sa, b_sa, a_sb, b_sb;
  (* keep *) wire         a_stpend, b_stpend;
  (* keep *) wire [10:0]  a_staddr, b_staddr;

  // Thread 0's ring entry in this cycle.
  wire [112:0] a_t0 = (tid == 2'd0) ? a_st0 : (tid == 2'd1) ? a_st3 : (tid == 2'd2) ? a_st2 : a_st1;
  wire [112:0] b_t0 = (tid == 2'd0) ? b_st0 : (tid == 2'd1) ? b_st3 : (tid == 2'd2) ? b_st2 : b_st1;

  always @* begin
    if (rst_n) begin
      // I1: thread 0's state
      assert (a_t0 == b_t0);
      // I2: its status bits for the host port
      assert ((a_run[0] == b_run[0]) && (a_late[0] == b_late[0]) && (a_err[0] == b_err[0]));
      if (tid == 2'd0) begin
        // I3: its instruction and register selects
        assert ((a_ir == b_ir) && (a_sa == b_sa) && (a_sb == b_sb));
        // I4: the side effects of its slot
        assert (a_ev_load == b_ev_load);
        if (a_ev_load != 15'd0) assert ((a_ev_time == b_ev_time) && (a_ev_value == b_ev_value) && (a_ev_oe == b_ev_oe));
        assert ((a_io_we == b_io_we) && (a_io_re == b_io_re));
        if (a_io_we || a_io_re) assert (a_io_addr == b_io_addr);
        if (a_io_we) assert (a_io_wdata == b_io_wdata);
        assert ((a_pull == b_pull) && (a_push == b_push));
        if (a_push) assert (a_fwd == b_fwd);
      end
      // I5: its store request (store register in the next cycle, memory request in the cycle after)
      if (tid == 2'd1) begin
        assert (a_stpend == b_stpend);
        if (a_stpend) assert ((a_staddr == b_staddr) && (a_stdata == b_stdata));
      end
      if (tid == 2'd2) begin
        assert ((a_mem_we == b_mem_we) && (a_mem_addr == b_mem_addr));
        if (a_mem_we) assert (a_mem_wdata == b_mem_wdata);
      end
    end
  end

  // The properties are not empty: thread 0 writes an event, and another thread's state differs.
  always @* begin
    cover (rst_n && tid == 2'd0 && a_ev_load != 15'd0);
    cover (rst_n && tid == 2'd0 && a_st1 != b_st1 && a_st0[95]);
  end

endmodule
