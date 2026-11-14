; onewire.s: 1-Wire bus controller firmware (standard speed) for one thread.
;
; What it does:
;   A protocol that the chip was not designed for: written from the 1-Wire
;   documents only, on the unchanged instruction set and assembler.
;   The thread takes each word from its host FIFO:
;     bit 8 = 1: reset pulse and presence detect. Reply: bit 0 = bus level
;                at the presence sample (0 = a device answered).
;     bit 8 = 0: one byte, bits 7 to 0, sent LSB first as a "touch byte":
;                a 1 bit is a write-1 slot that is also a read slot.
;                Reply: the 8 bus samples, bits 7 to 0. Send 0xFF to read
;                a byte; a written byte comes back unchanged.
;
; Timing (the .equ values below are cycles at 50 MHz, 50 cycles for each us;
; at 60 MHz the host assembles with TA 360, TE 540, TF 3300, TC 3900, TD 300,
; TI2 2100, H32 900, J32 769, docs/host_port.md 14): Analog Devices
; application note 126, "1-Wire Communication Through Software", Table 1
; (steps) and Table 2 (recommended standard-speed values, us): A 6, B 64,
; C 60, D 10, E 9, F 55, G 0, H 480, I 70, J 410.
;   Write 1 / read slot: low for A, release, sample at A + E, slot ends at
;   A + E + F = 70 us (= A + B).
;   Write 0 slot: low for C, release, slot ends at C + D = 70 us. C is 65 us,
;   not 60 us: the DS18B20 data sheet (19-7487 Rev 6, page 16) lets a device
;   sample a write slot up to 60 us after the falling edge, so a 60 us low
;   ends exactly at the end of that window (test_holdout_onewire: a device that
;   samples at 60 us reads the 0 as a 1). D is 5 us (tREC is 1 us minimum).
;   Reset: low for H, release, sample at H + I, the next slot starts at
;   H + I + J or later. I is two TADD of I/2: at 60 MHz I is 4,200 cycles,
;   more than the 4,095 of one TADD immediate (docs/isa.md 7).
; Each time comes from T (TADD), not from the time that the instructions
; take. SAMPLE at T + 2 reads the bus level at T (synchronizer, isa 6.2).
;
; Host configuration before RUN: output pin OW belongs to this thread,
; open-drain. The bus needs an external pull-up resistor (AN126 system
; requirement 1); the chip pins are 3.3 V.
; Registers: r0 = byte to send, r1 = bit counter and long times, r2 = samples.

        .equ    OW, UIO4        ; 1-Wire bus pin (output and input)
        .equ    TA, 300         ; A = 6 us
        .equ    TE, 450         ; E = 9 us
        .equ    TF, 2750        ; F = 55 us
        .equ    TC, 3250        ; C = 65 us (AN126: 60 us; see above)
        .equ    TD, 250         ; D = 5 us (AN126: 10 us), so that C + D stays 70 us
        .equ    TI2, 1750       ; I / 2 = 35 us
        .equ    H32, 750        ; H = 480 us = 750 x 32 cycles
        .equ    J32, 641        ; J = 410 us: 641 x 32 = 20,512 cycles (410.24 us)
        .equ    MARGIN, 32      ; the first OUT is 7 instructions after TSYNC: 7 x 4 + 4 = 32

start:  TNOW    MARGIN          ; T = now + MARGIN
idle:   TSYNC   MARGIN          ; keep T near now while the FIFO is empty (isa 7)
        PULL.NB r0              ; next host word; E = 1 if there was one
        BEC     idle
        LDI     r1, 256
        TST     r0, r1          ; bit 8: reset
        BNE     reset
        LDI     r1, 8           ; 8 slots
slot:   OUT     OW, 0           ; slot start: bus low at T
        SHR     r0, 1           ; C = next bit, LSB first
        BCC     wr0
        TADD    TA
        OUT     OW, Z           ; release after A
        TADD    TE
        SAMPLE  OW, r2>>, 2     ; bus level at A + E into bit 15 of r2
        TADD    TF              ; T = end of the slot
        JMP     next
wr0:    TADD    TC
        OUT     OW, Z           ; release after C
        SHR     r2, 1           ; sample bit 0 for a written 0
        TADD    TD              ; T = end of the slot
next:   DJNZ    r1, slot
        SHR     r2, 8           ; samples to bits 7 to 0
        PUSH    r2
        JMP     idle
reset:  OUT     OW, 0           ; reset pulse at T (G = 0)
        LDI     r1, H32
        SHL     r1, 5
        TADD    r1              ; T = T + H
        OUT     OW, Z           ; release
        TADD    TI2
        TADD    TI2             ; T = T + I
        SAMPLE  OW, r2>>, 2     ; presence sample at H + I
        LDI     r1, J32
        SHL     r1, 5
        TADD    r1              ; T = end of the reset sequence: the next slot starts here
        SHR     r2, 15          ; presence sample to bit 0
        PUSH    r2
        JMP     idle
