; fw/sbus_rx.s - S.BUS receiver (flight controller side).
;
; What it does:
;   Receives S.BUS frames on SBUS_RX = ui_in[3] (input pin 11, UI3) as the
;   S.BUS description of the RPG Quadrotor Control wiki (SBUS-Protocol page)
;   gives them: 100,000 bit/s, 8E2, voltage levels inverted (a "0" bit is
;   high, the idle line is low); 25 bytes: header 0x0F, 22 bytes with 16
;   channels of 11 bits (channel 1 from bit 0 of the first data byte on), the
;   flag byte (bits 7..4 zero), footer 0x00.
;   A frame starts with the first start bit after at least 4 x 4,000 cycles =
;   267 us of low line (more than two byte times, so the start of a frame is
;   never taken inside one). Each bit is sampled in its middle at the nominal
;   bit time (BIT = 600 cycles = 10.0 us) from the start edge of its byte;
;   each byte's start bit, both stop bits and the even parity are checked, and
;   each next start edge must come within one bit time after the end of the
;   byte before.
;   After a frame with 25 good bytes, header 0x0F, footer 0x00 and flag bits
;   7..4 zero, the thread gives the host the 16 channel values and the flag
;   byte. Any other frame is dropped (no words), and the receiver waits for the
;   next idle line.
;
; Why:
;   The receiving side of S.BUS: the thread finds frames by the idle gap,
;   checks every byte as a UART with parity does, and unpacks the 11-bit values
;   from the byte stream, so the host gets channel numbers.
;
; Host words to this thread: none.
; Words to the host, for each good frame: channels 1 to 16 (bits 10..0), then
;   the flag byte (bit 0 channel 17, bit 1 channel 18, bit 2 frame lost, bit 3
;   failsafe).
; Registers: r0 bits of a byte or a channel, r1 work and return address,
;   r2 pointer, r3 counter.
; Memory: BUF..BUF+24 in data memory (the frame's bytes), TMP, RET, NC after
;   the code.

        .equ    RX, UI3
        .equ    BIT, 600        ; 10.0 us at 60 MHz
        .equ    HALF, 300
        .equ    BUF, 512        ; frame bytes in data memory

sync:   TNOW    8               ; the line must stay low for 4 x 4,000 cycles
        TADD    4000
        TADD    4000
        TADD    4000
        TADD    4000
        WAIT    RX, ANY, r1, TO
        BES     sync            ; an edge: not idle yet
        IOR     r1, PINS
        SHR     r1, 11
        ANDI    r1, 1
        BNE     sync            ; the line is high: not idle
        LDI     r2, BUF
        LDI     r3, 25
        WAIT    RX, RISE, r1, CT        ; start bit of the header
        JMP     first
next:   TADD    300             ; T: one bit after the expected next start edge
        WAIT    RX, RISE, r1, TO, CT
        BEC     last            ; no next byte
        JAL     r1, store       ; check and keep the byte before
        BMI     sync            ; more than 25 bytes
first:  TADD    HALF            ; T = middle of the start bit
        LDI     r1, 12
smp:    SAMPLE  RX, r0>>        ; start bit, 8 data bits, parity, 2 stop bits
        TADD    BIT
        DJNZ    r1, smp
        JMP     next
last:   JAL     r1, store       ; the last byte
        MOV     r3, r3
        BNE     sync            ; not 25 bytes
        LDI     r1, BUF
        LDX     r0, [r1]
        CMPI    r0, 0x0F
        BNE     sync            ; header
        LDI     r1, BUF+24
        LDX     r0, [r1]
        CMPI    r0, 0
        BNE     sync            ; footer
        LDI     r1, BUF+23
        LDX     r0, [r1]
        CMPI    r0, 16
        BCS     sync            ; flag bits 7..4 not zero
        LDI     r2, BUF+1       ; unpack the 16 channels
        LDI     r1, 0           ; no bits of a byte left
        LDI     r0, 16
        ST      r0, NC
        LDI     r0, 0
        LDI     r3, 11
ubit:   CMPI    r1, 2
        BCS     have
        LDX     r1, [r2]        ; next byte, with an end mark above its 8 bits
        ADDI    r2, 1
        ADDI    r1, 256
have:   SHR     r1, 1           ; C = the next bit (LSB first)
        BCC     uz
        ORI     r0, 1
uz:     ROR     r0, 1           ; the bits gather from bit 15 down
        DJNZ    r3, ubit
        SHR     r0, 5           ; channel value, bits 10..0
        PUSH    r0
        LD      r0, NC
        ADDI    r0, -1
        ST      r0, NC
        LDI     r0, 0
        LDI     r3, 11
        BNE     ubit            ; flags of ADDI
        LDI     r1, BUF+23
        LDX     r0, [r1]
        PUSH    r0              ; flag byte
        JMP     sync

; Check the 12 samples in r0 (line levels, inverted) and keep the data byte at
; [r2]; r2 + 1, r3 - 1 (flags of r3 on return). A bad byte drops the frame.
store:  ST      r1, RET
        NOT     r0, r0          ; logical bits
        SHR     r0, 4           ; bit 0 start, 1-8 data, 9 parity, 10-11 stop bits
        MOV     r1, r0
        ANDI    r1, 1
        BNE     sync            ; start bit not 0
        MOV     r1, r0
        SHR     r1, 10
        CMPI    r1, 3
        BNE     sync            ; a stop bit is 0
        SHR     r0, 1           ; data bits 0-7, parity bit 8
        ST      r0, TMP
        MOV     r1, r0
        SHR     r1, 8
        XOR     r0, r1          ; bit 0: d0 xor parity
        ANDI    r0, 255
        MOV     r1, r0
        SHR     r1, 4
        XOR     r0, r1
        MOV     r1, r0
        SHR     r1, 2
        XOR     r0, r1
        MOV     r1, r0
        SHR     r1, 1
        XOR     r0, r1
        ANDI    r0, 1
        BNE     sync            ; odd number of ones: parity error
        LD      r0, TMP
        ANDI    r0, 255
        STX     r0, [r2]
        ADDI    r2, 1
        LD      r1, RET
        ADDI    r3, -1
        JR      r1

TMP:    .word   0
RET:    .word   0
NC:     .word   0
