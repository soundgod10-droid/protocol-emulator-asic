/*
 * Copyright (c) 2026 Steven Anthony
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// pe_timed_out: one timed output channel of the timed I/O unit
// (block D2 of architecture D, A1_architecture.md section 4.4 and 7).
//
// What it does:
//   The channel holds one pin event: a 16-bit cycle time, a pin value and
//   an output enable. When the event is valid and the cycle counter is
//   equal to the event time, the channel copies the value and the enable
//   into the pin flip-flops at that clock edge. Then the event slot is
//   empty again, and the source can write the next event.
//
// Why:
//   The pin edge comes from the event time, not from the time when the
//   source wrote the event. Thus, a source (in M2 a program thread) can be
//   late by some cycles and the pin edge does not move. The pin is the
//   output of a flip-flop, with no logic after it. Thus, the pin cannot
//   have glitches from this design.
//
// Rules for the source:
//   - Write an event only when ev_ready is 1. The channel ignores a write
//     when ev_ready is 0.
//   - Write the event at least 1 clock cycle before its time. A1 section
//     4.4 sets 4 cycles for the threads. If the time is already past, the
//     event occurs 65,536 cycles later (the counter must come back to it).
//
// Reset:
//   RESET_VALUE and RESET_OE set the pin state during and after reset.
//   For a UART TX pin this is 1 (idle level). The reset is asynchronous,
//   so the pin goes to this state also when the clock is stopped.

module pe_timed_out #(
    parameter [0:0] RESET_VALUE = 1'b0,  // pin value during and after reset
    parameter [0:0] RESET_OE    = 1'b0   // output enable during and after reset
) (
    input  wire        clk,
    input  wire        rst_n,      // active low, asynchronous assert
    input  wire [15:0] now,        // low 16 bits of the cycle counter
    // Event write port
    input  wire        ev_load,    // 1 = write the event below
    input  wire [15:0] ev_time,    // cycle time of the pin change
    input  wire        ev_value,   // pin value after the change
    input  wire        ev_oe,      // output enable after the change
    output wire        ev_ready,   // 1 = event slot is empty
    // Pin
    output reg         pin_value,
    output reg         pin_oe
);

  reg        ev_valid;  // 1 = the slot holds an event that did not occur yet
  reg [15:0] ev_time_q;
  reg        ev_value_q;
  reg        ev_oe_q;

  // The event occurs at the clock edge at the end of the cycle in which
  // the counter is equal to the event time.
  wire fire = ev_valid && (now == ev_time_q);

  // One slot only. A1 section 7 gives one event register for each pin.
  assign ev_ready = !ev_valid;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      ev_valid   <= 1'b0;
      // The event registers have a reset only so that the waveform and
      // the gate-level simulation show no X values. ev_valid = 0 makes
      // their content unimportant.
      ev_time_q  <= 16'd0;
      ev_value_q <= RESET_VALUE;
      ev_oe_q    <= RESET_OE;
      pin_value  <= RESET_VALUE;
      pin_oe     <= RESET_OE;
    end else begin
      if (fire) begin
        pin_value <= ev_value_q;
        pin_oe    <= ev_oe_q;
        ev_valid  <= 1'b0;
      end
      // fire needs ev_valid = 1 and a write needs ev_valid = 0. Thus,
      // the two conditions do not occur in the same cycle.
      if (ev_load && !ev_valid) begin
        ev_valid   <= 1'b1;
        ev_time_q  <= ev_time;
        ev_value_q <= ev_value;
        ev_oe_q    <= ev_oe;
      end
    end
  end

`ifdef FORMAL
  // ---------------------------------------------------------------------
  // Formal properties (formal/timed_out.sby with the harness
  // formal/timed_out_fv.v). Only Yosys with the FORMAL macro reads this
  // block; synthesis, the linter and the simulations do not. A monitor
  // (m_*) keeps a copy of each event that the slot must take and
  // calculates the pin from docs/isa.md 4.4, independently of the slot
  // registers above. The properties T1 to T4 hold for each sequence of
  // now. T5 and T6 are about the cycle count and apply while now adds 1
  // in each cycle (m_ok); START (counter clear) stops them for the event
  // in the slot.
  // ---------------------------------------------------------------------
  wire        f_take = ev_load && ev_ready;  // a write that the slot must take
  wire [15:0] f_lead = ev_time - now;        // lead of that write

  reg        m_pend;      // an event was taken and did not occur yet
  reg [15:0] m_t;         // its time
  reg        m_v, m_e;    // its value and output enable
  reg        m_pv, m_pe;  // expected pin value and output enable
  reg [16:0] m_j;         // cycles from the write: 1 in the cycle after it
  reg [16:0] m_d;         // expected cycles from the write to the event cycle
  reg [15:0] m_now;       // now in the cycle before
  reg        m_ok;        // now added 1 in each cycle since the write

  wire m_due = m_pend && (now == m_t);         // the event cycle (isa 4.4)
  wire f_ok  = m_ok && (now == m_now + 16'd1);

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      m_pend <= 1'b0;
      m_t    <= 16'd0;
      m_v    <= RESET_VALUE;
      m_e    <= RESET_OE;
      m_pv   <= RESET_VALUE;
      m_pe   <= RESET_OE;
      m_j    <= 17'd0;
      m_d    <= 17'd0;
      m_now  <= 16'd0;
      m_ok   <= 1'b0;
    end else begin
      m_now <= now;
      m_ok  <= f_ok;
      if (m_pend) m_j <= m_j + 17'd1;
      if (m_due) begin
        m_pend <= 1'b0;
        m_pv   <= m_v;
        m_pe   <= m_e;
      end
      if (f_take) begin
        m_pend <= 1'b1;
        m_t    <= ev_time;
        m_v    <= ev_value;
        m_e    <= ev_oe;
        m_j    <= 17'd1;
        // The first cycle after the write with now = t is d = (t - now)
        // mod 65,536 cycles later; for t = now it is 65,536 cycles later.
        m_d    <= (f_lead == 16'd0) ? 17'h10000 : {1'b0, f_lead};
        m_ok   <= 1'b1;
      end
    end
  end

  always @* begin
    // T1: The pin (value and output enable) is the reset value or the
    //     value of the last event that occurred. Thus, the pin changes only
    //     at the clock edge at the end of an event cycle, and it gets the
    //     value of that event.
    assert ((pin_value == m_pv) && (pin_oe == m_pe));
    // T2: The slot is full exactly while the monitor has an event that did
    //     not occur. Thus, a write into an empty slot is never lost, and the
    //     slot is empty again after the event cycle.
    assert (ev_ready == !m_pend);
    // T3: The slot keeps the time, value and output enable of the write.
    if (m_pend) assert ((ev_time_q == m_t) && (ev_value_q == m_v) && (ev_oe_q == m_e));
    if (m_pend && f_ok) begin
      // T4: The event did not occur early or late: 1 <= j <= d.
      assert ((m_j >= 17'd1) && (m_j <= m_d) && (m_d >= 17'd1) && (m_d <= 17'h10000));
      // T5: The cycles to the event time agree with the cycle count.
      assert ((m_t - now) == m_d[15:0] - m_j[15:0]);
      // T6: The event occurs exactly d cycles after its write, that is at
      //     cycle t: for 1 <= d <= 65,535 this is write cycle + d.
      if (m_due) assert (m_j == m_d);
    end
  end

  always @* begin
    cover (m_due && f_ok && (m_d == 17'd4));  // an event with lead 4 occurs
    cover (m_due && f_ok && (m_pv != m_v));   // an event changes the pin
  end
`endif

endmodule
