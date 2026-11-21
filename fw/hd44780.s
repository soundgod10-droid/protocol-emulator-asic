; fw/hd44780.s - HD44780U character LCD controller, 4-bit interface with busy flag read.
;
; What it does:
;   Drives an HD44780U LCD controller (Hitachi HD44780U data sheet ADE-207-272(Z),
;   Rev 0.0) on its 4-bit interface: DB4..DB7 = uio[0]..uio[3] (output pins
;   0 to 3, UIO0 to UIO3, push-pull while the chip writes, released while the
;   LCD drives them), E = uio[5] (UIO5, with a pull-down on the board, so that
;   E is low while the chip is in reset or stopped), RS = uo_out[4] (UO4) and
;   R/W = uo_out[5] (UO5). DB0..DB3 of the LCD are not connected (data sheet
;   page 22: disabled in 4-bit mode). Supply of the LCD: 2.7 to 4.5 V (3.3 V
;   logic), so the bus times of the data sheet table for VCC = 2.7 to 4.5 V
;   apply (page 49).
;   Each byte goes as two 4-bit transfers, the high nibble first (page 22,
;   Figure 9). One transfer (one E pulse): RS, R/W and DB change at the
;   transfer start, E rises TAS = 6 cycles later (100 ns; tAS 60 ns), falls
;   72 cycles after its rise (1.2 us... see TE; PWEH 450 ns), and the next
;   transfer starts 30 cycles after the fall: E cycle TC = 84 cycles = 1.4 us
;   (tcycE 1,000 ns), data set-up to the E fall 900 ns (tDSW 195 ns), data and
;   address hold after the E fall 500 ns (tH 10 ns, tAH 20 ns). A read
;   transfer releases DB4..DB7 and reads them with port PINS TS = 32 cycles
;   after the transfer start: 26 cycles = 433 ns after the E rise (tDDR
;   360 ns maximum), before the E fall (the LCD holds the data until tDHR
;   after the fall).
;   After the start the thread waits PON x 4,000 cycles = 40.7 ms (more than
;   40 ms after VCC rises to 2.7 V) and initializes the LCD by instruction for
;   the 4-bit interface (Figure 24, page 46): function set 0x3 (one transfer),
;   4.27 ms (more than 4.1 ms), 0x3, 135 us (more than 100 us), 0x3, 135 us
;   (longer than the 37 us execution time, also at fOSC = 190 kHz: 53 us),
;   0x2 (4-bit interface). From then on it reads the busy flag before each
;   instruction or data transfer (page 24: no instruction while BF = 1).
;
; Why:
;   The HD44780U has no clock line: every bus time is set by the E pulse, and
;   the execution times depend on the LCD's own oscillator (Table 6: 37 us,
;   1.52 ms at 270 kHz; 190 to 350 kHz on page 49), so a writer must read the
;   busy flag through the same 4-bit bus, turning the data lines around. The
;   firmware does the transfers, the turnarounds, the busy wait and the
;   initialization; the host sends bytes.
;
; Host words to this thread (after the initialization; earlier words wait):
;   bits 7..0 the byte, bit 8 RS (0 instruction, 1 data), bit 9 = 1: read (the
;   thread reads a byte with this RS instead of writing: RS = 0 gives BF and
;   the address counter, RS = 1 the RAM data at the address counter). The
;   host sends the function set (4-bit, N, F), display, clear and entry mode
;   instructions itself (Figure 24, after the 0x2 transfer).
; Words to the host: for a read word, the byte read (bits 7..0); 0xFFFF if
;   the busy flag stayed 1 for BUSYN reads (2.9 ms: longer than the 1.52 ms
;   of clear display and return home at the slowest oscillator, 2.16 ms at
;   190 kHz); then the word is dropped.
; Registers: r0 nibble and work, r1 bytes read, r2 counter, r3 return address.
; Memory: CMD after the code.

        .equ    DB4, UIO0
        .equ    DB5, UIO1
        .equ    DB6, UIO2
        .equ    DB7, UIO3
        .equ    E, UIO5
        .equ    RS, UO4
        .equ    RW, UO5
        .equ    TAS, 6          ; E rise after the transfer start: 100 ns (tAS 60 ns)
        .equ    TS, 32          ; read of DB after the transfer start: E rise + 433 ns (tDDR 360 ns)
        .equ    TE, 54          ; E fall after the transfer start: E high 48 cycles = 800 ns (PWEH 450 ns)
        .equ    TC, 84          ; next transfer start: E cycle 1.4 us (tcycE 1,000 ns)
        .equ    PON, 610        ; 610 x 4,000 cycles = 40.7 ms after the start
        .equ    W1, 64          ; 64 x 4,000 cycles = 4.27 ms
        .equ    W2, 2           ; 2 x 4,000 cycles = 133 us
        .equ    TADDW, 540      ; 9.0 us after BF = 0 before a read (tADD = 1.5 / fOSC: 7.9 us at 190 kHz)
        .equ    BUSYN, 1023     ; busy reads before the time-out: 1,023 x 2.8 us = 2.9 ms

        TSYNC   32
        OUT     E, 0
        OUT     RS, 0
        OUT     RW, 0
        LDI     r2, PON
        JAL     r3, wait
        LDI     r0, 3           ; function set, 8-bit interface (BF cannot be checked)
        JAL     r3, wnib
        LDI     r2, W1
        JAL     r3, wait
        LDI     r0, 3
        JAL     r3, wnib
        LDI     r2, W2
        JAL     r3, wait
        LDI     r0, 3
        JAL     r3, wnib
        LDI     r2, W2
        JAL     r3, wait
        LDI     r0, 2           ; function set: 4-bit interface
        JAL     r3, wnib
