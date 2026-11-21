; fw/sent.s - SENT transmitter (Single-Edge Nibble Transmission), fast channel with 6 data nibbles.
;
; What it does:
;   Sends SENT messages on SENT_OUT = uio[0] (pin 0, UIO0), open drain with a
;   pull-up on the board (idle high), as the Microchip dsPIC33/PIC24 Family
;   Reference Manual section DS70005145B (SENT module, based on SAE J2716)
;   describes them in 3.0 and 4.2: the time between falling edges carries
;   the data, in units of the tick TICK (3 us; 3 to 90 us allowed).
;     Sync/calibration period   56 ticks
;     status nibble             12 + value ticks (value 0 to 15)
;     6 data nibbles            12 + value ticks each
;     CRC nibble                12 + value ticks: CRC-4 of the 6 data nibbles,
;                               x^4 + x^3 + x^2 + 1, seed 0101, then one more
;                               step with a 0 nibble (Example 4-1)
;     pause pulse               the rest of PERIOD ticks (12 to 768 ticks), so
;                               that every message lasts PERIOD ticks
;   Each period starts with a falling edge: the line is driven low for LOWN
;   = 5 ticks and then released for the rest of the period (3.0).
;   The firmware sends messages without a stop. Each message takes the next
;   two host words if they are in the FIFO at the end of the sync's low
;   time; if not, it sends the values of the last message again (a sensor
;   repeats its latest value). Before the first two words the line stays
;   released (high).
;   PERIOD must be at least 56 + 8 x 27 + 12 = 284 ticks (longest message
;   and the shortest pause); PERIOD 300 ticks = 900 us at TICK 3 us.
;
; Why:
;   SENT sends a 24-bit sensor value as eight pulse periods measured in ticks
;   of a free clock, with its own CRC-4 and a calibration pulse, which a UART
;   or a timer peripheral does not make.
;
; Host words to this thread, two for each message:
;   1. bits 15..12 status nibble, bits 11..8 D1, 7..4 D2, 3..0 D3
;   2. bits 11..8 D4, 7..4 D5, 3..0 D6 (bits 15..12 not used)
;   D1 is sent first. Both words of a message must be in the FIFO together.
; Words to the host: none.
; Registers: r0 word / tick time / work, r1 data memory pointer, r2 link,
;   r3 tick count / CRC / pause.
; Memory: W1, W2 (last host words), PAUSEH, TICKW; data memory 512 to 519
;   holds the released time of the 8 nibbles in ticks (7 + value).

        .equ    SENT_OUT, UIO0
        .equ    TICK, 180       ; tick: 3.000 us at 60 MHz
        .equ    PERIOD, 300     ; message period in ticks, sync to sync
        .equ    LOWN, 5         ; low time of each period in ticks
        .equ    SYNCN, 56       ; sync/calibration period in ticks
        .equ    SEED, 5         ; CRC seed 0101
        .equ    NIBS, 512       ; data memory: nibble 0 (status) to 7 (CRC)

first:  TSYNC   8               ; no message before the first two words
        PULL.NB r0
        BEC     first
        ST      r0, W1
        PULL    r0
        ST      r0, W2
        TSYNC   32
frame:  OUT     SENT_OUT, 0     ; sync: falling edge at T
        LDI     r3, LOWN
        JAL     r2, ticks
        OUT     SENT_OUT, 1     ; release after LOWN ticks
        PULL.NB r0              ; the next values, if the host sent them
        BEC     same
        ST      r0, W1
        PULL    r0
        ST      r0, W2
same:   LDI     r1, NIBS        ; nibbles 0 to 6: status, D1 to D6
        LD      r0, W1
        SHR     r0, 12
        JAL     r2, put
        LD      r0, W1
        SHR     r0, 8
        JAL     r2, put
        LD      r0, W1
        SHR     r0, 4
        JAL     r2, put
        LD      r0, W1
        JAL     r2, put
        LD      r0, W2
        SHR     r0, 8
        JAL     r2, put
        LD      r0, W2
        SHR     r0, 4
        JAL     r2, put
        LD      r0, W2
        JAL     r2, put
        LDI     r1, NIBS + 1    ; CRC over D1 to D6 (status not included)
        LDI     r3, SEED
crcl:   JAL     r2, mul16       ; r3 = table(r3)
        LDX     r0, [r1]
        ADDI    r0, -7
        XOR     r3, r0          ; r3 = nibble xor table(r3)
        ADDI    r1, 1
        MOV     r0, r1
        ANDI    r0, 15
        CMPI    r0, 7
        BNE     crcl
        JAL     r2, mul16       ; one more step with a 0 nibble
        ADDI    r3, 7
        STX     r3, [r1]        ; nibble 7: CRC
        LDI     r1, NIBS        ; pause: PERIOD - SYNCN - sum(12 + v) - LOWN released ticks
        LDI     r3, PERIOD - SYNCN - LOWN - 40
suml:   LDX     r0, [r1]        ; 7 + v (the 5 low ticks of each nibble are in the 40)
        SUB     r3, r0
        ADDI    r1, 1
        MOV     r0, r1
        ANDI    r0, 15
        CMPI    r0, 8
        BNE     suml
        ST      r3, PAUSEH
        LDI     r3, SYNCN - LOWN
        JAL     r2, ticks       ; rest of the sync period
        LDI     r1, NIBS
nib:    OUT     SENT_OUT, 0     ; falling edge of the nibble
        LDI     r3, LOWN
        JAL     r2, ticks
        OUT     SENT_OUT, 1
        LDX     r3, [r1]        ; 7 + v ticks released
        JAL     r2, ticks
        ADDI    r1, 1
        MOV     r0, r1
        ANDI    r0, 15
        CMPI    r0, 8
        BNE     nib
        OUT     SENT_OUT, 0     ; pause pulse
        LDI     r3, LOWN
        JAL     r2, ticks
        OUT     SENT_OUT, 1
        LD      r3, PAUSEH
        JAL     r2, ticks
        JMP     frame           ; the next sync falls at T

ticks:  LD      r0, TICKW       ; T += r3 ticks; link r2; each TADD waits for T first, so the lead stays
tk:     TWAIT                   ; one tick or less (T stays within the 32,767-cycle horizon)
        TADD    r0
        DJNZ    r3, tk
        JR      r2

put:    ANDI    r0, 15          ; store 7 + nibble at [r1], r1 + 1; link r2
        ADDI    r0, 7
        STX     r0, [r1]
        ADDI    r1, 1
        JR      r2

mul16:  SHL     r3, 1           ; r3 = r3 x 16 mod x^4 + x^3 + x^2 + 1 (the CRC table of Example 4-1); uses r0
        MOV     r0, r3
        ANDI    r0, 16
        BEQ     m1
        XORI    r3, 29
m1:     SHL     r3, 1
        MOV     r0, r3
        ANDI    r0, 16
        BEQ     m2
        XORI    r3, 29
m2:     SHL     r3, 1
        MOV     r0, r3
        ANDI    r0, 16
        BEQ     m3
        XORI    r3, 29
m3:     SHL     r3, 1
        MOV     r0, r3
        ANDI    r0, 16
        BEQ     m4
        XORI    r3, 29
m4:     JR      r2

W1:     .word   0
W2:     .word   0
PAUSEH: .word   0
TICKW:  .word   TICK
