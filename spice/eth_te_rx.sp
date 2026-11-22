* 10BASE-Te receive front end of the demo-board module: RJ45 receive pair (driven here by an ideal differential
* source of peak A, a 10 MHz sine) -> 1:1 magnetics of the HR911105A (OCL 350 uH; insertion loss RW
* per winding: 0 = 0 dB, 12.2 ohm = -1.0 dB) -> 2 x 49.9 ohm termination to the bias node VB (3.3 V / 2 by
* 2 x 10 kohm, 100 nF) -> two TLV3501 comparators:
*   slicer:  +IN = RXP, -IN = RXM (zero crossing; chip input ui_in[0], SEL DIN)
*   squelch: +IN = RXP, -IN = VREF = 3.3 V x 10.0k / (7.87k + 10.0k) = 1.847 V = VB + 197 mV, i.e. a positive
*            differential threshold of 394 mV (chip input ui_in[1], SEL AUX0; the helper qualifies edge spacing).
*            The first try, 7.50k / 9.76k (432 mV), left only 4.6 mV of overdrive at 585 mV with -1.0 dB magnetics
*            loss; 197 mV is near the middle of the worst reject case (300 mV, 0 dB) and the worst accept case
*            (585 mV, -1.0 dB). Divider resistors 0.1 % (1 % would move VREF - VB by up to about 17 mV).
* IEEE 802.3 14.3.1.3.2: reject peaks below 300 mV; accept 585 mV and more (10BASE-T receiver; the 10BASE-Te
* receiver is the same, 14.3.1.3). The comparators are not simulated as devices: the deck measures the input
* overdrive of the squelch comparator (max V(RXP) - V(VREF)) and of the slicer, which must exceed the TLV3501 offset
* (6.5 mV max) plus half its 6 mV hysteresis (SBOS321E p.5) to switch, and stay below minus that to not switch.
.param A=0.585 RW=0
Vsup vcc 0 3.3
* bias and threshold dividers
Rb1 vcc vb 10k
Rb2 vb 0 10k
Cb vb 0 100n
Rt1 vcc vref 7.87k
Rt2 vref 0 10.0k
Ct vref 0 100n
* partner signal at the jack: 10 MHz sine of peak A (the fundamental of all-ones Manchester; zero mean, as a
* transformer-coupled line carries no DC), from 0.5 us
Vsrc lp lm SIN(0 {A} 10MEG 0.5u)
Rsrc lp jp 1
Rret lm jm 1m
* magnetics (1:1, RX winding pair of the jack)
Rwp jp jp1 {RW+1m}
Lpri jp1 jm 350u
Lsec s1 sm 350u
Kt Lpri Lsec 0.9995713
Rws s1 rxp {RW+1m}
Cww1 jp rxp 10p
Cww2 jm sm 10p
Rmid sm rxm 1m
Rtp rxp vb 49.9
Rtm rxm vb 49.9
Ccm vb 0 10n
Cinp rxp 0 4p
Cinm rxm 0 4p
Csq vref 0 2p
Bsq osq 0 V=V(rxp)-V(vref)
Bsl osl 0 V=V(rxp)-V(rxm)
.options method=gear reltol=1e-4
.control
set hcopydevtype=svg
foreach rw 0 12.2
  foreach a 0.300 0.585 1.0
    alterparam RW = $rw
    alterparam A = $a
    reset
    tran 0.2n 4u 0 0.5n
    echo "case rw=$rw a=$a"
    meas tran sq_over MAX v(osq) from=2u to=4u
    meas tran sl_max MAX v(osl) from=2u to=4u
    meas tran sl_min MIN v(osl) from=2u to=4u
    meas tran vref_dc AVG v(vref) from=2u to=4u
    meas tran vb_dc AVG v(vb) from=2u to=4u
  end
end
hardcopy eth_te_rx_a1p0_il1.svg v(rxp) v(rxm) v(vref) xlimit 3u 3.4u
.endc
.end
