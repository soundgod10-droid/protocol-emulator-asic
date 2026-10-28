* M2c: metastability time constant tau of sg13cmos5l_dfrbpq_1.
*
* What it does:
*   One rising clock edge at 10 ns (50 % point 10.025 ns). The D input
*   rises at 10 ns - dly (same 50 ps edge, so dly is the 50 % to 50 %
*   offset). A bisection finds the offset dly0 where Q just fails to go
*   high. Then the clock-to-Q delay tcq is measured at dly0 + delta for
*   delta = 10 ps to 0.001 ps. Near dly0, tcq = t0 - tau x ln(delta), so
*   tau is the slope of tcq against ln(delta).
*
* Why:
*   A1 section 9.4 and G1 register A-OWN-25 / A-OPEN-28: the PDK has no
*   metastability data, and the number of synchronizer stages of the host
*   SPI port (2 or 3) needs the MTBF = exp(tr / tau) / (T0 x fclk x fdata).
*
* Port order (read from the expanded SPICE deck, m2c_probe.sp): the
* dfrbp cells are "q [q_n] clk d reset_b vdd vss".
*
* Corner: change the .lib line and vdd / temp for the other corners.
.lib cornerMOSlv.lib mos_tt
.include sg13cmos5l_stdcell.spice
.param vdd=1.2
.temp 25
.param td=9.9n

VDD vdd 0 {vdd}
VSS vss 0 0
VR rb 0 PWL(0 0 2n 0 2.05n {vdd})
VCLK clk 0 PWL(0 0 10n 0 10.05n {vdd})
VD d 0 PWL(0 0 {td} 0 {td+50p} {vdd})
X1 q clk d rb vdd vss sg13cmos5l_dfrbpq_1
* Load: one dfrbpq D input (the second synchronizer stage) and wire.
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
  if qe > 0.6
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
  meas tran tq WHEN v(q)=0.6 RISE=1
  let tcq = tq - 10.025e-9
  echo "RESULT delta $delta tcq $&tcq"
end
.endc
.end
