; fw/sdi12.s - SDI-12 data recorder: break, commands, responses at 1200 baud.
;
; What it does:
;   Talks to SDI-12 sensors (SDI-12 Version 1.4, February 20, 2023, SDI-12
;   Support Group) on the data line SD = uio[4] (UIO4) with a pull-down on the
;   board (3.1.2: a device whose transmitter is off is 160 to 360 kOhm to
;   ground, so the released line is marking). Negative logic (Table 1):
;   marking (1) is the low level, spacing (0) the high level. Characters 1200
;   baud, 1 start bit, 7 data bits LSB first, even parity, 1 stop bit (4.1),
;   bit time 2 x HALF cycles (50,000 = 833.3 us). A break is spacing for
;   BRKN half bits (12.5 ms; at least 12 ms), then marking for MRKN half bits
;   (9.17 ms; the sensor needs 8.33 ms) before the command (7.0, 7.1). The
;   characters of a command follow each other with no gap (at most 1.66 ms
;   allowed). After the last character the recorder releases the line at the
;   end of its stop bit (at most 7.5 ms allowed) and receives the response:
;   it looks for a start bit every POLL cycles for TOUT polls (17.5 ms; the
;   first start bit comes within 15.0 ms + 0.4 ms), samples each bit in its
;   middle, checks parity and the stop bit, and waits at most TGAP polls
;   (8.33 ms, the retry condition of 7.2) for the next character, up to <LF>.
;   All times are .equ values, so a test can divide them by a common factor.
;
; Why:
;   SDI-12 sensors (hydrology, weather) share one line for both directions
;   with long, strictly timed gaps; a timed thread holds those times without
;   a UART or timers.
;
; Host words to this thread, one per command character:
;   bits 6..0  the character
;   bit 8      last character of the command: then receive the response
;   bit 9      send a break before this character
;   The characters of one command must be in the FIFO in time (a late word
;   makes a gap over 1.66 ms).
; Words to the host: each response character, bits 6..0, bit 8 parity error,
;   bit 9 stop bit not marking; 8000h no response within TOUT polls; 8001h
;   no next character within TGAP polls (response cut).
; Registers: r0 host word / counter / time, r1 character / frame / samples,
;   r2 pin bits / parity work, r3 bit count / parity.
; Memory: CMD, HALFW, HSPW, TOUTW, TGAPW.

        .equ    SD, UIO4
        .equ    HALF, 25000     ; half bit (bit 833.3 us)
        .equ    POLL, 240       ; start bit search step (4 us, 0.5 % of a bit)
        .equ    HSP, 24880      ; HALF - POLL / 2: from the poll that sees the start to the start bit middle
        .equ    BRKN, 30        ; break: 30 half bits = 12.5 ms
        .equ    MRKN, 22        ; marking after the break: 22 half bits = 9.17 ms
        .equ    TOUT, 4375      ; polls for the first response start bit (17.5 ms)
        .equ    TGAP, 2083      ; polls for the next response character (8.33 ms)
        .equ    RELN, 20        ; half bits after the <LF> stop bit middle before the next command (8.33 ms)

        TSYNC   32
        OUT     SD, Z
get:    TSYNC   8
        PULL.NB r0
        BEC     get
        ST      r0, CMD
        MOV     r1, r0
        SHR     r1, 9
        ANDI    r1, 1
        BEQ     char
        TSYNC   32
        OUT     SD, 1           ; break: spacing
        LDI     r3, BRKN
        JAL     r2, halves
        OUT     SD, 0           ; marking before the command
        LDI     r3, MRKN
        JAL     r2, halves
char:   LD      r0, CMD
        ANDI    r0, 127
        MOV     r1, r0          ; r1 = character
        MOV     r2, r0
        LDI     r3, 0           ; even parity: r3 = xor of the 7 data bits
        LDI     r0, 7
        ST      r0, CNT
