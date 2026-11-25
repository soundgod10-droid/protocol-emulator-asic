/*
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// Wrappers of formal/slu_gen_equiv.sby: the same input filter in front of the dedicated USB helper (pe_slu SPEC 1,
// the form proved by slu_usb.sby and slu_usb_equiv.sby) and of the chip's general line unit (pe_slu SPEC 1 with
// GEN 1, DDR_EN 0, KI_EN 0: helper select 0 since the general unit replaced the dedicated helper). The filter
// drops every CFGDAT (port 13) write while the unit's own CFGADR is not 8 (SEL), so both units stay at their
// reset configuration, which is the USB helper's; every other port access (SEL, TXDATA, TXLAST, TXCTRL, TXTIME,
// STATUS, EVMASK, CFGADR, reads) and every pin input reaches both unchanged. With the two units equal at every
// output and every register in every cycle, the USB claims proved and tested on the dedicated form hold for the
// general unit in that configuration. SLU_GEN_EQUIV_FREE (the tooth) lets the writes to MODE (CFGADR 0)
// through: the general unit then takes the whole word where the dedicated form masks it, so the proof must fail.
// uadr is left undriven here; the proof script connects it to u.cfg_adr after flatten.

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
`ifdef SLU_GEN_EQUIV_FREE
  wire we_f = p_we && !((p_addr == 4'd13) && (uadr != 5'd8) && (uadr != 5'd0));
`else
  wire we_f = p_we && !((p_addr == 4'd13) && (uadr != 5'd8));
`endif
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
