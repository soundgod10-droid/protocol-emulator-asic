# Source files of the FPGA build, relative to the repository root: the chip RTL of info.yaml source_files without
# src/pe_cells.v (IHP latch and clock-gate cells), src/pe_latch_ram.v (latch memory) and src/sram_bb.v (macro black
# boxes); with fpga/pe_latch_ram_fpga.v for the latch memory and src/sram_sim.v (the macro data sheet model, which
# synthesis maps to block RAM).
src/project.v
src/pe_sync.v
src/pe_cycle_counter.v
src/pe_timed_out.v
src/pe_sram.v
src/sram_sim.v
src/pe_slu_loop.v
src/pe_slu.v
src/pe_host_fifo.v
src/pe_timed_in.v
src/pe_config.v
src/pe_host_port.v
src/pe_core.v
src/pe_proc.v
fpga/pe_latch_ram_fpga.v
fpga/fpga_top.v
