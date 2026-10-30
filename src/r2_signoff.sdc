# R2 measurement only (research branches; not for cmos5l). Read by the signoff STA
# through "SIGNOFF_SDC_FILE": "dir::r2_signoff.sdc" in src/config.json.
#
# Why: in gds 40793488418 every setup report of the clk group starts with the 2,560
# memory and FIFO latch endpoints (borrow slack 0.000), so the flip-flop slack is not
# printed. These constraints are the LibreLane default constraints (FALLBACK_SDC) plus two
# path groups, so the signoff STA reports flip-flop and latch endpoints separately.
# Only the signoff STA reads SIGNOFF_SDC_FILE (LibreLane 3.1.0.dev3 steps/openroad.py
# lines 980-985); the PnR steps use PNR_SDC_FILE or FALLBACK_SDC, so the layout does not
# change.

source $::env(FALLBACK_SDC)

group_path -name r2_flops -to [all_registers -edge_triggered -data_pins]
group_path -name r2_latches -to [all_registers -level_sensitive -data_pins]
