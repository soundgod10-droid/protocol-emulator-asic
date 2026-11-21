; fw/mdb.s - MDB/ICP master (vending machine controller): blocks to a peripheral, its reply, ACK or NAK, bus reset.
;
; What it does:
;   Talks to MDB peripherals as the master (VMC) of NAMA MDB/ICP Version 4.3
;   (July 2019): section 2.1 byte format, 2.2 block format, CHK byte and
;   response codes, section 3 bus timing. TX = uo_out[4] (output pin 12, UO4;
;   1 = idle, as from reset) and RX = ui_in[3] (input pin 11, UI3), at logic
;   level behind the bus interface of section 4.
;   Bytes: 9600 bit/s (BIT = 2 x HB = 6,250 cycles at 60 MHz), start bit, 8
;   data bits LSB first, mode bit, stop bit.
;   For each block from the host the thread takes the N bytes into data
;   memory and adds the CHK byte (sum of the bytes, carry ignored), then sends
;   them back to back: the first (address) byte with the mode bit set, the
;   other bytes and the CHK with it clear (no gap between bytes).
;   Then it waits for the peripheral's reply: the first start bit must come
;   within OUTN x 24,000 cycles = 5.2 ms (t-response 5.0 ms) after the end of
;   the CHK byte, each later byte within the same time after the byte before
;   (the receiver tolerance of Appendix 3, longer than the sender's 1 ms
;   limit). A WAIT time-out reaches at most 32,767 cycles (546 us, isa 6.3),
;   so the thread samples RX every 24 cycles while it waits: no start bit can
;   pass unseen, and its edge is known to +/-12 cycles (0.2 % of a bit).
;   Each reply byte is read in the middle of its bits from its start edge and
;   goes to the host with its mode bit. A reply that ends with its first byte
;   (mode bit set: ACK 00H or NAK FFH) ends the exchange; after a data block
;   (its last byte, the CHK, has the mode bit) the thread checks the CHK and
;   sends ACK (00H) or NAK (FFH) half a bit after the block.
;   A block with N = 0 is a bus reset: TX active (0) for BRK x 8,000 cycles =
;   100.1 ms, then idle for the setup time, SET x 12,000 cycles = 200.2 ms.
;
; Why:
;   MDB is a 9-bit UART bus: the master must set the mode bit on the address
;   byte only, keep its own bytes without gaps, add the checksum, read the
;   peripheral's mode bit to find the end of the reply, time the reply out
;   and answer a data block within 5 ms.
;
; Host words to this thread: N (bytes of the block without CHK, 1 to 35; 0
;   = bus reset), then the N bytes (address byte first).
; Words to the host, for each block: each reply byte (bits 7..0, bit 8 its
;   mode bit), then a status word: 0 = done (ACK or NAK from the peripheral,
;   or a data block with a good CHK, answered with ACK), 0x4000 = data block
;   with a wrong CHK (answered with NAK), 0x2000 = framing error (stop bit
;   0), 0x8000 = no reply or no next byte in time. A bus reset gives 0.
; Registers: r0 work, r1 byte, r2 pointer and checksum, r3 count.
; Memory: BUF.. in data memory (the block and its CHK), CNT and OC after the
;   code.

        .equ    TX, UO4
        .equ    RX, UI3
        .equ    HB, 3125        ; half a bit time (9600 bit/s: 6,250 cycles)
        .equ    HBE, 3113       ; first sample after the start bit was seen: HB - 12 (the middle of the 24-cycle step)
        .equ    OUTN, 13        ; reply time-out: OUTN x 1,000 x 24 cycles = 312,000 cycles = 5.2 ms
        .equ    BRK, 751        ; bus reset: BRK x 8,000 cycles (100 ms minimum)
        .equ    SET, 1001       ; setup after a reset: SET x 12,000 cycles (200 ms minimum)
        .equ    BUF, 512

        TSYNC   32
        OUT     TX, 1           ; idle
get:    TSYNC   16              ; keep T near now while there is no word (isa 7)
        PULL.NB r3
        BEC     get
        MOV     r3, r3
        BEQ     breset
        ADDI    r3, 1
        ST      r3, CNT         ; bytes on the bus: N and the CHK
        ADDI    r3, -1
        LDI     r2, BUF
        LDI     r0, 0           ; checksum
