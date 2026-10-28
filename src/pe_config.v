/*
 * Copyright (c) 2026 Steven Anthony
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// pe_config: host configuration registers of the processor.
//
// What it does:
//   The host writes these registers with (we, addr, data) while the
//   threads stop (docs/isa.md 2.2):
//     addr 0 to 3   start address of thread addr (data[6:0])
//     addr 4        RUN of the 4 threads at start (data[3:0], bit n = thread n)
//     addr 16 to 30 output pin p = addr - 16: data[1:0] owner thread,
//                   data[2] owner enable, data[3] open-drain mode
//   Other addresses and pin 15 (addr 31) have no register.
//
// Why:
//   docs/isa.md 5.2: each output pin has one owner thread, which the host
//   sets. 5.3: open-drain mode for each pin. 2.2: a start address for each
//   thread. The write port is generic, so that the M2c host SPI port can
//   use it in place of the TEMPORARY M2b test port.

module pe_config (
    input  wire        clk,
    input  wire        rst_n,      // active low, asynchronous assert
    input  wire        we,
    input  wire [6:0]  addr,
    input  wire [15:0] data,
    output reg  [27:0] start_pc,   // thread n: [7n+6:7n]
    output reg  [3:0]  run_mask,
    output reg  [29:0] pin_owner,  // pin p: [2p+1:2p]
    output reg  [14:0] pin_en,
    output reg  [14:0] pin_od
);

  // docs/isa.md 2.4: start address 0, no owner, push-pull after reset.
  genvar n, p;
  generate
    for (n = 0; n < 4; n = n + 1) begin : g_start
      localparam [6:0] A = n;
      always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
          start_pc[7*n+:7] <= 7'd0;
        end else if (we && (addr == A)) begin
          start_pc[7*n+:7] <= data[6:0];
        end
      end
    end

    for (p = 0; p < 15; p = p + 1) begin : g_pin
      localparam [6:0] A = 7'd16 + p;
      always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
          pin_owner[2*p+:2] <= 2'd0;
          pin_en[p]         <= 1'b0;
          pin_od[p]         <= 1'b0;
        end else if (we && (addr == A)) begin
          pin_owner[2*p+:2] <= data[1:0];
          pin_en[p]         <= data[2];
          pin_od[p]         <= data[3];
        end
      end
    end
  endgenerate

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      run_mask <= 4'd0;
    end else if (we && (addr == 7'd4)) begin
      run_mask <= data[3:0];
    end
  end

  // The registers use data[6:0] only (G1 4.8: linter UNUSEDSIGNAL).
  wire _unused = &{data[15:7], 1'b0};

endmodule
