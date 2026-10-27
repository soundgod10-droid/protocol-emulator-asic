/*
 * Copyright (c) 2026 Steven Anthony
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// tt_um_soundgod10_droid_protocol_emulator: top level of the protocol
// emulator chip. Milestone M1: UART transmitter.
//
// What it does:
//   The chip sends UART frames (8 data bits, LSB first, 1 start bit,
//   1 stop bit, no parity) on uo_out[4]. With a 50 MHz clock the bit rate
//   is 115,207 bit/s. The host gives each byte on uio_in[7:0] and changes
//   the level of ui_in[0]. The chip sets uo_out[5] to the level of ui_in[0]
//   when it took the byte.
//
// Why it has this structure:
//   Architecture D (A1_architecture.md section 4.4) changes the pins only
//   through the timed I/O unit. M1 contains the first parts of that unit
//   (block D2): the free-running cycle counter and one timed output
//   channel. A TEMPORARY fixed sequencer does the work of a program thread
//   until milestone M2 adds the threads. The host handshake on ui_in[0],
//   uio_in and uo_out[5] is also TEMPORARY (M2: host SPI port, block D7).
//
// Pins in M1 mode (CORE_SEL = 0; A1 section 2.2 gives uo_out[4] as the
// default UART TX pin):
//   uo_out[4]  UART_TX  UART data out. High when idle and during reset.
//   uo_out[5]  TX_ACK   TEMPORARY. Equal to ui_in[0] when the chip took the byte.
//   ui_in[0]   TX_REQ   TEMPORARY. Change its level to send the byte on uio_in.
//   uio_in     TX_DATA  TEMPORARY. Byte to send. All uio pins are inputs.
//   All other outputs are 0.
//
// Milestone M2b adds the processor (pe_proc: barrel core, latch memory,
// host FIFOs, timed I/O unit, docs/isa.md). M2c removes the M1 sequencer
// and connects the host SPI port. Until then a TEMPORARY selection pin
// gives the pins to the processor:
//   ui_in[7]   CORE_SEL TEMPORARY. 1 = processor mode: uo_out[6:0] and
//                       uio are the processor output pins 8 to 14 and 0
//                       to 7 (docs/isa.md 5.1). 0 = M1 mode (above).
//   ui_in[6]   TP_CLK   TEMPORARY test port (pe_test_port): each level
//                       change is one bit.
//   ui_in[5]   TP_DAT   TEMPORARY test port: bit value.
//   uo_out[7]  TP_OUT   TEMPORARY test port readout (processor mode).
//   ui_in[4:0], uio    processor input pins 8 to 12 and 0 to 7.
// Reason: the M1 tests and the GitHub gate-level test stay green, and the
// M2b tests use only chip pins (also at gate level).

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

  // Bit time in clock cycles (A1 section 3):
  //   50,000,000 / 115,200 = 434.03 cycles. We use 434.
  //   50,000,000 / 434 = 115,207.4 bit/s. Error: +0.0064 %.
  //   Our part of the UART clock-error budget is +/-1 % (A1 section 3).
  localparam [15:0] BIT_CYCLES = 16'd434;

  // Minimum number of cycles from an event write to the event time.
  // A1 section 4.4 sets 4 cycles for the threads; M1 uses the same value.
  localparam [15:0] EVENT_LEAD = 16'd4;

  // Reset: assert at once (the UART pin goes high also without a clock),
  // release on a clock edge (all flip-flops leave reset in the same cycle).
  wire rst_n_int;
  pe_reset_sync u_reset_sync (
      .clk      (clk),
      .rst_n_in (rst_n),
      .rst_n_out(rst_n_int)
  );

  // D2 cycle counter: the common time base of all pin events.
  wire [31:0] cycle_count;
  wire        tp_start;  // test port START (declared here, used by the counter)
  pe_cycle_counter #(
      .WIDTH(32)
  ) u_cycle_counter (
      .clk  (clk),
      .rst_n(rst_n_int),
      .clear(tp_start),    // thread start: first thread cycle is cycle 0
      .count(cycle_count)
  );

  // TX_REQ comes from the host without a relation to our clock.
  // TX_DATA needs no synchronizer: the host keeps it stable while
  // TX_REQ is not equal to TX_ACK, and the sequencer reads it only then,
  // at least 2 cycles after TX_REQ changed.
  wire tx_req;
  pe_sync2 u_tx_req_sync (
      .clk  (clk),
      .rst_n(rst_n_int),
      .d    (ui_in[0]),
      .q    (tx_req)
  );

  // Event path from the sequencer to the timed output channel.
  wire        ev_load;
  wire [15:0] ev_time;
  wire        ev_value;
  wire        ev_ready;
  wire        tx_ack;

  // TEMPORARY: does the work of one M2 program thread (see the module).
  pe_uart_tx_seq #(
      .BIT_CYCLES(BIT_CYCLES),
      .LEAD      (EVENT_LEAD)
  ) u_uart_tx_seq (
      .clk     (clk),
      .rst_n   (rst_n_int),
      .now     (cycle_count[15:0]),
      .req     (tx_req),
      .data    (uio_in),
      .ack     (tx_ack),
      .ev_load (ev_load),
      .ev_time (ev_time),
      .ev_value(ev_value),
      .ev_ready(ev_ready)
  );

  // D2 timed output channel for the UART TX pin. Reset value 1 is the
  // UART idle level. The pin is a flip-flop output, so it has no glitches.
  wire tx_pin;
  wire tx_oe;
  pe_timed_out #(
      .RESET_VALUE(1'b1),
      .RESET_OE   (1'b1)
  ) u_tx_channel (
      .clk      (clk),
      .rst_n    (rst_n_int),
      .now      (cycle_count[15:0]),
      .ev_load  (ev_load),
      .ev_time  (ev_time),
      .ev_value (ev_value),
      .ev_oe    (1'b1),      // uo_out pins are always outputs
      .ev_ready (ev_ready),
      .pin_value(tx_pin),
      .pin_oe   (tx_oe)
  );

  // ---------------------------------------------------------------------
  // M2b processor and its TEMPORARY test port
  // ---------------------------------------------------------------------

  // CORE_SEL, TP_CLK and TP_DAT come from the test bench without a
  // relation to our clock: 2-stage synchronizers. TP_CLK and TP_DAT have
  // the same delay, so each bit arrives with its clock level change.
  wire core_sel;
  wire tp_clk;
  wire tp_dat;
  pe_sync2 u_core_sel_sync (
      .clk  (clk),
      .rst_n(rst_n_int),
      .d    (ui_in[7]),
      .q    (core_sel)
  );
  pe_sync2 u_tp_clk_sync (
      .clk  (clk),
      .rst_n(rst_n_int),
      .d    (ui_in[6]),
      .q    (tp_clk)
  );
  pe_sync2 u_tp_dat_sync (
      .clk  (clk),
      .rst_n(rst_n_int),
      .d    (ui_in[5]),
      .q    (tp_dat)
  );

  wire        tp_mem_we;
  wire        tp_cfg_we;
  wire        tp_push;
  wire        tp_pop;
  wire [6:0]  tp_arg;
  wire [15:0] tp_data;
  wire        tp_out;
  wire        h_out_valid;
  wire [15:0] h_out_data;

  // TEMPORARY: M2c replaces it with the host SPI port (A1 block D7).
  pe_test_port u_test_port (
      .clk           (clk),
      .rst_n         (rst_n_int),
      .active        (core_sel),
      .tp_clk        (tp_clk),
      .tp_dat        (tp_dat),
      .ro_bit        (tp_out),
      .mem_we        (tp_mem_we),
      .cfg_we        (tp_cfg_we),
      .start         (tp_start),
      .fifo_push     (tp_push),
      .fifo_pop      (tp_pop),
      .arg           (tp_arg),
      .data          (tp_data),
      .fifo_out_valid(h_out_valid),
      .fifo_out_data (h_out_data)
  );

  wire [27:0] start_pc;
  wire [3:0]  run_mask;
  wire [29:0] pin_owner;
  wire [14:0] pin_en;
  wire [14:0] pin_od;
  pe_config u_config (
      .clk      (clk),
      .rst_n    (rst_n_int),
      .we       (tp_cfg_we),
      .addr     (tp_arg),
      .data     (tp_data),
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
      .now        (cycle_count[15:0]),
      .start      (tp_start),
      .start_pc   (start_pc),
      .run_mask   (run_mask),
      .pin_owner  (pin_owner),
      .pin_en     (pin_en),
      .pin_od     (pin_od),
      .h_mem_we   (tp_mem_we),
      .h_mem_addr (tp_arg),
      .h_mem_data (tp_data),
      .h_tid      (tp_arg[1:0]),
      .h_push     (tp_push),
      .h_wdata    (tp_data),
      .h_pop      (tp_pop),
      .h_out_valid(h_out_valid),
      .h_out_data (h_out_data),
      .ui_in      (ui_in[4:0]),
      .uio_in     (uio_in),
      .pin_v      (pin_v),
      .pin_oe     (pin_oe)
  );

  // All output pins must be assigned. If not used, assign to 0.
  // M1 mode:        uo_out[7:6] = 0, [5] = TX_ACK, [4] = UART_TX, [3:0] = 0,
  //                 uio all inputs.
  // Processor mode: uo_out[7] = TP_OUT, [6:0] = V of pins 14 to 8; uio_out
  //                 and uio_oe = V and OE of pins 7 to 0. A uo_out pin has
  //                 no enable, so a released pin shows V = 1 (docs/isa.md 4.5).
  // core_sel is a flip-flop output, so the selection does not change in a cycle.
  assign uo_out  = core_sel ? {tp_out, pin_v[14:8]} : {2'b00, tx_ack, tx_pin, 4'b0000};
  assign uio_out = core_sel ? pin_v[7:0] : 8'h00;
  assign uio_oe  = core_sel ? pin_oe[7:0] : 8'h00;

  // Signals with no load:
  //   ena: always 1 (template).
  //   cycle_count[31:16]: docs/isa.md uses only the low 16 bits (now).
  //   tx_oe: uo_out pins have no output enable.
  //   pin_oe[14:8]: the same for the processor pins on uo_out.
  wire _unused = &{ena, cycle_count[31:16], tx_oe, pin_oe[14:8], 1'b0};

endmodule
