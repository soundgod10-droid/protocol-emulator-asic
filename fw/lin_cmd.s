; fw/lin_cmd.s - LIN 2.2A commander (master task): frame headers, its own responses, responses of responders.
;
; What it does:
;   Runs a LIN bus at 19,200 bit/s (BIT = 3,125 cycles at 60 MHz; LIN 2.2A
;   physical layer 6.3: 1 to 20 kbit/s) through a LIN transceiver: LIN_TX =
;   uo_out[4] (output pin 12, UO4) to its TXD (0 = dominant; 1 = recessive,
;   also the reset level of the pin), LIN_RX = ui_in[3] (input pin 11, UI3)
;   from its RXD (the bus level). For each header word from the host it sends
;   a frame header (LIN 2.2A protocol specification 2.3.1): a break of 13
;   dominant bit times, a break delimiter of 2 bit times (note 1 of 2.3.1.1:
;   longer than one bit), the sync byte 0x55 and the protected identifier
;   (frame identifier and parity bits P0, P1, equations (1) and (2)), each byte
;   with a start bit, 8 data bits LSB first and a stop bit. Then the response:
;   - commander publishes (bit 12 of the header word): the N data bytes from
;     the host, then the checksum;
;   - else a responder publishes: the thread receives N data bytes and the
;     checksum (each byte sampled in the middle of its bits from its start
;     edge) and checks the checksum (8-bit sum with carry plus the checksum
;     byte must be 0xFF).
;   The checksum is the enhanced one (protected identifier and data) except
;   for the identifiers 60 and 61, which use the classic one (data only)
;   (2.3.1.5). A responder may wait up to 130 bit times for each byte before
;   the thread reports "no response".
;
; Why:
;   LIN frames a UART byte stream with a break that a UART cannot make, a
;   parity-protected identifier and a checksum with end-around carry; the
;   commander also decides who sends the response. The thread computes the
;   identifier parity and both checksums itself.
;
; Host words to this thread, for each frame:
;   1. header: bits 5..0 frame identifier, bits 11..8 N (1 to 8 data bytes),
;      bit 12 = 1: the commander publishes the response;
;   2. if bit 12 = 1: N words, the data bytes (bits 7..0), data 1 first.
; Words to the host, for each frame that a responder publishes: N words (the
;   received data bytes), then a status word: 0 good, 1 checksum error, 2 no
;   response (a byte did not start in time), 3 framing error (a stop bit 0).
; Registers: r0 byte, r1 work, r2 counter, r3 return address.
; Memory: BUF.. in data memory (received bytes); HDR, PIDW, SUM, NN, PTR,
;   STAT, TMP after the code.

        .equ    TX, UO4
        .equ    RX, UI3
        .equ    BIT, 3125       ; 19,200 bit/s at 60 MHz
        .equ    HALF, 1562
        .equ    BRK, 13         ; break: dominant bit times
        .equ    CHUNKS, 102     ; wait for each response byte: 102 x 4,000 cycles = 130 bit times
        .equ    BUF, 512

idle:   TSYNC   32
        PULL.NB r0
        BEC     idle
        ST      r0, HDR
        MOV     r1, r0          ; P0 = parity of ID0, ID1, ID2, ID4
        ANDI    r1, 0x17
        JAL     r3, par
        SHL     r1, 6
        ST      r1, TMP
        LD      r0, HDR
        MOV     r1, r0          ; P1 = not parity of ID1, ID3, ID4, ID5
        ANDI    r1, 0x3A
        JAL     r3, par
        XORI    r1, 1
        SHL     r1, 7
        LD      r2, TMP
        OR      r1, r2
        MOV     r2, r0
        ANDI    r2, 63          ; frame identifier
        OR      r1, r2          ; protected identifier
        ST      r1, PIDW
        CMPI    r2, 60
        BCC     enh             ; identifiers 0 to 59: enhanced checksum
        CMPI    r2, 62
        BCS     enh
        LDI     r1, 0           ; 60, 61: classic checksum
