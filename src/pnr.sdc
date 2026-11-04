# Placement, CTS, resizer and routing constraints (PNR_SDC_FILE). Research branch only.
#
# The LibreLane default constraints, with the setup checks of the latch data pins left out.
# Their slack is exactly 0.000 by time borrowing, so with a positive setup margin of the
# post-CTS resizer they are 2,560 violators that no repair can change. The signoff
# STA (src/signoff.sdc) keeps these checks.

source $::env(FALLBACK_SDC)

set_false_path -setup -to [all_registers -level_sensitive -data_pins]

# Slew limit for the repair steps only: gds 41901058875 has 1 net 0.5 % over the slow-corner liberty limit.
set_max_transition 1.2 [current_design]
