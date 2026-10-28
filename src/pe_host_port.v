/*
 * Copyright (c) 2026 Steven Anthony
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// pe_host_port: host SPI port of the chip (A1 block D7).
//
// What it does:
//   The host (the RP2350 of the demo board) uses this SPI target to stop
//   and start the threads, to write and read the program memory, to
//   write the configuration, to read the chip ID and the status, and to
//   use the host FIFOs of the threads (PUSH, PULL). docs/host_port.md
//   gives the protocol at byte level.
//   SPI mode 0: SCK is low when idle, the host changes MOSI after the
//   falling SCK edge, and both sides sample at the rising SCK edge. Bytes
//   are MSB first. CS_N low starts a transaction. The first byte is the
//   command. CS_N high ends the transaction at any time.
//   SCK, CS_N and MOSI come through 2-stage synchronizers in the top
//   level, all with the same delay. The port samples MOSI in the clock
//   cycle in which it sees the rising SCK edge. MISO is a flip-flop
//   output. It changes at the end of that cycle to the next bit, so the
//   next bit is on MISO 3 clock cycles or less after the rising SCK edge
//   at the pin, and the host has nearly a full SCK period to sample it.
//
// Why:
//   A1 section 2.2 and block D7: the RP2350 loads the firmware through 4
//   pins (HOST_SCK ui_in[7], HOST_CS_N ui_in[6], HOST_MOSI ui_in[5],
//   HOST_MISO uo_out[7]). The port samples the SPI pins with the chip
//   clock, so the chip has one clock domain and STA checks all paths.
//   The memory write port, the configuration write port and the FIFO
//   ports are those that the TEMPORARY M2b test port used.
//   Each command code is {c, not c} (high nibble c, low nibble its
//   complement). Thus two codes differ in 2 bits or more, a single wrong
//   bit gives no command, and a stuck MOSI line (0x00 or 0xFF) gives no
//   command (design note M2c_loader.md, section 1).
//
// Rules (docs/host_port.md):
//   - WRITE_MEM, READ_MEM and WRITE_CFG only while the threads stop.
//     PUSH and PULL only while they run. START only while they stop.
//     A command that breaks a rule does nothing and sets CMD_ERR.
//   - A PUSH word for a full FIFO is dropped and sets PUSH_DROP. A host
//     that reads the free space (status) first never loses a word.
//   - STOP holds the processor (threads, FIFOs, event slots, output pins)
//     in reset. The memory and the configuration do not change. START
//     releases it: the threads then start as after a chip reset.

module pe_host_port #(
    parameter [31:0] CHIP_ID = 32'h5045_0203  // "PE", milestone 2, run c
) (
    input  wire        clk,
    input  wire        rst_n,          // chip reset, active low, asynchronous assert
    // SPI pins after the synchronizers
    input  wire        sck,            // HOST_SCK
    input  wire        cs,             // not HOST_CS_N: 1 = transaction
    input  wire        mosi,           // HOST_MOSI
    output wire        miso,           // HOST_MISO (flip-flop output)
    // Threads
    output reg         running,        // 0 = processor held in reset
    output wire        start,          // 1 in the first cycle after the release
    // Program memory (pe_latch_ram), only while running = 0
    output wire        mem_we,
    output wire [6:0]  mem_addr,       // write address, and read address while running = 0
    output wire [15:0] mem_wdata,
    input  wire [15:0] mem_rdata,      // word at mem_addr
    // Configuration (pe_config)
    output wire        cfg_we,
    output wire [6:0]  cfg_addr,
    output wire [15:0] cfg_wdata,
    // Host FIFOs (pe_host_fifo)
    output wire [1:0]  fifo_tid,
    output wire        fifo_push,
    output wire [15:0] fifo_wdata,
    input  wire        fifo_in_full,   // in-FIFO of fifo_tid has 4 words
    output wire        fifo_pop,
    input  wire [15:0] fifo_out_data,  // head word of the out-FIFO of fifo_tid
    // Status
    input  wire [11:0] cnt_in,         // words in the in-FIFO of thread n at [3n+2:3n]
    input  wire [11:0] cnt_out,        // words in the out-FIFO of thread n
    input  wire [3:0]  th_run,
    input  wire [3:0]  th_late,
    input  wire [3:0]  th_err
);

  // Command codes {c, not c} (high nibble c).
  localparam [3:0] K_READ_ID     = 4'h1;
  localparam [3:0] K_READ_STATUS = 4'h2;
  localparam [3:0] K_STOP        = 4'h3;
  localparam [3:0] K_START       = 4'h4;
  localparam [3:0] K_WRITE_MEM   = 4'h5;
  localparam [3:0] K_READ_MEM    = 4'h6;
  localparam [3:0] K_WRITE_CFG   = 4'h7;
  localparam [3:0] K_PUSH        = 4'h8;
  localparam [3:0] K_PULL        = 4'h9;
  localparam [3:0] K_CLEAR       = 4'hA;

  // Transaction state: what the byte that is now on the line means.
  localparam [3:0] S_CMD  = 4'd0;  // command byte
  localparam [3:0] S_RESP = 4'd1;  // READ_ID / READ_STATUS: response byte idx - 1 on MISO
  localparam [3:0] S_ARG  = 4'd2;  // address (WRITE_MEM, READ_MEM, WRITE_CFG) or thread (PUSH, PULL)
  localparam [3:0] S_WHI  = 4'd3;  // high byte of a word that the host writes
  localparam [3:0] S_WLO  = 4'd4;  // low byte of a word that the host writes
  localparam [3:0] S_RDA  = 4'd5;  // READ_MEM: high byte on MISO
  localparam [3:0] S_RDB  = 4'd6;  // READ_MEM: low byte on MISO
  localparam [3:0] S_PLA  = 4'd7;  // PULL: count byte or low byte on MISO
  localparam [3:0] S_PLB  = 4'd8;  // PULL: high byte on MISO
  localparam [3:0] S_IGN  = 4'd9;  // the rest of the transaction has no effect

  reg        sck_q;      // SCK in the cycle before
  reg [2:0]  nbit;       // bits of the byte received so far
  reg [6:0]  sr;         // the first 7 bits of the byte
  reg [7:0]  tx;         // MISO shift register, tx[7] is on MISO
  reg [3:0]  st;
  reg [3:0]  cmd;        // command (high nibble)
  reg [6:0]  addr;       // memory or configuration address of the next word
  reg [1:0]  tid;        // thread of PUSH and PULL
  reg [7:0]  hold;       // high byte of a written word, or low byte of a read word
  reg [2:0]  idx;        // index of the next response byte (READ_ID, READ_STATUS)
  reg [2:0]  remain;     // PULL: words that are still valid
  reg        running_q;
  reg        err_cmd;    // sticky: a command broke a rule or was not valid
  reg        err_drop;   // sticky: a PUSH word was dropped

  wire       rise    = cs && sck && !sck_q;     // rising SCK edge in a transaction
  wire       done    = rise && (nbit == 3'd7);  // the last bit of a byte
  wire [7:0] bin     = {sr, mosi};              // the byte, valid when done = 1
  wire       code_ok = (bin[3:0] == ~bin[7:4]);

  // Response bytes. sidx is the index of the byte to load now.
  wire [2:0] sidx = (st == S_CMD) ? 3'd0 : idx;
  reg  [7:0] id_b;
  reg  [7:0] status_b;
  always @* begin
    case (sidx[1:0])
      2'd0:    id_b = CHIP_ID[31:24];
      2'd1:    id_b = CHIP_ID[23:16];
      2'd2:    id_b = CHIP_ID[15:8];
      default: id_b = CHIP_ID[7:0];
    endcase
    if (sidx[2]) id_b = 8'h00;
    case (sidx)
      3'd0:    status_b = {5'd0, err_drop, err_cmd, running};
      3'd1:    status_b = {4'd0, th_run};
      3'd2:    status_b = {th_err, th_late};
      3'd3:    status_b = {1'b0, cnt_out[2:0], 1'b0, cnt_in[2:0]};
      3'd4:    status_b = {1'b0, cnt_out[5:3], 1'b0, cnt_in[5:3]};
      3'd5:    status_b = {1'b0, cnt_out[8:6], 1'b0, cnt_in[8:6]};
      3'd6:    status_b = {1'b0, cnt_out[11:9], 1'b0, cnt_in[11:9]};
      default: status_b = 8'h00;
    endcase
  end

  // PULL: word count of the out-FIFO of the thread in the argument byte.
  reg [2:0] cnt_sel;
  always @* begin
    case (bin[1:0])
      2'd0:    cnt_sel = cnt_out[2:0];
      2'd1:    cnt_sel = cnt_out[5:3];
      2'd2:    cnt_sel = cnt_out[8:6];
      default: cnt_sel = cnt_out[11:9];
    endcase
  end

  // Write ports: the word completes with the last bit of its low byte.
  wire w_word = done && (st == S_WLO);
  assign mem_we     = w_word && (cmd == K_WRITE_MEM) && !running;
  assign cfg_we     = w_word && (cmd == K_WRITE_CFG) && !running;
  assign fifo_push  = w_word && (cmd == K_PUSH) && running;
  assign mem_addr   = (st == S_ARG) ? bin[6:0] : addr;
  assign mem_wdata  = {hold, bin};
  assign cfg_addr   = addr;
  assign cfg_wdata  = {hold, bin};
  assign fifo_wdata = {hold, bin};
  assign fifo_tid   = tid;
  assign fifo_pop   = done && (st == S_PLA) && (remain != 3'd0);

  assign miso  = tx[7];
  assign start = running && !running_q;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      sck_q     <= 1'b0;
      nbit      <= 3'd0;
      sr        <= 7'd0;
      tx        <= 8'd0;
      st        <= S_CMD;
      cmd       <= 4'd0;
      addr      <= 7'd0;
      tid       <= 2'd0;
      hold      <= 8'd0;
      idx       <= 3'd0;
      remain    <= 3'd0;
      running   <= 1'b0;
      running_q <= 1'b0;
      err_cmd   <= 1'b0;
      err_drop  <= 1'b0;
    end else begin
      sck_q     <= sck;
      running_q <= running;
      if (!cs) begin
        // No transaction: the next byte is a command, MISO is 0.
        nbit <= 3'd0;
        st   <= S_CMD;
        tx   <= 8'd0;
      end else if (rise) begin
        sr   <= bin[6:0];
        nbit <= nbit + 3'd1;
        if (!done) begin
          tx <= {tx[6:0], 1'b0};
        end else begin
          tx <= 8'd0;
          case (st)
            S_CMD: begin
              cmd <= bin[7:4];
              st  <= S_IGN;
              if (!code_ok) begin
                err_cmd <= 1'b1;
              end else begin
                case (bin[7:4])
                  K_READ_ID: begin
                    tx  <= id_b;
                    idx <= 3'd1;
                    st  <= S_RESP;
                  end
                  K_READ_STATUS: begin
                    tx  <= status_b;
                    idx <= 3'd1;
                    st  <= S_RESP;
                  end
                  K_STOP:  running <= 1'b0;
                  K_START: begin
                    if (running) err_cmd <= 1'b1;
                    else running <= 1'b1;
                  end
                  K_WRITE_MEM, K_READ_MEM, K_WRITE_CFG: begin
                    if (running) err_cmd <= 1'b1;
                    else st <= S_ARG;
                  end
                  K_PUSH, K_PULL: begin
                    if (!running) err_cmd <= 1'b1;
                    else st <= S_ARG;
                  end
                  K_CLEAR: begin
                    err_cmd  <= 1'b0;
                    err_drop <= 1'b0;
                  end
                  default: err_cmd <= 1'b1;
                endcase
              end
            end
            S_RESP: begin
              tx <= (cmd == K_READ_ID) ? id_b : status_b;
              if (idx != 3'd7) idx <= idx + 3'd1;
            end
            S_ARG: begin
              addr <= bin[6:0];
              tid  <= bin[1:0];
              if (cmd == K_READ_MEM) begin
                tx   <= mem_rdata[15:8];
                hold <= mem_rdata[7:0];
                addr <= bin[6:0] + 7'd1;
                st   <= S_RDA;
              end else if (cmd == K_PULL) begin
                tx     <= {5'd0, cnt_sel};
                remain <= cnt_sel;
                st     <= S_PLA;
              end else begin
                st <= S_WHI;
              end
            end
            S_WHI: begin
              hold <= bin;
              st   <= S_WLO;
            end
            S_WLO: begin
              addr <= addr + 7'd1;
              st   <= S_WHI;
              if ((cmd == K_PUSH) && fifo_in_full) err_drop <= 1'b1;
            end
            S_RDA: begin
              tx <= hold;
              st <= S_RDB;
            end
            S_RDB: begin
              tx   <= mem_rdata[15:8];
              hold <= mem_rdata[7:0];
              addr <= addr + 7'd1;
              st   <= S_RDA;
            end
            S_PLA: begin
              if (remain != 3'd0) begin
                tx     <= fifo_out_data[15:8];
                hold   <= fifo_out_data[7:0];
                remain <= remain - 3'd1;
              end else begin
                hold <= 8'd0;
              end
              st <= S_PLB;
            end
            S_PLB: begin
              tx <= hold;
              st <= S_PLA;
            end
            default: ;  // S_IGN
          endcase
        end
      end
    end
  end

endmodule
