/*
 * Copyright (c) 2026 Steven Anthony
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// tt_um_soundgod10_droid_protocol_emulator: top level of the protocol
// emulator chip. Milestone M2 (run M2c): programmable processor with a
// host SPI port.
//
// What it does:
//   The chip is a 4-thread barrel processor with timed pin I/O
//   (docs/isa.md). The host loads the program and the configuration
//   through the host SPI port, starts the threads, and gives and takes
//   data words through the host FIFOs (docs/host_port.md). The chip
//   changes a protocol pin only when a program tells it to. Example:
//   fw/uart_tx.s makes thread 0 a UART transmitter on uo_out[4].
//
// Why it has this structure:
//   Architecture D (A1_architecture.md section 4.4): the threads change
//   the pins only through the timed I/O unit (event slots), so each pin
//   edge is at the cycle of its event time. The host SPI port (A1 block
//   D7) replaces the TEMPORARY M1 sequencer and the TEMPORARY M2b test
//   port (removed in M2c).
//
// Pins (A1 section 2.2, docs/isa.md 5.1):
//   ui_in[7]     HOST_SCK   host SPI clock (SPI mode 0)
//   ui_in[6]     HOST_CS_N  host SPI chip select, active low
//   ui_in[5]     HOST_MOSI  host SPI data, host to chip
//   uo_out[7]    HOST_MISO  host SPI data, chip to host
//   ui_in[4:0]   processor inputs 8 to 12 (ui_in[3]: default UART RX)
//   uo_out[6:0]  processor outputs 8 to 14 (uo_out[4]: default UART TX)
//   uio[7:0]     processor pins 0 to 7 (output with enable, and input)
//   During and after reset, and while the threads stop, each processor
//   output has V = 1, OE = 0: uo_out[6:0] = 1 (UART TX idle level) and
//   all uio pins are inputs (docs/isa.md 2.4).

module tt_um_soundgod10_droid_protocol_emulator (
    input  wire [7:0] ui_in,    // Dedicated inputs
    output wire [7:0] uo_out,   // Dedicated outputs
    input  wire [7:0] uio_in,   // IOs: Input path
    output wire [7:0] uio_out,  // IOs: Output path
    output wire [7:0] uio_oe,   // IOs: Enable path (active high: 0=input, 1=output)
    input  wire       ena,      // always 1 when the design is powered, so you can ignore it
    input  wire       clk,      // clock
    input  wire       rst_n     // reset_n - low to reset
);

  // Reset: assert at once (the pins go to the reset level also without a
  // clock), release on a clock edge (all flip-flops leave reset in the
  // same cycle).
  wire rst_n_int;
  pe_reset_sync u_reset_sync (
      .clk      (clk),
      .rst_n_in (rst_n),
      .rst_n_out(rst_n_int)
  );

  // Host SPI pins. They come from the host without a relation to our
  // clock: 2-stage synchronizers (M1 pe_sync2), all three with the same
  // delay, so the port sees MOSI and CS_N in the same cycle as the SCK
  // edge that goes with them. Two stages: the measured metastability
  // constant of dfrbpq_1 (103 ps at the slow corner, spice/m2c_sync_tau_ss.sp)
  // gives a mean time between failures of more than 1e70 s (M2c_loader.md
  // section 1). CS_N is inverted before its synchronizer, so the reset
  // value 0 means "no transaction".
  wire host_sck;
  wire host_cs;
  wire host_mosi;
  pe_sync2n u_sck_sync (
      .clk  (clk),
      .rst_n(rst_n_int),
      .d    (ui_in[7]),
      .q    (host_sck)
  );
  pe_sync2n u_cs_sync (
      .clk  (clk),
      .rst_n(rst_n_int),
      .d    (~ui_in[6]),
      .q    (host_cs)
  );
  pe_sync2 u_mosi_sync (
      .clk  (clk),
      .rst_n(rst_n_int),
      .d    (ui_in[5]),
      .q    (host_mosi)
  );

  wire        host_miso;
  wire        running;
  wire        h_start;
  wire        h_mem_we;
  wire [6:0]  h_mem_addr;
  wire [15:0] h_mem_wdata;
  wire [15:0] h_mem_rdata;
  wire        h_cfg_we;
  wire [6:0]  h_cfg_addr;
  wire [15:0] h_cfg_wdata;
  wire [1:0]  h_tid;
  wire        h_push;
  wire [15:0] h_wdata;
  wire        h_in_full;
  wire        h_pop;
  wire [15:0] h_out_data;
  wire [11:0] h_cnt_in;
  wire [11:0] h_cnt_out;
  wire [3:0]  th_run;
  wire [3:0]  th_late;
  wire [3:0]  th_err;

  // A1 block D7: host SPI port (docs/host_port.md).
  pe_host_port u_host_port (
      .clk          (clk),
      .rst_n        (rst_n_int),
      .sck          (host_sck),
      .cs           (host_cs),
      .mosi         (host_mosi),
      .miso         (host_miso),
      .running      (running),
      .start        (h_start),
      .mem_we       (h_mem_we),
      .mem_addr     (h_mem_addr),
      .mem_wdata    (h_mem_wdata),
      .mem_rdata    (h_mem_rdata),
      .cfg_we       (h_cfg_we),
      .cfg_addr     (h_cfg_addr),
      .cfg_wdata    (h_cfg_wdata),
      .fifo_tid     (h_tid),
      .fifo_push    (h_push),
      .fifo_wdata   (h_wdata),
      .fifo_in_full (h_in_full),
      .fifo_pop     (h_pop),
      .fifo_out_data(h_out_data),
      .cnt_in       (h_cnt_in),
      .cnt_out      (h_cnt_out),
      .th_run       (th_run),
      .th_late      (th_late),
      .th_err       (th_err)
  );

  // D2 cycle counter: the common time base of all pin events. START
  // clears it, so the first thread cycle is cycle 0 (docs/isa.md 3.1).
  wire [31:0] cycle_count;
  pe_cycle_counter #(
      .WIDTH(32)
  ) u_cycle_counter (
      .clk  (clk),
      .rst_n(rst_n_int),
      .clear(h_start),
      .count(cycle_count)
  );

  // Processor reset: the chip reset, or the threads stop. Both inputs are
  // flip-flop outputs (pe_reset_sync, pe_host_port), so the AND has no
  // glitch, and the release is at a clock edge.
  wire run_rst_n = rst_n_int & running;

  // ---------------------------------------------------------------------
  // Configuration and processor
  // ---------------------------------------------------------------------

  wire [27:0] start_pc;
  wire [3:0]  run_mask;
  wire [29:0] pin_owner;
  wire [14:0] pin_en;
  wire [14:0] pin_od;
  pe_config u_config (
      .clk      (clk),
      .rst_n    (rst_n_int),
      .we       (h_cfg_we),
      .addr     (h_cfg_addr),
      .data     (h_cfg_wdata),
      .start_pc (start_pc),
      .run_mask (run_mask),
      .pin_owner(pin_owner),
      .pin_en   (pin_en),
      .pin_od   (pin_od)
  );

  wire [14:0] pin_v;
  wire [14:0] pin_oe;
  pe_proc u_proc (
      .clk        (clk),
      .rst_n      (rst_n_int),
      .run_rst_n  (run_rst_n),
      .now        (cycle_count[15:0]),
      .start      (h_start),
      .start_pc   (start_pc),
      .run_mask   (run_mask),
      .pin_owner  (pin_owner),
      .pin_en     (pin_en),
      .pin_od     (pin_od),
      .h_mem_host (!running),
      .h_mem_we   (h_mem_we),
      .h_mem_addr (h_mem_addr),
      .h_mem_data (h_mem_wdata),
      .h_mem_rdata(h_mem_rdata),
      .h_tid      (h_tid),
      .h_push     (h_push),
      .h_wdata    (h_wdata),
      .h_in_full  (h_in_full),
      .h_pop      (h_pop),
      .h_out_data (h_out_data),
      .h_cnt_in   (h_cnt_in),
      .h_cnt_out  (h_cnt_out),
      .th_run     (th_run),
      .th_late    (th_late),
      .th_err     (th_err),
      .ui_in      (ui_in[4:0]),
      .uio_in     (uio_in),
      .pin_v      (pin_v),
      .pin_oe     (pin_oe)
  );

  // All output pins are assigned. uo_out[7] = HOST_MISO (a flip-flop
  // output), uo_out[6:0] = V of pins 14 to 8; uio_out and uio_oe = V and
  // OE of pins 7 to 0. A uo_out pin has no enable, so a released pin
  // shows V = 1 (docs/isa.md 4.5).
  assign uo_out  = {host_miso, pin_v[14:8]};
  assign uio_out = pin_v[7:0];
  assign uio_oe  = pin_oe[7:0];

  // Signals with no load:
  //   ena: always 1 (template).
  //   cycle_count[31:16]: docs/isa.md uses only the low 16 bits (now).
  //   pin_oe[14:8]: uo_out pins have no output enable.
  wire _unused = &{ena, cycle_count[31:16], pin_oe[14:8], 1'b0};

endmodule
