/*
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// Wrappers of formal/slu_usb_equiv.sby: the same input filter in front of the USB helper of commit f49819e (the
// USB-only form before the clocked and NRZ modes; pe_slu_ref, fetched by the workflow) and of the present USB
// helper (pe_slu SPEC 1). The filter drops a CFGDAT (port 13) write while the unit's own CFGADR is 0 (MODE),
// 1 (TXPER) or 18 (LTC), so the present helper stays in its USB configuration, and drops every STATUS (port 2)
// write: the present helper keeps a flag that an event sets in the cycle of a STATUS write (finding CV1-F1), the
// old one lost it, so the two differ there by design; that behaviour is checked by formal/slc_rx.sby R4 and
// test_slc_status_clear_same_cycle. Every other input reaches both units unchanged. SLU_EQUIV_MODE_FREE (the
// tooth) lets MODE writes through, so the proof must fail.
// uadr is left undriven here; the proof script connects it to u.cfg_adr after flatten.

module slu_equiv_old (
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
`ifdef SLU_EQUIV_MODE_FREE
  wire we_f = p_we && !((p_addr == 4'd13) && ((uadr == 5'd1) || (uadr == 5'd18))) && (p_addr != 4'd2);
`else
  wire we_f = p_we && !((p_addr == 4'd13) && ((uadr == 5'd0) || (uadr == 5'd1) || (uadr == 5'd18))) &&
              (p_addr != 4'd2);
`endif
  pe_slu_ref #(.FULL(0), .SPEC(1)) u (
      .clk(clk), .rst_n(rst_n), .now(now), .p_we(we_f), .p_re(p_re), .p_addr(p_addr),
      .p_wdata(p_wdata), .p_rdata(p_rdata), .pin_raw(pin_raw), .pin_sync(pin_sync), .tx_v(tx_v),
      .tx_oe(tx_oe), .ev(ev));
endmodule

module slu_equiv_new (
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
`ifdef SLU_EQUIV_MODE_FREE
  wire we_f = p_we && !((p_addr == 4'd13) && ((uadr == 5'd1) || (uadr == 5'd18))) && (p_addr != 4'd2);
`else
  wire we_f = p_we && !((p_addr == 4'd13) && ((uadr == 5'd0) || (uadr == 5'd1) || (uadr == 5'd18))) &&
              (p_addr != 4'd2);
`endif
  pe_slu #(.FULL(0), .SPEC(1)) u (
      .clk(clk), .rst_n(rst_n), .now(now), .p_we(we_f), .p_re(p_re), .p_addr(p_addr),
      .p_wdata(p_wdata), .p_rdata(p_rdata), .pin_raw(pin_raw), .pin_sync(pin_sync), .tx_v(tx_v),
      .tx_oe(tx_oe), .ev(ev));
endmodule
