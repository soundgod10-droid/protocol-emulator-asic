## Pins of the fmax harness (fpga/fpga_fmax_top.v) on a Digilent Arty A7-35T (xc7a35tcsg324-1): the 100 MHz board
## clock pin and two Pmod JA pins. Used only for the post-route fmax builds of fpga.yaml (openXC7 flow).
set_property LOC E3 [get_ports clk]
set_property IOSTANDARD LVCMOS33 [get_ports {clk}]

set_property LOC G13 [get_ports sin]
set_property IOSTANDARD LVCMOS33 [get_ports {sin}]

set_property LOC B11 [get_ports sout]
set_property IOSTANDARD LVCMOS33 [get_ports {sout}]
