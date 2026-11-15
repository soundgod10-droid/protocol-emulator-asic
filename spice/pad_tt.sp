* pad_tt: the I/O pad cells of the Tiny Tapeout CMOS5L chip (uio: IOPadInOut30mA, ui_in: IOPadIn),
* typical corner (mos_tt, 1.20 V core, 3.3 V IO, 25 C), schematic netlist sg13cmos5l_io.spice (no extraction).
* DC: output voltage of the 30 mA pad driving 0 while a current is forced into it (I2C VOL, UM10204 Table 10).
* Transient: output delay and edge into 5 pF; input delay of the input pad with 2 ns pad edges.
* Pin order from the I/O subcircuits in sg13cmos5l_io.spice. cornerDIO.lib has only dio_tt.
.lib cornerMOSlv.lib mos_tt
.lib cornerMOShv.lib mos_tt
.lib cornerDIO.lib dio_tt
.lib cornerRES.lib res_typ
.include sg13cmos5l_io.spice
.temp 25
.param vd=1.2 vio=3.3
Vdd vdd 0 {vd}
Vio iovdd 0 {vio}
* DC: pad drives 0, current is forced into the pad
X1 pad1 c1 p1 e1 vdd 0 iovdd 0 sg13cmos5l_IOPadInOut30mA
Vc1 c1 0 0
Ve1 e1 0 {vd}
I1 0 pad1 0
* Transient: output 0 to 1 and 1 to 0 at 5 pF
X2 pad2 c2 p2 e2 vdd 0 iovdd 0 sg13cmos5l_IOPadInOut30mA
Ve2 e2 0 {vd}
Vc2 c2 0 PULSE(0 {vd} 5n 0.1n 0.1n 20n 40n)
Cl2 pad2 0 5p
* Input pad: pad edge with 2 ns rise, to the core side
X3 pad3 p3 vdd 0 iovdd 0 sg13cmos5l_IOPadIn
Vp3 pad3 0 PULSE(0 {vio} 5n 2n 2n 20n 40n)
Cp3 p3 0 5f
.control
dc I1 0 40m 1m
meas dc vol3 find v(pad1) at=3m
meas dc vol20 find v(pad1) at=20m
meas dc vol30 find v(pad1) at=30m
tran 10p 50n
meas tran tout_r trig v(c2) val=0.6 rise=1 targ v(pad2) val=1.65 rise=1
meas tran tout_f trig v(c2) val=0.6 fall=1 targ v(pad2) val=1.65 fall=1
meas tran edge_r trig v(pad2) val=0.33 rise=1 targ v(pad2) val=2.97 rise=1
meas tran edge_f trig v(pad2) val=2.97 fall=1 targ v(pad2) val=0.33 fall=1
meas tran tin_r trig v(pad3) val=1.65 rise=1 targ v(p3) val=0.6 rise=1
meas tran tin_f trig v(pad3) val=1.65 fall=1 targ v(p3) val=0.6 fall=1
.endc
.end
