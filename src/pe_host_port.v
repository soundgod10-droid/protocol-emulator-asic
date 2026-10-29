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

`ifdef FORMAL
  // ---------------------------------------------------------------------
  // Formal properties (formal/host_port.sby, formal/host_port_cover.sby).
  // Only Yosys with the FORMAL macro reads this block; synthesis, the
  // linter and the simulations do not.
  //
  // A monitor (m_*) decodes the SPI pins after the synchronizers as
  // docs/host_port.md tells: bytes MSB first at the rising SCK edge, CS_N
  // high ends the transaction, byte 0 is the command, byte 1 the address
  // or thread, then words (high byte first) at address A, A + 1, ...
  // (wrap 127 to 0). STOP and START change RUNNING (START only when
  // stopped). The monitor does not use the port registers. The ID bytes
  // below are the values of docs/host_port.md section 4, not CHIP_ID.
  //
  // H1 to H7 are the properties. J1 to J8 are invariants: they connect
  // the port registers to the monitor, so that k-induction can prove H1
  // to H7 for all time and all SPI pin sequences (the pins are free in
  // each clock cycle, also faster than the timing rules allow).
  // ---------------------------------------------------------------------
  initial assume (!rst_n);  // the check starts in reset; later resets are free

  reg        m_sck_q;   // SCK in the cycle before
  reg        m_cs_q;    // cs in the cycle before
  reg [2:0]  m_bit;     // bits of this byte received so far
  reg [6:0]  m_sr;      // these bits
  reg [2:0]  m_nb;      // complete bytes in this transaction (stops at 7)
  reg [7:0]  m_cmd;     // byte 0 of this transaction
  reg [6:0]  m_addr;    // address of the next word
  reg [1:0]  m_tid;     // thread of PUSH and PULL
  reg        m_lo;      // 1 = the next data byte is the low byte of a word
  reg [7:0]  m_hi;      // high byte of the word
  reg        m_run;     // RUNNING from the STOP and START commands

  wire        m_rise  = cs && sck && !m_sck_q;
  wire        m_done  = m_rise && (m_bit == 3'd7);
  wire [7:0]  m_byte  = {m_sr, mosi};
  wire [15:0] m_word  = {m_hi, m_byte};
  wire        m_wword = m_done && (m_nb >= 3'd2) && m_lo;  // last bit of a word

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      m_sck_q <= 1'b0;
      m_cs_q  <= 1'b0;
      m_bit   <= 3'd0;
      m_sr    <= 7'd0;
      m_nb    <= 3'd0;
      m_cmd   <= 8'd0;
      m_addr  <= 7'd0;
      m_tid   <= 2'd0;
      m_lo    <= 1'b0;
      m_hi    <= 8'd0;
      m_run   <= 1'b0;
    end else begin
      m_sck_q <= sck;
      m_cs_q  <= cs;
      if (!cs) begin
        m_bit <= 3'd0;
        m_nb  <= 3'd0;
        m_lo  <= 1'b0;
      end else if (m_rise) begin
        m_sr  <= m_byte[6:0];
        m_bit <= m_bit + 3'd1;
        if (m_done) begin
          if (m_nb != 3'd7) m_nb <= m_nb + 3'd1;
          case (m_nb)
            3'd0: begin
              m_cmd <= m_byte;
              if (m_byte == 8'h3C) m_run <= 1'b0;                 // STOP
              else if ((m_byte == 8'h4B) && !m_run) m_run <= 1'b1; // START
            end
            3'd1: begin
              m_addr <= m_byte[6:0];
              m_tid  <= m_byte[1:0];
              m_lo   <= 1'b0;
            end
            default: begin
              if (!m_lo) m_hi <= m_byte;
              else m_addr <= m_addr + 7'd1;
              m_lo <= !m_lo;
            end
          endcase
        end
      end
    end
  end

  // ID byte of response byte m_nb (docs/host_port.md section 4).
  reg [7:0] m_idb;
  always @* begin
    case (m_nb)
      3'd1:    m_idb = 8'h50;
      3'd2:    m_idb = 8'h45;
      3'd3:    m_idb = 8'h02;
      3'd4:    m_idb = 8'h03;
      default: m_idb = 8'h00;
    endcase
  end

  // Expected port state from the monitor (for the invariant J4).
  wire      m_ok  = (m_cmd[3:0] == ~m_cmd[7:4]);
  wire [3:0] m_c  = m_cmd[7:4];
  wire      m_wr  = m_ok && (m_nb >= 3'd2) &&
                    ((((m_c == K_WRITE_MEM) || (m_c == K_WRITE_CFG)) && !m_run) ||
                     ((m_c == K_PUSH) && m_run));
  reg [3:0] m_st;
  always @* begin
    if (m_nb == 3'd0) m_st = S_CMD;
    else if (!m_ok) m_st = S_IGN;
    else begin
      case (m_c)
        K_READ_ID, K_READ_STATUS: m_st = S_RESP;
        K_WRITE_MEM, K_WRITE_CFG:
          m_st = m_run ? S_IGN : (m_nb == 3'd1) ? S_ARG : (m_lo ? S_WLO : S_WHI);
        K_READ_MEM:
          m_st = m_run ? S_IGN : (m_nb == 3'd1) ? S_ARG : (m_lo ? S_RDB : S_RDA);
        K_PUSH:
          m_st = !m_run ? S_IGN : (m_nb == 3'd1) ? S_ARG : (m_lo ? S_WLO : S_WHI);
        K_PULL:
          m_st = !m_run ? S_IGN : (m_nb == 3'd1) ? S_ARG : (m_lo ? S_PLB : S_PLA);
        default: m_st = S_IGN;
      endcase
    end
  end

  always @* begin
    // H1: The memory is written only by the last bit of a word of a
    //     WRITE_MEM (0x5A) while the threads stop, and each such word is
    //     written, at address A + n (wrap 127 to 0) with its two bytes.
    assert (mem_we == (m_wword && (m_cmd == 8'h5A) && !m_run));
    if (mem_we) assert (mem_addr == m_addr);
    if (mem_we) assert (mem_wdata == m_word);
    // H2: The same for the configuration (WRITE_CFG 0x78).
    assert (cfg_we == (m_wword && (m_cmd == 8'h78) && !m_run));
    if (cfg_we) assert (cfg_addr == m_addr);
    if (cfg_we) assert (cfg_wdata == m_word);
    // H3: A word goes into a host FIFO only from a PUSH (0x87) while the
    //     threads run, into the FIFO of the thread in byte 1.
    assert (fifo_push == (m_wword && (m_cmd == 8'h87) && m_run));
    if (fifo_push) assert (fifo_tid == m_tid);
    if (fifo_push) assert (fifo_wdata == m_word);
    // H4: RUNNING follows STOP and START (START only while stopped).
    assert (running == m_run);
    // H5: No write into the memory or the configuration while the
    //     threads run.
    assert (!(mem_we && running));
    assert (!(cfg_we && running));
    // H6: READ_ID gives the constant ID 0x50 0x45 0x02 0x03, then 0x00:
    //     at each rising SCK edge of response byte k, MISO is bit 7 - b of
    //     ID byte k (b = bits of the byte before this edge).
    if (m_rise && (m_nb != 3'd0) && (m_cmd == 8'h1E)) assert (miso == m_idb[3'd7 - m_bit]);
    // H7: MISO (all of its shift register) is 0 in the cycle after a
    //     cycle with CS_N high.
    if (!m_cs_q) assert (tx == 8'd0);

    // J1 to J3: the bit counter and the receive register agree.
    assert (sck_q == m_sck_q);
    assert (nbit == m_bit);
    assert (sr == m_sr);
    // J4: the transaction state agrees with the monitor.
    assert (st == m_st);
    // J5: the command register holds byte 0.
    if (m_nb != 3'd0) assert (cmd == m_c);
    // J6: the response index is the number of complete bytes.
    if (st == S_RESP) assert (idx == m_nb);
    // J7: address, high byte and thread of a write.
    if (m_wr) assert (addr == m_addr);
    if (m_wr && m_lo) assert (hold == m_hi);
    if (m_ok && (m_nb >= 3'd2) && m_run && ((m_c == K_PUSH) || (m_c == K_PULL))) assert (tid == m_tid);
    // J8: in READ_ID the shift register holds the rest of the ID byte.
    if ((m_nb != 3'd0) && (m_cmd == 8'h1E)) assert (tx == (m_idb << m_bit));
  end

  // Covers (formal/host_port_cover.sby defines the macro): each property
  // has a reachable case, so the proof is not empty.
`ifdef PE_HOST_PORT_COVER
  always @* begin
    cover (mem_we);                                               // a word written
    cover (cfg_we);                                               // a configuration word
    cover (fifo_push);                                            // START, then PUSH
    cover (m_rise && (m_cmd == 8'h1E) && (m_nb == 3'd4) && (m_bit == 3'd7));  // last ID bit
  end
`endif
`endif

endmodule
