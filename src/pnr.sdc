# Place-and-route constraints: the default constraints plus a max transition limit of 1.2 ns,
# so that timing-driven placement does not leave max-slew violations (reserve of decision 5 (d)
# in docs/decisions.md: gds 41856662241 at 50 MHz on 6x4 had none).

source $::env(FALLBACK_SDC)

set_max_transition 1.2 [current_design]

# The FIFO latch D pins out of the place-and-route setup checks: they borrow and report 0.000, which a setup
# margin above 0 (config.json PL_RESIZER_SETUP_SLACK_MARGIN) would treat as violators. The signoff constraints
# (signoff.sdc) still check every latch path (docs/decisions.md entry 21).
set_false_path -to [get_cells -hierarchical -filter "ref_name == sg13cmos5l_dlhq_1" *]
