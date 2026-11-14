; fw/swd_flat.s - SWD host with the bit loops in line (structure for
; comparison with fw/swd.s: same pins, host words and SWCLK timing, no
; subroutine calls).

        .equ    SWCLK, UO3
        .equ    SWDIO, UIO4
        .equ    H1, 12          ; falling to rising SWCLK edge (cycles)
        .equ    H2, 20          ; rising to falling SWCLK edge (cycles)

swd:    TNOW    8
        OUT     SWCLK, 0
cmd:    TSYNC   12
        PULL.NB r1
        BEC     cmd
        BMI     raw
        LDI     r3, 8
        TSYNC   12
rq:     OUT     SWDIO, r1>>     ; request
        TADD    H1
        OUT     SWCLK, 1
        TADD    H2
        OUT     SWCLK, 0
        DJNZ    r3, rq
        LDI     r3, 4
        TSYNC   12
        OUT     SWDIO, Z
ak:     TADD    H1              ; turnaround, ACK[0:2]
        OUT     SWCLK, 1
        SAMPLE  SWDIO, r2>>
        TADD    H2
        OUT     SWCLK, 0
        DJNZ    r3, ak
        PUSH    r2
        ANDI    r1, 4
        SHR     r2, 13
        CMPI    r2, 1
        BNE     fail
        MOV     r1, r1
        BNE     rd
        TSYNC   12              ; write: turnaround
        TADD    H1
        OUT     SWCLK, 1
        TADD    H2
        OUT     SWCLK, 0
        LDI     r2, 3
        JMP     rg
rd:     LDI     r1, 3           ; RDATA[0:15], RDATA[16:31], parity and turnaround
rl:     LDI     r3, 16
        CMPI    r1, 1
        BNE     rs
        LDI     r3, 2
rs:     TSYNC   12
rb:     TADD    H1
        OUT     SWCLK, 1
        SAMPLE  SWDIO, r2>>
        TADD    H2
        OUT     SWCLK, 0
        DJNZ    r3, rb
        PUSH    r2
        DJNZ    r1, rl
        JMP     cmd
fail:   TSYNC   12              ; turnaround
        TADD    H1
        OUT     SWCLK, 1
        TADD    H2
        OUT     SWCLK, 0
        MOV     r1, r1
        BNE     cmd
        LDI     r2, 3
fw:     PULL    r1
        DJNZ    r2, fw
        JMP     cmd
raw:    MOV     r2, r1
        ANDI    r2, 0xFF
rg:     PULL    r1
        LDI     r3, 16
        TSYNC   12
og:     OUT     SWDIO, r1>>
        TADD    H1
        OUT     SWCLK, 1
        TADD    H2
        OUT     SWCLK, 0
        DJNZ    r3, og
        DJNZ    r2, rg
        JMP     cmd
