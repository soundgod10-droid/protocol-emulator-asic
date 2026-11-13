# Place-and-route constraints: the default constraints plus a max transition limit of 1.2 ns,
# so that timing-driven placement does not leave max-slew violations (reserve of decision 5 (d)
# in docs/decisions.md: gds 41856662241 at 50 MHz on 6x4 had none).

source $::env(FALLBACK_SDC)

set_max_transition 1.2 [current_design]
