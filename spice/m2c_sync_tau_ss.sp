* M2c: metastability time constant tau of sg13cmos5l_dfrbpq_1, slow corner
* (mos_ss, 1.08 V, 125 C: the STA slow corner nom_slow_1p08V_125C).
* Same method as m2c_sync_tau.sp (see that file); only the corner changes.
.lib cornerMOSlv.lib mos_ss
.include sg13cmos5l_stdcell.spice
.param vdd=1.08
.temp 125
.param td=9.9n

VDD vdd 0 {vdd}
VSS vss 0 0
VR rb 0 PWL(0 0 2n 0 2.05n {vdd})
VCLK clk 0 PWL(0 0 10n 0 10.05n {vdd})
VD d 0 PWL(0 0 {td} 0 {td+50p} {vdd})
X1 q clk d rb vdd vss sg13cmos5l_dfrbpq_1
X2 q2 clk q rb vdd vss sg13cmos5l_dfrbpq_1
CL q 0 2f

.option reltol=1e-6 abstol=1e-15 vntol=1e-9
.control
set lo = -300e-12
set hi = 300e-12
let k = 0
while k < 44
  let mid = ($lo + $hi) / 2
  let tdv = 10e-9 - mid
  set tds = $&tdv
  alterparam td = $tds
  reset
  tran 0.5p 14n
  meas tran qe FIND v(q) AT=13.9n
  if qe > 0.54
    set hi = $&mid
  else
    set lo = $&mid
  end
  let k = k + 1
end
echo "RESULT dly0_lo $lo dly0_hi $hi"
foreach delta 1e-11 3e-12 1e-12 3e-13 1e-13 3e-14 1e-14 3e-15 1e-15
  let dv = $hi + $delta
  let tdv = 10e-9 - dv
  set tds = $&tdv
  alterparam td = $tds
  reset
  tran 0.5p 14n
  meas tran tq WHEN v(q)=0.54 RISE=1
  let tcq = tq - 10.025e-9
  echo "RESULT delta $delta tcq $&tcq"
end
.endc
.end
