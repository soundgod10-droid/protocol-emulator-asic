/*
 * Copyright (c) 2026 Steven Anthony
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// pe_proc: the programmable part of the chip (architecture D, A1 section
// 4.4): barrel core, memory, host FIFOs and the timed I/O unit.
//
// What it does:
//   It connects the barrel core (pe_core, block D1) to:
//   - the 128 x 16 latch memory for program and data (pe_latch_ram,
//     decision D2),
//   - the host FIFOs (pe_host_fifo, block D6),
//   - 15 output event slots with their pin registers (pe_timed_out from
//     M1, block D2), one for each output pin 0 to 14 (docs/isa.md 5.1),
//   - the input synchronizers and the input history (pe_timed_in, D3).
//   pin_v and pin_oe are the outputs of the pin flip-flops: pins 0 to 7
//   go to uio, pins 8 to 14 to uo_out[6:0].
//
// Why:
//   All pin changes come from the event slots, so each pin edge is at
//   the cycle of its event time and comes from a flip-flop (no glitch).
//   The host side (memory write and read, configuration, start, FIFO
//   access) is a separate port for the host SPI port (pe_host_port).
//
// Resets (M2c):
//   rst_n is the chip reset. run_rst_n is 0 while the host port stops the
//   threads (and during the chip reset). It resets the core, the FIFOs
//   and the event slots with their pin registers, so STOP gives the state
//   after a chip reset and the next START has no old event and no old
//   FIFO word (M2b open item 3). The memory keeps its words (docs/isa.md
//   2.4) and the input synchronizers keep running (rst_n only), so the
//   input history at START is the real pin history.
//
// Memory ports:
//   A store of the core and a host write use the same write port. A
//   store has priority. The host writes and reads only while the threads
//   stop (h_mem_host = 1): then the core is in reset and does not store,
//   and the read port reads h_mem_addr for the host.

module pe_proc (
    input  wire        clk,
    input  wire        rst_n,       // chip reset, active low, asynchronous assert
    input  wire        run_rst_n,   // 0 = threads stopped (processor in reset)
    input  wire [15:0] now,         // low 16 bits of the cycle counter
    // Host side
    input  wire        start,
    input  wire [27:0] start_pc,
    input  wire [3:0]  run_mask,
    input  wire [29:0] pin_owner,
    input  wire [14:0] pin_en,
    input  wire [14:0] pin_od,
    input  wire        h_mem_host,  // 1 = the host uses the memory ports
    input  wire        h_mem_we,
    input  wire [6:0]  h_mem_addr,
    input  wire [15:0] h_mem_data,
    output wire [15:0] h_mem_rdata, // word at h_mem_addr while h_mem_host = 1
    input  wire [1:0]  h_tid,
    input  wire        h_push,
    input  wire [15:0] h_wdata,
    output wire        h_in_full,
    input  wire        h_pop,
    output wire [15:0] h_out_data,
    output wire [11:0] h_cnt_in,
    output wire [11:0] h_cnt_out,
    output wire [3:0]  th_run,
    output wire [3:0]  th_late,
    output wire [3:0]  th_err,
    // Chip pins
    input  wire [4:0]  ui_in,       // ui_in[4:0]: inputs 8 to 12
    input  wire [7:0]  uio_in,
    output wire [14:0] pin_v,
    output wire [14:0] pin_oe
);

  // Core to memory
  wire [6:0]  mem_raddr;
  wire [15:0] mem_rdata;
  wire        c_mem_we;
  wire [6:0]  c_mem_waddr;
  wire [15:0] c_mem_wdata;

  // Core to event slots
  wire [14:0] ev_ready;
  wire [14:0] ev_load;
  wire [15:0] ev_time;
  wire        ev_value;
  wire        ev_oe;

  // Timed input unit to core
  wire [12:0] s_now;
  wire [64:0] s_hist;

  // Core to FIFOs
  wire        fifo_pull;
  wire        fifo_push;
  wire [15:0] fifo_wdata;
  wire        fifo_in_valid;
  wire [15:0] fifo_in_data;
  wire        fifo_out_full;
  wire        h_out_valid;
  wire [6:0]  c_mem_raddr;

  pe_core u_core (
      .clk          (clk),
      .rst_n        (run_rst_n),
      .now          (now),
      .start        (start),
      .start_pc     (start_pc),
      .run_mask     (run_mask),
      .pin_owner    (pin_owner),
      .pin_en       (pin_en),
      .pin_od       (pin_od),
      .mem_raddr    (c_mem_raddr),
      .mem_rdata    (mem_rdata),
      .mem_we       (c_mem_we),
      .mem_waddr    (c_mem_waddr),
      .mem_wdata    (c_mem_wdata),
      .ev_ready     (ev_ready),
      .ev_load      (ev_load),
      .ev_time      (ev_time),
      .ev_value     (ev_value),
      .ev_oe        (ev_oe),
      .s_now        (s_now),
      .s_hist       (s_hist),
      .fifo_pull    (fifo_pull),
      .fifo_push    (fifo_push),
      .fifo_wdata   (fifo_wdata),
      .fifo_in_valid(fifo_in_valid),
      .fifo_in_data (fifo_in_data),
      .fifo_out_full(fifo_out_full),
      .th_run       (th_run),
      .th_late      (th_late),
      .th_err       (th_err)
  );

  assign mem_raddr   = h_mem_host ? h_mem_addr : c_mem_raddr;
  assign h_mem_rdata = mem_rdata;

  pe_latch_ram #(
      .WORDS(128),
      .AW   (7),
      .WIDTH(16)
  ) u_mem (
      .clk  (clk),
      .we   (c_mem_we || h_mem_we),
      .waddr(c_mem_we ? c_mem_waddr : h_mem_addr),
      .wdata(c_mem_we ? c_mem_wdata : h_mem_data),
      .raddr(mem_raddr),
      .rdata(mem_rdata)
  );

  pe_host_fifo u_fifo (
      .clk        (clk),
      .rst_n      (run_rst_n),
      .t_tid      (now[1:0]),
      .t_pull     (fifo_pull),
      .t_push     (fifo_push),
      .t_wdata    (fifo_wdata),
      .t_in_valid (fifo_in_valid),
      .t_in_data  (fifo_in_data),
      .t_out_full (fifo_out_full),
      .h_tid      (h_tid),
      .h_push     (h_push),
      .h_wdata    (h_wdata),
      .h_in_full  (h_in_full),
      .h_pop      (h_pop),
      .h_out_valid(h_out_valid),
      .h_out_data (h_out_data)
  );

  // docs/isa.md 2.4: after reset each output pin has V = 1, OE = 0. Thus
  // UART_TX (pin 12, uo_out[4]) is at the idle level and all uio pins are
  // inputs.
  genvar p;
  generate
    for (p = 0; p < 15; p = p + 1) begin : g_out
      pe_timed_out #(
          .RESET_VALUE(1'b1),
          .RESET_OE   (1'b0)
      ) u_slot (
          .clk      (clk),
          .rst_n    (rst_n),
          .now      (now),
          .ev_load  (ev_load[p]),
          .ev_time  (ev_time),
          .ev_value (ev_value),
          .ev_oe    (ev_oe),
          .ev_ready (ev_ready[p]),
          .pin_value(pin_v[p]),
          .pin_oe   (pin_oe[p])
      );
    end
  endgenerate

  pe_timed_in u_in (
      .clk   (clk),
      .rst_n (rst_n),
      .ui_in (ui_in),
      .uio_in(uio_in),
      .pin_v (pin_v[7:0]),
      .pin_oe(pin_oe[7:0]),
      .s_now (s_now),
      .s_hist(s_hist)
  );

  // A host write to a full FIFO has no effect; the test bench knows the
  // FIFO state from the model, so the flag has no load here.
  wire _unused = &{h_in_full, 1'b0};

endmodule
