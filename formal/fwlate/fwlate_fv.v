/*
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// fwlate_fv: "LATE is never set" for one firmware image on the processor RTL (formal/fwlate/gen.py writes the jobs).
//
// pe_proc with its real core and event slots, as formal/proc_fv.v; the memory is a ROM with the program image
// (fwlate_rom, written by gen.py: the word of each address, HALT elsewhere) read as the SRAM macros are read (the word
// of the last read, from the clock edge after the request); the host FIFOs, the line helpers and the input unit are
// the free models of formal/proc_stubs.v (every host word, input level and port word free in every cycle). Thread 0
// runs from address 0; the other threads do not run; the host configuration gives the program's output pins to thread
// 0 (OWNED, from gen.py) and leaves the other pins free. Reset at step 0.
// Asserted: thread 0's LATE flag stays 0; the program never writes the memory (a store would need a RAM model).

module fwlate_fv (
    input wire        clk,
    input wire        rst_n,
    input wire [29:0] pin_owner,
    input wire [14:0] pin_en,
    input wire [14:0] pin_od,
    input wire [4:0]  ui_in,
    input wire [7:0]  uio_in
);
  `include "fwlate_params.vh"

  initial assume (!rst_n);
  reg f_past = 1'b0;
  always @(posedge clk) f_past <= 1'b1;
  always @* if (f_past) assume (rst_n);       // one reset at the start, as at power-up

  // the program's output pins belong to thread 0 and are enabled, not given to a helper
  genvar p;
  generate
    for (p = 0; p < 15; p = p + 1) begin : g_own
      if (OWNED[p]) begin : g_o
        always @* assume (pin_en[p] && pin_owner[2*p+:2] == 2'd0);
      end
    end
  endgenerate

  reg  rst_q, pre1, pre2;
  wire h_start = rst_n && !rst_q;
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      rst_q <= 1'b0;
      pre1  <= 1'b0;
      pre2  <= 1'b0;
    end else begin
      rst_q <= 1'b1;
      pre1  <= h_start;
      pre2  <= pre1;
    end
  end
  wire [1:0]  prestart = h_start ? 2'd1 : (pre1 ? 2'd2 : 2'd0);
  wire        start    = pre2;
  wire [15:0] now;
  pe_cycle_counter #(.WIDTH(16)) u_cnt (.clk(clk), .rst_n(rst_n), .clear(start), .count(now));

  wire [14:0] pin_v, pin_oe;
  wire [3:0]  th_run, th_late, th_err;
  pe_proc u_proc (
      .clk(clk), .rst_n(rst_n), .run_rst_n(rst_n), .now(now), .start(start), .prestart(prestart),
      .start_pc(36'd0), .run_mask(4'b0001), .pin_owner(pin_owner), .pin_en(pin_en), .pin_od(pin_od),
      .pin_hdrv(15'd0), .pin_hsel(15'd0), .pin_hsig(45'd0),
      .h_mem_host(1'b0), .h_mem_req(1'b0), .h_mem_we(1'b0), .h_mem_addr(11'd0), .h_mem_data(16'd0), .h_mem_rdata(),
      .h_tid(2'd0), .h_push(1'b0), .h_wdata(16'd0), .h_in_full(), .h_pop(1'b0), .h_out_data(), .h_cnt_in(),
      .h_cnt_out(), .th_run(th_run), .th_late(th_late), .th_err(th_err), .ui_in(ui_in), .uio_in(uio_in),
      .pin_v(pin_v), .pin_oe(pin_oe));

  wire mon_mem_we;   // u_proc.mem_req && u_proc.mem_we, connected by the job script
  always @* begin
    if (rst_n) begin
      assert (!th_late[0]);
      assert (!mon_mem_we);
    end
  end
  always @* cover (rst_n && pin_v != 15'h7FFF);   // the program changes a pin within the depth
endmodule

// ROM in place of the two SRAM macros (ports of src/pe_sram.v): the word of the last read, from the edge after it.
module pe_sram (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        req,
    input  wire        we,
    input  wire [10:0] addr,
    input  wire [15:0] wdata,
    output reg  [15:0] rdata
);
  `include "fwlate_rom.vh"
  always @(posedge clk) if (req && !we) rdata <= fwlate_rom(addr);
endmodule
