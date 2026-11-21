# Makefile
# See https://docs.cocotb.org/en/stable/quickstart.html for more info

# defaults
SIM ?= icarus
FST ?= -fst # Use more efficient FST format
TOPLEVEL_LANG ?= verilog
SRC_DIR = $(PWD)/../src
PROJECT_SOURCES = project.v pe_sync.v pe_cycle_counter.v pe_timed_out.v \
                  pe_cells.v pe_latch_ram.v pe_sram.v pe_host_fifo.v pe_timed_in.v pe_config.v \
                  pe_host_port.v pe_core.v pe_proc.v \
                  pe_slu_loop.v pe_slu.v sram_sim.v

ifneq ($(GATES),yes)

# RTL simulation (pe_slu_loop.v: serial line unit block, tested alone in tb.v by test/test_slu.py):
SIM_BUILD				= sim_build/rtl
VERILOG_SOURCES += $(addprefix $(SRC_DIR)/,$(PROJECT_SOURCES))

else

# Gate level simulation:
SIM_BUILD				= sim_build/gl
COMPILE_ARGS    += -DGL_TEST
ifeq ($(SDF_FILE),)
COMPILE_ARGS    += -DFUNCTIONAL
else
# SDF job (.github/workflows/sdf.yaml): cell models with their specify paths, delays from SDF_FILE.
COMPILE_ARGS    += -gspecify -DSDF_FILE='"$(SDF_FILE)"'
endif
COMPILE_ARGS    += -DSIM
# Cell models for the PDK in $(PDK) (ihp-sg13cmos5l or ihp-sg13g2), e.g. sg13cmos5l_stdcell.v
PDK ?= ihp-sg13cmos5l
PDK_CELL_PREFIX = $(patsubst ihp-%,%,$(PDK))
PDK_LIBS_REF = $(PDK_ROOT)/$(PDK)/libs.ref
VERILOG_SOURCES += $(PDK_LIBS_REF)/$(PDK_CELL_PREFIX)_io/verilog/$(PDK_CELL_PREFIX)_io.v
# Newer PDK versions ship the UDP (user-defined primitive) models in a separate file
VERILOG_SOURCES += $(wildcard $(PDK_LIBS_REF)/$(PDK_CELL_PREFIX)_stdcell/verilog/$(PDK_CELL_PREFIX)_udp.v)
VERILOG_SOURCES += $(PDK_LIBS_REF)/$(PDK_CELL_PREFIX)_stdcell/verilog/$(PDK_CELL_PREFIX)_stdcell.v

# this gets copied in by the GDS action workflow
VERILOG_SOURCES += $(PWD)/gate_level_netlist.v
# the netlist keeps the SRAM macros as instances: their functional models; in the SDF job the same models with the
# read delay of the layout SDF (test/sram_sdf.v; test/sram_delay.vh written by test/sdf_names.py --macro-vh)
ifeq ($(SDF_FILE),)
VERILOG_SOURCES += $(SRC_DIR)/sram_sim.v
else
VERILOG_SOURCES += $(PWD)/sram_sdf.v
COMPILE_ARGS    += -I$(PWD)
endif

endif

ifeq ($(PIN_VCD),1)
# Waveform figures (.github/workflows/docs.yaml): the chip pins as VCD (test/tb.v PIN_VCD); run with FST= so that
# Icarus writes VCD. Own build folder, so that the normal RTL build is not reused.
SIM_BUILD       = sim_build/pins
COMPILE_ARGS    += -DPIN_VCD
endif

ifeq ($(SIM),verilator)
# Coverage job (.github/workflows/coverage.yaml): line, branch and toggle coverage of the RTL
# tests; behavioral latch and clock-gate models (src/pe_cells.v); lint is the flow's job.
COMPILE_ARGS    += --coverage-line --coverage-toggle --timing -DPE_RTL_SIM
COMPILE_ARGS    += -Wno-fatal -Wno-lint -Wno-style
endif

# Allow sharing configuration between design and testbench via `include`:
COMPILE_ARGS 		+= -I$(SRC_DIR)

# Include the testbench sources:
VERILOG_SOURCES += $(PWD)/tb.v
TOPLEVEL = tb

# List test modules to run, separated by commas and without the .py suffix. At gate level the protocol tests
# (test_rtl_fw) run in their own parallel jobs (.github/workflows/gds.yaml gl_protocols), not in gl_test:
ifneq ($(GATES),yes)
COCOTB_TEST_MODULES = test,test_isa,test_core,test_fw_full,test_flex,test_holdout,test_rtl_fw,test_slu,test_slu_clk,test_linemodel,test_usb_dev,test_eth_dev,test_i2c_tgt,test_dmx512,test_dcc,test_sbus,test_sigrok,test_helpers_model,test_helpers_rtl,test_slu_cov,test_rtl_sweep,test_trace
else
COCOTB_TEST_MODULES = test,test_isa,test_core,test_fw_full,test_flex,test_holdout
endif

# include cocotb's make rules to take care of the simulator setup
include $(shell cocotb-config --makefiles)/Makefile.sim
