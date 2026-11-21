; fw/dcc.s - DCC command station (NMRA Digital Command Control signal generator).
;
; What it does:
;   Makes the NMRA digital signal on two pins in opposite phase, DCC_A =
;   uo_out[4] (output pin 12, UO4) and DCC_B = uo_out[5] (output pin 13, UO5),
;   the two inputs of the power station's H-bridge (S-9.1 section 2: a bipolar
;   square wave; A - B is the track signal). Both pins change at the same clock
;   edge, so A and B are never equal while the signal runs.
;   Bits (S-9.1 Table 2.1): a "1" has two half bits of H1 = 3,480 cycles =
;   58.0 us (t1: 55 to 61 us), a "0" two half bits of 2 x H0 = 6,000 cycles =
;   100.0 us (t0: 95 to 123 us); the two halves of a bit are equal (t1d, t0d:
;   3 us at most). No stretched zeros.
;   Packets (S-9.2 section A): PRE = 16 preamble "1" bits, the packet start
;   bit "0", the address byte MSB first, then for each further byte a data byte
;   start bit "0" and the byte MSB first; the firmware adds the error detection
;   byte (XOR of all bytes before it, S-9.2 section B and RP-9.2.1) and the
;   packet end bit "1". Between packets and while the host has no packet, the
;   signal goes on with "1" bits (preamble), so the track never loses power
;   (S-9.1 2.4) and the stream continues after the packet end bit (S-9.2
;   note 3).
;   A byte that comes too late inside a packet ends the packet with "1" bits:
;   a decoder then sees no data byte start bit and an error byte that does not
;   match, and ignores the packet (S-9.2 section C).
;
; Why:
;   DCC codes bits by the duration of the half periods of a power waveform,
;   not by levels sampled at a fixed rate, and frames bytes with start bits and
;   a check byte. The firmware times every half bit to the cycle and computes
;   the check byte, so the host sends only the packet bytes.
;
; Host words to this thread:
;   One word for each byte of a packet, bits 7..0; bit 8 = 1 on the last byte
;   (then the firmware sends the error byte and the end bit). The host keeps the
;   S-9.2 rules on content and repetition (5 ms between two packets to one
;   address; a packet at least every 30 ms if decoders need it).
;
; Words to the host: none.
; Registers: r0 bits to send (MSB first), r1 XOR of the packet's bytes,
;   r2 bits left, r3 host word, or the next step after the bits (bits 9..8).

        .equ    A, UO4
        .equ    B, UO5
        .equ    H1, 3480        ; "1" half bit: 58.0 us at 60 MHz
        .equ    H0, 3000        ; half of a "0" half bit: 2 x 3,000 = 6,000 cycles = 100.0 us
        .equ    PRE, 16         ; preamble bits after the packet end bit

        TSYNC   32              ; T near now at the start (isa 7)
pre:    LDI     r3, 0x200       ; after the preamble: look for a packet
        LDI     r1, 0
        LDI     r2, PRE
ones:   LDI     r0, 0
        NOT     r0, r0          ; "1" bits
send:   OUT     A, 1            ; first half of the bit at T
        OUT     B, 0
        SHL     r0, 1           ; C = the bit
        BCS     one
        TADD    H0
        TADD    H0
        OUT     A, 0            ; last half of a "0"
        OUT     B, 1
        TADD    H0
        TADD    H0
        DJNZ    r2, send
        JMP     next
one:    TADD    H1
        OUT     A, 0            ; last half of a "1"
        OUT     B, 1
        TADD    H1
        DJNZ    r2, send
next:   MOV     r0, r3
        SHR     r0, 8           ; 0: a byte was sent, 1: the last byte, 2: preamble, 3: the end bit
        BEQ     get
        CMPI    r0, 2
        BEQ     poll
        BCS     pre             ; the packet end bit was sent
        MOV     r0, r1          ; error detection byte: start bit 0, the XOR byte,
        SHL     r0, 7
        ORI     r0, 64          ; and the packet end bit 1
        LDI     r2, 10
        LDI     r3, 0x300
        JMP     send
poll:   PULL.NB r3              ; first byte of a packet?
        BES     byte
        LDI     r2, 1           ; no: one more preamble bit
        JMP     ones
get:    PULL.NB r3              ; next byte of the packet
        BEC     pre             ; too late: end the packet with "1" bits
byte:   MOV     r0, r3
        ANDI    r0, 255
        XOR     r1, r0
        SHL     r0, 7           ; start bit 0, then the byte MSB first
        LDI     r2, 9
        JMP     send
