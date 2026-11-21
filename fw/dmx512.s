; fw/dmx512.s - DMX512-A controller (transmitter of DMX512 packets), 250 kbit/s.
;
; What it does:
;   Sends DMX512 packets on DMX_TX = uo_out[4] (output pin 12, UO4, push-pull) to
;   the DI input of an EIA-485 transceiver (DE tied high: a controller always
;   drives the link). Each packet is the reset sequence and the slots of ANSI
;   E1.11-2008 (R2018) clauses 8.1 to 8.10:
;     BREAK (line low) 176 us        = 4 x BRK4 cycles  (Table 6 #1: 92 us or more)
;     MARK AFTER BREAK 16 us         = MAB cycles       (Table 6 #2: 12 us to < 1 s)
;     slot 0 (START Code), then the data slots, each 1 start bit (low), 8 data
;     bits LSB first, 2 stop bits (high) (Table 5), bit time BIT = 240 cycles =
;     4.000 us at 60 MHz (Table 6: 3.92 to 4.08 us), back to back.
;   Break-to-break time (Table 6 #11: 1,204 us or more): a packet with fewer
;   than NMIN slots is followed by mark for the missing slot times, so that
;   each packet lasts at least 176 + 16 + 24 x 44 = 1,248 us (44 us margin, so
;   that a clock up to 3.6 % fast still meets 1,204 us).
;   If the next slot word is late, the line stays mark between the slots
;   (Table 6 #9: 0 to < 1 s); if the next packet is late, it stays mark before
;   the BREAK (#10). The host must send a packet at least once a second
;   (#11: break-to-break at most 1 s).
;
; Why:
;   DMX512 is a UART-framed link with its own packet framing: a BREAK that a
;   UART cannot make, a minimum mark after it, and a packet-rate limit. The
;   firmware makes the frames itself from the slot values, so the host sends
;   only the values.
;
; Host words to this thread:
;   1. F: number of slots in the packet, START Code included (1 to 513).
;   2. F words: the slot values, START Code first (bits 7..0; bits 15..8 are
;      ignored). Then the next packet, from word 1.
;
; Words to the host: none.
; Registers: r0 slot frame, r1 slots left, r3 mark slots to add (NMIN - F).

        .equ    TX, UO4
        .equ    BIT, 240        ; bit time: 4.000 us at 60 MHz (250 kbit/s)
        .equ    STOP2, 480      ; the two stop bits
        .equ    BRK4, 2640      ; a quarter of the BREAK: 4 x 2,640 = 10,560 cycles = 176 us
        .equ    MAB, 960        ; MARK AFTER BREAK: 16 us
        .equ    SLOT, 2640      ; one slot time (11 bits) of mark
        .equ    NMIN, 24        ; slot times from the START Code to the next BREAK

idle:   TSYNC   32              ; keep T near now while there is no packet (isa 7)
        PULL.NB r1              ; F
        BEC     idle
        LDI     r3, NMIN
        SUB     r3, r1          ; mark slots to add after the packet (if > 0)
        OUT     TX, 0           ; BREAK at T
        TADD    BRK4
        TADD    BRK4
        TADD    BRK4
        TADD    BRK4
        OUT     TX, 1           ; MARK AFTER BREAK at the end of the BREAK
        TADD    MAB
slot:   TSYNC   32              ; T stays at the end of the last stop bit unless the word is late
        PULL.NB r0
        BEC     slot
        ANDI    r0, 255
        ADDI    r0, 256         ; end mark above the 8 data bits
        OUT     TX, 0           ; start bit at T
        TADD    BIT
data:   OUT     TX, r0>>        ; data bits, LSB first
        TADD    BIT
        CMPI    r0, 2
        BCS     data            ; data bits left
        OUT     TX, 1           ; the two stop bits
        TADD    STOP2
        DJNZ    r1, slot
        TST     r3, r3
        BEQ     idle
        BMI     idle            ; F >= NMIN: no mark to add
pad:    TWAIT                   ; one slot time of mark (each lead is SLOT or less)
        TADD    SLOT
        DJNZ    r3, pad
        JMP     idle
