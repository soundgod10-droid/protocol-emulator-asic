/*
 * Copyright (c) 2026 Steven Anthony
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// timed_out_fv: formal harness of one output event slot (pe_timed_out,
// A1 block D2, M1).
//
// What it does:
//   It connects the slot, with the parameters of pe_proc (reset V = 1,
//   OE = 0, docs/isa.md 2.4), to the cycle counter. The counter clear
//   (START) and all event write inputs are free in each cycle: a write
//   can come in each cycle, with each time, also when the slot is full.
//   The properties are in pe_timed_out (`ifdef FORMAL: T1 to T6).
//
// Why:
//   The core properties (pe_core E1 to E8) show that the core writes an
//   event only into an empty slot, 4 to 32,767 cycles before its time.
//   These properties show that the slot then keeps the event and changes
//   the pin exactly at that time. Together: no pin event is lost or
//   applied at a wrong cycle.

module timed_out_fv (
    input wire        clk,
    input wire        rst_n,
    input wire        clear,     // START: counter is 0 in the next cycle
    input wire        ev_load,
    input wire [15:0] ev_time,
    input wire        ev_value,
    input wire        ev_oe
);

  // The check starts in reset. After it, reset can occur at any cycle.
  initial assume (!rst_n);

  wire [15:0] now;
  wire        ev_ready;
  wire        pin_value;
  wire        pin_oe;

  pe_cycle_counter #(
      .WIDTH(16)
  ) u_cnt (
      .clk  (clk),
      .rst_n(rst_n),
      .clear(clear),
      .count(now)
  );

  pe_timed_out #(
      .RESET_VALUE(1'b1),
      .RESET_OE   (1'b0)
  ) u_slot (
      .clk      (clk),
      .rst_n    (rst_n),
      .now      (now),
      .ev_load  (ev_load),
      .ev_time  (ev_time),
      .ev_value (ev_value),
      .ev_oe    (ev_oe),
      .ev_ready (ev_ready),
      .pin_value(pin_value),
      .pin_oe   (pin_oe)
  );

endmodule
