; fw/i2c_ctl.s - I2C controller: START, repeated START, STOP, write, read,
; ACK and NACK, clock stretching, arbitration loss.
;
; What it does:
;   Pins (open drain, external pull-ups): SCL = uio[6], SDA = uio[7]. Each
;   host byte word sends 9 bits on SDA (8 data bits and the ACK bit time)
;   and reads the bus level of each of the 9 bits. After each release of
;   SCL the thread waits for the SCL rise (WAIT RISE with CT), so a target
;   can stretch the clock, and the high time starts at the real rise.
;   Arbitration: for each bit where the host check word has a 1 (the
;   controller sends 1 and must see 1), a 0 on the bus stops the byte at
;   once: SCL and SDA stay released and the thread tells the host.
;   Times are set with .equ when the host assembles the file (Fast-mode at
;   50 MHz below). The times after a WAIT start at the synchronized edge,
;   2 cycles after the pin edge, so each real time is 2 cycles longer.
;
; Why:
;   G1 step 3a: measured word count of the full I2C controller of A1
;   section 3 and 8.2 for the memory size (decision D2, D4 item 13).
;
; Host words to this thread, two for each byte:
;   1. Byte word: bit 15 = START before the byte (repeated START in a
;      transfer), bits 14 to 6 = the 9 bits P8 to P0 (first bit at 14),
;      bit 5 = STOP after the byte.
;      Write: P = data, then 1 (released for the ACK of the target).
;      Read: P = eight 1 (released), then the ACK bit (0 ACK, 1 NACK).
;   2. Check word: bits 15 to 7 = the P bits that the controller sends as
;      1 and must see as 1 (write: P8 to P1; read: P0 if it is 1).
;   The first byte after STOP or start always has a START.
; Word to the host, one for each byte: bits 8 to 0 = bus levels of the 9
;   bits (write: bit 0 is the ACK of the target, 0 = ACK; read: bits 8 to 1
;   are the data). Bit 15 = 1: arbitration lost in this byte (in bits 1 to
;   8; a loss in bit 9, a NACK against an ACK, shows as bit 0 = 0).
;   After a loss the thread takes the next host word as a new transfer
;   with START: the host must not queue words after a byte that can lose.
; Limits of the .equ values (G1 model runs): TSMP 16 or more, THIGH2 + HD
;   36 or more. No input spike filter: a 40 ns spike on SCL or SDA breaks
;   a transfer (G1 test_fw_full_i2c_spikes).
; Registers: r0 byte word, r1 WAIT time and return address, r2 check
;   word, r3 bus levels and bit count mark.

        .equ    SCL, UIO6
        .equ    SDA, UIO7
        .equ    HD, 4           ; SDA change after SCL low (tHD;DAT 0 minimum)
        .equ    TLOW, 75        ; SCL low time (Fast-mode tLOW 1.3 us = 65 cycles)
        .equ    TSMP, 16        ; SDA sample after the SCL rise
        .equ    THIGH2, 32      ; SCL high after the sample (TSMP + THIGH2 >= tHIGH 0.6 us)
        .equ    TSUSTA, 32      ; tSU;STA 0.6 us
        .equ    THDSTA, 32      ; tHD;STA 0.6 us
        .equ    TSUSTO, 32      ; tSU;STO 0.6 us
        .equ    TBUF, 68        ; tBUF 1.3 us

idle:   JAL     r1, get
        OUT     SDA, 0          ; START: SDA low while SCL is high
start2: TADD    THDSTA
        OUT     SCL, 0
byte:   LDI     r3, 0x1FE
        SHL     r3, 6           ; 0x7F80: bit 15 stays 1 for 8 samples, then 0
bit:    OUT     SDA, r0<<, HD
        SHL     r2, 1           ; C = 1: check this bit
        TADD    TLOW
        OUT     SCL, 1
        WAIT    SCL, RISE, r1, CT ; T = SCL rise (clock stretching)
        TADD    TSMP
        SAMPLE  SDA, r3<<
        BES     ok
        BCS     lost            ; sent 1, bus 0: arbitration lost
ok:     TADD    THIGH2
        OUT     SCL, 0
        MOV     r3, r3
        BMI     bit
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
stop:   TSYNC   16              ; the last SCL low is 6 instructions back: lead for the next event
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
