/*
 * Copyright (c) 2026 Steven Anthony
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// uart_tx_fv: formal check of the M1 UART transmitter.
//
// What it does:
//   This module contains the chip top level and a reference UART receiver
//   (the "monitor"). The monitor looks only at the UART_TX pin (uo_out[4]).
//   The properties (P1 to P8) compare the pin and the handshake with the
//   monitor. The invariants (I0 to I13, H1 to H6) connect the internal
//   registers of the design to the monitor. With the invariants, the solver can prove the properties
//   for all time (k-induction), not only for a number of clock cycles.
//
// Why the internal signals come from "connect -set":
//   The Yosys on this workstation does not read hierarchical names in a
//   property. The script in uart_tx.sby flattens the design and connects
//   the f_* wires below to the internal registers before optimization.
//
// Values:
//   BIT_CYCLES = 434 and EVENT_LEAD = 4, the same as project.v. The proof
//   is for these values.
//
// Monitor timing:
//   The design changes the pin at the clock edge at the end of the cycle in
//   which the cycle counter is equal to the event time. Bit k of a frame
//   with start time t0 is on the pin when the counter is t0 + k*B + 1 to
//   t0 + (k + 1)*B. The monitor sees the start bit first in the cycle with
//   counter t0 + 1 (f_start). It then counts B cycles for each bit.

module uart_tx_fv (
    input wire       clk,
    input wire       rst_n,
    input wire [7:0] ui_in,
    input wire [7:0] uio_in,
    input wire       ena
);

  localparam [15:0] B    = 16'd434;  // project.v BIT_CYCLES
  localparam [15:0] LEAD = 16'd4;    // project.v EVENT_LEAD

  wire [7:0] uo_out;
  wire [7:0] uio_out;
  wire [7:0] uio_oe;

  tt_um_soundgod10_droid_protocol_emulator dut (
      .ui_in  (ui_in),
      .uo_out (uo_out),
      .uio_in (uio_in),
      .uio_out(uio_out),
      .uio_oe (uio_oe),
      .ena    (ena),
      .clk    (clk),
      .rst_n  (rst_n)
  );

  wire f_pin = uo_out[4];  // UART_TX
  wire f_ack = uo_out[5];  // TX_ACK
  wire f_req = ui_in[0];   // TX_REQ

  // Internal signals. uart_tx.sby connects these wires (connect -set).
  wire        f_rst_i;      // dut.rst_n_int
  wire [1:0]  f_rsync;      // dut.u_reset_sync.stage
  wire [31:0] f_count;      // dut.cycle_count
  wire        f_take;       // dut.u_uart_tx_seq.take
  wire [1:0]  f_state;      // dut.u_uart_tx_seq.state
  wire [9:0]  f_frame;      // dut.u_uart_tx_seq.frame
  wire [3:0]  f_nbits;      // dut.u_uart_tx_seq.nbits
  wire [15:0] f_tnext;      // dut.u_uart_tx_seq.t_next
  wire        f_v;          // dut.u_tx_channel.ev_valid
  wire [15:0] f_te;         // dut.u_tx_channel.ev_time_q
  wire        f_ve;         // dut.u_tx_channel.ev_value_q
  wire        f_eoe;        // dut.u_tx_channel.ev_oe_q
  wire        f_pin_oe;     // dut.u_tx_channel.pin_oe
  wire [1:0]  f_rq;         // dut.u_tx_req_sync.stage (TX_REQ synchronizer)

  wire [15:0] f_now = f_count[15:0];

  reg f_past_valid = 1'b0;
  always @(posedge clk) f_past_valid <= 1'b1;

  // ---------------------------------------------------------------------
  // Assumptions: what the host and the test board do.
  // ---------------------------------------------------------------------

  // The check starts in reset.
  initial assume (!rst_n);

  // The host sets TX_REQ to 0 during reset (TX_ACK is 0 after reset).
  always @(*) if (!rst_n) assume (!f_req);

  // The host changes TX_REQ, and TX_DATA, only when TX_REQ is equal to
  // TX_ACK. While a request waits, both stay stable.
  always @(posedge clk)
    if (f_past_valid && $past(rst_n) && rst_n && ($past(f_req) != $past(f_ack))) begin
      assume (f_req == $past(f_req));
      assume (uio_in == $past(uio_in));
    end

  // The same rule for TX_REQ without $past: while the design has the new
  // TX_REQ level in synchronizer stage 0 and TX_ACK is not yet equal to
  // it, the host keeps TX_REQ at that level. Each host that obeys the rule
  // above also obeys this rule. Reason: k-induction can start in a state
  // where the $past values are not valid (f_past_valid is 0); then only
  // this rule stops TX_REQ from changing in the cycle of the take.
  always @(*) assume ((f_rq[0] == f_ack) || (f_req == f_rq[0]));

  // ---------------------------------------------------------------------
  // Monitor: a reference UART receiver on the UART_TX pin.
  // ---------------------------------------------------------------------

  reg        m_busy;   // a frame is on the pin (after its first cycle)
  reg [3:0]  m_bit;    // bit number: 0 start, 1 to 8 data (LSB first), 9 stop
  reg [15:0] m_cnt;    // cycle in this bit, 0 to B-1
  reg [7:0]  m_byte;   // byte of the frame on the pin
  reg        m_pend;   // the design took a byte, and its frame did not start yet
  reg [7:0]  m_pbyte;  // the byte that the design took (TX_DATA at the take)
  reg        m_seen;   // the design took a byte after the last reset

  wire [9:0] m_word   = {1'b1, m_byte, 1'b0};  // stop, data, start
  wire       f_expect = m_word[m_bit];
  wire       f_start  = !m_busy && !f_pin;     // first cycle of a start bit

  always @(posedge clk or negedge f_rst_i) begin
    if (!f_rst_i) begin
      m_busy  <= 1'b0;
      m_bit   <= 4'd0;
      m_cnt   <= 16'd0;
      m_byte  <= 8'd0;
      m_pend  <= 1'b0;
      m_pbyte <= 8'd0;
      m_seen  <= 1'b0;
    end else begin
      if (f_take) begin
        m_pend  <= 1'b1;
        m_pbyte <= uio_in;
        m_seen  <= 1'b1;
      end
      if (f_start) begin
        m_pend <= 1'b0;
        m_byte <= m_pbyte;
        m_busy <= 1'b1;
        m_bit  <= 4'd0;
        m_cnt  <= 16'd1;
      end else if (m_busy) begin
        if (m_cnt == B - 16'd1) begin
          m_cnt <= 16'd0;
          if (m_bit == 4'd9) m_busy <= 1'b0;
          else m_bit <= m_bit + 4'd1;
        end else begin
          m_cnt <= m_cnt + 16'd1;
        end
      end
    end
  end

  // m_age:  clock cycles from the last take (stops at the maximum value).
  // h_wait: clock cycles that the sampled request level (synchronizer
  //         stage 0) is not equal to TX_ACK, that is, how long a host
  //         request waits for its acknowledge.
  reg [15:0] m_age;
  reg [15:0] h_wait;
  wire       h_req = (f_rq[0] != f_ack);  // a request was sampled and waits

  always @(posedge clk or negedge f_rst_i) begin
    if (!f_rst_i) begin
      m_age  <= 16'd0;
      h_wait <= 16'd0;
    end else begin
      if (f_take) m_age <= 16'd0;
      else if (m_age != 16'hFFFF) m_age <= m_age + 16'd1;
      if (!h_req) h_wait <= 16'd0;
      else if (h_wait != 16'hFFFF) h_wait <= h_wait + 16'd1;
    end
  end

  reg f_pin_q;
  always @(posedge clk) f_pin_q <= f_pin;

  // ---------------------------------------------------------------------
  // Properties
  // ---------------------------------------------------------------------

  // P1a: This checks that UART_TX is 1 (idle) while rst_n is 0.
  //      Reason: a UART receiver must not see a start bit during reset.
  always @(*) if (!rst_n) assert (f_pin);

  // P1b: This checks that UART_TX stays 1 after reset until the design
  //      takes the first byte. Reason: no frame without a request.
  always @(*) if (!m_seen) assert (f_pin);

  // P2: This checks each bit of each frame: start bit 0, the 8 data bits
  //     LSB first equal to the byte that the design took, stop bit 1. The
  //     monitor counts B cycles for each bit, so each bit must be on the
  //     pin for exactly B clock cycles. Reason: this is the UART frame
  //     (8N1) at 115,207 bit/s.
  always @(*) if (m_busy) assert (f_pin == f_expect);

  // P3: This checks that UART_TX changes only at the start of a bit (or
  //     when a reset starts). Reason: a change between bit boundaries is
  //     a glitch that a receiver can read as a wrong bit.
  always @(*)
    if (f_past_valid && f_rst_i && (f_pin != f_pin_q))
      assert ((m_busy && m_cnt == 16'd0) || f_start);

  // P4: This checks that a frame starts only for a byte that the design
  //     took and did not send yet. Reason: a request must not be sent two
  //     times, and there must be no frame without a request.
  always @(*) if (f_start) assert (m_pend);

  // P5: This checks that the design does not take a new byte while an
  //     earlier byte waits for its frame. Reason: then the earlier byte
  //     would be lost.
  always @(*) if (f_take) assert (!m_pend);

  // P6: This checks the pins that M1 does not use: uo_out[7:6] and
  //     uo_out[3:0] are 0, uio_out is 0, all uio pins are inputs, and the
  //     UART_TX output is always enabled. Reason: the template requires
  //     that all outputs have a value, and TX_DATA comes in on uio.
  always @(*) begin
    assert (uo_out[7:6] == 2'b00);
    assert (uo_out[3:0] == 4'b0000);
    assert (uio_out == 8'h00);
    assert (uio_oe == 8'h00);
    assert (f_pin_oe);
  end

  // P7: This checks that the frame of a byte that the design took starts
  //     not later than B-1 cycles after the take. Reason: a byte that the
  //     design took must not stay in the design without a frame (lost).
  always @(*) assert (!m_pend || (m_age <= B - 16'd1));

  // P8: This checks that the design takes a request (TX_ACK changes) not
  //     later than 11 bit times after it sampled the new TX_REQ level.
  //     Reason: a request from the host must not be lost. The longest
  //     wait is a request that comes just after a take: the design takes
  //     it in the stop bit of that frame.
  always @(*) assert (!h_req || (h_wait <= B * 16'd11 - 16'd1));

  // ---------------------------------------------------------------------
  // Invariants. They tell the solver which states the design can reach,
  // so that k-induction can prove P1 to P6 for all time.
  // ---------------------------------------------------------------------

  wire        f_idle = (f_state == 2'd0);
  wire        f_send = (f_state == 2'd1);
  wire        f_stop = (f_state == 2'd2);

  // f_w: number of bit events of the current frame that the sequencer
  // wrote into the timed output channel (0 to 10).
  wire [3:0]  f_w      = f_stop ? 4'd10 : (4'd10 - f_nbits);
  wire [15:0] f_t0     = f_tnext - ({12'd0, f_w} * B);  // start-bit event time
  wire [15:0] f_since  = f_now - f_t0;                  // cycles from t0
  wire [15:0] f_until  = f_t0 - f_now;                  // cycles to t0
  wire [15:0] f_pos    = ({12'd0, m_bit} * B) + m_cnt;  // monitor position
  wire        f_started = f_stop || (f_send && ((f_w >= 4'd2) || ((f_w == 4'd1) && !f_v)));
  wire        f_online  = f_started && (f_since != 16'd1);  // monitor has the byte
  wire [7:0]  f_fbyte   = f_online ? m_byte : m_pbyte;    // byte of the current frame
  wire [9:0]  f_word    = {1'b1, f_fbyte, 1'b0};
  wire [9:0]  f_frame_exp = (f_word >> f_w) | ~(10'h3FF >> f_w);

  // I0: The sequencer state is IDLE, SEND or STOP.
  always @(*) assert (f_state != 2'd3);

  // I1: The event output enable is always 1 (uo_out pins are outputs).
  always @(*) assert (f_eoe);

  // I2: In IDLE the event slot is empty, the frame register is all ones,
  //     the pin is 1, and the monitor has no frame and no waiting byte.
  always @(*)
    if (f_idle)
      assert (!f_v && (f_nbits == 4'd0) && (f_frame == 10'h3FF) && f_pin && !m_busy && !m_pend);

  // I3: In STOP all 10 bit events are written.
  always @(*) if (f_stop) assert (f_nbits == 4'd0);

  // I4: In SEND 1 to 10 bit events are still to write.
  always @(*) if (f_send) assert ((f_nbits >= 4'd1) && (f_nbits <= 4'd10));

  // I5: Before the start-bit event is written, the slot is empty and the
  //     start time is LEAD-1 to B-2 cycles in the future.
  always @(*)
    if (f_send && (f_nbits == 4'd10))
      assert (!f_v && (f_until >= LEAD - 16'd1) && (f_until <= B - 16'd2));

  // I6: The frame register holds the bits that are not yet written, and
  //     ones in the places of the written bits.
  always @(*) if (f_send || f_stop) assert (f_frame == f_frame_exp);

  // I7: An event in the slot is the last written bit: its time is one bit
  //     before t_next, it is 0 to B-1 cycles in the future, and its value
  //     is that bit of the frame.
  always @(*)
    assert (!f_v ||
            ((f_send || f_stop) && (f_w >= 4'd1) && (f_te == f_tnext - B) &&
             ((f_te - f_now) <= B - 16'd1) && (f_ve == f_word[f_w - 4'd1])));

  // I8: In SEND with an empty slot after the first event, the last event
  //     occurred in the cycle before, so t_next is B-1 cycles away.
  always @(*)
    if (f_send && (f_w >= 4'd1) && !f_v) assert ((f_tnext - f_now) == B - 16'd1);

  // I9: In STOP after the stop-bit event, the end of the stop bit is 0 to
  //     B-1 cycles away.
  always @(*) if (f_stop && !f_v) assert ((f_tnext - f_now) <= B - 16'd1);

  // I10: Before the start bit of the current frame is on the pin, the byte
  //      waits in the monitor, the pin is 1, and the monitor either has no
  //      frame or is in the stop bit of the frame before.
  always @(*)
    if (f_send && !f_started)
      assert (m_pend && f_pin && (f_until <= B - 16'd1) &&
              (!m_busy || ((m_bit == 4'd9) && (m_cnt == f_now - f_t0 + B - 16'd1))));

  // I11: After the start-bit event, the monitor follows the frame: in the
  //      first cycle of the start bit the pin is 0 and the byte still
  //      waits; after it the monitor position is the time from t0.
  always @(*)
    if (f_started)
      assert ((f_since >= 16'd1) && (f_since <= B * 16'd10) &&
              ((f_since == 16'd1) ? (!m_busy && m_pend && !f_pin)
                                  : (m_busy && !m_pend && (f_pos == f_since - 16'd1))));

  // I12: The monitor counters stay in their ranges.
  always @(*) if (m_busy) assert ((m_bit <= 4'd9) && (m_cnt <= B - 16'd1));

  // I13: Activity after reset means that a byte was taken; the reset
  //      synchronizer releases stage 1 only after stage 0.
  always @(*) assert (f_idle && !m_busy && !m_pend || m_seen);
  always @(*) assert (!f_rsync[1] || f_rsync[0]);
  always @(*) assert (!(f_start && f_take));

  // H1: A request that is in stage 0 but not yet in stage 1 of the
  //     synchronizer arrived in this cycle, so its wait is 0.
  always @(*) assert (!(h_req && (f_rq[1] == f_ack)) || (h_wait == 16'd0));

  // H2: While a request waits in stage 1, stage 0 has the same level
  //     (the host does not change TX_REQ before TX_ACK).
  always @(*) assert ((f_rq[1] == f_ack) || (f_rq[0] == f_rq[1]));

  // H3: A request that waits came after the last take, so its wait is
  //     not longer than the age of the last take.
  always @(*) assert (!h_req || (h_wait <= m_age));

  // H4: Before the start bit is on the pin, the age of the take and the
  //     cycles to the start bit together are not more than B-2.
  always @(*)
    assert (!(f_send && !f_started) ||
            ((m_age <= B - 16'd2) && (f_until <= B - 16'd2 - m_age)));

  // H5: After the start bit, the age of the take is not more than the
  //     time from the start bit plus B-2.
  always @(*) assert (!f_started || (m_age <= f_since + B - 16'd2));

  // H6: In IDLE, a request in stage 1 waited not more than 11*B-1 cycles
  //     (it came in the frame before, or in the cycle before).
  always @(*)
    assert (!(f_idle && (f_rq[1] != f_ack)) || (h_wait <= B * 16'd11 - 16'd1));

  // ---------------------------------------------------------------------
  // Cover: the frame start can occur (the properties are not empty).
  // ---------------------------------------------------------------------
  always @(*) begin
    cover (f_take);
    cover (f_start);
    cover (m_busy && (m_bit == 4'd0) && (m_cnt == 16'd2));
    cover (h_req && f_send);  // a request waits while a frame is sent (P8)
  end

endmodule
