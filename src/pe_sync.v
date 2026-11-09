/*
 * Copyright (c) 2026 Steven Anthony
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// pe_reset_sync: reset synchronizer.
//
// What it does:
//   When rst_n_in goes low, rst_n_out goes low at once (no clock is
//   necessary). When rst_n_in goes high, rst_n_out goes high at the
//   second rising clock edge.
//
// Why:
//   The reset comes from a pad, without a relation to the clock. If the
//   flip-flops leave reset near a clock edge, some can leave it one cycle
//   later than others (recovery time). This block releases the reset on a
//   clock edge, so that all flip-flops leave reset in the same cycle. The
//   assert is asynchronous, so that the UART pin goes to idle (high) at
//   once, also without a clock.

module pe_reset_sync (
    input  wire clk,
    input  wire rst_n_in,   // reset from the pad, active low
    output wire rst_n_out   // reset for the design, active low
);

  reg [1:0] stage;

  always @(posedge clk or negedge rst_n_in) begin
    if (!rst_n_in) begin
      stage <= 2'b00;
    end else begin
      // Two stages: the first stage can go metastable, the second gives
      // it one clock cycle to become stable.
      stage <= {stage[0], 1'b1};
    end
  end

  assign rst_n_out = stage[1];

endmodule

// pe_sync2: two flip-flop synchronizer for one input bit.
//
// What it does:
//   The output is the input, 2 clock cycles later.
//
// Why:
//   An input pin can change at any time relative to the clock. The first
//   flip-flop can go metastable. The second flip-flop gives it one clock
//   cycle to become stable. A1 section 2.6 gives this 2-stage synchronizer
//   for each input sample.

module pe_sync2 (
    input  wire clk,
    input  wire rst_n,  // active low, asynchronous assert
    input  wire d,      // asynchronous input
    output wire q       // synchronized output
);

  reg [1:0] stage;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      stage <= 2'b00;
    end else begin
      stage <= {stage[0], d};
    end
  end

  assign q = stage[1];

endmodule

// pe_spike_filter: input spike filter for one synchronized bit.
//
// What it does:
//   The output q takes the level of d only after d has differed from q in
//   K clock samples in a row. Thus a pulse that is sampled K - 1 times or
//   less does not reach q, and a clean level change reaches q K cycles
//   after the first sample of the new level.
//
// Why:
//   I2C Fast-mode needs input filters on SDA and SCL that suppress spikes
//   of less than 50 ns (NXP UM10204 Rev. 7.0, Table 10 tSP and note [8],
//   PDF pages 43 and 44). A pulse of width w is sampled floor(w / T) or
//   ceil(w / T) times, depending on its phase, so at 50 MHz (T = 20 ns) a
//   41 to 49 ns spike can give 3 samples. K = 4 suppresses every spike
//   shorter than 3 T: 60 ns at 50 MHz, 75 ns at 40 MHz. The input d must
//   come from a synchronizer (pe_sync2).

module pe_spike_filter #(
    parameter integer K = 4  // samples in a row for a change, 2 to 4
) (
    input  wire clk,
    input  wire rst_n,  // active low, asynchronous assert
    input  wire d,      // synchronized input
    output reg  q       // filtered output
);

  localparam [1:0] LAST = K[1:0] - 2'd1;  // K - 1 in 2 bits

  reg [1:0] cnt;  // samples in a row before this one with d not equal to q

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      q   <= 1'b0;  // as the synchronizer (docs/isa.md 2.4)
      cnt <= 2'd0;
    end else if (d == q) begin
      cnt <= 2'd0;
    end else if (cnt == LAST) begin
      q   <= d;
      cnt <= 2'd0;
    end else begin
      cnt <= cnt + 2'd1;
    end
  end

endmodule
