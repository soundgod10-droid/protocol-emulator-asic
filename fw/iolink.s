; fw/iolink.s - IO-Link (SDCI) Device, COM2 (38.4 kbit/s), M-sequence TYPE_0, Direct Parameter page 1.
;
; What it does:
;   The Device side of the SDCI link at the logic pins of a line transceiver:
;   RX = ui_in[3] (UI3, the receiver output, idle high), TX = uo_out[4] (UO4,
;   the driver input, idle high) and EN = uo_out[5] (UO5, driver enable, 1 =
;   the Device drives C/Q). IO-Link Interface and System Specification
;   V1.1.3 (June 2019):
;   - Wake-up (5.3.3.3, Table 10): a low pulse on RX of 60 to 100 us (the
;     Master's TWU is 75 to 85 us) switches from SIO to COM2; the Device is
;     ready at once (TREN <= 500 us).
;   - UART frames (Figure 21, Table 9): start bit, 8 data bits LSB first,
;     even parity, stop bit; TBIT = BIT cycles (1,563 = 26.05 us at 60 MHz,
;     +0.03 %; Table 9: 26.04 us). Each Master frame is sampled in the middle
;     of its bits from the start-bit fall; framing or parity errors drop the
;     message.
;   - M-sequence TYPE_0 (A.2.2): Master MC, CKT (and one OD octet for a
;     write); Device OD and CKS for a read, CKS for a write. MC bit 7 = read,
;     bits 6..5 channel, bits 4..0 address (A.1.2); only the page channel (1)
;     has data (other channels read 0, writes ignored, A.1.2). The checksum
;     (A.1.6): seed 52h XOR all octets with the checksum bits 0, compressed
;     from 8 to 6 bits with equations (A.1); a message with a wrong checksum
;     or another M-sequence type gets no reply.
;   - Timing (A.3): the next Master frame must start within TO1 after the stop
;     sample (t1 <= 1 TBIT, A.3.3); the reply starts tA = TAN bit times after
;     the end of the Master's last stop bit (1 <= tA <= 10, A.3.5), its frames
;     follow with t2 = 0 (A.3.4); EN rises 2 cycles before the start bit and
;     falls at the end of the last stop bit.
;   Page 1 (B.1) is in data memory: the host loads it before the wake-up; each
;   Master write to page 1 is stored and reported to the host.
;
; Why:
;   SDCI is a point-to-point UART protocol with a 6-bit checksum and a fixed
;   reply window; a timed thread meets tA and t2 with no UART peripheral.
;
; Host words to this thread (before the wake-up): bits 12..8 page address,
;   bits 7..0 value; bit 15 = the last configuration word.
; Words to the host: 8000h | address << 8 | value for each Master write to
;   page 1; 4000h | MC for a message with a wrong checksum; 2000h for a
;   framing or parity error (the message is dropped).
; Registers: r0 edge time / work, r1 octet, r2 count / work, r3 link.
; Memory: MC, CKT, OD, CK; data memory PG .. PG + 31 (page 1).

        .equ    RX, UI3
        .equ    TX, UO4
        .equ    EN, UO5
        .equ    BIT, 1563       ; TBIT (26.05 us)
        .equ    HB, 781         ; half a bit
        .equ    WTO, 3000       ; wake-up low time-out: 2 x WTO = 100 us
        .equ    WMIN, 225       ; wake-up low minimum: 225 x 16 cycles = 60 us
        .equ    TO1, 3900       ; stop sample to the time-out of the next start bit (2.5 bits)
        .equ    TAN, 2          ; tA in bit times
        .equ    SEED, 0x52      ; checksum seed (A.1.6)
        .equ    PG, 512         ; page 1 in data memory

        TNOW    16
        OUT     TX, 1
        OUT     EN, 0           ; the driver is off (uo pins are 1 from reset)
        LDI     r1, PG          ; clear page 1 (no defined content after power-up)
        LDI     r2, 32
        LDI     r0, 0
clr:    STX     r0, [r1]
        ADDI    r1, 1
        DJNZ    r2, clr
cfg:    PULL    r0              ; page 1 from the host
        MOV     r1, r0
        SHR     r1, 8
        ANDI    r1, 31
        LDI     r2, PG
        ADD     r1, r2
        MOV     r2, r0
        ANDI    r2, 255
        STX     r2, [r1]
        TST     r0, r0
        BMI     sio
        JMP     cfg

sio:    WAIT    RX, FALL, r0, CT ; wake-up request: a low pulse of 60 to 100 us
        TADD    WTO
        TADD    WTO
        WAIT    RX, RISE, r1, TO
        BEC     sio             ; low longer than 100 us
        SUB     r1, r0
        SHR     r1, 4
        CMPI    r1, WMIN
        BCC     sio             ; shorter than 60 us

msg:    WAIT    RX, FALL, r0, CT ; MC start bit (COM2 from here)
        JAL     r3, rxo
        ST      r1, MC
        XORI    r1, SEED
        ST      r1, CK
        JAL     r3, nxt         ; CKT
        ST      r1, CKT
        ANDI    r1, 0xC0
        BNE     msg             ; only M-sequence TYPE_0
        LD      r0, MC
        ANDI    r0, 0x80
        BNE     chk             ; read: no OD from the Master
        JAL     r3, nxt         ; write: OD
        ST      r1, OD
        LD      r2, CK
        XOR     r2, r1
        ST      r2, CK
chk:    LD      r1, CK
        JAL     r3, cmp6
        LD      r0, CKT
        ANDI    r0, 63
        CMP     r0, r1
        BNE     ckerr
        LD      r0, MC
        MOV     r2, r0
        SHR     r2, 5
        ANDI    r2, 3
        CMPI    r2, 1
        BNE     other
        MOV     r1, r0
        ANDI    r1, 31
        LDI     r2, PG
        ADD     r1, r2          ; r1 = page entry
        ANDI    r0, 0x80
        BNE     prd
        LD      r2, OD          ; page write: store and report
        STX     r2, [r1]
        LD      r0, MC
        ANDI    r0, 31
        SHL     r0, 8
        OR      r0, r2
        LDI     r2, 1
        SHL     r2, 15
        OR      r0, r2
        PUSH.NB r0
        JMP     wrep
prd:    LDX     r1, [r1]
        JMP     rrep
other:  ANDI    r0, 0x80
        BEQ     wrep            ; write to another channel: ignored
        LDI     r1, 0           ; read of another channel: 0
rrep:   ST      r1, OD          ; reply OD, CKS
        XORI    r1, SEED
        JAL     r3, cmp6        ; CKS: Event flag 0, PD status 0
        ST      r1, CK
        JAL     r3, tstart
        LD      r1, OD
        JAL     r3, txo
        LD      r1, CK
        JAL     r3, txo
        JMP     tend
wrep:   LDI     r1, SEED        ; reply CKS only
        JAL     r3, cmp6
        ST      r1, CK
        JAL     r3, tstart
        LD      r1, CK
        JAL     r3, txo
tend:   OUT     EN, 0, 2        ; driver off at the end of the last stop bit
        JMP     msg
ckerr:  LD      r0, MC
        LDI     r2, 1
        SHL     r2, 14
        OR      r0, r2
        PUSH.NB r0
        JMP     msg
ferr:   LDI     r0, 1
        SHL     r0, 13
        PUSH.NB r0
        JMP     msg

nxt:    TADD    TO1             ; next Master frame: start bit within TO1 after the stop sample
        WAIT    RX, FALL, r0, TO, CT
        BEC     msg             ; none: the message is dropped
rxo:    TADD    HB              ; one frame; T = its start-bit fall on entry, its stop sample on return
        LDI     r2, 10
rxb:    SAMPLE  RX, r1>>        ; start, d0 .. d7, parity
        TADD    BIT
        DJNZ    r2, rxb
        SAMPLE  RX, r1>>        ; stop bit (bit 15)
        BEC     ferr
        MOV     r2, r1
        ANDI    r2, 32          ; start bit (bit 5)
        BNE     ferr
        SHL     r1, 1
        SHR     r1, 7           ; d0 .. d7 in bits 0..7, parity in bit 8
        MOV     r2, r1          ; even parity over bits 0..8
        MOV     r0, r2
        SHR     r0, 8
        XOR     r2, r0
        MOV     r0, r2
        SHR     r0, 4
        XOR     r2, r0
        MOV     r0, r2
        SHR     r0, 2
        XOR     r2, r0
        MOV     r0, r2
        SHR     r0, 1
        XOR     r2, r0
        ANDI    r2, 1
        BNE     ferr
        ANDI    r1, 255
        JR      r3

cmp6:   MOV     r2, r1          ; 8 to 6 bit compression, equations (A.1); r1 in and out
        SHR     r2, 1
        XOR     r2, r1          ; bits 0, 2, 4, 6: D0, D1, D2, D3
        ANDI    r2, 0x55
        MOV     r0, r2
        SHR     r0, 1
        OR      r2, r0
        ANDI    r2, 0x33
        MOV     r0, r2
        SHR     r0, 2
        OR      r2, r0
        ANDI    r2, 0x0F
        MOV     r0, r1
        SHR     r0, 2
        XOR     r0, r1
        MOV     r1, r0
        SHR     r1, 4
        XOR     r1, r0          ; bit 0: D4, bit 1: D5
        ANDI    r1, 3
        SHL     r1, 4
        OR      r1, r2
        JR      r3

tstart: TADD    HB              ; T = the stop sample: end of the Master's stop bit
        LDI     r2, TAN
tsl:    TADD    BIT
        DJNZ    r2, tsl
        OUT     EN, 1           ; driver on, start bit 2 cycles later
        JR      r3

txo:    MOV     r2, r1          ; one frame of octet r1; T = its start; returns with T = its end
        MOV     r0, r2
        SHR     r0, 4
        XOR     r2, r0
        MOV     r0, r2
        SHR     r0, 2
        XOR     r2, r0
        MOV     r0, r2
        SHR     r0, 1
        XOR     r2, r0
        ANDI    r2, 1           ; even parity bit
        SHL     r2, 8
        OR      r1, r2
        LDI     r2, 512
        OR      r1, r2          ; stop bit
        OUT     TX, 0, 2        ; start bit
        LDI     r2, 10
txb:    TADD    BIT
        OUT     TX, r1>>, 2
        DJNZ    r2, txb
        TADD    BIT
        JR      r3

MC:     .word   0
CKT:    .word   0
OD:     .word   0
CK:     .word   0
