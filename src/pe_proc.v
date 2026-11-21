/*
 * Copyright (c) 2026 Steven Anthony
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// pe_proc: the programmable part of the chip: barrel core, memory, host
// FIFOs, the timed I/O unit and the two line helpers.
//
// What it does:
//   It connects the barrel core (pe_core) to:
//   - the program and data memory, two SRAM macros (pe_sram: 512 + 1,024
//     words of 16 bits),
//   - the host FIFOs (pe_host_fifo),
//   - 15 output event slots with their pin registers (pe_timed_out), one
//     for each output pin 0 to 14 (docs/isa.md 5.1),
//   - the input synchronizers and the input history (pe_timed_in),
//   - the USB low-speed helper (pe_slu SPEC 1) on I/O ports 16 to 31 and
//     event input 13, and the 10BASE-Te helper (pe_slu SPEC 2) on ports 32
//     to 47 and event input 14.
//   pin_v and pin_oe are the outputs of flip-flops: pins 0 to 7 go to uio,
//   pins 8 to 14 to uo_out[6:0].
//
// Why:
//   Pin changes of the threads come from the event slots, so each pin edge
//   is at the cycle of its event time and comes from a flip-flop. A pin
//   that the host gives to a helper (pe_config data[4]) shows the helper
//   signal through one more flip-flop, so the first symbol of a frame that
//   starts at TXTIME is on the pad from TXTIME + 1, like an OUT event.
//   The host side (memory, configuration, start, FIFO access) is a separate
//   port for the host SPI port (pe_host_port).
//
// Resets:
//   rst_n is the chip reset. run_rst_n is 0 while the host port stops the
//   threads (and during the chip reset). It resets the core, the FIFOs,
//   the event slots with their pin registers and the helpers, so STOP gives
//   the state after a chip reset. The memory keeps its words (docs/isa.md
//   2.4) and the input synchronizers keep running (rst_n only).
//
// Memory ports:
//   While the threads run, the core sends one request per cycle. While they
//   stop (h_mem_host = 1), the core is in reset and the host port's requests
//   go to the memory.

module pe_proc (
    input  wire        clk,
    input  wire        rst_n,       // chip reset, active low, asynchronous assert
    input  wire        run_rst_n,   // 0 = threads stopped (processor in reset)
    input  wire [15:0] now,         // low 16 bits of the cycle counter
    // Host side
    input  wire        start,
    input  wire [1:0]  prestart,
    input  wire [35:0] start_pc,
    input  wire [3:0]  run_mask,
    input  wire [29:0] pin_owner,
    input  wire [14:0] pin_en,
    input  wire [14:0] pin_od,
    input  wire [14:0] pin_hdrv,
    input  wire [14:0] pin_hsel,
    input  wire [44:0] pin_hsig,
    input  wire        h_mem_host,  // 1 = the host uses the memory
    input  wire        h_mem_req,
    input  wire        h_mem_we,
    input  wire [10:0] h_mem_addr,
    input  wire [15:0] h_mem_data,
    output wire [15:0] h_mem_rdata, // word of the last host read
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
  wire [10:0] c_mem_addr;
  wire        c_mem_we;
  wire [15:0] c_mem_wdata;
  wire [15:0] mem_rdata;

  // Core to event slots
  wire [14:0] ev_ready;
  wire [14:0] ev_load;
  wire [15:0] ev_time;
  wire        ev_value;
  wire        ev_oe;

  // Timed input unit to core
  wire [12:0] level;
  wire [14:0] s_now;
  wire [74:0] s_hist;

  // Core to FIFOs
  wire        fifo_pull;
  wire        fifo_push;
  wire [15:0] fifo_wdata;
  wire        fifo_in_valid;
  wire [15:0] fifo_in_data;
  wire        fifo_out_full;
  wire        h_out_valid;

  // Core to helpers
  wire        io_we, io_re;
  wire [5:0]  io_addr;
  wire [15:0] io_wdata;
  wire [15:0] usb_rdata, eth_rdata;
  wire [3:0]  usb_v, eth_v;
  wire        usb_oe, eth_oe, usb_ev, eth_ev;

  pe_core u_core (
      .clk          (clk),
      .rst_n        (run_rst_n),
      .now          (now),
      .start        (start),
      .prestart     (prestart),
      .start_pc     (start_pc),
      .run_mask     (run_mask),
      .pin_owner    (pin_owner),
      .pin_en       (pin_en),
      .pin_od       (pin_od),
      .pin_hdrv     (pin_hdrv),
      .mem_addr     (c_mem_addr),
      .mem_we       (c_mem_we),
      .mem_wdata    (c_mem_wdata),
      .mem_rdata    (mem_rdata),
      .io_we        (io_we),
      .io_re        (io_re),
      .io_addr      (io_addr),
      .io_wdata     (io_wdata),
      .io_rdata     (io_addr[5] ? eth_rdata : usb_rdata),
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

  // While running the core reads in every cycle; while stopped only the host's requests reach
  // the macros. Both request sets come from flip-flops.
  pe_sram u_mem (
      .clk  (clk),
      .rst_n(rst_n),
      .req  (h_mem_host ? h_mem_req : 1'b1),
      .we   (h_mem_host ? h_mem_we : c_mem_we),
      .addr (h_mem_host ? h_mem_addr : c_mem_addr),
      .wdata(h_mem_host ? h_mem_data : c_mem_wdata),
      .rdata(mem_rdata)
  );
  assign h_mem_rdata = mem_rdata;

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
      .h_out_data (h_out_data),
      .cnt_in     (h_cnt_in),
      .cnt_out    (h_cnt_out)
  );

  // Line helpers. DIN is captured on both clock edges from the pad level; AUX inputs use s(c).
  pe_slu #(
      .FULL(0),
      .SPEC(1)
  ) u_usb (
      .clk     (clk),
      .rst_n   (run_rst_n),
      .now     (now),
      .p_we    (io_we && (io_addr[5:4] == 2'b01)),
      .p_re    (io_re && (io_addr[5:4] == 2'b01)),
      .p_addr  (io_addr[3:0]),
      .p_wdata (io_wdata),
      .p_rdata (usb_rdata),
      .pin_raw (level),
      .pin_sync(s_now[12:0]),
      .tx_v    (usb_v),
      .tx_oe   (usb_oe),
      .ev      (usb_ev)
  );

  pe_slu #(
      .FULL(1),
      .SPEC(2)
  ) u_eth (
      .clk     (clk),
      .rst_n   (run_rst_n),
      .now     (now),
      .p_we    (io_we && (io_addr[5:4] == 2'b10)),
      .p_re    (io_re && (io_addr[5:4] == 2'b10)),
      .p_addr  (io_addr[3:0]),
      .p_wdata (io_wdata),
      .p_rdata (eth_rdata),
      .pin_raw (level),
      .pin_sync(s_now[12:0]),
      .tx_v    (eth_v),
      .tx_oe   (eth_oe),
      .ev      (eth_ev)
  );

  // docs/isa.md 2.4: after reset each output pin has V = 1, OE = 0. Thus
  // UART_TX (pin 12, uo_out[4]) is at the idle level and all uio pins are
  // inputs. A helper pin drives its signal from a flip-flop, with OE = 1 while the threads run.
  wire [14:0] slot_v, slot_oe;
  reg  [14:0] hv;
  // A helper pin is an output only while the threads run: STOP and the chip reset release it as
  // they release every other output (docs/isa.md 2.4, host STOP rules).
  reg         hrun;
  always @(posedge clk or negedge run_rst_n) begin
    if (!run_rst_n) hrun <= 1'b0;
    else hrun <= 1'b1;
  end
  genvar p;
  generate
    for (p = 0; p < 15; p = p + 1) begin : g_out
      pe_timed_out #(
          .RESET_VALUE(1'b1),
          .RESET_OE   (1'b0)
      ) u_slot (
          .clk      (clk),
          .rst_n    (run_rst_n),
          .pin_rst_n(rst_n),
          .now      (now),
          .ev_load  (ev_load[p]),
          .ev_time  (ev_time),
          .ev_value (ev_value),
          .ev_oe    (ev_oe),
          .ev_ready (ev_ready[p]),
          .pin_value(slot_v[p]),
          .pin_oe   (slot_oe[p])
      );

      wire [3:0] hx = pin_hsel[p] ? eth_v : usb_v;
      wire       ho = pin_hsel[p] ? eth_oe : usb_oe;
      reg        hs;
      always @* begin
        case (pin_hsig[3*p+:3])
          3'd0:    hs = hx[0];
          3'd1:    hs = hx[1];
          3'd2:    hs = hx[2];
          3'd3:    hs = hx[3];
          3'd4:    hs = ho;
          default: hs = !ho;
        endcase
      end
      always @(posedge clk or negedge run_rst_n) begin
        if (!run_rst_n) hv[p] <= 1'b1;
        else hv[p] <= hs;
      end
      assign pin_v[p]  = pin_hdrv[p] ? hv[p] : slot_v[p];
      assign pin_oe[p] = pin_hdrv[p] ? hrun : slot_oe[p];
    end
  endgenerate

  pe_timed_in u_in (
      .clk   (clk),
      .rst_n (rst_n),
      .ui_in (ui_in),
      .uio_in(uio_in),
      .pin_v (pin_v[7:0]),
      .pin_oe(pin_oe[7:0]),
      .ev_in ({eth_ev, usb_ev}),
      .level (level),
      .s_now (s_now),
      .s_hist(s_hist)
  );

  // The host port reads the word counts (PULL count byte), not the valid
  // flag of the selected out-FIFO.
  wire _unused = &{h_out_valid, usb_v[3:2], 1'b0};

endmodule
