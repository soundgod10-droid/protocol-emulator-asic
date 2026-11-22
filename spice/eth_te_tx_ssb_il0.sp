* 10BASE-Te transmit of the demo-board module (docs/bench.md, decisions entry for the 10BASE-Te claim): two
* sg13cmos5l_IOPadOut30mA pads per leg (helper signals TXA/TXC on TD+, TXB/TXD on TD-, docs/helpers.md section 3),
* each with its own 71.5 ohm resistor, the second pad of each leg one clock (16.667 ns) later; 3rd-order L-C-L
* Butterworth low-pass at 20 MHz (100 ohm differential, leakage 0.3 uH taken from the second inductor); 1:1
* magnetics of the HR911105A jack (OCL 350 uH minimum; insertion loss up to -1.0 dB, 300 kHz-100 MHz, modelled as
* RW in series with each winding: 2 x 12.2 ohm = -1.0 dB in a 100 ohm system; RW = 0 here); 100 ohm load.
* Leakage inductance and the 2 x 10 pF winding capacitance are assumed (not in the data sheet).
* Pattern: idle (both legs low) to 0.5 us, then all-ones Manchester (CD1: low half, high half), 10 MHz.
* Corner ssb: slow MOS, worst-case resistors, 125 C, core 1.08 V, IO 3.2 V (3.3 V - 3 %).
.lib cornerMOSlv.lib mos_ss
.lib cornerMOShv.lib mos_ss
.lib cornerDIO.lib dio_tt
.lib cornerRES.lib res_wcs
.include sg13cmos5l_io.spice
.temp 125
.param VD=1.08 VIO=3.2 RTX=69.8 RW=0
Vdd vdd 0 {VD}
Vio iovdd 0 {VIO}
Vcp0 cp0 0 PULSE(0 {VD} 0.55u 0.1n 0.1n 49.9n 100n)
Vcp1 cp1 0 PULSE(0 {VD} 0.566667u 0.1n 0.1n 49.9n 100n)
Vcm0 cm0 0 PULSE(0 {VD} 0.5u 0.1n 0.1n 49.9n 100n)
Vcm1 cm1 0 PULSE(0 {VD} 0.516667u 0.1n 0.1n 49.9n 100n)
Xp0 padp0 cp0 vdd 0 iovdd 0 sg13cmos5l_IOPadOut30mA
Xp1 padp1 cp1 vdd 0 iovdd 0 sg13cmos5l_IOPadOut30mA
Xm0 padm0 cm0 vdd 0 iovdd 0 sg13cmos5l_IOPadOut30mA
Xm1 padm1 cm1 vdd 0 iovdd 0 sg13cmos5l_IOPadOut30mA
Cpkp0 padp0 0 3p
Cpkp1 padp1 0 3p
Cpkm0 padm0 0 3p
Cpkm1 padm1 0 3p
Rp0 padp0 tp {RTX}
Rp1 padp1 tp {RTX}
Rm0 padm0 tm {RTX}
Rm1 padm1 tm {RTX}
L1p tp f1p 397.9n
L1m tm f1m 397.9n
C2 f1p f1m 159.2p
L3p f1p pp 247.9n
L3m f1m pm 247.9n
Rwp pp pp1 {RW+1m}
Lpri pp1 pm 350u
Lsec sp1 sm 350u
Kt Lpri Lsec 0.9995713
Rws sp1 sp {RW+1m}
Rdcs sm 0 1e6
Cww1 pp sp 10p
Cww2 pm sm 10p
Rl sp sm 100
Bdiff vdiff 0 V=V(sp)-V(sm)
.options method=gear reltol=1e-3
.tran 0.1n 3u 0 0.2n
.meas tran vmax MAX v(vdiff) from=1.5u to=3u
.meas tran vmin MIN v(vdiff) from=1.5u to=3u
.meas tran iovdd_avg AVG i(Vio) from=1.5u to=3u
.control
set hcopydevtype=svg
run
hardcopy eth_te_tx_ssb_il0.svg v(vdiff) xlimit 2u 2.4u
.endc
.end
