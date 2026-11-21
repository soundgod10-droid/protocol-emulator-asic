; fw/ssi.s - SSI absolute encoder controller (synchronous serial interface, monoflop protocol).
;
; What it does:
;   Reads an absolute encoder with an SSI interface (Hengstler, "SSI Interface
;   for Absolute Encoders", knowledge center page: transmission process, block
;   diagram times, recommended clock rates) through RS-422 transceivers:
;   SSI_CLK = uo_out[4] (output pin 12, UO4; high when idle, also the pin's
;   reset level) and SSI_DATA = ui_in[3] (input pin 11, UI3).
;   For each read word from the host the thread first waits until DATA is high
;   (the encoder's monoflop has run out: a new transmission may start only
;   then); if DATA stays low for 4,000 cycles = 66.7 us (longer than the
;   longest monoflop time, 30 us), it reports "not ready". Then it makes N + 1
;   clock pulses with half period H: the first falling edge makes the encoder
;   store the position and start its monoflop, each rising edge shifts the
;   next bit to DATA (MSB first, 100 ns after the edge), and the thread reads
;   DATA just before each of the falling edges 2 to N + 1. After the last
;   rising edge (clock back to idle high) the encoder drives DATA low until
;   its monoflop time tm (10 to 30 us) has run out; the thread checks that
;   DATA is low H after that edge (a bit count or a line fault shows as a 1).
;   The default H = 90 cycles = 1.5 us gives 333 kHz (the page's rate for a
;   cable shorter than 50 m: below 400 kHz); the clock period stays far below
;   tm, as the monoflop requires.
;
; Why:
;   SSI is clocked by the controller, but the encoder's monoflop sets when a
;   transmission may start and ends it; the controller must count N + 1
;   pulses, read on the right edges and keep the clock period below tm.
;
; Host words to this thread: N (data bits, 1 to 30) and H (half period,
;   cycles, 16 or more, so that every clock event keeps its lead) once; then
;   one word for each read (its value is not used).
; Words to the host, for each read: bits 15..14 status (bit 15: DATA did not
;   go high in time, no read was made; bit 14: DATA was not low after the
;   last pulse), bits 13..0 data bits 29..16; then data bits 15..0.
; Registers: r0 high bits and status, r1 low bits, r2 counter, r3 work.
; Memory: NB (N), NH (N - 16 or 0), HH (H) after the code.

        .equ    CLK, UO4
        .equ    DATA, UI3

        TSYNC   32
        OUT     CLK, 1          ; idle high
        PULL    r2              ; N
        ST      r2, NB
        ADDI    r2, -16
        BMI     short
        ST      r2, NH          ; bits above the low word
        JMP     cfg
short:  LDI     r2, 0
        ST      r2, NH
cfg:    PULL    r3              ; H
        ST      r3, HH
idle:   TSYNC   32
        PULL.NB r0
        BEC     idle
        LDI     r0, 0           ; status and high bits
        LDI     r1, 0
        IOR     r3, PINS        ; DATA high: ready
        SHR     r3, 11
        ANDI    r3, 1
        BNE     ready
        TNOW    16
        TADD    4000
        WAIT    DATA, RISE, r3, TO
        BES     ready
        LDI     r0, 1           ; not ready
        SHL     r0, 15
        JMP     done
ready:  LD      r3, HH
        TSYNC   32
        OUT     CLK, 0          ; first falling edge: the encoder stores the position
        LD      r2, NH
        MOV     r2, r2
        BEQ     lowpart
hip:    TADD    r3
        OUT     CLK, 1          ; next bit to DATA
        TADD    r3
        OUT     CLK, 0
        SAMPLE  DATA, r1<<      ; at the falling edge's time: the level before it
        DJNZ    r2, hip
        MOV     r0, r1          ; bits above the low word
lowpart:
        LD      r2, NB
        LD      r1, NH
        SUB     r2, r1          ; bits in the low word
        LDI     r1, 0
lop:    TADD    r3
        OUT     CLK, 1
        TADD    r3
        OUT     CLK, 0
        SAMPLE  DATA, r1<<
        DJNZ    r2, lop
        TADD    r3
        OUT     CLK, 1          ; last rising edge: clock idle
        TADD    r3
        SAMPLE  DATA, r2<<      ; DATA must be low now (monoflop)
        BEC     done
        LDI     r2, 1
        SHL     r2, 14
        OR      r0, r2
done:   PUSH    r0
        PUSH    r1
        JMP     idle

NB:     .word   0
NH:     .word   0
HH:     .word   0
