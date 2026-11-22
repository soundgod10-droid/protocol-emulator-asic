; fw/mil1553_bc.s - MIL-STD-1553B bus controller transmitter: command and data words from the host.
;
; What it does:
;   Sends one message per host request at the logic pins of a 1553 transceiver:
;   TX = uo_out[4] (UO4, 1 = positive half of the bus waveform) and INH =
;   uo_out[5] (UO5, transmitter inhibit, 1 = off). MIL-STD-1553B (Notice 2):
;   Manchester II bi-phase at 1.0 Mbit/s (4.3.3.2, 4.3.3.3): a 1 is positive
;   then negative, a 0 negative then positive, a transition in the middle of
;   each bit (HALF = 30 cycles = 0.5 us at 60 MHz); 20-bit words (4.3.3.4): a
;   3-bit sync (command/status: positive 1.5 us then negative 1.5 us; data:
;   negative then positive, 4.3.3.5.1.1 and 4.3.3.5.2.1), 16 bits MSB first
;   (4.3.2) and an odd parity bit (4.3.3.5.1.6). The words of a message follow
;   with no gap. All words are gathered and their parity computed before the
;   transmitter is enabled, so a late host cannot split a message; messages
;   are at least 4 us apart (4.3.3.7) because of the gathering.
;   Edges fall on the 60 MHz grid at exact multiples of 0.5 us, so the zero
;   crossing deviation of the logic signal is 0 (4.5.2.1.1.2 allows 25 ns).
;
; Why:
;   1553 needs exact half-bit timing over long messages and contiguous words;
;   a timed thread gives both without a Manchester encoder.
;
; Host words to this thread, one message each:
;   first word  bits 5..0 = N words in the message (1 to 63), bit 8 = the
;               second word is also a command (RT to RT transfer)
;   then N words: the 16-bit words in bus order; the first word has a
;   command/status sync (and the second one too with bit 8), the others a
;   data sync.
; Words to the host: 8000h | N when the message has been sent.
; Registers: r0 entry / count, r1 word, r2 inverted word / pointer, r3 count.
; Memory: HDR, P; data memory BUF.. (two words per bus word: the word, then
;   parity | command sync << 1 | last << 2).

        .equ    TX, UO4
        .equ    INH, UO5
        .equ    HALF, 30        ; half a bit (0.5 us)
        .equ    SYNC, 90        ; half a sync (1.5 us)
        .equ    BUF, 512

        TNOW    16
        OUT     TX, 0
        OUT     INH, 1          ; transmitter off (uo pins are 1 from reset)
msg:    TNOW    8
        PULL.NB r0
        BEC     msg
        ST      r0, HDR
        ANDI    r0, 63
        LDI     r2, BUF
gat:    TNOW    8
        PULL.NB r1
        BEC     gat
        STX     r1, [r2]
        ADDI    r2, 1
        MOV     r3, r1          ; odd parity: 1 if the word has an even number of ones
        SHR     r3, 8
        XOR     r1, r3
        MOV     r3, r1
        SHR     r3, 4
        XOR     r1, r3
        MOV     r3, r1
        SHR     r3, 2
        XOR     r1, r3
        MOV     r3, r1
        SHR     r3, 1
        XOR     r1, r3
        ANDI    r1, 1
        XORI    r1, 1
        LDI     r3, BUF + 1     ; first word: command sync
        CMP     r2, r3
        BEQ     tcmd
        LDI     r3, BUF + 3     ; second word: command sync for an RT to RT transfer
        CMP     r2, r3
        BNE     tdat
        LD      r3, HDR
        SHR     r3, 8
        ANDI    r3, 1
        BEQ     tdat
tcmd:   ORI     r1, 2
tdat:   CMPI    r0, 1
        BNE     nlast
        ORI     r1, 4           ; last word
nlast:  STX     r1, [r2]
        ADDI    r2, 1
        DJNZ    r0, gat
        LDI     r0, BUF
        ST      r0, P
        TNOW    100
        OUT     INH, 0          ; transmitter on, the first sync 0.5 us later
        TADD    HALF
word:   LD      r3, P           ; T = start of the sync
        ADDI    r3, 1
        LDX     r0, [r3]        ; entry
        MOV     r1, r0
        ANDI    r1, 2
        BNE     cs
        OUT     TX, 0           ; data sync: negative, then positive
        TADD    SYNC
        OUT     TX, 1
        JMP     sd
cs:     OUT     TX, 1           ; command/status sync: positive, then negative
        TADD    SYNC
        OUT     TX, 0
sd:     TADD    SYNC
        ADDI    r3, -1
        LDX     r1, [r3]
        ADDI    r3, 2
        ST      r3, P
        NOT     r2, r1
        LDI     r3, 16
bit:    OUT     TX, r1<<        ; first half: the bit
        TADD    HALF
        OUT     TX, r2<<        ; second half: its inverse
        TADD    HALF
        DJNZ    r3, bit
        OUT     TX, r0          ; parity bit (entry bit 0)
        TADD    HALF
        XORI    r0, 1
        OUT     TX, r0
        TADD    HALF
        ANDI    r0, 4
        BEQ     word            ; the next word follows with no gap
        OUT     TX, 0           ; end of the message
        OUT     INH, 1
        LD      r0, HDR
        ANDI    r0, 63
        LDI     r1, 1
        SHL     r1, 15
        OR      r0, r1
        PUSH.NB r0
        JMP     msg

HDR:    .word   0
P:      .word   0
