* pad_5v: are the CMOS5L input pads 5 V tolerant? Pad voltage swept from 0 to 5.5 V with IOVDD = 3.3 V,
* typical corner; the input pad (ui_in) and the bidirectional 30 mA pad (uio) with its output off.
* Result: the current into the pad at 3.3, 3.6, 4.0 and 5.0 V.
.lib cornerMOSlv.lib mos_tt
.lib cornerMOShv.lib mos_tt
.lib cornerDIO.lib dio_tt
.lib cornerRES.lib res_typ
.include sg13cmos5l_io.spice
.temp 25
Vdd vdd 0 1.2
Vio iovdd 0 3.3
X3 pad3 p3 vdd 0 iovdd 0 sg13cmos5l_IOPadIn
V3 pad3 0 0
X4 pad4 c4 p4 e4 vdd 0 iovdd 0 sg13cmos5l_IOPadInOut30mA
Vc4 c4 0 0
Ve4 e4 0 0
V4 pad4 0 0
.control
dc V3 0 5.5 0.05
meas dc i_in_33 find i(V3) at=3.3
meas dc i_in_36 find i(V3) at=3.6
meas dc i_in_40 find i(V3) at=4.0
meas dc i_in_50 find i(V3) at=5.0
dc V4 0 5.5 0.05
meas dc i_io_33 find i(V4) at=3.3
meas dc i_io_36 find i(V4) at=3.6
meas dc i_io_40 find i(V4) at=4.0
meas dc i_io_50 find i(V4) at=5.0
.endc
.end
