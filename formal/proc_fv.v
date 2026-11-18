/*
 * Copyright (c) 2026 Steven Anthony
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// proc_fv: formal wrapper of pe_proc for the chip timing contract (formal/proc.sby).
//
// What it does:
//   pe_proc with its real barrel core (pe_core) and its 15 real event slots (pe_timed_out) with their pin
//   multiplexers; the memory, host FIFOs, line helpers and input unit are free models (formal/proc_stubs.v),
//   so every program word, data word, port word and input is free in every cycle. The cycle counter and the
//   start sequence are those of project.v (as in core_fv.v). Reset can occur in any cycle. The properties of
//   pe_core (B, E, W, I) and of each pe_timed_out (T1 to T6) are checked here in their composition, and the
//   contract below on the outputs of pe_proc:
//     C1  An event is written only into an empty slot, 4 to 32,767 cycles before its time (no event is lost,
//         none is applied in the past).
//     C2  An event of an OUT is at the OUT's time T + offset when the lead is 4 to 32,767; otherwise at the
//         issue cycle + 4, and then the LATE flag of the thread is set in the next cycle.
//     C3  Only the owner thread of an enabled thread pin writes its events (and only in its own slot).
//     C4  A thread pin (not given to a helper) shows exactly the value and output enable of the last event of
//         its slot whose cycle has come, or the reset state: so the pin changes only at the clock edge at the
//         end of an event cycle.
//   The monitor wires mon_* are not driven here: formal/proc.sby connects them to the named internal nets of
//   pe_proc after flattening.
//
// Why:
//   The core proof gives the core free event-slot inputs and the slot proof gives the slot free event
//   writes. This job checks the two together inside pe_proc, as the chip connects them, for every program:
//   "on time, or flagged LATE at the issue cycle + 4, never otherwise" (docs/isa.md 4.2, 4.3, 4.4, 5.2).

module proc_fv (
    input wire        clk,
    input wire        rst_n,
    input wire [35:0] start_pc,
    input wire [3:0]  run_mask,
    input wire [29:0] pin_owner,
    input wire [14:0] pin_en,
    input wire [14:0] pin_od,
    input wire [14:0] pin_hdrv,
    input wire [14:0] pin_hsel,
    input wire [44:0] pin_hsig,
    input wire [4:0]  ui_in,
    input wire [7:0]  uio_in
);

  initial assume (!rst_n);

  // Start sequence of project.v (as core_fv.v).
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

  wire [14:0] pin_v, pin_oe;
  wire [3:0]  th_run, th_late, th_err;

  pe_proc u_proc (
      .clk        (clk),
      .rst_n      (rst_n),
      .run_rst_n  (rst_n),
      .now        (now),
      .start      (start),
      .prestart   (prestart),
      .start_pc   (start_pc),
      .run_mask   (run_mask),
      .pin_owner  (pin_owner),
      .pin_en     (pin_en),
      .pin_od     (pin_od),
      .pin_hdrv   (pin_hdrv),
      .pin_hsel   (pin_hsel),
      .pin_hsig   (pin_hsig),
      .h_mem_host (1'b0),
      .h_mem_req  (1'b0),
      .h_mem_we   (1'b0),
      .h_mem_addr (11'd0),
      .h_mem_data (16'd0),
      .h_mem_rdata(),
      .h_tid      (2'd0),
      .h_push     (1'b0),
      .h_wdata    (16'd0),
      .h_in_full  (),
      .h_pop      (1'b0),
      .h_out_data (),
      .h_cnt_in   (),
      .h_cnt_out  (),
      .th_run     (th_run),
      .th_late    (th_late),
      .th_err     (th_err),
      .ui_in      (ui_in),
      .uio_in     (uio_in),
      .pin_v      (pin_v),
      .pin_oe     (pin_oe)
  );

  // Internal nets of pe_proc, connected by formal/proc.sby (not driven here).
  wire [14:0] mon_load;    // u_proc.ev_load: event write strobes of the core
  wire [14:0] mon_ready;   // u_proc.ev_ready: slot empty
  wire [15:0] mon_time;    // u_proc.ev_time
`ifndef CORE_IF
  wire        mon_ontime;  // u_proc.u_core.f_ontime: the OUT's lead is 4 to 32,767
  wire [15:0] mon_ft;      // u_proc.u_core.f_t: the OUT's time T + offset
`endif
  wire [14:0] mon_pv;      // u_proc.g_out[p].u_slot.m_pv: the slot's expected pin value (pe_timed_out T1)
  wire [14:0] mon_pe;      // u_proc.g_out[p].u_slot.m_pe: the slot's expected output enable

  wire [15:0] lead = mon_time - now;

`ifndef CORE_IF
  // C2 (LATE part): a late write sets the writer's LATE flag, seen in the next cycle.
  reg       late_q;
  reg [1:0] late_tid;
  reg       f_past = 1'b0;
  always @(posedge clk) begin
    f_past   <= 1'b1;
    late_q   <= rst_n && (mon_load != 15'd0) && !mon_ontime && !start;
    late_tid <= now[1:0];
  end
`else
  reg f_past = 1'b0;
  always @(posedge clk) f_past <= 1'b1;
`endif

  // The start sequence runs once after each reset: pre-start 1, pre-start 2, START (as in project.v, where the
  // host starts the threads only from STOP, which holds them in reset). Invariants of that sequence:
  always @* begin
    if (rst_n) begin
      assert (rst_q || (!pre1 && !pre2));
      assert (!(pre1 && pre2));
    end
  end

  always @* begin
    if (rst_n) begin
      // C1: only into an empty slot, at most one write a cycle, lead 4 to 32,767.
      assert ((mon_load & ~mon_ready) == 15'd0);
      assert ((mon_load & (mon_load - 15'd1)) == 15'd0);
      if (mon_load != 15'd0) begin
        assert ((lead >= 16'd4) && (lead <= 16'd32767));
        // C2: on time at T + offset, or late at the issue cycle + 4.
        if (mon_ontime) assert (mon_time == mon_ft);
        else assert (mon_time == now + 16'd4);
      end
    end
    if (f_past && rst_n && late_q && !start) assert (th_late[late_tid]);
  end

  genvar p;
  generate
    for (p = 0; p < 15; p = p + 1) begin : g_pin
      always @* begin
        if (rst_n) begin
          // C3: the writer is the owner thread of an enabled thread pin, in its slot (tid = now[1:0]).
          if (mon_load[p]) assert (pin_en[p] && !pin_hdrv[p] && (pin_owner[2*p+:2] == now[1:0]));
          // C4: a thread pin shows the slot's state of its last event that has come (or the reset state).
          if (!pin_hdrv[p]) assert ((pin_v[p] == mon_pv[p]) && (pin_oe[p] == mon_pe[p]));
        end
      end
    end
  endgenerate

  // The properties are not empty: an event is written on time, one is written late, and a thread pin changes.
  reg [14:0] pv_q;
  always @(posedge clk) pv_q <= pin_v;
  always @* begin
    cover (rst_n && (mon_load != 15'd0) && mon_ontime);
    cover (rst_n && (mon_load != 15'd0) && !mon_ontime);
    cover (f_past && rst_n && ((pin_v & ~pin_hdrv) != (pv_q & ~pin_hdrv)));
  end

endmodule
