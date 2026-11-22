; fw/mil1553_rt.s - MIL-STD-1553B remote terminal receiver: words of a message, validated, to the host.
;
; What it does:
;   Receives the words of each message at the logic outputs of a 1553
;   receiver: RXA = ui_in[3] (UI3, 1 = bus positive) and RXB = ui_in[4] (UI4,
;   1 = bus negative; both 0 = bus idle). MIL-STD-1553B (Notice 2): sync
;   4.3.3.5.1.1 and 4.3.3.5.2.1 (command/status: positive then negative,
;   data: negative then positive, 1.5 us each), Manchester II bits at 1.0
;   Mbit/s (4.3.3.2, 4.3.3.3), 16 bits MSB first and odd parity (4.3.3.4,
;   4.3.3.5.1.6), contiguous words, word validation 4.4.1.1 (valid sync,
;   Manchester II, 16 bits plus parity, odd parity).
;   The sync type comes from which receiver output is high in the sync's
;   first half; the timing comes from the edge in the middle of the sync and
;   then from the edge in the middle of every bit (a Manchester bit always
;   has one): the thread waits for each mid-bit edge in a window that starts
;   after the bit-boundary edge, and reads the level just after it with IOR
;   PINS (the second half: negative = 1). So each bit is timed from the previous mid-bit
;   edge, and zero-crossing deviations of +/-150 ns from the previous
;   crossing (4.5.2.2.2.1) are accepted. A missing mid-bit edge is a
;   Manchester error and ends the message; a missing mid-sync edge is a sync
;   error.
;   During a message the words are stored raw in data memory; when the bus is
;   idle after a word, the thread checks the parity of each word and sends
;   two words per bus word to the host. It does not watch the bus while it
;   does that (measured in test/test_mil1553.py: busy 0.6 us + 3.0 us per
;   word after the end of a message, 99.5 us after a 33-word message; a
;   message that starts earlier is missed). After a Manchester or sync error
;   it waits until both receiver outputs have been low for 2.7 us, so the
;   rest of a broken word is not taken as a new word.
;
; Why:
;   A 1553 terminal must decode 1 Mbit/s Manchester with its own bit
;   recovery; a timed thread does it with one wait per bit and no decoder.
;
; Host words: none.
; Words to the host, two per bus word: 8000h | flags (bit 0 = command/status
;   sync, bit 1 = Manchester error, bit 2 = sync error, bit 3 = parity
;   error), then the 16 data bits.
; Registers: r0 edge time / work, r1 word, r2 pointer, r3 count / work.
; Memory: ENDP, PP; data memory RING.. (three words per bus word: sync type,
;   second-half levels, parity sample | error bits).

        .equ    RXA, UI3
        .equ    RXB, UI4
        .equ    RING, 512

new:    LDI     r2, RING
        LD      r0, ERRF        ; after an error: wait until the bus has been idle for 2.7 us
        MOV     r0, r0
        BEQ     idle
        LDI     r0, 0
        ST      r0, ERRF
quiet:  LDI     r3, 8
qt:     IOR     r0, PINS
        SHR     r0, 11
        ANDI    r0, 3
        BNE     quiet
        DJNZ    r3, qt
idle:   IOR     r0, PINS        ; wait for a sync: RXA (command/status) or RXB (data) high
        SHR     r0, 11
        ANDI    r0, 3
        BEQ     idle
        TNOW    127             ; the mid-sync edge comes within 1.5 us
        CMPI    r0, 1
        BNE     fdat
        WAIT    RXA, FALL, r0, TO, CT
        BEC     syerr
        LDI     r0, 1
        JMP     word
fdat:   CMPI    r0, 2
        BNE     idle            ; both high: not a bus level
        WAIT    RXA, RISE, r0, TO, CT
        BEC     syerr
        LDI     r0, 0
word:   STX     r0, [r2]        ; T = mid-sync edge; sync type first
        ADDI    r2, 1
        LDI     r3, 16
        TADD    60              ; the loop adds 38: past the sync's end edge (1.5 us +/- 150 ns)
bit:    TADD    38
        TWAIT                   ; past the bit-boundary edge (0.5 us +/- 150 ns after the last mid-bit edge)
        TADD    34              ; time-out of the next mid-bit edge (1.0 us +/- 150 ns)
        WAIT    RXA, ANY, r0, TO, CT
        IOR     r0, PINS        ; the level just after it: the second half (positive = 0)
        BEC     merr
        SHR     r0, 11
        ANDI    r0, 1
        SHL     r1, 1
        OR      r1, r0
        DJNZ    r3, bit
        TADD    38              ; parity bit, the same way
        TWAIT
        TADD    34
        WAIT    RXA, ANY, r0, TO, CT
        IOR     r0, PINS
        BEC     merr
        SHR     r0, 11
        ANDI    r0, 1
        STX     r1, [r2]
        ADDI    r2, 1
        STX     r0, [r2]
        ADDI    r2, 1
        TADD    135             ; time-out of the next word's mid-sync (2.0 us +/- 150 ns after the parity mid-bit edge)
        IOR     r0, PINS        ; next sync or idle
        SHR     r0, 11
        ANDI    r0, 3
        BEQ     eom
        CMPI    r0, 1
        BNE     ndat
        WAIT    RXA, FALL, r0, TO, CT
        BEC     syerr
        LDI     r0, 1
        JMP     word
ndat:   WAIT    RXA, RISE, r0, TO, CT
        BEC     syerr
        LDI     r0, 0
        JMP     word
merr:   STX     r1, [r2]        ; Manchester error: the word so far, error bit 1
        ADDI    r2, 1
        LDI     r0, 2
        STX     r0, [r2]
        ADDI    r2, 1
        ST      r0, ERRF
        JMP     eom
syerr:  LDI     r0, 1
        ST      r0, ERRF
        LDI     r0, 0           ; sync error: an entry with error bit 2
        STX     r0, [r2]
        ADDI    r2, 1
        STX     r0, [r2]
        ADDI    r2, 1
        LDI     r0, 4
        STX     r0, [r2]
        ADDI    r2, 1

eom:    ST      r2, ENDP        ; to the host
        LDI     r3, RING
pl:     LD      r2, ENDP
        CMP     r3, r2
        BEQ     new
        LDX     r0, [r3]        ; sync type
        ADDI    r3, 1
        LDX     r1, [r3]        ; second-half levels
        ADDI    r3, 1
        LDX     r2, [r3]        ; parity sample, error bits
        ADDI    r3, 1
        ST      r3, PP
        NOT     r1, r1          ; data
        MOV     r3, r2
        ANDI    r3, 6
        BEQ     pchk
        OR      r0, r3          ; error bits 1, 2: the parity of a broken word is not checked
        JMP     pmark
pchk:   MOV     r3, r2          ; parity: data bits xor parity sample must be 0
        ANDI    r3, 1
        XOR     r3, r1
        MOV     r2, r3
        SHR     r2, 8
        XOR     r3, r2
        MOV     r2, r3
        SHR     r2, 4
        XOR     r3, r2
        MOV     r2, r3
        SHR     r2, 2
        XOR     r3, r2
        MOV     r2, r3
        SHR     r2, 1
        XOR     r3, r2
        ANDI    r3, 1
        SHL     r3, 3
        OR      r0, r3          ; bit 3 parity error
pmark:  LDI     r2, 1
        SHL     r2, 15
        OR      r0, r2
        PUSH    r0
        PUSH    r1
        LD      r3, PP
        JMP     pl

ENDP:   .word   0
PP:     .word   0
ERRF:   .word   0
