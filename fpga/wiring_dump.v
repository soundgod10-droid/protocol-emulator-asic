/*
 * SPDX-License-Identifier: Apache-2.0
 */

// Waveform of the wiring-check simulation test (fpga/test_wiring_check.py): a second simulation root that dumps the
// design under test as VCD, for the review on screen. Simulation only; not part of any bitstream.
module wiring_dump;
  initial begin
    $dumpfile("wiring_check.vcd");
    $dumpvars(0, wiring_check);
  end
endmodule
