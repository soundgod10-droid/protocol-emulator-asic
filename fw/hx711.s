; fw/hx711.s - HX711 24-bit load-cell ADC reader: data retrieval, input and gain selection, power down.
;
; What it does:
;   Reads an Avia Semiconductor HX711 through its two-wire interface (HX711
;   data sheet, "Serial Interface", "Reset and Power-Down", Table 3 and the
;   timing table under Fig. 2, page 5): PD_SCK = uo_out[4] (output pin 12, UO4)
;   and DOUT = ui_in[3] (input pin 11, UI3).
;   For each conversion word from the host the thread waits until DOUT is low
;   (data ready), then gives 25, 26 or 27 positive PD_SCK pulses: high
;   TH = 60 cycles = 1.0 us (T3: 0.2 to 50 us), low TL = 60 cycles = 1.0 us
;   (T4: 0.2 us minimum), the first rise at least 30 cycles = 0.5 us after the
;   thread saw DOUT low (T1: 0.1 us). It reads DOUT TS = 40 cycles after each of
;   the first 24 rises (667 ns; T2, the data delay after the rise, is 0.1 us at
;   most), MSB first. The number of pulses selects the input and gain of the
;   next conversion (Table 3: 25 channel A gain 128, 26 channel B gain 32,
;   27 channel A gain 64).
;   A power-down word holds PD_SCK high for 4,000 cycles = 66.7 us (Fig. 3:
;   more than 60 us) and longer, until the next host word; then PD_SCK goes
;   low and the HX711 resets (channel A, gain 128). The thread starts by
;   setting PD_SCK low; PD_SCK is high (the uo_out reset level) while the chip
;   is in reset, which for more than 60 us powers the HX711 down, so the start
;   is also an HX711 reset.
;
; Why:
;   The HX711 has no registers: the clock count after each read selects the
;   input and gain, a long clock-high is the power-down, and a PD_SCK high
;   longer than 50 us inside a read corrupts it. The firmware gives the exact
;   pulse count with fixed pulse widths and checks DOUT before each read.
;
; Host words to this thread:
;   bits 1..0: 1, 2 or 3 = 25, 26 or 27 pulses (input and gain of the next
;   conversion); bit 2 = 1: power down instead of a read (bits 1..0 are not
;   used); the next word ends the power-down.
; Words to the host, for each read: the 24-bit result in two's complement as
;   bits 23..16 sign-extended to 16 bits, then bits 15..0.
;   The thread has no time-out: it waits for DOUT low as long as it takes
;   (400 ms after a reset at 10 samples/s).
; Registers: r0 high byte and work, r1 low word, r2 counter, r3 host word.

        .equ    SCK, UO4
        .equ    DOUT, UI3
        .equ    TH, 60          ; PD_SCK high 1.0 us (T3: 0.2 to 50 us)
        .equ    TL, 60          ; PD_SCK low 1.0 us (T4: 0.2 us)
        .equ    TS, 40          ; DOUT read after the rise (T2: 0.1 us)
        .equ    T1, 30          ; first rise after DOUT low was seen (T1: 0.1 us)

        TSYNC   32
        OUT     SCK, 0          ; PD_SCK low: HX711 out of power-down (reset)
idle:   TSYNC   32
        PULL.NB r3
        BEC     idle
cmd:    MOV     r0, r3
        ANDI    r0, 4
        BEQ     rdy
        TSYNC   32
        OUT     SCK, 1          ; power down: PD_SCK high for more than 60 us
        TADD    4000
        TWAIT
        PULL    r3              ; until the next host word
        TSYNC   32
        OUT     SCK, 0          ; reset, normal mode
        JMP     cmd
rdy:    IOR     r0, PINS        ; DOUT low: data ready
        SHR     r0, 11
        ANDI    r0, 1
        BEQ     go
        TNOW    16
        TADD    4000
        WAIT    DOUT, FALL, r0, TO
        JMP     rdy
go:     TNOW    T1
        LDI     r2, 8           ; bits 23..16
hi:     OUT     SCK, 1
        TADD    TS
        SAMPLE  DOUT, r1<<
        TADD    TH-TS
        OUT     SCK, 0
        TADD    TL
        DJNZ    r2, hi
        MOV     r0, r1
        LDI     r2, 16          ; bits 15..0
lo:     OUT     SCK, 1
        TADD    TS
        SAMPLE  DOUT, r1<<
        TADD    TH-TS
        OUT     SCK, 0
        TADD    TL
        DJNZ    r2, lo
        ANDI    r3, 3           ; 1 to 3 more pulses: input and gain
ex:     OUT     SCK, 1
        TADD    TH
        OUT     SCK, 0
        TADD    TL
        DJNZ    r3, ex
        ANDI    r0, 255
        MOV     r2, r0
        ANDI    r2, 128
        BEQ     pos
        LDI     r2, 0           ; sign extension
        NOT     r2, r2
        SHL     r2, 8
        OR      r0, r2
pos:    PUSH    r0
        PUSH    r1
        JMP     idle
