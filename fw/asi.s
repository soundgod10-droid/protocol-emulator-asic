; fw/asi.s - AS-Interface master at the logic pins of an AS-i transceiver IC in Master Mode: requests out, slave responses in.
;
; What it does:
;   Sends AS-i master requests and reads the slave responses through the
;   digital Manchester-II interface of an ASI4U in Master Mode (IDT/Renesas
;   ASI4U data sheet, 26 January 2016, sections 3.3.2, 4.3.4, 4.9.3, 4.12):
;   IRD (request in to the IC) = uo_out[4] (UO4), LED1 (response out of the
;   IC) = ui_in[3] (UI3). Board wiring: DI0 = DI1 (IRD active low, Table 4.6)
;   and DI2 /= DI3 (LED1 active low, Table 4.21), so both lines idle high, a
;   telegram starts with a falling edge in the middle of the start bit, and
;   each bit is a falling (0) or rising (1) edge in the middle of its 6 us bit
;   time (Figure 4.13).
;   Master request (14 bits, 84 us): ST = 0, CB, A4..A0, I4..I0, PB, EB = 1;
;   PB makes CB..I0 + PB even (section 4.12, Parity_error of the master request). The end-bit edge
;   comes 13 x 6 us = 78 us after the start edge (End_bit_error).
;   Slave response (7 bits): ST, I3..I0, PB, EB, read at a quarter bit before
;   and after each bit middle, timed from the start edge; up to TRESP cycles
;   after the end of the request (10 bit times of master pause and the half
;   start bit, with margin).
;
; Why:
;   AS-i is a field bus for binary sensors and actuators; the transceiver IC
;   does the line coding, the master logic (requests, timing, checks) is a
;   timed program.
;
; Host words to this thread: bits 10..0 = CB, A4..A0, I4..I0 of one request.
; Words to the host, one per request: 8000h | I3..I0 = valid response;
;   4000h = response with a Manchester, end-bit or parity error;
;   2000h = no response.
; Registers: r0 request / response, r1 inverted copy / levels, r2 work, r3 count.

        .equ    IRD, UO4
        .equ    LED1, UI3
        .equ    HB, 180         ; half bit time (3 us; bit time 6 us)
        .equ    TRESP, 3900     ; end of request to the response start edge, latest (65 us)
        .equ    Q1, 270         ; start edge to the first read (bit 1 middle - a quarter bit)
        .equ    QB, 180         ; between the two reads of a bit, and to the next bit
        .equ    GAP, 720        ; pause before the next request (2 bit times)

        TNOW    16
        OUT     IRD, 1
idle:   TNOW    8
        PULL.NB r0
        BEC     idle
        SHL     r0, 5           ; keep bits 10..0
        SHR     r0, 5
        MOV     r2, r0
        LDI     r1, 0
        LDI     r3, 11
par:    XOR     r1, r2
        SHR     r2, 1
        DJNZ    r3, par
        ANDI    r1, 1           ; PB
        SHL     r0, 1
        OR      r0, r1
        SHL     r0, 1
        ORI     r0, 1           ; EB
        SHL     r0, 2           ; ST (0) in bit 15
        NOT     r1, r0
        LDI     r3, 14
        TNOW    24
tx:     OUT     IRD, r1<<       ; first half: not the bit
        TADD    HB
        OUT     IRD, r0<<       ; second half: the bit (edge in the middle)
        TADD    HB
        DJNZ    r3, tx
        TADD    TRESP
        WAIT    LED1, FALL, r2, TO, CT
        BEC     noresp
        TADD    Q1
        LDI     r0, 0
        LDI     r1, 0
        LDI     r3, 6
rx:     SAMPLE  LED1, r1<<      ; before the bit middle
        TADD    QB
        SAMPLE  LED1, r0<<      ; after it: the bit
        TADD    QB
        DJNZ    r3, rx
        XOR     r1, r0
        ANDI    r1, 63
        CMPI    r1, 63          ; every bit has its middle edge
        BNE     bad
        MOV     r2, r0
        ANDI    r2, 1
        BEQ     bad             ; end bit 0
        SHR     r0, 1           ; I3..I0, PB
        MOV     r2, r0
        LDI     r1, 0
        LDI     r3, 5
par2:   XOR     r1, r2
        SHR     r2, 1
        DJNZ    r3, par2
        ANDI    r1, 1
        BNE     bad             ; odd
        SHR     r0, 1
        ANDI    r0, 15
        LDI     r1, 1
        SHL     r1, 15
        OR      r0, r1
        JMP     done
bad:    LDI     r0, 1
        SHL     r0, 14
        JMP     done
noresp: LDI     r0, 1
        SHL     r0, 13
done:   PUSH    r0
        TADD    GAP
        TWAIT
        JMP     idle
