; fw/lin_resp.s - LIN 2.2A responder (slave task): answers or receives the responses of configured frames.
;
; What it does:
;   On a LIN bus at 19,200 bit/s (BIT = 3,125 cycles at 60 MHz) through a LIN
;   transceiver: LIN_TX = uo_out[4] (output pin 12, UO4) to its TXD (0 =
;   dominant, 1 = recessive and the pin's reset level), LIN_RX = ui_in[3]
;   (input pin 11, UI3) from its RXD (the bus level).
;   The thread finds each frame by its break (LIN 2.2A protocol specification
;   2.3.1.1: a slave detects a break of 11 dominant bit times; this one waits
;   for 9 x 4,000 cycles = 11.5 bit times), then reads the sync byte (0x55)
;   and the protected identifier, and checks the identifier's parity bits
;   (equations (1) and (2)). A frame with another sync value or a parity error
;   is ignored until the next break. Then, by its table:
;   - a published identifier: it sends the N data bytes that the host gave and
;     the checksum right after the stop bit of the protected identifier;
;   - a subscribed identifier: it receives N data bytes and the checksum and
;     reports them with a status;
;   - any other identifier: nothing until the next break.
;   The checksum is the enhanced one (protected identifier and data) except
;   for the identifiers 60 and 61 (classic, data only) (2.3.1.5).
;
; Why:
;   The responder side of LIN: it must find a frame start in any bus state,
;   check the identifier, and answer within the frame slot with the right
;   checksum, with a table the host can change between frames.
;
; Host words to this thread (between frames, any time):
;   an entry word: bits 5..0 frame identifier, bits 11..8 N (1 to 8), bit 12 =
;   1: this node publishes the response (then N data words follow, bits 7..0,
;   data 1 first); bit 12 = 0: this node subscribes to it. A new entry for an
;   identifier replaces the old one (and its data).
; Words to the host, for each subscribed frame: the identifier with the status
;   in bits 9..8 (0 good, 1 checksum error, 2 no response, 3 framing error),
;   then N words, the received data bytes.
; Registers: r0 byte, r1 work, r2 counter, r3 return address.
; Memory: TAB (64 entry words: bit 15 known, bit 12 publish, bits 11..8 N),
;   DAT (8 data bytes for each identifier), BUF (received bytes) in data
;   memory; RXP, ENT, SUM, NN, PTR, STAT, TMP after the code.

        .equ    TX, UO4
        .equ    RX, UI3
        .equ    BIT, 3125       ; 19,200 bit/s at 60 MHz
        .equ    HALF, 1562
        .equ    CHUNKS, 102     ; wait for each byte: 102 x 4,000 cycles = 130 bit times
        .equ    TAB, 512
        .equ    DAT, 576
        .equ    BUFH, 544       ; received bytes at 2 x 544 = 1,088 (above the LDI range)

idle:   PULL.NB r0              ; a table entry from the host?
        BES     cfg
        IOR     r1, PINS
        SHR     r1, 11
        ANDI    r1, 1
        BEQ     low             ; the bus is dominant now
        TNOW    16
        TADD    BIT
        WAIT    RX, FALL, r1, TO, CT
        BEC     idle
        JMP     brk
low:    TNOW    0
brk:    LDI     r2, 9           ; a break: dominant for 9 x 4,000 cycles
bk:     TADD    4000
        WAIT    RX, RISE, r1, TO
        BES     idle            ; recessive too early: no break
        DJNZ    r2, bk
        WAIT    RX, RISE, r1, CT        ; end of the break (any length)
        JAL     r3, recvb       ; sync byte
        BEC     idle
        CMPI    r0, 0x55
        BNE     idle
        JAL     r3, recvb       ; protected identifier
        BEC     idle
        MOV     r1, r0
        SHR     r1, 8
        BNE     idle            ; framing error
        ST      r0, RXP
        MOV     r1, r0          ; parity bits of the received identifier
        ANDI    r1, 0x17
        JAL     r3, par
        SHL     r1, 6
        ST      r1, TMP
        LD      r0, RXP
        MOV     r1, r0
        ANDI    r1, 0x3A
        JAL     r3, par
        XORI    r1, 1
        SHL     r1, 7
        LD      r2, TMP
        OR      r1, r2
        MOV     r2, r0
        ANDI    r2, 63
        OR      r1, r2
        CMP     r1, r0
        BNE     idle            ; parity error
        CMPI    r2, 60          ; checksum start: enhanced, or classic for 60 and 61
        BCC     enh
        CMPI    r2, 62
        BCS     enh
        LDI     r0, 0
enh:    ST      r0, SUM
        LDI     r1, TAB
        ADD     r1, r2
        LDX     r0, [r1]        ; table entry
        ST      r0, ENT
        MOV     r1, r0
        SHL     r1, 1           ; C = known
        BCC     idle
        MOV     r1, r0
        SHR     r1, 8
        ANDI    r1, 15
        ST      r1, NN          ; N
        SHR     r0, 12
        ANDI    r0, 1
        BNE     pub
; subscribed: receive N data bytes and the checksum
        LDI     r1, BUF
        ST      r1, PTR
        LD      r2, NN
        ADDI    r2, 1
rloop:  JAL     r3, recvb
        BEC     tmo
        MOV     r1, r0
        SHR     r1, 8
        BNE     fer
        LD      r1, PTR
        STX     r0, [r1]
        ADDI    r1, 1
        ST      r1, PTR
        JAL     r3, addsum
        DJNZ    r2, rloop
        LD      r0, SUM
        CMPI    r0, 255
        BNE     cse
        LDI     r0, 0
        JMP     report
cse:    LDI     r0, 1
        JMP     report
tmo:    LDI     r0, 2
        JMP     report
fer:    LDI     r0, 3
report: SHL     r0, 8
        LD      r1, RXP
        ANDI    r1, 63
        OR      r0, r1
        PUSH    r0              ; identifier and status
        LD      r2, NN
        LDI     r1, BUF
rep:    LDX     r0, [r1]
        PUSH    r0
        ADDI    r1, 1
        DJNZ    r2, rep
        JMP     idle
; published: the N data bytes of the table, then the checksum
pub:    LD      r1, RXP
        ANDI    r1, 63
        SHL     r1, 3
        LDI     r0, DAT
        ADD     r1, r0
        ST      r1, PTR
        LD      r2, NN
ploop:  LD      r1, PTR
        LDX     r0, [r1]
        ADDI    r1, 1
        ST      r1, PTR
        JAL     r3, sendb
        JAL     r3, addsum
        DJNZ    r2, ploop
        LD      r0, SUM
        NOT     r0, r0
        ANDI    r0, 255
        JAL     r3, sendb       ; checksum
        JMP     idle
; a table entry: known, publish flag, N; then the data of a published identifier
cfg:    MOV     r1, r0
        ANDI    r1, 63
        LDI     r2, TAB
        ADD     r2, r1
        LDI     r3, 1
        SHL     r3, 15
        OR      r0, r3          ; known
        STX     r0, [r2]
        MOV     r3, r0
        SHR     r3, 12
        ANDI    r3, 1
        BEQ     idle            ; subscribed: no data
        SHL     r1, 3
        LDI     r2, DAT
        ADD     r2, r1
        SHR     r0, 8
        ANDI    r0, 15
        MOV     r3, r0
cdat:   PULL    r0
        STX     r0, [r2]
        ADDI    r2, 1
        DJNZ    r3, cdat
        JMP     idle

; Byte r0 at time T: start bit, 8 data bits LSB first, stop bit; T ends at the
; end of the stop bit. r0 is kept.
sendb:  MOV     r1, r0
        ANDI    r1, 255
        ADDI    r1, 256
        OUT     TX, 0
        TADD    BIT
sbit:   OUT     TX, r1>>
        TADD    BIT
        CMPI    r1, 2
        BCS     sbit
        OUT     TX, 1
        TADD    BIT
        JR      r3

; Receive one byte (see fw/lin_cmd.s): E = 0 no start edge in time, else r0 =
; the byte or 256 for a framing error.
recvb:  LDI     r0, CHUNKS
rwait:  TADD    4000
        WAIT    RX, FALL, r1, TO, CT
        BES     rgot
        DJNZ    r0, rwait
        JR      r3
rgot:   TADD    HALF
        LDI     r0, 10
rs:     SAMPLE  RX, r1>>
        TADD    BIT
        DJNZ    r0, rs
        SHR     r1, 6
        MOV     r0, r1
        ANDI    r0, 1
        BNE     rbad
        MOV     r0, r1
        SHR     r0, 9
        BEQ     rbad
        SHR     r1, 1
        ANDI    r1, 255
        MOV     r0, r1
        JR      r3
rbad:   LDI     r0, 8
        IOW     r0, FLAGS       ; E = 1: a byte came, with a framing error
        LDI     r0, 256
        JR      r3

addsum: LD      r1, SUM
        ADD     r1, r0
        MOV     r0, r1
        SHR     r0, 8
        ANDI    r1, 255
        ADD     r1, r0
        ST      r1, SUM
        JR      r3

par:    MOV     r2, r1
        SHR     r2, 4
        XOR     r1, r2
        MOV     r2, r1
        SHR     r2, 2
        XOR     r1, r2
        MOV     r2, r1
        SHR     r2, 1
        XOR     r1, r2
        ANDI    r1, 1
        JR      r3

RXP:    .word   0
ENT:    .word   0
SUM:    .word   0
NN:     .word   0
PTR:    .word   0
STAT:   .word   0
TMP:    .word   0
