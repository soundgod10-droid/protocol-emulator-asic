; fw/unio.s - UNI/O bus master: standby pulse, start header, bytes with the MAK/SAK acknowledge sequence.
;
; What it does:
;   Drives the single-wire UNI/O bus (Microchip UNI/O Bus Specification
;   DS22076D, 2009) on SCIO = uio[0] (UIO0) with a pull-up on the board (2.0,
;   2.3: idle high; the master drives push-pull and releases the line while a
;   slave sends). Manchester bits of TE = 2 x HB cycles (default 1,200 cycles
;   = 20 us, 50 kbit/s): first half the inverted bit, second half the bit, so
;   the middle edge rises for 1 and falls for 0 (4.0); bytes MSB first (5.0),
;   each followed by MAK (master: 1 continue, 0 NoMAK) and SAK (slave: a
;   rising middle edge; no edge = NoSAK) (5.1). Start header: line high for
;   TSS, low for THDR, then 55h with MAK (6.0). Standby pulse: high for STBN
;   x 4,000 cycles (666.7 us; TSTBY >= 600 us, 3.0); before the first one
;   after reset a low pulse gives the low-to-high edge needed after power-up.
;   Bits from the slave are found by edge time, not at fixed sample points,
;   so that the slave's output edge jitter (+/-0.25 UI in the 11XX data
;   sheet) is covered: the thread waits for edges; an edge earlier than a
;   quarter bit before the middle is a bit-start edge and is skipped; the
;   first later edge is the middle edge and the level SD cycles after it is
;   the bit. No edge up to 0.3 TE after the middle: a missing edge (NoSAK for
;   the acknowledge bit; error flag for a data bit).
;
; Why:
;   UNI/O devices (serial EEPROMs, sensors) need one pin; the master sets the
;   bit period and keeps it within one command, which a timed thread does
;   without a clock pin.
;
; Host words to this thread, one per byte:
;   bits 7..0  byte to send (ignored for a read)
;   bit 8      MAK after the byte (1) or NoMAK (0, ends the command)
;   bit 9      read: the slave sends this byte
;   bit 10     start header before the byte (line high TSS first)
;   bit 11     standby pulse and start header before the byte
;   The words of one command must be in the FIFO in time (the next byte
;   starts right after the SAK; there is no hold): a late word makes the
;   device leave the command (it sees no edge), and the next command needs a
;   standby pulse.
; Words to the host, one per byte (the start header's 55h too):
;   bits 7..0  the byte on the line (sent or read), bit 8 SAK seen,
;   bit 15     a data bit of a read byte had no middle edge.
; Registers: r0 host word / edge time, r1 byte, r2 inverted byte / window
;   start, r3 bit count / link.
; Memory: PEND (word waiting behind its start header), MAKF, RES, CNT, ERRF.

        .equ    SCIO, UIO0
        .equ    HB, 600         ; half bit: TE = 1,200 cycles = 20 us
        .equ    QE, 300         ; TE / 4
        .equ    HBQ, 300        ; HB - QE: bit start to the middle-edge window
        .equ    TOW, 660        ; window start to the time-out: TE / 4 + 0.3 TE
        .equ    SD, 40          ; middle edge to the sample of the bit
        .equ    THDR, 600       ; start header low pulse (THDR >= 5 us)
        .equ    TSS, 1200       ; line high before a start header (TSS >= 10 us)
        .equ    TLO, 600        ; low pulse before the standby pulse
        .equ    STBN, 10        ; standby pulse high STBN x 4,000 cycles

        LDI     r0, 0           ; SCIO is released from reset (pull-up: high)
        ST      r0, PEND
        TSYNC   32
get:    LD      r0, PEND
        MOV     r0, r0
        BNE     pend            ; the byte behind a start header
wget:   TSYNC   8
        PULL.NB r0
        BEC     wget
        ST      r0, PEND
        MOV     r1, r0
        SHR     r1, 11
        ANDI    r1, 1
        BEQ     nostb
        TSYNC   16
        LD      r1, PORF
        MOV     r1, r1
        BNE     stb1
        OUT     SCIO, 0         ; first standby pulse after reset: a low pulse before it
        TADD    TLO
        LDI     r1, 1
        ST      r1, PORF
stb1:   OUT     SCIO, 1         ; standby pulse
        LDI     r2, STBN
stbl:   TADD    4000
        TWAIT
        DJNZ    r2, stbl
        JMP     hdr
nostb:  LD      r0, PEND
        MOV     r1, r0
        SHR     r1, 10
        ANDI    r1, 1
        BEQ     pend            ; no header: the byte itself
        TSYNC   16
        OUT     SCIO, 1         ; line high TSS before the start header
        TADD    TSS
hdr:    OUT     SCIO, 0         ; start header: low THDR, then 55h with MAK
        TADD    THDR
        LDI     r0, 0x155
        JMP     byte
pend:   LDI     r1, 0
        ST      r1, PEND
byte:   MOV     r1, r0          ; T = the bit start of the byte's MSB
        SHR     r1, 8
        ANDI    r1, 1
        ST      r1, MAKF
        LDI     r1, 0
        ST      r1, ERRF
        MOV     r1, r0
        SHR     r1, 9
        ANDI    r1, 1
        BNE     rbyte
        ANDI    r0, 255
        ST      r0, RES
        MOV     r1, r0
        SHL     r1, 8
        NOT     r2, r1
        LDI     r3, 8
sbit:   OUT     SCIO, r2<<      ; first half: the inverted bit
        TADD    HB
        OUT     SCIO, r1<<      ; second half: the bit (middle edge)
        TADD    HB
        DJNZ    r3, sbit
        JMP     mak
rbyte:  OUT     SCIO, Z         ; the slave drives the 8 bits
        LDI     r1, 0
        LDI     r0, 8
        ST      r0, CNT
rbl:    JAL     r3, rbit
        LD      r0, CNT
        ADDI    r0, -1
        ST      r0, CNT
        BNE     rbl
        LD      r0, ERRF
        SHL     r0, 15
        OR      r1, r0
        ST      r1, RES
mak:    LD      r1, MAKF        ; acknowledge sequence: MAK from the master
        NOT     r2, r1
        OUT     SCIO, r2
        TADD    HB
        OUT     SCIO, r1
        TADD    HB
        OUT     SCIO, Z         ; SAK from the slave
        LDI     r1, 0
        ST      r1, ERRF
        JAL     r3, rbit
        LD      r0, ERRF
        XORI    r0, 1
        AND     r1, r0          ; SAK = a rising middle edge
        SHL     r1, 8
        LD      r0, RES
        OR      r0, r1
        PUSH    r0
        JMP     get

rbit:   TADD    HBQ             ; one bit from the slave; T = its bit start on entry, the next bit start on return
        TGET    r2              ; r2 = window start (middle - TE / 4)
        TADD    TOW             ; time-out (middle + 0.3 TE)
rw:     WAIT    SCIO, ANY, r0, TO
        BEC     rto
        SUB     r0, r2
        BMI     rw              ; before the window: a bit-start edge
        ADD     r0, r2
        TSET    r0              ; T = the middle edge
rs:     TADD    SD
        SAMPLE  SCIO, r1<<      ; the level after the middle edge is the bit
        TSET    r2
        TADD    HB
        TADD    QE
        JR      r3
rto:    LDI     r0, 1           ; no middle edge
        ST      r0, ERRF
        JMP     rs

PEND:   .word   0
MAKF:   .word   0
RES:    .word   0
CNT:    .word   0
ERRF:   .word   0
PORF:   .word   0
