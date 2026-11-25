/*
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// Wrappers of formal/slu_gen_equiv.sby: the dedicated USB helper (pe_slu SPEC 1, the form proved by slu_usb.sby and
// slu_usb_equiv.sby) and the chip's general line unit (pe_slu SPEC 1 with GEN 1, DDR_EN 0, KI_EN 0: helper select 0
// since the general unit replaced the dedicated helper) behind the same port filter. The dedicated form can be
// configured only through SEL (CFGADR 8), MODE bits 0, 1, 3, 4 and 9 (the clocked and NRZ modes), TXPER[7:0] (1 to
// 255) and LTC[12:8]; every other CFGDAT write is masked or ignored there. The filter drops every CFGDAT write
// outside this set on both sides (a TXPER write with a low byte of 0 too: the port definition needs TXPER >= 1), and
// the general unit, which takes a whole configuration word, is given the masked word that the dedicated form keeps.
// Every other port access (TXDATA, TXLAST, TXCTRL, TXTIME, STATUS, EVMASK, CFGADR, reads) and every pin input reaches
// both unchanged. With the two units equal at every output and every register in every cycle (except the bit
// monitor's sent-bit history, which only the general unit keeps and which no output reads while EXT1 is 0), the USB
// and helper-mode claims proved and tested on the dedicated form hold for the general unit over the whole
// configuration space of the dedicated form. SLU_GEN_EQUIV_FREE (the tooth) gives the general unit the whole MODE
// word, so it can leave that space and the proof must fail. uadr and txper_lo are left undriven here; the proof
// script connects them to u.cfg_adr and u.c_txper[7:0] after flatten (txper_nz is the invariant TXPER[7:0] != 0 that
// the induction needs, stated as an equiv_add constant).

module slu_gequiv_old (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [15:0] now,
    input  wire        p_we,
    input  wire        p_re,
    input  wire [3:0]  p_addr,
    input  wire [15:0] p_wdata,
    output wire [15:0] p_rdata,
    input  wire [12:0] pin_raw,
    input  wire [12:0] pin_sync,
    output wire [3:0]  tx_v,
    output wire        tx_oe,
    output wire        ev
);
  wire [4:0] uadr;
  (* keep *) wire [7:0] txper_lo;
  (* keep *) wire       txper_nz = |txper_lo;
  // CFGDAT writes that the dedicated form takes: SEL, MODE (masked), TXPER (low byte, not 0), LTC (masked)
  wire cfg_wr = p_we && (p_addr == 4'd13);
  wire cfg_ok = (uadr == 5'd8) || (uadr == 5'd0) || (uadr == 5'd18) || ((uadr == 5'd1) && (p_wdata[7:0] != 8'd0));
  wire we_f   = p_we && !(cfg_wr && !cfg_ok);
  pe_slu #(.FULL(0), .SPEC(1)) u (
      .clk(clk), .rst_n(rst_n), .now(now), .p_we(we_f), .p_re(p_re), .p_addr(p_addr),
      .p_wdata(p_wdata), .p_rdata(p_rdata), .pin_raw(pin_raw), .pin_sync(pin_sync), .tx_v(tx_v),
      .tx_oe(tx_oe), .ev(ev));
endmodule

module slu_gequiv_new (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [15:0] now,
    input  wire        p_we,
    input  wire        p_re,
    input  wire [3:0]  p_addr,
    input  wire [15:0] p_wdata,
    output wire [15:0] p_rdata,
    input  wire [12:0] pin_raw,
    input  wire [12:0] pin_sync,
    output wire [3:0]  tx_v,
    output wire        tx_oe,
    output wire        ev
);
  wire [4:0] uadr;
`ifdef SLU_GEN_EQUIV_FREE
  wire we_f = p_we && !((p_addr == 4'd13) && (uadr != 5'd8) && (uadr != 5'd0));
`else
  wire we_f = p_we && !((p_addr == 4'd13) && (uadr != 5'd8));
`endif
  pe_slu #(.FULL(0), .SPEC(1), .GEN(1), .DDR_EN(0), .KI_EN(0)) u (
      .clk(clk), .rst_n(rst_n), .now(now), .p_we(we_f), .p_re(p_re), .p_addr(p_addr),
      .p_wdata(p_wdata), .p_rdata(p_rdata), .pin_raw(pin_raw), .pin_sync(pin_sync), .tx_v(tx_v),
      .tx_oe(tx_oe), .ev(ev));
endmodule
