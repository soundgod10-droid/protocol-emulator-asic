/*
 * Copyright (c) 2026 Steven Anthony
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// g1_meas: area measurements of run G1 (design note G1_ground_register.md
// section 4). NOT part of the chip: the top level does not use these
// modules, and info.yaml lists this file only during the measurement.
//
// What it does:
//   Each module is the storage part (registers, counters, shift registers)
//   of an A1 block that is not built yet, or a hardware alternative to a
//   choice of DECISIONS.md D4 or of M2b section 1. chiptools__synth gives
//   the cell area of each module.
//
// Why:
//   DECISIONS.md rule G: an area estimate is not a ground. A1 section 7
//   counted flip-flops by hand and used 10 um2 for each other cell. The
//   synthesis of the largest part of a block gives a measured value before
//   the block is designed (M3). The synthesis of an alternative gives the
//   comparison that rule G asks for.

// ---------------------------------------------------------------------------
// Memory size (register line M2a-03): a 96 x 16 latch memory, the same
// module as the 128 x 16 program memory (pe_latch_ram).
// ---------------------------------------------------------------------------
module g1_ram96 (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        we,
    input  wire [6:0]  waddr,
    input  wire [15:0] wdata,
    input  wire [6:0]  raddr,
    output wire [15:0] rdata
);
  pe_latch_ram #(
      .WORDS(96),
      .AW   (7),
      .WIDTH(16)
  ) u_ram (
      .clk  (clk),
      .rst_n(rst_n),
      .we   (we),
      .waddr(waddr),
      .wdata(wdata),
      .raddr(raddr),
      .rdata(rdata)
  );
endmodule

// ---------------------------------------------------------------------------
// Host FIFO words (register line C-08): the two 16 x 16 word stores of
// pe_host_fifo in latches (as built) and in flip-flops with an enable.
// ---------------------------------------------------------------------------
module g1_mem_flop #(
    parameter integer WORDS = 16,
    parameter integer AW    = 4,
    parameter integer WIDTH = 16
) (
    input  wire             clk,
    input  wire             rst_n,
    input  wire             we,
    input  wire [AW-1:0]    waddr,
    input  wire [WIDTH-1:0] wdata,
    input  wire [AW-1:0]    raddr,
    output wire [WIDTH-1:0] rdata
);
  localparam integer WB = $clog2(WIDTH);
  reg [WORDS*WIDTH-1:0] q;
  genvar w;
  generate
    for (w = 0; w < WORDS; w = w + 1) begin : g_word
      localparam [AW-1:0] ADDR = w;
      always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
          q[w*WIDTH+:WIDTH] <= {WIDTH{1'b0}};
        end else if (we && (waddr == ADDR)) begin
          q[w*WIDTH+:WIDTH] <= wdata;
        end
      end
    end
  endgenerate
  wire [AW+WB-1:0] rbase = {raddr, {WB{1'b0}}};
  assign rdata = q[rbase+:WIDTH];
endmodule

module g1_fifo_store_flop (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        we_a,
    input  wire [3:0]  waddr_a,
    input  wire [15:0] wdata_a,
    input  wire [3:0]  raddr_a,
    output wire [15:0] rdata_a,
    input  wire        we_b,
    input  wire [3:0]  waddr_b,
    input  wire [15:0] wdata_b,
    input  wire [3:0]  raddr_b,
    output wire [15:0] rdata_b
);
  g1_mem_flop u_a (
      .clk  (clk),
      .rst_n(rst_n),
      .we   (we_a),
      .waddr(waddr_a),
      .wdata(wdata_a),
      .raddr(raddr_a),
      .rdata(rdata_a)
  );
  g1_mem_flop u_b (
      .clk  (clk),
      .rst_n(rst_n),
      .we   (we_b),
      .waddr(waddr_b),
      .wdata(wdata_b),
      .raddr(raddr_b),
      .rdata(rdata_b)
  );
endmodule

module g1_fifo_store_latch (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        we_a,
    input  wire [3:0]  waddr_a,
    input  wire [15:0] wdata_a,
    input  wire [3:0]  raddr_a,
    output wire [15:0] rdata_a,
    input  wire        we_b,
    input  wire [3:0]  waddr_b,
    input  wire [15:0] wdata_b,
    input  wire [3:0]  raddr_b,
    output wire [15:0] rdata_b
);
  pe_latch_ram #(
      .WORDS(16),
      .AW   (4),
      .WIDTH(16)
  ) u_a (
      .clk  (clk),
      .rst_n(rst_n),
      .we   (we_a),
      .waddr(waddr_a),
      .wdata(wdata_a),
      .raddr(raddr_a),
      .rdata(rdata_a)
  );
  pe_latch_ram #(
      .WORDS(16),
      .AW   (4),
      .WIDTH(16)
  ) u_b (
      .clk  (clk),
      .rst_n(rst_n),
      .we   (we_b),
      .waddr(waddr_b),
      .wdata(wdata_b),
      .raddr(raddr_b),
      .rdata(rdata_b)
  );
endmodule

// ---------------------------------------------------------------------------
// Timed input (register line C-05): the A1 block D3 plan, a 16-bit sample
// time, a 16-bit capture register and a pin select for each thread. The
// capture takes the cycle count (now) at the sample time or at an edge of the selected
// pin. M2b uses a shared 5-cycle input history instead.
// ---------------------------------------------------------------------------
module g1_capture (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [1:0]  tid,    // slot thread
    input  wire        arm,    // the slot thread sets its sample time and pin
    input  wire [15:0] t,
    input  wire [3:0]  pin,
    input  wire [15:0] now,
    input  wire [12:0] s,      // synchronized inputs
    output wire [15:0] cap,    // capture register of the slot thread
    output wire        hit     // the capture of the slot thread is valid
);
  reg [63:0] stime;
  reg [63:0] capr;
  reg [15:0] psel;
  reg [3:0]  valid;
  reg [15:0] s_prev;
  wire [15:0] sx = {3'b000, s};
  wire [15:0] edges = sx ^ s_prev;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) s_prev <= 16'd0;
    else s_prev <= sx;
  end

  genvar n;
  generate
    for (n = 0; n < 4; n = n + 1) begin : g_thr
      localparam [1:0] N = n;
      wire [3:0] p = psel[4*n+:4];
      always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
          stime[16*n+:16] <= 16'd0;
          capr[16*n+:16]  <= 16'd0;
          psel[4*n+:4]    <= 4'd0;
          valid[n]        <= 1'b0;
        end else if (arm && (tid == N)) begin
          stime[16*n+:16] <= t;
          psel[4*n+:4]    <= pin;
          valid[n]        <= 1'b0;
        end else if (!valid[n] && ((now == stime[16*n+:16]) || edges[p])) begin
          capr[16*n+:16] <= now;
          valid[n]       <= 1'b1;
        end
      end
    end
  endgenerate

  assign cap = capr[16*tid+:16];
  assign hit = valid[tid];
endmodule

// ---------------------------------------------------------------------------
// Data registers (D4 item 1): the thread-state ring of pe_core with 4 and
// with 8 data registers of 16 bits for each thread (the same method as
// pe_rf_ring in src/pe_rf_alt.v).
// ---------------------------------------------------------------------------
module g1_rf_ring #(
    parameter integer NREG = 4,
    parameter integer SW   = 2
) (
    input  wire          clk,
    input  wire          rst_n,
    input  wire [SW-1:0] sa,
    input  wire [SW-1:0] sb,
    input  wire          we,
    input  wire [SW-1:0] wsel,
    input  wire [15:0]   wval,
    output wire [15:0]   ra,
    output wire [15:0]   rb
);
  localparam integer B = 16 * NREG;
  reg  [B-1:0] st0, st1, st2, st3;
  wire [B-1:0] nc;
  genvar i;
  generate
    for (i = 0; i < NREG; i = i + 1) begin : g_w
      localparam [SW-1:0] R = i;
      assign nc[16*i+:16] = (we && (wsel == R)) ? wval : st0[16*i+:16];
    end
  endgenerate
  assign ra = st0[16*sa+:16];
  assign rb = st0[16*sb+:16];
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      st0 <= {B{1'b0}};
      st1 <= {B{1'b0}};
      st2 <= {B{1'b0}};
      st3 <= {B{1'b0}};
    end else begin
      st3 <= nc;
      st2 <= st3;
      st1 <= st2;
      st0 <= st1;
    end
  end
endmodule

module g1_rf_ring4 (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [1:0]  sa,
    input  wire [1:0]  sb,
    input  wire        we,
    input  wire [1:0]  wsel,
    input  wire [15:0] wval,
    output wire [15:0] ra,
    output wire [15:0] rb
);
  g1_rf_ring #(
      .NREG(4),
      .SW  (2)
  ) u_rf (
      .clk  (clk),
      .rst_n(rst_n),
      .sa   (sa),
      .sb   (sb),
      .we   (we),
      .wsel (wsel),
      .wval (wval),
      .ra   (ra),
      .rb   (rb)
  );
endmodule

module g1_rf_ring8 (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [2:0]  sa,
    input  wire [2:0]  sb,
    input  wire        we,
    input  wire [2:0]  wsel,
    input  wire [15:0] wval,
    output wire [15:0] ra,
    output wire [15:0] rb
);
  g1_rf_ring #(
      .NREG(8),
      .SW  (3)
  ) u_rf (
      .clk  (clk),
      .rst_n(rst_n),
      .sa   (sa),
      .sb   (sb),
      .we   (we),
      .wsel (wsel),
      .wval (wval),
      .ra   (ra),
      .rb   (rb)
  );
endmodule

// ---------------------------------------------------------------------------
// Second memory read port (D4 item 7): one more 128:1 x 16 multiplexer on
// the latch outputs of the 128 x 16 memory.
// ---------------------------------------------------------------------------
module g1_rdport2 (
    input  wire [2047:0] q,
    input  wire [6:0]    raddr,
    output wire [15:0]   rdata
);
  wire [10:0] rbase = {raddr, 4'b0000};
  assign rdata = q[rbase+:16];
endmodule

// ---------------------------------------------------------------------------
// Late-event rule (D4 item 3): the event time is T + offset if the lead is
// 4 to 32,767 cycles, else now + 4 with LATE. The alternative (M1 rule) uses
// the time as it is and has no logic: a late event then waits a full
// counter turn (65,536 cycles).
// ---------------------------------------------------------------------------
module g1_late (
    input  wire [15:0] t,
    input  wire [15:0] now,
    output wire [15:0] at,
    output wire        late
);
  wire [15:0] d = t - now;
  assign late = (d < 16'd4) || d[15];
  assign at   = late ? (now + 16'd4) : t;
endmodule

// ---------------------------------------------------------------------------
// Pin owner (D4 item 5): owner thread and owner enable of the 15 output
// pins, and the check of the pin of an OUT. The alternative has no owner
// (any thread can drive any pin) and no logic.
// ---------------------------------------------------------------------------
module g1_owner (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       we,
    input  wire [3:0] wpin,
    input  wire [2:0] wdata,   // [1:0] owner thread, [2] owner enable
    input  wire [1:0] tid,
    input  wire [3:0] pin,
    output wire       owned
);
  reg [29:0] own;
  reg [14:0] en;
  genvar p;
  generate
    for (p = 0; p < 15; p = p + 1) begin : g_pin
      localparam [3:0] P = p;
      always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
          own[2*p+:2] <= 2'd0;
          en[p]       <= 1'b0;
        end else if (we && (wpin == P)) begin
          own[2*p+:2] <= wdata[1:0];
          en[p]       <= wdata[2];
        end
      end
    end
  endgenerate
  wire [31:0] own_x = {2'b00, own};
  wire [15:0] en_x = {1'b0, en};
  assign owned = en_x[pin] && (own_x[2*pin+:2] == tid);
endmodule

// ---------------------------------------------------------------------------
// Input spike filter (D4 item 8): an output changes only after K equal
// input samples in a row that differ from it. UM10204 Rev. 7.0 Table 10
// note [8] (page 44): spikes of less than 50 ns are suppressed. At 50 MHz
// 50 ns is 2.5 cycles, so K = 3.
// ---------------------------------------------------------------------------
module g1_spike_filter #(
    parameter integer N = 13,
    parameter integer K = 3
) (
    input  wire         clk,
    input  wire         rst_n,
    input  wire [N-1:0] d,
    output wire [N-1:0] q
);
  reg [N-1:0] qr;
  genvar i;
  generate
    for (i = 0; i < N; i = i + 1) begin : g_in
      reg [1:0] cnt;
      always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
          qr[i] <= 1'b1;
          cnt   <= 2'd0;
        end else if (d[i] == qr[i]) begin
          cnt <= 2'd0;
        end else if (cnt == K - 1) begin
          qr[i] <= d[i];
          cnt   <= 2'd0;
        end else begin
          cnt <= cnt + 2'd1;
        end
      end
    end
  endgenerate
  assign q = qr;
endmodule

module g1_spike13 (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [12:0] d,
    output wire [12:0] q
);
  g1_spike_filter #(
      .N(13),
      .K(3)
  ) u_f (
      .clk  (clk),
      .rst_n(rst_n),
      .d    (d),
      .q    (q)
  );
endmodule

module g1_spike2 (
    input  wire       clk,
    input  wire       rst_n,
    input  wire [1:0] d,
    output wire [1:0] q
);
  g1_spike_filter #(
      .N(2),
      .K(3)
  ) u_f (
      .clk  (clk),
      .rst_n(rst_n),
      .d    (d),
      .q    (q)
  );
endmodule

// ---------------------------------------------------------------------------
// Store-and-read rule (D4 item 12): the alternative is a bypass that gives
// the data of the last 3 stores to a read of the same address, so that a
// read 1 to 3 cycles after a store is defined.
// ---------------------------------------------------------------------------
module g1_bypass (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        we,
    input  wire [6:0]  waddr,
    input  wire [15:0] wdata,
    input  wire [6:0]  raddr,
    input  wire [15:0] mem_rdata,
    output wire [15:0] rdata
);
  reg [2:0]  v;
  reg [20:0] a;
  reg [47:0] dd;
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      v  <= 3'd0;
      a  <= 21'd0;
      dd <= 48'd0;
    end else begin
      v  <= {v[1:0], we};
      a  <= {a[13:0], waddr};
      dd <= {dd[31:0], wdata};
    end
  end
  wire h0 = v[0] && (a[6:0] == raddr);
  wire h1 = v[1] && (a[13:7] == raddr);
  wire h2 = v[2] && (a[20:14] == raddr);
  assign rdata = h0 ? dd[15:0] : h1 ? dd[31:16] : h2 ? dd[47:32] : mem_rdata;
endmodule

// ---------------------------------------------------------------------------
// Expired time (D4 item 9): the alternative to TSYNC. A sticky bit for each
// thread (in the ring order of pe_core) is set when T is 16,384 to 32,767
// cycles behind now, before the 16-bit difference turns into a time in the
// future. Each thread has a slot every 4 cycles, so the check cannot miss it.
// ---------------------------------------------------------------------------
module g1_expired (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [15:0] t,      // T of the slot thread
    input  wire [15:0] now,
    input  wire        t_set,  // the slot thread writes T
    output wire        expired
);
  reg [3:0] e;  // e[0]: slot thread
  wire [15:0] behind = now - t;
  wire e_new = !t_set && (e[0] || (behind[15:14] == 2'b01));
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) e <= 4'd0;
    else e <= {e_new, e[3:1]};
  end
  assign expired = e[0];
endmodule

// ---------------------------------------------------------------------------
// A1 block D4 (line-code helpers, 4): the storage of A1 section 7 for each
// helper (shift out 16, shift in 16, bit count 5, stuff count 3, state 4)
// with the shift, count and NRZI functions.
// ---------------------------------------------------------------------------
module g1_helper_store (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [1:0]  tid,
    input  wire        load,       // the slot thread loads its helper
    input  wire [15:0] wdata,
    input  wire [3:0]  shift_out,  // one bit out, for each helper
    input  wire [3:0]  shift_in,   // one bit in, for each helper
    input  wire [3:0]  din,
    output wire [15:0] rdata,      // shift-in register of the slot thread
    output wire [4:0]  bcount,     // bit count of the slot thread
    output wire [3:0]  state,      // state of the slot thread
    output wire [3:0]  dout,
    output wire [3:0]  stuff       // six 1 bits sent: insert a 0
);
  reg [63:0] so, si;
  reg [19:0] bc;
  reg [11:0] sc;
  reg [15:0] st;
  genvar h;
  generate
    for (h = 0; h < 4; h = h + 1) begin : g_h
      localparam [1:0] H = h;
      always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
          so[16*h+:16] <= 16'd0;
          si[16*h+:16] <= 16'd0;
          bc[5*h+:5]   <= 5'd0;
          sc[3*h+:3]   <= 3'd0;
          st[4*h+:4]   <= 4'd0;
        end else begin
          if (load && (tid == H)) begin
            so[16*h+:16] <= wdata;
            bc[5*h+:5]   <= 5'd0;
            st[4*h+:4]   <= wdata[3:0];
          end else if (shift_out[h]) begin
            so[16*h+:16] <= {1'b0, so[16*h+1+:15]};
            bc[5*h+:5]   <= bc[5*h+:5] + 5'd1;
            sc[3*h+:3]   <= so[16*h] ? sc[3*h+:3] + 3'd1 : 3'd0;
            st[4*h+:4]   <= {st[4*h+:3], st[4*h] ^ ~so[16*h]};
          end
          if (shift_in[h]) si[16*h+:16] <= {din[h], si[16*h+1+:15]};
        end
      end
      assign dout[h]  = st[4*h];
      assign stuff[h] = (sc[3*h+:3] == 3'd6);
    end
  endgenerate
  assign rdata  = si[16*tid+:16];
  assign bcount = bc[5*tid+:5];
  assign state  = st[4*tid+:4];
endmodule

// ---------------------------------------------------------------------------
// A1 block D5 (CRC unit): 32-bit CRC register and 32-bit polynomial
// register, one bit in each step (reflected form, covers CRC5, CRC16 and
// CRC32 with the polynomial and the start value from the thread).
// ---------------------------------------------------------------------------
module g1_crc (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [1:0]  wsel,   // 0 crc low, 1 crc high, 2 poly low, 3 poly high
    input  wire        we,
    input  wire [15:0] wdata,
    input  wire        step,
    input  wire        din,
    input  wire        rsel,
    output wire [15:0] rdata
);
  reg [31:0] crc, poly;
  wire fb = crc[0] ^ din;
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      crc  <= 32'd0;
      poly <= 32'd0;
    end else if (we) begin
      case (wsel)
        2'd0: crc[15:0] <= wdata;
        2'd1: crc[31:16] <= wdata;
        2'd2: poly[15:0] <= wdata;
        default: poly[31:16] <= wdata;
      endcase
    end else if (step) begin
      crc <= {1'b0, crc[31:1]} ^ (poly & {32{fb}});
    end
  end
  assign rdata = rsel ? crc[31:16] : crc[15:0];
endmodule

// ---------------------------------------------------------------------------
// A1 block D7 (host SPI port): synchronizers (3 stages for SCK, 2 for CS
// and MOSI, A1 9.4), a 32-bit receive shift register, bit count, address
// counter and a 16-bit transmit shift register. M2c designs the real port.
// ---------------------------------------------------------------------------
module g1_hostspi_store (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        sck,
    input  wire        cs_n,
    input  wire        mosi,
    input  wire [15:0] rd_word,   // word for the host (memory, FIFO, status)
    output wire        miso,
    output reg         we,
    output reg  [6:0]  addr,
    output reg  [15:0] wdata,
    output reg  [7:0]  cmd
);
  reg [2:0]  sck_s;
  reg [1:0]  cs_s, mosi_s;
  reg [31:0] rx;
  reg [4:0]  bits;
  reg [15:0] tx;
  wire rise = sck_s[1] && !sck_s[2];
  wire fall = !sck_s[1] && sck_s[2];
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      sck_s  <= 3'd0;
      cs_s   <= 2'b11;
      mosi_s <= 2'd0;
      rx     <= 32'd0;
      bits   <= 5'd0;
      tx     <= 16'd0;
      we     <= 1'b0;
      addr   <= 7'd0;
      wdata  <= 16'd0;
      cmd    <= 8'd0;
    end else begin
      sck_s  <= {sck_s[1:0], sck};
      cs_s   <= {cs_s[0], cs_n};
      mosi_s <= {mosi_s[0], mosi};
      we     <= 1'b0;
      if (cs_s[1]) begin
        bits <= 5'd0;
      end else if (rise) begin
        rx   <= {rx[30:0], mosi_s[1]};
        bits <= bits + 5'd1;
        if (bits == 5'd31) begin
          cmd   <= rx[30:23];
          wdata <= {rx[14:0], mosi_s[1]};
          we    <= rx[30];
          addr  <= (rx[30] && rx[29]) ? addr + 7'd1 : rx[22:16];
          tx    <= rd_word;
        end
      end else if (fall) begin
        tx <= {tx[14:0], 1'b0};
      end
    end
  end
  assign miso = tx[15];
endmodule

// ---------------------------------------------------------------------------
// A1 block D9 (process monitor): a 20-bit gate counter on clk and a 20-bit
// counter on the ring-oscillator clock. The 31-stage ring itself is a
// calculation from the LEF cell areas (a combinational loop is not
// synthesized here).
// ---------------------------------------------------------------------------
module g1_pmon (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        ro_clk,
    input  wire        start,
    input  wire [19:0] gate_len,
    output reg  [19:0] count,
    output wire        busy
);
  reg [19:0] gate;
  reg        en;
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      gate <= 20'd0;
      en   <= 1'b0;
    end else if (start) begin
      gate <= gate_len;
      en   <= 1'b1;
    end else if (gate != 20'd0) begin
      gate <= gate - 20'd1;
    end else begin
      en <= 1'b0;
    end
  end
  reg [1:0] en_s;
  always @(posedge ro_clk or negedge rst_n) begin
    if (!rst_n) begin
      en_s  <= 2'd0;
      count <= 20'd0;
    end else begin
      en_s <= {en_s[0], en};
      if (en_s[1]) count <= count + 20'd1;
      else if (start) count <= 20'd0;
    end
  end
  assign busy = en;
endmodule

// ---------------------------------------------------------------------------
// A1 block D10 (event recorder): last pin state, the time of a change and a
// write address. A change of a pin gives two memory words: the time, then
// the pins.
// ---------------------------------------------------------------------------
module g1_evrec (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        en,
    input  wire [15:0] pins,
    input  wire [15:0] now,
    output wire        we,
    output wire [6:0]  waddr,
    output wire [15:0] wdata
);
  reg [15:0] prev, tlat, chg;
  reg [6:0]  addr;
  reg        ph;
  wire change = en && (pins != prev) && !ph;
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      prev <= 16'd0;
      tlat <= 16'd0;
      chg  <= 16'd0;
      addr <= 7'd0;
      ph   <= 1'b0;
    end else begin
      if (!ph) prev <= pins;
      if (change) begin
        tlat <= now;
        chg  <= pins;
        ph   <= 1'b1;
        addr <= addr + 7'd1;
      end else if (ph) begin
        ph   <= 1'b0;
        addr <= addr + 7'd1;
      end
    end
  end
  assign we    = change || ph;
  assign waddr = addr;
  assign wdata = ph ? chg : now;
endmodule
