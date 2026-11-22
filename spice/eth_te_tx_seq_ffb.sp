* 10BASE-Te transmit of the demo-board module: the fast-corner maximum with the alternating data pattern 1, 0, 1, 0
* (CD1 CD0: pulses of 2 half bits, 5 MHz), which settles longer than all-ones and gives the highest peak through the
* filter, for RTX 56 to 71.5 ohm and magnetics loss 0 dB. Network and assumptions as spice/eth_te_tx_ssb_il0.sp.
* Corner ffb: fast MOS, best-case resistors, -40 C, core 1.32 V, IO 3.4 V.
.lib cornerMOSlv.lib mos_ff
.lib cornerMOShv.lib mos_ff
.lib cornerDIO.lib dio_tt
.lib cornerRES.lib res_bcs
.include sg13cmos5l_io.spice
.temp -40
.param VD=1.32 VIO=3.4 RTX=71.5 RW=0
Vdd vdd 0 {VD}
Vio iovdd 0 {VIO}
Vcp0 cp0 0 PULSE(0 {VD} 0.55u 0.1n 0.1n 99.9n 200n)
Vcp1 cp1 0 PULSE(0 {VD} 0.566667u 0.1n 0.1n 99.9n 200n)
Vcm0 cm0 0 PULSE(0 {VD} 0.45u 0.1n 0.1n 99.9n 200n)
Vcm1 cm1 0 PULSE(0 {VD} 0.466667u 0.1n 0.1n 99.9n 200n)
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
.control
set hcopydevtype=svg
foreach rtx 56 66.5 68.1 71.5 69.8
  alterparam RTX = $rtx
  reset
  tran 0.1n 3u 0 0.2n
  echo "case corner=ffb pattern=1010 rw=0 rtx=$rtx"
  meas tran vmax MAX v(vdiff) from=1.5u to=3u
  meas tran vmin MIN v(vdiff) from=1.5u to=3u
end
hardcopy eth_te_tx_seq_ffb_r69p8.svg v(vdiff) xlimit 2u 2.8u
.endc
.end
