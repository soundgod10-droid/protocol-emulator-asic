* M2c probe: find the standard-cell SPICE file and the port order of
* sg13cmos5l_dfrbpq_1. A flip-flop must take D at the rising CLK edge.
.lib cornerMOSlv.lib mos_tt
.include sg13cmos5l_stdcell.spice

.param vdd=1.2
VDD vdd 0 {vdd}
VSS vss 0 0
VCLK clk 0 PULSE(0 {vdd} 5n 50p 50p 10n 20n)
VD d 0 PULSE(0 {vdd} 2n 50p 50p 30n 80n)
VR rb 0 {vdd}
X1 clk d q rb vdd vss sg13cmos5l_dfrbpq_1
CL q 0 5f

.tran 10p 100n
.control
run
meas tran q1 FIND v(q) AT=14n
meas tran q2 FIND v(q) AT=54n
meas tran q3 FIND v(q) AT=94n
.endc
.end
