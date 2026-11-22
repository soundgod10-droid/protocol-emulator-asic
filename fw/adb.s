; fw/adb.s - ADB (Apple Desktop Bus) host: Talk, Listen, SendReset, Flush, Global Reset, service requests.
;
; What it does:
;   The computer side of the Apple Desktop Bus (Guide to the Macintosh Family
;   Hardware, 2nd edition, 1990, chapter 8: ADB transactions, commands,
;   signals, Table 8-14 ADB timing) on ADB = uio[0] (pin 0, UIO0), open
;   drain with the pull-up of the bus. Times at 60 MHz (Table 8-14, host):
;     Attention low 800 us, Sync high 65 us, then the command byte MSB first
;     and a stop bit; each bit cell 100 us: "0" low 65 us, "1" low 35 us,
;     then released for the rest of the cell; stop bit low 70 us.
;   Talk (command bits 3..2 = 11): the device answers within 260 us with a
;     start bit, 2 to 8 data bytes and a stop bit at its own bit-cell time
;     (100 us +/- 30 %). The host times each cell from falling edge to
;     falling edge and the low time from falling to rising edge: "1" if the
;     low time is less than half of the cell, else "0". A low with no next
;     falling edge within 150 us of its rise is the stop bit.
;   Listen (bits 3..2 = 10): 200 us after the stop bit the host sends a start
;     bit ("1"), the N data bytes MSB first and a stop bit.
;   SendReset, Flush (bits 3..2 = 00): command only.
;   Global Reset: the bus low for 3 ms.
;   Service Request: 10 us after the end of its stop bit the host reads the
;     bus; if a device still holds it low (it extends the stop bit by 140 us
;     or more), the request is noted and the host waits for the release.
;
; Why:
;   ADB codes each bit as a low/high ratio inside a cell, uses long low
;   signals for Attention and reset, and lets a device answer at a clock up
;   to 30 % off; a UART or SPI peripheral cannot make or read it.
;
; Host words to this thread, one for each transaction:
;   bits 7..0  the command byte (address, command, register; Table 8-13)
;   bits 11..8 N, the number of data bytes of a Listen (2 to 8); then N more
;              words, one data byte each (bits 7..0), all before the command
;   bit 15     Global Reset (3 ms low) in place of a command
; Words to the host: for a Talk, each data byte (bits 7..0, bit 15 = 0);
;   then for every transaction one status word: 8000h + bits 3..0 the number
;   of data bytes received (0: no answer), bit 8 a service request was seen
;   in the stop bit, bit 9 a bit count that is not whole bytes, bit 10 the
;   bus stayed low.
; Registers: r0 work / link of cell, r1 bits, r2 bit count, r3 pointer / time.
; Memory: CMD, SRQF, ERRF, NB, ENDW and the time words; data memory 512 to
;   519 holds the bytes of a Listen.

        .equ    ADB, UIO0
        .equ    L0, 3900        ; "0": low 65 us
        .equ    H0, 2100
        .equ    L1, 2100        ; "1": low 35 us
        .equ    H1, 3900
        .equ    SYNC, 3900      ; Sync: high 65 us
        .equ    STOPH, 2100     ; stop bit: low 2 x 35 us = 70 us
        .equ    SRQS, 600       ; bus read 10 us after the end of the stop bit
        .equ    NBASE, 512

get:    TSYNC   16              ; keep T near now while there is no word (isa 7)
        PULL.NB r0
        BEC     get
        ST      r0, CMD
        LDI     r1, 0
        ST      r1, SRQF
        ST      r1, ERRF
        ST      r1, NB
        TST     r0, r0
        BMI     greset          ; bit 15: Global Reset
        MOV     r1, r0          ; a Listen: take its N data words first
        ANDI    r1, 12
        CMPI    r1, 8
        BNE     cmd
        MOV     r2, r0
        SHR     r2, 8
        ANDI    r2, 15
        LDI     r3, NBASE
        MOV     r1, r3
        ADD     r1, r2
        ST      r1, ENDW        ; address after the last byte
gath:   PULL    r1
        STX     r1, [r3]
        ADDI    r3, 1
        DJNZ    r2, gath
cmd:    TSYNC   32
        OUT     ADB, 0          ; Attention: low 800 us
        LD      r1, ATTW
        TADD    r1
        TWAIT                   ; T stays within the horizon
        TADD    r1
        OUT     ADB, 1          ; Sync: released 65 us
        TADD    SYNC
        LD      r1, CMD         ; command byte, MSB first
        SHL     r1, 8
        LDI     r2, 8
