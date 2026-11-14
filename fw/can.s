; fw/can.s - CAN 2.0A node (standard format) on two threads: transmitter
; with arbitration (thread 0, start at label tx) and receiver with CRC-15
; check and ACK (thread 1, start at label rx).
;
; What it does:
;   Pins: CAN_RX = ui_in[4] (from the RXD output of a CAN transceiver, the
;   bus level). TXA = uio[4] (thread 0) and TXB = uio[5] (thread 1), both
;   open drain, wired together to the TXD input of the transceiver with a
;   pull-up resistor (wired AND: either thread can send a dominant bit).
;   Bit time BT and sample point SP in clock cycles are set with .equ when
;   the host assembles the file. The sample reads the pin level SP - 2
;   cycles after the start of a bit of this node (docs/isa.md 6.2).
;   Thread 0 waits for 11 bit times with no bus edge (bus idle), sends the
;   bit stream that the host gives (already stuffed, with the CRC), and in
;   the first 16 bits (SOF, identifier, RTR and their stuff bits) compares
;   each sent recessive bit with the bus: a dominant bus bit there is a lost
;   arbitration: it stops at once.
;   Thread 1 synchronizes on each SOF edge (hard sync) and on each
;   recessive-to-dominant edge up to SP - 20 cycles into a bit
;   (resynchronization), removes the stuff bits, computes the CRC-15 of the
;   received bits, reads the DLC to find the end of the frame, and sends a
;   dominant ACK slot when the CRC matches and thread 0 does not send this
;   frame. It receives every frame on the bus, also the frames of thread 0.
;   Bosch CAN Specification 2.0 Part A: frame (pages 11 to 14), interframe
;   space (19), stuffing (22), bit monitoring (23), bit timing and
;   synchronization (27 to 30).
;
; Why:
;   A protocol that the instruction set was not designed for (flexibility).
;   Two threads, because the node does not fit in one: the same functions
;   in one thread need 171 words (more than the 128-word memory), and the
;   CRC of a bit is ready only after the next bit must be sent. A pin has
;   one owner thread (docs/isa.md 5.2), so the two threads drive two pins
;   that the board joins.
;
; Host words to thread 0, one frame at a time:
;   1. M, the number of stream words after the first.
;   2. M + 1 stream words, MSB first: SOF to the end of the CRC sequence
;      with the stuff bits, then CRC delimiter, ACK slot, ACK delimiter (all
;      1), filled with 1 to the end of the last word (EOF bits).
; Word from thread 0 for each frame: the bus levels of the last stream word
;   (the host reads the ACK slot in it: 0 = acknowledged), or 0xFFFF if the
;   arbitration was lost (then the host sends the frame again).
; Words from thread 1 for each frame on the bus: the destuffed bits from
;   SOF to the last CRC bit: first word 12 bits (SOF, identifier) in bits
;   11 to 0, then words of 16 bits, then the rest under a mark bit (the
;   highest 1); then the CRC word: 0 = the CRC matches.
; Not done: error frames, overload frames, error counters, stuff and form
;   error checks (a stuff error shows as a CRC error), DLC 9 to 15, bit
;   errors after the arbitration field (thread 0).

        .equ    RX, UI4
        .equ    TXA, UIO4
        .equ    TXB, UIO5
        .equ    BT, 400         ; bit time in cycles (125 kbit/s at 50 MHz)
        .equ    SP, 300         ; sample point after the start of the bit

; ----- thread 0: transmitter. r0 stream word, r1 bus levels, r2 bit count, r3 temporary.
tx:     TNOW    8
        OUT     TXA, 1          ; released
tidle:  PULL    r3              ; M
        ST      r3, TN
        PULL    r0              ; first stream word
tw:     TSYNC   24
        LDI     r2, 11
tw1:    TADD    BT
        WAIT    RX, ANY, r3, TO
        BES     tw              ; a bus edge: wait for 11 quiet bit times again
        DJNZ    r2, tw1
        TADD    16              ; SOF time
        LDI     r3, 1
        ST      r3, TXING       ; thread 1 must not ACK this frame
        LDI     r2, 16
ta:     ROR     r0, 15          ; next bit to bit 0
        OUT     TXA, r0
        TADD    SP
        SAMPLE  RX, r1<<
        TADD    BT - SP
        BES     ta1
        MOV     r3, r0
        ANDI    r3, 1
        BNE     lost            ; sent recessive, read dominant