par:    MOV     r0, r2
        ANDI    r0, 1
        XOR     r3, r0
        SHR     r2, 1
        LD      r0, CNT
        ADDI    r0, -1
        ST      r0, CNT
        BNE     par
        SHL     r1, 1           ; frame, LSB first: start 0, 7 data bits, parity, stop 1
        SHL     r3, 8
        OR      r1, r3
        LDI     r3, 512
        OR      r1, r3
        NOT     r2, r1          ; pin levels: negative logic
        LDI     r3, 10
        TSYNC   32
sbit:   OUT     SD, r2>>
        LD      r0, HALFW
        TWAIT                   ; wait until the bit starts: T stays within the horizon
        TADD    r0
        TWAIT
        TADD    r0
        DJNZ    r3, sbit
        LD      r0, CMD         ; T = the end of the stop bit
        SHR     r0, 8
        ANDI    r0, 1
        BEQ     get             ; more characters follow
        OUT     SD, Z           ; release the line, then the response
        LD      r0, TOUTW
rwait:  TADD    POLL
        SAMPLE  SD, r1>>
        BES     rstart          ; high level: spacing, a start bit
        ADDI    r0, -1
        BNE     rwait
        LDI     r1, 1           ; no response
        SHL     r1, 15
        PUSH    r1
        JMP     get
rstart: LD      r0, HSPW
        TADD    r0              ; T = middle of the start bit
        SAMPLE  SD, r1>>
        BEC     rgap0           ; low again: a spike, not a start bit
        LDI     r3, 9           ; 7 data bits, parity, stop
rbit:   LD      r0, HALFW
        TADD    r0
        TWAIT
        TADD    r0
        SAMPLE  SD, r1>>        ; pin level into bit 15: after 9 bits, bits 15..7
        DJNZ    r3, rbit
        NOT     r1, r1          ; logic levels: data bits 13..7, parity 14, stop 15
        MOV     r2, r1
        SHR     r2, 7           ; r2 = data, parity, stop in bits 0..8
        MOV     r3, r2
        ANDI    r3, 127         ; character
        LDI     r0, 8           ; parity check over 7 data bits and the parity bit
        ST      r0, CNT
        LDI     r0, 0
        ST      r0, PACC
pchk:   MOV     r0, r2
        ANDI    r0, 1
        LD      r1, PACC
        XOR     r1, r0
        ST      r1, PACC
        SHR     r2, 1
        LD      r0, CNT
        ADDI    r0, -1
        ST      r0, CNT
        BNE     pchk
        LD      r1, PACC        ; 1: parity error
        SHL     r1, 8
        OR      r3, r1
        XORI    r2, 1           ; r2 = stop bit: 1 marking (good) -> 0
        ANDI    r2, 1
        SHL     r2, 9
        OR      r3, r2
        PUSH    r3
        ANDI    r3, 127
        CMPI    r3, 10
        BNE     rgap0
        LDI     r3, RELN        ; <LF>: the response is complete; the sensor may hold the line
        JAL     r2, halves      ; for 7.5 ms + 0.4 ms more, so wait before the next command
        JMP     get
rgap0:  LD      r0, TGAPW       ; T = middle of the stop bit (or of the spike)
rwait1: TADD    POLL
        SAMPLE  SD, r1>>
        BES     rstart
        ADDI    r0, -1
        BNE     rwait1
        LDI     r1, 1           ; the next character did not come
        SHL     r1, 15
        ORI     r1, 1
        PUSH    r1
        JMP     get

halves: LD      r0, HALFW       ; T += r3 half bits; waits until one half bit before the new T (lead); link r2
hl:     TWAIT                   ; wait until T first: T stays within the 32,767-cycle horizon
        TADD    r0
        DJNZ    r3, hl
        JR      r2

CMD:    .word   0
CNT:    .word   0
PACC:   .word   0
HALFW:  .word   HALF
HSPW:   .word   HSP
TOUTW:  .word   TOUT
TGAPW:  .word   TGAP
