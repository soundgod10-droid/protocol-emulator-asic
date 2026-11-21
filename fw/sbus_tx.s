; fw/sbus_tx.s - S.BUS transmitter (radio receiver side): 16 proportional channels, 2 digital channels, 2 flags.
;
; What it does:
;   Sends S.BUS frames on SBUS_TX = uo_out[4] (output pin 12, UO4) as the
;   S.BUS description of the RPG Quadrotor Control wiki (SBUS-Protocol page)
;   gives them: serial at 100,000 bit/s, 8E2 (start bit, 8 data bits LSB
;   first, even parity, 2 stop bits), voltage levels inverted (a "0" bit is
;   high, the idle line is low); 25 bytes back to back (3 ms): header 0x0F,
;   22 bytes with the 16 channels of 11 bits packed from the least significant
;   bit of channel 1 on, the flag byte (bit 0 channel 17, bit 1 channel 18,
;   bit 2 frame lost, bit 3 failsafe, bits 7..4 zero), footer 0x00. The bit time
;   is BIT = 600 cycles = 10.0 us at 60 MHz.
;   After each frame the line stays idle for at least GAPN x 4,000 cycles =
;   4 ms, so that a receiver finds the frame start (frames every 7 ms or more,
;   the high-speed rate; the host sends a frame every 14 ms for the analog
;   rate).
;   The firmware takes the 17 words of a frame into data memory before it
;   sends the frame, so a host that is slow never makes a gap inside a frame.
;
; Why:
;   S.BUS packs 11-bit values across byte borders, adds a parity bit to each
;   byte and inverts the line. The firmware does the packing and the parity,
;   so the host sends the channel values as numbers.
;
; Host words to this thread, for each frame:
;   16 words: channels 1 to 16 (bits 10..0; bits 15..11 are ignored);
;   1 word: bit 0 channel 17, bit 1 channel 18, bit 2 frame lost, bit 3
;   failsafe (bits 15..4 ignored).
;
; Words to the host: none.
; Registers: r0 byte, r1 work, r2 counter, r3 bit counter and return address.
; Memory: BUF..BUF+16 in data memory (the frame's words), TMP, CH, CB, PTR
;   after the code.

        .equ    TX, UO4
        .equ    BIT, 600        ; 10.0 us at 60 MHz (100,000 bit/s)
        .equ    GAPN, 60        ; idle after a frame: 60 x 4,000 cycles = 4.0 ms
        .equ    BUF, 512        ; frame buffer in data memory

        TSYNC   32
        OUT     TX, 0           ; idle line (inverted: low)
frame:  LDI     r2, BUF
        LDI     r3, 17
get:    PULL    r0              ; the 17 words of the next frame
        STX     r0, [r2]
        ADDI    r2, 1
        DJNZ    r3, get
        LDI     r1, BUF
        ST      r1, PTR
        LDI     r1, 0
        ST      r1, CB          ; no bits left: the first bit loads channel 1
        TSYNC   120             ; the first event comes 26 slots later
        LDI     r0, 0x0F        ; header
        JAL     r3, byte
        LDI     r2, 22          ; 22 bytes of channel bits
cbyte:  LDI     r0, 0
        LDI     r3, 8
cbit:   SHR     r0, 1           ; the next bit goes to bit 7 (LSB first)
        LD      r1, CB
        ADDI    r1, -1
        BMI     load
        ST      r1, CB
        JMP     take
load:   LD      r1, PTR         ; next channel word
        ADDI    r1, 1
        ST      r1, PTR
        ADDI    r1, -1
        LDX     r1, [r1]
        ST      r1, CH
        LDI     r1, 10          ; 11 bits, one is taken now
        ST      r1, CB
take:   LD      r1, CH
        SHR     r1, 1           ; C = the channel's next bit
        ST      r1, CH
        BCC     zero
        ORI     r0, 128
zero:   DJNZ    r3, cbit
        JAL     r3, byte
        DJNZ    r2, cbyte
        LDI     r1, BUF+16
        LDX     r0, [r1]
        ANDI    r0, 15          ; flag byte
        JAL     r3, byte
        LDI     r0, 0           ; footer
        JAL     r3, byte
        LDI     r2, GAPN
gap:    TWAIT                   ; idle after the frame
        TADD    4000
        DJNZ    r2, gap
        JMP     frame

; One byte (r0) at time T: start bit, 8 data bits, even parity, 2 stop bits,
; all inverted on the line. T ends at the end of the second stop bit.
byte:   ST      r0, TMP
        MOV     r1, r0          ; even parity: XOR of the 8 data bits
        SHR     r1, 4
        XOR     r0, r1
        MOV     r1, r0
        SHR     r1, 2
        XOR     r0, r1
        MOV     r1, r0
        SHR     r1, 1
        XOR     r0, r1
        ANDI    r0, 1
        XORI    r0, 1           ; line level of the parity bit
        SHL     r0, 9
        LD      r1, TMP
        XORI    r1, 255         ; line levels of the data bits
        SHL     r1, 1
        ORI     r1, 1           ; start bit: high
        OR      r1, r0          ; stop bits (bits 10, 11): low
        LDI     r0, 1
        SHL     r0, 12          ; end mark
        OR      r1, r0
bits:   OUT     TX, r1>>
        TADD    BIT
        CMPI    r1, 2
        BCS     bits
        JR      r3

TMP:    .word   0
CH:     .word   0
CB:     .word   0
PTR:    .word   0
