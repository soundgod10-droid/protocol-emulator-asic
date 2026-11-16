/*
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// pe_sram: program and data memory of the processor, two IHP SG13 single-port SRAM macros.
//
// What it does:
//   Address space of 2,048 words: 0 to 511 the program memory (RM_IHPSG13_1P_512x16, instance
//   u_pmem: instructions, and data for LD/ST), 512 to 1,535 the data memory (RM_IHPSG13_1P_1024x16,
//   instance u_dmem: frame buffers and tables for LDX/STX); 1,536 to 2,047 read and write 512 to
//   1,023 again. One access per cycle: the request (addr, we, wdata) comes from flip-flops of the
//   core or of the host port and the selected macro takes it at the next rising clock edge; the
//   word read is on rdata from about 7.4 ns (slow corner, liberty) after that edge until the next
//   read of the same macro. rdata selects the macro of the last read.
//
// Why:
//   The 128 x 16 latch array (97,044 um2 in synthesis) holds neither the USB device firmware with
//   its descriptors nor one 1,518-byte Ethernet frame. The two macros hold 512 program words and
//   1,024 data words in 124,983 um2 of macro area (LEF). The macro power and placement are set in
//   src/config.json (MACROS) and src/pdn_cfg.tcl. A_DLY must be 1 (datasheet); the BIST port is
//   not used.

module pe_sram (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        req,       // 1 = an access in this cycle's request
    input  wire        we,
    input  wire [10:0] addr,
    input  wire [15:0] wdata,
    output wire [15:0] rdata
);

  wire        sel_d = (addr[10:9] != 2'b00);            // data memory
  wire [9:0]  daddr = {addr[10:9] == 2'b10, addr[8:0]};  // (addr - 512) mod 1,024
  wire [15:0] pdout, ddout;
  reg         rsel;                                      // macro of the last read

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) rsel <= 1'b0;
    else if (req && !we) rsel <= sel_d;
  end

  RM_IHPSG13_1P_512x16_c2_bm_bist u_pmem (
      .A_CLK      (clk),
      .A_MEN      (req && !sel_d),
      .A_WEN      (we),
      .A_REN      (!we),
      .A_ADDR     (addr[8:0]),
      .A_DIN      (wdata),
      .A_DLY      (1'b1),
      .A_DOUT     (pdout),
      .A_BM       (16'hFFFF),
      .A_BIST_CLK (1'b0),
      .A_BIST_EN  (1'b0),
      .A_BIST_MEN (1'b0),
      .A_BIST_WEN (1'b0),
      .A_BIST_REN (1'b0),
      .A_BIST_ADDR(9'd0),
      .A_BIST_DIN (16'd0),
      .A_BIST_BM  (16'd0)
  );

  RM_IHPSG13_1P_1024x16_c2_bm_bist u_dmem (
      .A_CLK      (clk),
      .A_MEN      (req && sel_d),
      .A_WEN      (we),
      .A_REN      (!we),
      .A_ADDR     (daddr),
      .A_DIN      (wdata),
      .A_DLY      (1'b1),
      .A_DOUT     (ddout),
      .A_BM       (16'hFFFF),
      .A_BIST_CLK (1'b0),
      .A_BIST_EN  (1'b0),
      .A_BIST_MEN (1'b0),
      .A_BIST_WEN (1'b0),
      .A_BIST_REN (1'b0),
      .A_BIST_ADDR(10'd0),
      .A_BIST_DIN (16'd0),
      .A_BIST_BM  (16'd0)
  );

  assign rdata = rsel ? ddout : pdout;

endmodule
