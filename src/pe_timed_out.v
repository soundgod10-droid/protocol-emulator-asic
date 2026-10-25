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

endmodule
