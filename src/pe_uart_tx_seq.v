/*
 * Copyright (c) 2026 Steven Anthony
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// REMOVED IN M2c. This file is not a source of the chip any more: info.yaml
// and test/Makefile do not list it, and no module instantiates it. The UART
// transmitter is now firmware (fw/uart_tx.s) that the host loads through
// the host SPI port (src/pe_host_port.v). The text below is the last
// version (chip commit 634b990 and earlier), kept only as a record until
// the file is deleted with git rm.
//
// pe_uart_tx_seq: TEMPORARY UART frame sequencer (milestone M1).
//
// What it does:
//   This module sends one UART frame for each byte that the host gives.
//   A frame has 1 start bit (0), 8 data bits (LSB first) and 1 stop bit
//   (1). The module does not drive the pin. For each bit, it writes one
//   pin event into the timed output channel (pe_timed_out). The event
//   time of bit k is t0 + k * BIT_CYCLES. The channel changes the pin at
//   that cycle.
//
// Why it is TEMPORARY:
//   In architecture D (A1_architecture.md section 4.4), a program thread
//   writes these pin events ("SET pins AT time"). The threads come in
//   milestone M2. Until then, this fixed sequencer does the work of one
//   thread that runs a UART transmit program. M2 removes this module and
//   its host handshake. The timed output channel and the cycle counter
//   stay.
//
// Why it uses event times and not a bit-time counter:
//   The bit time comes only from the event times. Thus, the pin timing
//   does not change when the sequencer writes an event late (but before
//   its time). The M2 firmware will use the same method.
//
// Back-to-back frames:
//   If the host gives the next byte before the stop bit ends, the next
//   start bit occurs exactly at the end of the stop bit. Then each frame
//   is exactly 10 bit times. If the byte comes later, the next start bit
//   occurs LEAD cycles after the module takes the byte (the line stays
//   idle, high, between the frames).
//
// Host handshake (TEMPORARY, two-phase; M2 replaces it with the host SPI
// port, block D7):
//   1. The host puts the byte on "data".
//   2. The host changes the level of "req".
//   3. When the module takes the byte, it changes "ack" to the level of
//      "req". The host must keep "data" stable until ack is equal to req.
//   A level change (not a pulse) is used, so that a slow host cannot miss
//   the acknowledge.

module pe_uart_tx_seq #(
    parameter [15:0] BIT_CYCLES = 16'd434,  // clock cycles for one bit
    parameter [15:0] LEAD       = 16'd4     // minimum cycles from event write to event time
) (
    input  wire        clk,
    input  wire        rst_n,     // active low, asynchronous assert
    input  wire [15:0] now,       // low 16 bits of the cycle counter
    // Host side (TEMPORARY)
    input  wire        req,       // request level, already synchronized
    input  wire [7:0]  data,      // byte to send, stable while req != ack
    output reg         ack,       // acknowledge level
    // Event write port to pe_timed_out
    output wire        ev_load,
    output wire [15:0] ev_time,
    output wire        ev_value,
    input  wire        ev_ready
);

  localparam [1:0] S_IDLE = 2'd0;  // no frame; line is idle (high)
  localparam [1:0] S_SEND = 2'd1;  // write the events of the 10 bits
  localparam [1:0] S_STOP = 2'd2;  // all events written; wait for the end of the stop bit

  reg [1:0]  state;
  reg [9:0]  frame;   // bits that are not yet written as events, bit 0 next
  reg [3:0]  nbits;   // number of bits that are not yet written as events
  reg [15:0] t_next;  // cycle time of the next bit edge

  // A byte is waiting when the request level is not equal to the acknowledge level.
  wire pending = (req != ack);

  // Cycles from now to the end of the stop bit. In S_STOP this is
  // 0 to BIT_CYCLES, so the 16-bit difference does not wrap.
  wire [15:0] remaining = t_next - now;

  // In S_STOP the module can start the next frame back-to-back when:
  // - the stop-bit event occurred (the slot is empty), and
  // - there are more than LEAD cycles to the end of the stop bit, so the
  //   new start-bit event is written before its time.
  wire stop_can_chain = (state == S_STOP) && ev_ready && (remaining > LEAD);

  wire take = pending && ((state == S_IDLE) || stop_can_chain);

  // Write the next bit as soon as the slot is empty. The slot is empty
  // after the previous bit edge occurred, so there are about BIT_CYCLES
  // cycles before the time of this event.
  assign ev_load  = (state == S_SEND) && ev_ready;
  assign ev_time  = t_next;
  assign ev_value = frame[0];

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state  <= S_IDLE;
      frame  <= 10'h3FF;  // all ones = idle level; no effect in S_IDLE
      nbits  <= 4'd0;
      t_next <= 16'd0;
      ack    <= 1'b0;     // the host must set req = 0 during reset
    end else if (take) begin
      // Frame bits in send order (bit 0 first): start 0, data LSB first, stop 1.
      frame <= {1'b1, data, 1'b0};
      nbits <= 4'd10;
      ack   <= ~ack;      // now ack = req: the host can change data
      state <= S_SEND;
      // From S_IDLE: the start bit occurs LEAD cycles from now.
      // From S_STOP: t_next already is the end of the stop bit, so the
      // new start bit follows the old stop bit with no gap.
      if (state == S_IDLE) begin
        t_next <= now + LEAD;
      end
    end else begin
      case (state)
        S_SEND: begin
          if (ev_ready) begin
            // ev_load is 1 in this cycle: the channel takes the event.
            frame  <= {1'b1, frame[9:1]};
            t_next <= t_next + BIT_CYCLES;
            nbits  <= nbits - 4'd1;
            if (nbits == 4'd1) begin
              // The stop bit is written. t_next is now the end of the stop bit.
              state <= S_STOP;
            end
          end
        end
        S_STOP: begin
          // At the end of the stop bit with no new byte, the frame is complete.
          if (now == t_next) begin
            state <= S_IDLE;
          end
        end
        default: begin
          state <= S_IDLE;
        end
      endcase
    end
  end

endmodule
