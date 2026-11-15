; pdi.s: AVR XMEGA PDI (Program and Debug Interface) programmer, physical layer,
; for one thread.
;
; What it does:
;   A protocol that the chip was not designed for: written from the XMEGA AU
;   manual only (Atmel 8331F-AVR-04/2013, section 32), on the unchanged
;   instruction set and assembler.
;   The thread drives PDI_CLK and PDI_DATA. At start it enables the PDI (32.3.1):
;   DATA high for TEN, then 16 clock cycles with DATA high. After that the clock
;   never stops: between host words the thread sends IDLE bits (DATA high),
;   because the PDI turns itself off when PDI_CLK is slower than about 10 kHz
;   (32.3.2).
;   Host words:
;     bit 15 = 0: send one character; bits 11..0 are the 12 line bits, bit 0 first
;       (start 0, data bits 0 to 7, even parity, two stop bits 1; 32.3.3). The host
;       makes the parity bit (the instruction set has no parity instruction).
;       0x000 is a BREAK; after each character at least one IDLE bit follows.
;     bit 15 = 1: receive bits 7..0 characters (1 to 255). The thread releases DATA
;       at the next falling edge and keeps the clock running; for each character it
;       waits up to SEARCH bit times for a start bit, then reads the 12 line bits.
;       Reply for each character: the 12 line bits in bits 11..0, bit 0 first (the
;       host checks the start, parity and stop bits). No start bit in SEARCH bit
;       times: reply 0xFFFF and no more characters. Then DATA is driven high again
;       (at least one IDLE bit before the next start bit, 32.3.7).
;     Send the receive word right after the instruction that has an answer: the
;     device drives DATA after its guard time (2 to 128 IDLE bits, 32.3.7).
;   Both sides change DATA at the falling edge of PDI_CLK and sample it at the
;   rising edge (32.3.4).
;
; Timing: one bit is 2 x HALF cycles: HALF 30 is 1 MHz at 60 MHz (at 50 MHz the
; host assembles with HALF 25, TEN 2500). The manual gives no upper limit for
; PDI_CLK (the device data sheet does); test_holdout_pdi_rate measures the
; smallest HALF of this firmware.
; TEN = 50 us: the RESET function is off after DATA has been high for the reset
; pulse width of the device data sheet, and the first clock must start within
; 100 us after that (32.3.1). The first falling edge comes TEN + HALF after DATA
; goes high, so both rules hold for any reset pulse width up to 50 us.
; Each time comes from T (TADD), not from the time that the instructions take.
;
; Host configuration before RUN: output pins CLK and DAT belong to this thread,
; push-pull. CLK is released until the first clock (isa 2.4); PDI_CLK is the RESET
; pin of the device. The device must run from 3.3 V (the chip pins are 3.3 V).
; Registers: r0 = host word and character count, r1 = bit counter, r2 = bits.

        .equ    CLK, UIO2       ; PDI_CLK (the RESET pin of the device)
        .equ    DAT, UIO3       ; PDI_DATA (output and input)
        .equ    HALF, 30        ; half bit time: 1 MHz at 60 MHz
        .equ    TEN, 3000       ; 50 us of DATA high before the first clock
        .equ    SEARCH, 255     ; bit times to wait for a start bit
        .equ    MARGIN, 12      ; the first OUT is 1 instruction after TNOW

start:  TNOW    MARGIN          ; T = now + MARGIN
        OUT     DAT, 1          ; enable (32.3.1): DATA high
        TADD    TEN
        LDI     r1, 16          ; then 16 clock cycles with DATA high
en:     TADD    HALF
        OUT     CLK, 0
        TADD    HALF
        OUT     CLK, 1
        DJNZ    r1, en
idle:   TADD    HALF            ; T = last rising edge here
        OUT     CLK, 0
        OUT     DAT, 1, 1       ; IDLE bit
        TADD    HALF
        OUT     CLK, 1
        PULL.NB r0
        BEC     idle            ; no host word: the clock keeps running
        BMI     rx              ; bit 15 = 1: receive
        LDI     r1, 12
tx:     TADD    HALF
        OUT     CLK, 0          ; falling edge
        OUT     DAT, r0>>, 1    ; next line bit, 1 cycle after the falling edge
        TADD    HALF
        OUT     CLK, 1          ; rising edge: the device samples
        DJNZ    r1, tx
        JMP     idle
rx:     ANDI    r0, 255         ; characters to receive
        TADD    HALF
        OUT     CLK, 0
        OUT     DAT, Z, 1       ; release DATA after this falling edge
        TADD    HALF            ; T = next rising edge
frame:  LDI     r1, SEARCH
find:   OUT     CLK, 1          ; rising edge at T
        SAMPLE  DAT, r2>>, 2    ; DATA at the rising edge (isa 6.2)
        TADD    HALF
        OUT     CLK, 0
        TADD    HALF
        BEC     got             ; a 0: start bit
        DJNZ    r1, find
        PUSH    r2              ; no start bit: r2 is 0xFFFF
        JMP     done
got:    LDI     r1, 11          ; the other 11 line bits
bit:    OUT     CLK, 1
        SAMPLE  DAT, r2>>, 2
        TADD    HALF
        OUT     CLK, 0
        TADD    HALF
        DJNZ    r1, bit
        SHR     r2, 4           ; the 12 line bits to bits 11..0, start bit = bit 0
        PUSH    r2
        DJNZ    r0, frame
done:   OUT     CLK, 1          ; the idle loop drives DATA high at the next falling edge
        JMP     idle