cbit:   TST     r1, r1
        JAL     r0, cell
        SHL     r1, 1
        DJNZ    r2, cbit
        OUT     ADB, 0          ; stop bit
        TADD    STOPH
        TADD    STOPH
        OUT     ADB, 1          ; released at T
        TADD    SRQS
        SAMPLE  ADB, r0>>       ; bus level 10 us after the stop bit
        BES     nosrq
        LDI     r0, 256         ; still low: a device asks for service
        ST      r0, SRQF
        LD      r0, SRQTW
        TADD    r0
        WAIT    ADB, RISE, r0, TO, CT
        BES     nosrq           ; T = the end of the service request
        LDI     r0, 1           ; the bus stays low
        SHL     r0, 10
        ST      r0, ERRF
        JMP     done
nosrq:  LD      r0, CMD
        ANDI    r0, 12
        CMPI    r0, 12
        BEQ     talk
        CMPI    r0, 8
        BNE     done            ; SendReset, Flush: no data
        LD      r0, STTW        ; Listen: stop-bit-to-start-bit time 200 us
        TADD    r0
        LDI     r1, 1
        ROR     r1, 1           ; start bit "1" (N = 1)
        JAL     r0, cell
        LDI     r3, NBASE
lbyte:  LDX     r1, [r3]
        SHL     r1, 8
        LDI     r2, 8
lbit:   TST     r1, r1
        JAL     r0, cell
        SHL     r1, 1
        DJNZ    r2, lbit
        ADDI    r3, 1
        LD      r0, ENDW
        CMP     r3, r0
        BNE     lbyte
        OUT     ADB, 0          ; stop bit
        TADD    STOPH
        TADD    STOPH
        OUT     ADB, 1
        JMP     done
talk:   LD      r0, TOW         ; the answer starts within 260 us (window 290 us)
        TADD    r0
        WAIT    ADB, FALL, r0, TO, CT
        BEC     status          ; no answer: 0 bytes
        LDI     r1, 0
        LDI     r2, 9           ; first byte: start bit and 8 data bits
rx:     TGET    r0              ; r0 = F, falling edge of this cell
        LD      r3, RTOW
        TADD    r3
        WAIT    ADB, RISE, r3, TO   ; r3 = R, the rising edge
        BEC     stuck
        TSET    r3
        SUB     r3, r0
        SHL     r3, 1
        ADD     r3, r0          ; r3 = F + 2 x low time
        TADD    4095            ; the next cell must start within 150 us of R
        TADD    4095
        TADD    810
        WAIT    ADB, FALL, r0, TO, CT
        BEC     rxend           ; no next cell: that low was the stop bit
        SUB     r0, r3          ; cell time - 2 x low time
        BMI     rx0             ; low longer than half the cell: "0"
        SHL     r1, 1
        ORI     r1, 1
        JMP     rxn
rx0:    SHL     r1, 1
rxn:    DJNZ    r2, rx
        ANDI    r1, 255         ; (the start bit went to bit 8)
        PUSH    r1
        LD      r0, NB
        ADDI    r0, 1
        ST      r0, NB
        LDI     r1, 0
        LDI     r2, 8
        JMP     rx
rxend:  CMPI    r2, 8           ; the stop bit came after a whole byte
        BEQ     status
stuck:  LDI     r0, 512         ; bits that are not whole bytes, or a low that does not end
        ST      r0, ERRF
        JMP     status
greset: TSYNC   32
        OUT     ADB, 0          ; Global Reset: low 3 ms
        LD      r3, GRW
        LDI     r2, 6
gr:     TWAIT
        TADD    r3
        DJNZ    r2, gr
        OUT     ADB, 1
status: LDI     r1, 1
        ROR     r1, 1           ; 8000h
        LD      r0, SRQF
        OR      r1, r0
        LD      r0, ERRF
        OR      r1, r0
        LD      r0, NB
        OR      r1, r0
        PUSH    r1
        LD      r0, IDLEW       ; the bus stays free 200 us before the next Attention
        TADD    r0
        JMP     get

cell:   OUT     ADB, 0          ; one bit cell from T, the bit is flag N (JAL keeps the flags); link r0
        BMI     c1
        TADD    L0
        OUT     ADB, 1
        TADD    H0
        JR      r0
c1:     TADD    L1
        OUT     ADB, 1
        TADD    H1
        JR      r0

CMD:    .word   0
SRQF:   .word   0
ERRF:   .word   0
NB:     .word   0
ENDW:   .word   0
ATTW:   .word   24000           ; half of the Attention
SRQTW:  .word   27000           ; service request: wait for the release up to 450 us
TOW:    .word   17400           ; answer window after the stop bit
RTOW:   .word   9000            ; a low longer than 150 us is a fault
STTW:   .word   11400           ; Listen: to 200 us after the stop bit
GRW:    .word   30000           ; a sixth of the Global Reset
IDLEW:  .word   12000           ; bus free time after a transaction