ta1:    DJNZ    r2, ta
nw:     LD      r3, TN
        ADDI    r3, -1
        BMI     tend            ; no word left
        ST      r3, TN
        PULL.NB r0
        LDI     r2, 16
tb:     ROR     r0, 15
        OUT     TXA, r0
        TADD    SP
        SAMPLE  RX, r1<<
        TADD    BT - SP
        DJNZ    r2, tb
        JMP     nw
tend:   PUSH    r1              ; bus levels of the last word
        LDI     r3, 0
        ST      r3, TXING
        JMP     tidle
lost:   LDI     r3, 0
        ST      r3, TXING
        LD      r3, TN          ; take the rest of the frame
dl:     ADDI    r3, -1
        BMI     dd
        PULL    r0
        JMP     dl
dd:     NOT     r3, r3          ; r3 = -1 here: 0 ... use 0xFFFF
        LDI     r3, 0
        NOT     r3, r3
        PUSH    r3              ; 0xFFFF: lost
        JMP     tidle

; ----- thread 1: receiver. r0 bus history, r1 CRC_RG(14:0) in bits 15 to 1, r2 destuffed bits with a
; word mark, r3 temporary.
rx:     TNOW    8
        OUT     TXB, 1          ; released
ridle:  WAIT    RX, FALL, r3, CT ; SOF: hard synchronization
        TADD    4093            ; T = edge - 3: start of the SOF bit
        LDI     r0, 0x155       ; history with no 5 equal bits: SOF is not a stuff bit
        LDI     r1, 0
        LDI     r2, 16          ; word mark at bit 4: the first word has 12 bits
        LDI     r3, 39          ; field count, step -2: negative at bit 20
        ST      r3, CNT
rb:     TADD    SP - 20         ; last resynchronization time
        WAIT    RX, FALL, r3, TO, CT
        BEC     rs
        TADD    SP - 23         ; resynchronized: edge - 3 + SP - 20
rs:     TADD    20              ; T = sample point
        SAMPLE  RX, r0<<
        TADD    BT - SP         ; T = start of the next bit
        MOV     r3, r0
        SHR     r3, 1
        ANDI    r3, 0x1F
        BEQ     rb              ; the 5 bits before it were equal: a stuff bit
        CMPI    r3, 0x1F
        BEQ     rb
        LD      r3, CNT
        ADDI    r3, -2
        ST      r3, CNT
        BMI     re1             ; bit 20: the DLC is known
        BEQ     ack             ; CRC delimiter: the next bit is the ACK slot
rc:     SHL     r1, 1           ; C = CRC_RG(14)
        BES     q1
        BCC     q3              ; bit 0, CRC_RG(14) 0
        JMP     q2
q1:     BCS     q3              ; bit 1, CRC_RG(14) 1
q2:     ROR     r1, 8
        XORI    r1, 0x8B        ; xor 4599hex in bits 15 to 1 (8B32hex)
        ROR     r1, 8
        XORI    r1, 0x32
q3:     SHL     r2, 1
        BEC     q4
        ORI     r2, 1
q4:     BCC     rb
        PUSH.NB r2              ; 16 destuffed bits
        LDI     r2, 1
        JMP     rb
re1:    MOV     r3, r2
        ANDI    r3, 0x4F        ; RTR (bit 6) and DLC
        CMPI    r3, 0x40
        BCC     re2             ; data frame
        LDI     r3, 0           ; remote frame: no data field
re2:    SHL     r3, 4           ; 2 x 8 x DLC
        ADDI    r3, 30          ; + 2 x (15 CRC bits)
        ST      r3, CNT
        JMP     rc
ack:    MOV     r1, r1
        BNE     ra              ; CRC error: no ACK
        LD      r3, TXING
        MOV     r3, r3
        BNE     ra              ; thread 0 sends this frame
        OUT     TXB, 0          ; ACK slot
ra:     TADD    BT
        OUT     TXB, 1          ; released at the ACK delimiter
        PUSH.NB r2              ; last destuffed bits under the mark
        PUSH.NB r1              ; CRC word: 0 = the CRC matches
        TWAIT                   ; past the ACK slot edge
        JMP     ridle
TN:     .word   0
TXING:  .word   0
CNT:    .word   0
