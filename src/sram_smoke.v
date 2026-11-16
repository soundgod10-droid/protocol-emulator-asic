/*
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// SRAM layout smoke test (research branch only, not the chip).
//
// The two single-port IHP SG13 macros planned for the processor, the 512 x 16 program memory
// (u_pmem) and the 1024 x 16 frame buffer (u_dmem), at their planned positions (src/config.json
// MACROS), driven from flip-flops. The layout flow and the layout precheck then check the power
// grid through the macro power columns, LVS, DRC and the pin rules for this macro set before the
// processor RTL uses it.
//
// One command per clock, sampled on the rising edge of clk:
//   ui_in[7:5] = 000  read both macros at the address register
//                001  address bits 7:0 <= uio_in
//                010  address bits 9:8 <= uio_in[1:0]
//                100  write the low byte of the addressed word (ui_in[4]: 0 program, 1 buffer)
//                101  write the high byte
//   ui_in[1:0]   uo_out byte: 0 program low, 1 program high, 2 buffer low, 3 buffer high
// Every macro input comes from a flip-flop; each read word is registered before the output mux.

module tt_um_soundgod10_droid_protocol_emulator (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);

  reg        men_q;
  reg        pwen_q, dwen_q;
  reg        hi_q;
  reg  [7:0] data_q;
  reg  [9:0] addr;
  reg  [2:0] cmd_q;

  wire       is_wr = ui_in[7];

  always @(posedge clk) begin
    men_q  <= rst_n;
    pwen_q <= rst_n & is_wr & ~ui_in[4];
    dwen_q <= rst_n & is_wr &  ui_in[4];
    hi_q   <= ui_in[5];
    cmd_q  <= rst_n ? ui_in[7:5] : 3'b000;
    data_q <= uio_in;
    if (!rst_n) begin
      addr <= 10'd0;
    end else if (cmd_q == 3'b001) begin
      addr[7:0] <= data_q;
    end else if (cmd_q == 3'b010) begin
      addr[9:8] <= data_q[1:0];
    end
  end

  wire [15:0] pdout, ddout;
  wire [15:0] bm = hi_q ? 16'hFF00 : 16'h00FF;

  // Instance names are referenced by MACROS and PDN_MACRO_CONNECTIONS in src/config.json.
  RM_IHPSG13_1P_512x16_c2_bm_bist u_pmem (
      .A_CLK      (clk),
      .A_MEN      (men_q),
      .A_WEN      (pwen_q),
      .A_REN      (~pwen_q),
      .A_ADDR     (addr[8:0]),
      .A_DIN      ({data_q, data_q}),
      .A_DLY      (1'b1),               // datasheet: must be tied to 1
      .A_DOUT     (pdout),
      .A_BM       (bm),
      .A_BIST_CLK (1'b0),
      .A_BIST_EN  (1'b0),
      .A_BIST_MEN (1'b0),
      .A_BIST_WEN (1'b0),
      .A_BIST_REN (1'b0),
      .A_BIST_ADDR(9'd0),
      .A_BIST_DIN (16'd0),
      .A_BIST_BM  (16'd0)
  );

  // Fallback memory set: a second 512 x 16 macro as the buffer.
  RM_IHPSG13_1P_512x16_c2_bm_bist u_dmem (
      .A_CLK      (clk),
      .A_MEN      (men_q),
      .A_WEN      (dwen_q),
      .A_REN      (~dwen_q),
      .A_ADDR     (addr[8:0]),
      .A_DIN      ({data_q, data_q}),
      .A_DLY      (1'b1),
      .A_DOUT     (ddout),
      .A_BM       (bm),
      .A_BIST_CLK (1'b0),
      .A_BIST_EN  (1'b0),
      .A_BIST_MEN (1'b0),
      .A_BIST_WEN (1'b0),
      .A_BIST_REN (1'b0),
      .A_BIST_ADDR(9'd0),
      .A_BIST_DIN (16'd0),
      .A_BIST_BM  (16'd0)
  );

  reg [15:0] pword, dword;
  always @(posedge clk) begin
    pword <= pdout;
    dword <= ddout;
  end

  assign uo_out  = ui_in[1] ? (ui_in[0] ? dword[15:8] : dword[7:0])
                            : (ui_in[0] ? pword[15:8] : pword[7:0]);
  assign uio_out = 8'd0;
  assign uio_oe  = 8'd0;

  wire _unused = &{ena, ui_in[3:2], cmd_q[2], 1'b0};

endmodule