idle:   TSYNC   32
        PULL.NB r0
        BEC     idle
        ST      r0, CMD
        LDI     r2, BUSYN
poll:   OUT     RS, 0           ; read busy flag and address
        OUT     RW, 1
        JAL     r3, rnib
        JAL     r3, rnib
        MOV     r0, r1
        ANDI    r0, 128
        BEQ     ready
        DJNZ    r2, poll
        LDI     r0, 0           ; time-out
        NOT     r0, r0
        PUSH    r0
        JMP     idle
ready:  LD      r0, CMD
        MOV     r1, r0
        SHR     r1, 8
        MOV     r2, r1
        ANDI    r2, 2
        BNE     read
        OUT     RS, r1>>
        OUT     RW, 0
        SHR     r0, 4           ; high nibble
        JAL     r3, wnib
        LD      r0, CMD         ; low nibble
        JAL     r3, wnib
        JMP     idle
read:   TADD    TADDW
        OUT     RS, r1>>
        OUT     RW, 1
        JAL     r3, rnib
        JAL     r3, rnib
        ANDI    r1, 255
        PUSH    r1
        JMP     idle

; Write transfer at T: DB4..DB7 = r0 bits 0..3 (RS and R/W set by the caller
; at T). T ends at the next transfer start.
wnib:   OUT     DB4, r0>>
        OUT     DB5, r0>>
        OUT     DB6, r0>>
        OUT     DB7, r0>>
        OUT     E, 1, TAS
        TADD    TE
        OUT     E, 0
        TADD    TC-TE
        JR      r3

; Read transfer at T: r1 = (r1 << 4) | DB7..DB4. T ends at the next transfer
; start.
rnib:   OUT     DB4, Z
        OUT     DB5, Z
        OUT     DB6, Z
        OUT     DB7, Z
        OUT     E, 1, TAS
        TADD    TS
        TWAIT
        IOR     r0, PINS        ; DB4..DB7 = input pins 0..3
        TADD    TE-TS
        OUT     E, 0
        TADD    TC-TE
        SHL     r1, 4
        ANDI    r0, 15
        OR      r1, r0
        JR      r3

; Wait r2 x 4,000 cycles from T.
wait:   TADD    4000
        TWAIT
        DJNZ    r2, wait
        JR      r3

CMD:    .word   0
