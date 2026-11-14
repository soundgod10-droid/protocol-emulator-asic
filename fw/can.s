; fw/can.s - CAN 2.0A node (standard format): send data and remote frames
; with arbitration, receive frames, CRC-15 check, ACK.
;
; What it does:
;   Pins: CAN_TX = uio[4] (push-pull, to the TXD input of a CAN transceiver,
;   1 = recessive), CAN_RX = uio[5] (from the RXD output, the bus level).
;   One thread does the bit timing (bit time BT, sample point SP, both in
;   clock cycles, set with .equ when the host assembles the file), the hard
;   synchronization on the SOF edge and a resynchronization on each
;   recessive-to-dominant edge up to SP - 20 cycles after the start of the
;   bit, bit stuffing and destuffing (SOF to the end of the CRC sequence),
;   bit monitoring for arbitration, the CRC-15 of the received bits, the
;   field count from the DLC, and the ACK slot. The node receives every
;   frame on the bus, also its own.
;   Bosch CAN Specification 2.0 Part A: frame (pages 11 to 14), stuffing
;   (22), bit monitoring and errors (23), bit timing and synchronization
;   (27 to 30).
;
; Why:
;   A protocol that the instruction set was not designed for (flexibility).
;   The transmit CRC comes from the host: the firmware computes the CRC of
;   a bit after the next bit is already sent (the next bit must be decided
;   in the phase segment 2), so it cannot send its own CRC in time.
;
; Host words to this thread (one frame at a time; send the next frame
;   after the status word of this one):
;   1. bits 15 to 4 = SOF (0) and ID-10 to ID-0; bits 3 to 0 = N, the
;      number of words that follow.
;   2. N words: the destuffed bits from RTR to the last CRC bit (RTR, r1,
;      r0, DLC, data, CRC-15), MSB first, 16 in each word, the last word
;      filled with 1. Keep the FIFO from running empty during the frame.
; Words to the host for each frame on the bus: the destuffed bits from SOF
;   to the last CRC bit: first word 12 bits (SOF, ID) in bits 11 to 0, then
;   words of 16 bits, then the rest of the bits under a mark bit (the
;   highest 1); then a status word: bit 0 = bus level in the ACK slot (0 =
;   acknowledged), bit 1 = CRC error, bit 2 = this node sent the frame,
;   bit 3 = this node lost the arbitration (or read a bit other than the
;   one it sent): send the frame again.
; Not done: error frames, overload frames, error counters, stuff error and
;   form error detection (a stuff error shows as a CRC error), DLC 9 to 15.
; Registers: r0 bits to send (next one to bit 0 by a rotation; 0xFFFF in
;   a receiver), r1 bus history (all bits, also stuff bits), r2 destuffed
;   bits with a word mark, r3 temporary. Memory words S, CRC, CNT.

        .equ    TX, UIO4
        .equ    RX, UIO5
        .equ    BT, 400         ; bit time in cycles (125 kbit/s at 50 MHz)
        .equ    SP, 300         ; sample point after the start of the bit (75 %)

        TNOW    8
        OUT     TX, 1           ; recessive
idle:   TSYNC   16
        PULL.NB r0              ; first word of a frame to send
        BES     send
        TADD    BT
        WAIT    RX, FALL, r3, TO, CT
        BEC     idle            ; no SOF for one more bit time
        TADD    4093            ; T = edge - 3 = start of the SOF bit (hard sync)
        LDI     r0, 0
        ST      r0, S           ; S = 0: receiver
        NOT     r0, r0          ; send recessive bits; N = 1
init:   LDI     r1, 0x3FF       ; bus history: recessive
        LDI     r2, 16          ; word mark at bit 4: the first word has 12 bits
        LDI     r3, 0
        ST      r3, CRC
        LDI     r3, 37          ; field count, step -2: negative after 19 bits
        ST      r3, CNT
        BMI     smp             ; receiver: the SOF bit is on the bus
        ROR     r0, 15
        OUT     TX, r0          ; SOF
smp:    TADD    SP - 20         ; T = last resynchronization time
        WAIT    RX, FALL, r3, TO, CT
        BEC     s0
        TADD    SP - 23         ; resynchronized: edge - 3 + SP - 20
s0:     TADD    20              ; T = sample point
        SAMPLE  RX, r1<<
        TADD    BT - SP         ; T = start of the next bit
        BES     s2              ; bus recessive
        NOT     r3, r0
        BEQ     s2              ; receiver
        ANDI    r3, 1
        BEQ     lost            ; sent recessive, read dominant
s2:     MOV     r3, r1
        ANDI    r3, 0x1F
        BEQ     so              ; 5 equal bits: the next bit is a stuff bit
        CMPI    r3, 0x1F
        BEQ     so
        ROR     r0, 15
        OUT     TX, r0          ; next bit
        JMP     book
