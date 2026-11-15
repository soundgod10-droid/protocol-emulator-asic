* pad_ff: the same circuit as pad_tt.sp at the fast corner (mos_ff, 1.32 V core, 3.6 V IO, -40 C,
* res_bcs). Adds VOL at 10 mA and the sink current at VOL = 0.4 V.
.lib cornerMOSlv.lib mos_ff
.lib cornerMOShv.lib mos_ff
.lib cornerDIO.lib dio_tt
.lib cornerRES.lib res_bcs
.include sg13cmos5l_io.spice
.temp -40
.param vd=1.32 vio=3.6
Vdd vdd 0 {vd}
Vio iovdd 0 {vio}
X1 pad1 c1 p1 e1 vdd 0 iovdd 0 sg13cmos5l_IOPadInOut30mA
Vc1 c1 0 0
Ve1 e1 0 {vd}
I1 0 pad1 0
X2 pad2 c2 p2 e2 vdd 0 iovdd 0 sg13cmos5l_IOPadInOut30mA
Ve2 e2 0 {vd}
Vc2 c2 0 PULSE(0 {vd} 5n 0.1n 0.1n 20n 40n)
Cl2 pad2 0 5p
X3 pad3 p3 vdd 0 iovdd 0 sg13cmos5l_IOPadIn
Vp3 pad3 0 PULSE(0 {vio} 5n 2n 2n 20n 40n)
Cp3 p3 0 5f
.control
dc I1 0 40m 1m
meas dc vol3 find v(pad1) at=3m
meas dc vol10 find v(pad1) at=10m
meas dc vol20 find v(pad1) at=20m
meas dc i04 find i-sweep when v(pad1)=0.4
tran 10p 50n
meas tran tout_r trig v(c2) val=0.66 rise=1 targ v(pad2) val=1.8 rise=1
meas tran tout_f trig v(c2) val=0.66 fall=1 targ v(pad2) val=1.8 fall=1
meas tran edge_r trig v(pad2) val=0.36 rise=1 targ v(pad2) val=3.24 rise=1
meas tran edge_f trig v(pad2) val=3.24 fall=1 targ v(pad2) val=0.36 fall=1
meas tran tin_r trig v(pad3) val=1.8 rise=1 targ v(p3) val=0.66 rise=1
meas tran tin_f trig v(pad3) val=1.8 fall=1 targ v(p3) val=0.66 fall=1
.endc
.end
