# Placement, CTS, resizer and routing constraints (PNR_SDC_FILE). Research branch only.
#
# The LibreLane default constraints, with a design-wide slew limit of 1.2 ns for the repair
# steps. The signoff STA (src/signoff.sdc) does not read this file, so it keeps the liberty limit.

source $::env(FALLBACK_SDC)

set_max_transition 1.2 [current_design]