so:     LD      r3, S
        ADDI    r3, -1
        BMI     book            ; receiver: it sends nothing
        NOT     r3, r1
        OUT     TX, r3          ; stuff bit: the complement of the last bit
        JMP     book
lost:   LD      r3, S
        ADDI    r3, -1          ; words of this frame still to come
        NOT     r3, r3
        ST      r3, S           ; negative: lost, take the words at the end
        LDI     r0, 0
        NOT     r0, r0          ; send recessive bits
        JMP     s2
book:   LD      r3, CRC         ; CRC_RG(14:0) in bits 15 to 1
        SHL     r3, 1           ; C = CRC_RG(14)
        BES     k1
        BCC     k3              ; bit 0, CRC_RG(14) 0
        JMP     k2
k1:     BCS     k3              ; bit 1, CRC_RG(14) 1
k2:     ROR     r3, 8
        XORI    r3, 0x8B        ; xor 4599hex in bits 15 to 1 (8B32hex)
        ROR     r3, 8
        XORI    r3, 0x32
k3:     ST      r3, CRC
        SHL     r2, 1
        BEC     k4
        ORI     r2, 1
k4:     BCS     wd              ; word complete
        BMI     rf              ; mark at bit 15: the next sent bit is in a new word
k5:     LD      r3, CNT
        ADDI    r3, -2
        ST      r3, CNT
        BMI     e1              ; 19 bits: the DLC is known
        BEQ     e3              ; end of the CRC sequence
k6:     MOV     r3, r1
        ANDI    r3, 0x1F
        BEQ     ssm
        CMPI    r3, 0x1F
        BEQ     ssm
        JMP     smp
wd:     PUSH.NB r2
        LDI     r2, 1
        JMP     k5
rf:     LD      r3, S
        ADDI    r3, -1
        BMI     k5              ; receiver
        BEQ     k5              ; no word left
        ST      r3, S
        PULL.NB r0
        JMP     k5
e1:     MOV     r3, r2
        ANDI    r3, 0x4F        ; RTR (bit 6) and DLC
        CMPI    r3, 0x40
        BCC     e2              ; data frame
        LDI     r3, 0           ; remote frame: no data field
e2:     SHL     r3, 4           ; 2 x 8 x DLC
        ADDI    r3, 30          ; + 2 x 15 CRC bits
        ST      r3, CNT
        JMP     k6
ssm:    TADD    SP - 20         ; stuff bit: sample it into the history only
        WAIT    RX, FALL, r3, TO, CT
        BEC     t0
        TADD    SP - 23
t0:     TADD    20
        SAMPLE  RX, r1<<
        TADD    BT - SP
        ROR     r0, 15
        OUT     TX, r0          ; next bit (no stuff bit after a stuff bit)
        JMP     smp
e3:     MOV     r3, r1          ; was a stuff bit sent after the last CRC bit?
        ANDI    r3, 0x1F
        BEQ     e4
        CMPI    r3, 0x1F
        BNE     e5
e4:     TADD    BT
        OUT     TX, 1           ; CRC delimiter after the stuff bit
e5:     TADD    BT              ; T = ACK slot
        LD      r3, S
        ADDI    r3, -1
        BMI     e7              ; receiver (or lost): ACK if the CRC matches
        JMP     e8
e7:     LD      r3, CRC
        MOV     r3, r3
        BNE     e8
        OUT     TX, 0           ; ACK
e8:     TADD    SP
        LDI     r1, 0
        SAMPLE  RX, r1<<        ; bus level in the ACK slot
        TADD    BT - SP
        OUT     TX, 1           ; ACK delimiter
        PUSH.NB r2              ; last destuffed bits under the mark
        LD      r3, CRC
        MOV     r3, r3
        BEQ     f1
        ORI     r1, 2           ; CRC error
f1:     LD      r3, S
        MOV     r3, r3
        BEQ     f3              ; receiver
        BMI     f2
        ORI     r1, 4           ; frame sent
        JMP     f3
f2:     ORI     r1, 8           ; lost: the host sends the frame again
f3:     PUSH.NB r1
        MOV     r3, r3
        BMI     dr
        JMP     w0
dr:     NOT     r3, r3          ; words of the lost frame still to come
        BEQ     w0
dl:     PULL    r0
        ADDI    r3, -1
        BNE     dl
w0:     LDI     r3, 11          ; ACK delimiter, EOF 7, intermission 3
w1:     TADD    BT
        DJNZ    r3, w1
        JMP     idle
S:      .word   0
CRC:    .word   0
CNT:    .word   0