enh:    ST      r1, SUM
        MOV     r1, r0
        SHR     r1, 8
        ANDI    r1, 15
        ADDI    r1, 1
        ST      r1, NN          ; N + 1 bytes in the response
        TSYNC   32
        OUT     TX, 0           ; break
        LDI     r2, BRK
brk:    TWAIT
        TADD    BIT
        DJNZ    r2, brk
        OUT     TX, 1           ; break delimiter
        TADD    BIT
        TADD    BIT
        LDI     r0, 0x55        ; sync byte
        JAL     r3, sendb
        LD      r0, PIDW
        JAL     r3, sendb
        LD      r0, HDR
        SHR     r0, 12
        ANDI    r0, 1
        BNE     pub
; a responder publishes: receive N data bytes and the checksum
        LDI     r1, BUF
        ST      r1, PTR
        LD      r2, NN
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
        LDI     r0, 0           ; good
        JMP     report
cse:    LDI     r0, 1           ; checksum error
        JMP     report
tmo:    LDI     r0, 2           ; no response
        JMP     report
fer:    LDI     r0, 3           ; framing error
report: ST      r0, STAT
        LD      r2, NN
        ADDI    r2, -1
        LDI     r1, BUF
rep:    LDX     r0, [r1]
        PUSH    r0
        ADDI    r1, 1
        DJNZ    r2, rep
        LD      r0, STAT
        PUSH    r0
        JMP     idle
; the commander publishes: N data bytes from the host, then the checksum
pub:    LD      r2, NN
        ADDI    r2, -1
ploop:  PULL    r0
        TSYNC   32              ; a late data word only lengthens the inter-byte space
        JAL     r3, sendb
        JAL     r3, addsum
        DJNZ    r2, ploop
        LD      r0, SUM
        NOT     r0, r0
        ANDI    r0, 255
        JAL     r3, sendb       ; checksum
        JMP     idle

; Byte r0 at time T: start bit, 8 data bits LSB first, stop bit; T ends at the
; end of the stop bit. r0 is kept.
sendb:  MOV     r1, r0
        ANDI    r1, 255
        ADDI    r1, 256         ; end mark above the 8 data bits
        OUT     TX, 0
        TADD    BIT
sbit:   OUT     TX, r1>>
        TADD    BIT
        CMPI    r1, 2
        BCS     sbit
        OUT     TX, 1
        TADD    BIT
        JR      r3

; Receive one byte: wait for its start edge (up to CHUNKS x 4,000 cycles after
; T), sample the 10 bits in their middles. Returns E = 0 if no start edge came,
; else E = 1 and r0 = the byte, or r0 = 256 for a framing error.
recvb:  LDI     r0, CHUNKS
rwait:  TADD    4000
        WAIT    RX, FALL, r1, TO, CT
        BES     rgot
        DJNZ    r0, rwait
        JR      r3              ; E = 0
rgot:   TADD    HALF
        LDI     r0, 10
rs:     SAMPLE  RX, r1>>        ; start bit, 8 data bits, stop bit
        TADD    BIT
        DJNZ    r0, rs
        SHR     r1, 6           ; bit 0 start, 1-8 data, 9 stop
        MOV     r0, r1
        ANDI    r0, 1
        BNE     rbad
        MOV     r0, r1
        SHR     r0, 9
        BEQ     rbad
        SHR     r1, 1
        ANDI    r1, 255
        MOV     r0, r1
        JR      r3              ; E = 1 (from the stop bit sample)
rbad:   LDI     r0, 256
        JR      r3

; SUM = SUM + r0 with end-around carry (2.3.1.5). r0 is changed.
addsum: LD      r1, SUM
        ADD     r1, r0
        MOV     r0, r1
        SHR     r0, 8           ; carry
        ANDI    r1, 255
        ADD     r1, r0
        ST      r1, SUM
        JR      r3

; r1 = parity (XOR) of the bits of r1 (8 bits) in bit 0. r2 is changed.
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

HDR:    .word   0
PIDW:   .word   0
SUM:    .word   0
NN:     .word   0
PTR:    .word   0
STAT:   .word   0
TMP:    .word   0
