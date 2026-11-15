* pad_load_ss: slow corner (mos_ss, 1.08 V core, 3.0 V IO, 125 C). Output pad delay and edge into 5, 15
* and 30 pF (the board load is not known), and the input pad at 50 and 100 MHz with 2 ns pad edges:
* pulse width and swing on the core side (does the pad limit the clock?).
.lib cornerMOSlv.lib mos_ss
.lib cornerMOShv.lib mos_ss
.lib cornerDIO.lib dio_tt
.lib cornerRES.lib res_wcs
.include sg13cmos5l_io.spice
.temp 125
.param vd=1.08 vio=3.0
Vdd vdd 0 {vd}
Vio iovdd 0 {vio}
Ve ec 0 {vd}
Vc2 c2 0 PULSE(0 {vd} 5n 0.1n 0.1n 40n 80n)
X5 pad5 c2 p5 ec vdd 0 iovdd 0 sg13cmos5l_IOPadInOut30mA
Cl5 pad5 0 5p
X15 pad15 c2 p15 ec vdd 0 iovdd 0 sg13cmos5l_IOPadInOut30mA
Cl15 pad15 0 15p
X30 pad30 c2 p30 ec vdd 0 iovdd 0 sg13cmos5l_IOPadInOut30mA
Cl30 pad30 0 30p
* Input pad, clock on the pad
Vp50 ip50 0 PULSE(0 {vio} 5n 2n 2n 8n 20n)
X50 ip50 ic50 vdd 0 iovdd 0 sg13cmos5l_IOPadIn
Ci50 ic50 0 50f
Vp100 ip100 0 PULSE(0 {vio} 5n 2n 2n 3n 10n)
X100 ip100 ic100 vdd 0 iovdd 0 sg13cmos5l_IOPadIn
Ci100 ic100 0 50f
.control
tran 10p 120n
meas tran t5_r trig v(c2) val=0.54 rise=1 targ v(pad5) val=1.5 rise=1
meas tran t5_f trig v(c2) val=0.54 fall=1 targ v(pad5) val=1.5 fall=1
meas tran e5_r trig v(pad5) val=0.3 rise=1 targ v(pad5) val=2.7 rise=1
meas tran e5_f trig v(pad5) val=2.7 fall=1 targ v(pad5) val=0.3 fall=1
meas tran t15_r trig v(c2) val=0.54 rise=1 targ v(pad15) val=1.5 rise=1
meas tran t15_f trig v(c2) val=0.54 fall=1 targ v(pad15) val=1.5 fall=1
meas tran e15_r trig v(pad15) val=0.3 rise=1 targ v(pad15) val=2.7 rise=1
meas tran e15_f trig v(pad15) val=2.7 fall=1 targ v(pad15) val=0.3 fall=1
meas tran t30_r trig v(c2) val=0.54 rise=1 targ v(pad30) val=1.5 rise=1
meas tran t30_f trig v(c2) val=0.54 fall=1 targ v(pad30) val=1.5 fall=1
meas tran e30_r trig v(pad30) val=0.3 rise=1 targ v(pad30) val=2.7 rise=1
meas tran e30_f trig v(pad30) val=2.7 fall=1 targ v(pad30) val=0.3 fall=1
meas tran h50 trig v(ic50) val=0.54 rise=2 targ v(ic50) val=0.54 fall=2
meas tran l50 trig v(ic50) val=0.54 fall=2 targ v(ic50) val=0.54 rise=3
meas tran max50 max v(ic50) from=20n to=60n
meas tran min50 min v(ic50) from=20n to=60n
meas tran h100 trig v(ic100) val=0.54 rise=2 targ v(ic100) val=0.54 fall=2
meas tran l100 trig v(ic100) val=0.54 fall=2 targ v(ic100) val=0.54 rise=3
meas tran max100 max v(ic100) from=20n to=60n
meas tran min100 min v(ic100) from=20n to=60n
.endc
.end
