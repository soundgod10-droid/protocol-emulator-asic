# Signoff STA constraints.
#
# The LibreLane default constraints (FALLBACK_SDC: clock, I/O delay, load, clock
# uncertainty and transition, derate) and two path groups. Without the groups every
# setup report of the clk group starts with the 2,560 memory and FIFO latch endpoints
# (time borrowing, slack 0.000), so the flip-flop setup slack is not printed. With the
# groups, 55-openroad-stapostpnr/<corner>/checks.rpt starts with the worst path of the
# group "flops".
#
# Only the signoff STA reads SIGNOFF_SDC_FILE; placement, CTS, resizer and routing use
# the default constraints, so the layout does not change (gds 40955889046 with this
# file and gds 40793488418 without it: identical final metrics.csv layout rows).

source $::env(FALLBACK_SDC)

group_path -name flops -to [all_registers -edge_triggered -data_pins]
group_path -name latches -to [all_registers -level_sensitive -data_pins]