gat:    PULL    r1
        ANDI    r1, 255
        STX     r1, [r2]
        ADD     r0, r1
        ADDI    r2, 1
        DJNZ    r3, gat
        ANDI    r0, 255
        STX     r0, [r2]        ; CHK
        LDI     r2, BUF
        LDI     r3, 256         ; mode bit of the address byte
        TSYNC   120
        TADD    80              ; the first event comes about 15 slots later
txb:    LDX     r1, [r2]
        ADDI    r2, 1
        OR      r1, r3          ; mode bit (bit 8)
        LDI     r3, 0
        SHL     r1, 1           ; bit 0: start bit (0)
        LDI     r0, 768
        SHL     r0, 2           ; bit 10 stop bit (1), bit 11 end mark
        OR      r1, r0
tbit:   OUT     TX, r1>>
        TADD    HB
        TADD    HB
        CMPI    r1, 2
        BCS     tbit
        LD      r0, CNT
        ADDI    r0, -1
        ST      r0, CNT
        BNE     txb
        LDI     r3, 0           ; reply bytes before the last one
        LDI     r2, 0           ; their sum
wait:   LDI     r0, OUTN        ; T = end of the last stop bit sent (or the middle of a reply byte's stop bit)
        ST      r0, OC
wout:   LDI     r0, 1000
wsmp:   TADD    24
        SAMPLE  RX, r1>>        ; the line every 24 cycles (a WAIT time-out cannot reach 5 ms)
        BEC     start           ; low: a start bit
        DJNZ    r0, wsmp
        LD      r0, OC
        ADDI    r0, -1
        ST      r0, OC
        TADD    40              ; lead for the next sample after these instructions
        BNE     wout
        JMP     tout
start:  TADD    HBE             ; middle of the start bit: its edge lies 0 to 24 cycles before this sample
        LDI     r0, 10
smp:    SAMPLE  RX, r1>>        ; start bit, 8 data bits, mode bit
        TADD    HB
        TADD    HB
        DJNZ    r0, smp
        SAMPLE  RX, r1>>        ; stop bit; T stays at its middle, where the search for the next start bit begins
        SHR     r1, 5           ; bit 0 start, 1-8 data, 9 mode, 10 stop
        MOV     r0, r1
        SHR     r0, 10
        BEQ     ferr            ; stop bit 0
        SHR     r1, 1
        SHL     r1, 7
        SHR     r1, 7           ; bits 7..0 data, bit 8 mode
        PUSH    r1
        MOV     r0, r1
        SHR     r0, 8
        BNE     last            ; mode bit: the last byte of the reply
        ADD     r2, r1
        ADDI    r3, 1
        JMP     wait
last:   MOV     r3, r3
        BEQ     ok              ; ACK or NAK alone
        ANDI    r1, 255
        ANDI    r2, 255
        CMP     r1, r2
        BNE     bad
        LDI     r1, 0           ; ACK
        LDI     r2, 0
        JMP     reply
bad:    LDI     r1, 255         ; NAK
        LDI     r2, 1
        SHL     r2, 14
reply:  TADD    HB
        TADD    HB              ; half a bit after the end of the reply's stop bit
        SHL     r1, 1           ; mode bit 0, start bit 0
        LDI     r0, 768
        SHL     r0, 2
        OR      r1, r0
rbit:   OUT     TX, r1>>
        TADD    HB
        TADD    HB
        CMPI    r1, 2
        BCS     rbit
        PUSH    r2
        JMP     get
ok:     LDI     r2, 0
        PUSH    r2
        JMP     get
ferr:   LDI     r2, 1
        SHL     r2, 13
        PUSH    r2
        JMP     get
tout:   LDI     r2, 1
        SHL     r2, 15
        PUSH    r2
        JMP     get
breset: TSYNC   32
        OUT     TX, 0           ; bus reset: TX active
        LDI     r0, BRK
bw:     TWAIT
        TADD    4000
        TADD    4000
        DJNZ    r0, bw
        OUT     TX, 1
        LDI     r0, SET
sw:     TWAIT
        TADD    4000
        TADD    4000
        TADD    4000
        DJNZ    r0, sw
        TWAIT
        LDI     r2, 0
        PUSH    r2
        JMP     get

CNT:    .word   0
OC:     .word   0
