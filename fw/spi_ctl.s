; fw/spi_ctl.s - SPI controller, all four modes, 8-bit transfers, MSB first.
;
; What it does:
;   Pins (push-pull): CS = uio[0], MOSI = uio[1], SCK = uio[3]; MISO =
;   uio[2] (input). SCK half period H comes from the host. For each host
;   word the thread sets CS low, sends 8 bits on MOSI and reads 8 bits on
;   MISO, and sends the received byte to the host. Bit 8 of the host word
;   sets CS high after the byte (end of the transfer).
;   Mode (CPOL, CPHA) is set with .equ when the host assembles the file:
;   each bit has a change edge (SCK to CL, MOSI changes MO cycles later)
;   and a sample edge (SCK to SL, MISO sampled D cycles later).
;   CPHA = 0: CL = CPOL; the first change edge does not change SCK.
;   CPHA = 1: CL = 1 - CPOL; the last edge (to IDLE) does not change SCK.
;
; Why:
;   G1 step 3a: measured word count of the full SPI controller of A1
;   section 3 and 8.2 (four modes, sample delay) for the memory size
;   (decision D2, D4 item 13). The sample delay D (0 to 7 cycles) is the
;   programmable delay of A1 section 9.3.
;
; Host words to this thread:
;   1. H: SCK half period in cycles. G1 model measurement, all 4 modes:
;      25 or more with D = 0 (1.0 MHz at 50 MHz), 32 or more with D = 7.
;   2. Then one word for each byte: bits 7 to 0 data, bit 8 = 1: CS high
;      after this byte.
; Word to the host: bits 7 to 0 the MISO byte. Bits 15 to 8 are not defined.
; Registers: r0 MOSI data, r1 MISO data, r2 bit count, r3 H.

        .equ    CS, UIO0
        .equ    MOSI, UIO1
        .equ    MISO, UIO2
        .equ    SCK, UIO3
        .equ    CPOL, 0
        .equ    CL, 0           ; CPHA = 0: CPOL. CPHA = 1: 1 - CPOL.
        .equ    SL, 1 - CL
        .equ    IDLE, CPOL
        .equ    D, 0            ; MISO sample delay after the sample edge, 0 to 7
        .equ    MO, 4           ; MOSI change after the change edge, 0 to 7

        PULL    r3              ; H
        TNOW    12
        OUT     CS, 1
        OUT     SCK, IDLE
idle:   TSYNC   16              ; keep T near now (isa 7)
        PULL.NB r0
        BEC     idle
byte:   TADD    r3              ; CS low H after the last SCK edge (or after CS high)
        OUT     CS, 0
        SHL     r0, 8           ; data to bits 15 to 8; C = bit 8 (CS high after)
        LDI     r2, 8
bit:    TADD    r3
        OUT     SCK, CL         ; change edge
        OUT     MOSI, r0<<, MO
        TADD    r3
        OUT     SCK, SL         ; sample edge
        SAMPLE  MISO, r1<<, D
        DJNZ    r2, bit
        PUSH    r1
        TADD    r3
        OUT     SCK, IDLE
        BCC     next            ; C from SHL: no flag instruction since then
        OUT     CS, 1, 7        ; CS stays high H - 7 cycles before the next CS low
next:   PULL.NB r0
        BES     byte
        JMP     idle
