; fw/i2c_ctl_fmp.s - I2C controller with Fast-mode Plus timing (SCL 1 MHz at 60 MHz): START, repeated START,
; STOP, write, read, ACK and NACK, arbitration loss, clock stretching after each acknowledged byte.
;
; What it does:
;   The host words, the replies and the pins are those of fw/i2c_ctl.s (SCL = uio[6], SDA = uio[7], open drain,
;   external pull-ups). The difference is the bit loop. fw/i2c_ctl.s waits for the SCL rise after every SCL
;   release, which costs 7 cycles of input filter and up to one slot each bit: too slow for a 60-cycle bit. Here
;   only the first bit of each byte waits for the SCL rise; bits 2 to 9 are timed from T with no wait, so each
;   of them is TLOW + THIGH = 60 cycles (1.000 us). A target that holds SCL low after it has acknowledged
;   a byte (byte-level clock stretching, NXP UM10204 Rev. 7.0, 3.1.9) is seen, because the first bit of the next
;   byte, the repeated START and the STOP wait for the SCL rise. A target that stretches SCL inside a byte (bit
;   level) is not seen: the controller then shortens the target's HIGH period. Use this program only with targets
;   that do not stretch inside a byte; fw/i2c_ctl.s handles both.
;   Bit timing (60 MHz, .equ below), against UM10204 Table 11, Fast-mode Plus column (PDF page 44):
;     tLOW = TLOW = 33 cycles = 0.550 us (0.5 us minimum); tHIGH = TSMP + THIGH2 = 27 cycles = 0.450 us (0.26 us);
;     the SDA change comes HD = 12 cycles (0.200 us) after the SCL fall: tHD;DAT 0 minimum, tVD;DAT 0.45 us
;     maximum, tSU;DAT = TLOW - HD = 21 cycles = 0.350 us (50 ns minimum); tHD;STA, tSU;STA, tSU;STO 16 cycles or
;     more (0.26 us), tBUF 30 cycles or more (0.5 us). Times after a WAIT start at the filtered edge, 7 cycles
;     after the pin edge (synchronizer and spike filter of SCL and SDA, K = 5, docs/isa.md 6.1).
;   The SDA sample of bits 2 to 9 is taken at the SCL release + TSMP; with the 7-cycle filter it reads SDA as it
;   was 29 cycles (0.483 us) after the SCL fall, so a target's data must be valid within 0.483 us of the SCL fall
;   at the chip pins (UM10204: tVD;DAT 0.45 us maximum, plus any delay between the target and the chip pins).
;
; Why:
;   C2-7 of the close plan: Fast-mode Plus timing in firmware. The pads cannot drive Fast-mode Plus currents
;   (docs/verification.md section 9: 7.6 mA at 0.4 V at the slow corner, 20 mA needed), so a bus at 1 MHz needs
;   an Fm+ buffer between the chip pins and the bus (docs/info.md, External hardware).
;
; Limits of the .equ values (found in model runs): THIGH2 24 or more (the SCL fall event is issued 4 slots after
;   the SAMPLE completes); HD 12 or more; TLOW + TSMP + THIGH2 is the bit time of bits 2 to 9; TSMPW 16 or more.
; Registers: r0 byte word, r1 WAIT time and return address, r2 check word, r3 bus levels and bit count mark.

        .equ    SCL, UIO6
        .equ    SDA, UIO7
        .equ    HD, 7           ; SDA change after the SCL fall (0.117 us; an OUT offset, 0 to 7)
        .equ    TLOW, 35        ; SCL low time of bits 2 to 9 (tLOW 0.5 us = 30 cycles)
        .equ    TSMP, 0         ; SDA sample at the SCL release (bits 2 to 9; a SAMPLE offset, 0 to 7)
        .equ    THIGH, 25       ; SCL high time of bits 2 to 9 (tHIGH 0.26 us = 16 cycles)
        .equ    TSMPW, 16       ; SDA sample after the filtered SCL rise (bit 1, after the WAIT)
        .equ    THIGH2W, 26     ; SCL high after the sample of bit 1
        .equ    TSUSTA, 16      ; tSU;STA 0.26 us
        .equ    THDSTA, 20      ; tHD;STA 0.26 us (20: lead of the first SDA event after a repeated START)
        .equ    TSUSTO, 16      ; tSU;STO 0.26 us
        .equ    TBUF, 30        ; tBUF 0.5 us

idle:   JAL     r1, get
        OUT     SDA, 0          ; START: SDA low while SCL is high
start2: TADD    THDSTA
        OUT     SCL, 0
byte:   LDI     r3, 0x1FE
        SHL     r3, 6           ; 0x7F80: bit 15 stays 1 for 8 samples, then 0
        OUT     SDA, r0<<, HD   ; bit 1: wait for the SCL rise (a target may stretch after the ACK)
        SHL     r2, 1           ; C = 1: check this bit
        TADD    TLOW
        OUT     SCL, 1
        WAIT    SCL, RISE, r1, CT ; T = SCL rise
        TADD    TSMPW
        SAMPLE  SDA, r3<<
        BES     ok1
        BCS     lost            ; sent 1, bus 0: arbitration lost
ok1:    TADD    THIGH2W
        OUT     SCL, 0
        LDI     r1, 8           ; bits 2 to 9
bit:    OUT     SDA, r0<<, HD   ; timed from T (the SCL fall), no wait: 12 slots = 48 of the 60 cycles
        SHL     r2, 1
        TADD    TLOW            ; T = SCL release
        OUT     SCL, 1
        SAMPLE  SDA, r3<<, TSMP
        BES     ok
        BCS     lost
ok:     TADD    THIGH           ; T = SCL fall
        OUT     SCL, 0
        DJNZ    r1, bit
        PUSH    r3
        SHL     r0, 1           ; C = STOP
        BCS     stop
        JAL     r1, get
        BCC     byte            ; no START: next byte
        OUT     SDA, 1, HD      ; repeated START: SDA high while SCL is low
        TADD    TLOW
        OUT     SCL, 1
        WAIT    SCL, RISE, r1, CT
        TADD    TSUSTA
        OUT     SDA, 0
        JMP     start2
stop:   TSYNC   16              ; lead for the next event
        OUT     SDA, 0, HD
        TADD    TLOW
        OUT     SCL, 1
        WAIT    SCL, RISE, r1, CT
        TADD    TSUSTO
        OUT     SDA, 1
        TADD    TBUF
        JMP     idle
lost:   PUSH    r3              ; bit 15 = 1 (count mark not complete)
        JMP     idle
get:    TSYNC   16              ; keep T near now while there is no word (isa 7)
        PULL.NB r0
        BEC     get
        PULL    r2
        TSYNC   24
        SHL     r0, 1           ; C = START; P to bits 15 to 7, STOP to bit 6
        JR      r1
