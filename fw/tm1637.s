; fw/tm1637.s - TM1637 LED driver and key scan controller (two-wire bus).
;
; What it does:
;   Drives a Titan Micro TM1637 (data sheet V2.4: interface interpretation,
;   commands and timing, pages 2 to 5, 9 and 10) over its two-wire bus:
;   CLK = uio[0] (pin 0, UIO0) and DIO = uio[1] (pin 1, UIO1), both open
;   drain with the 10 kOhm pull-ups of the data sheet's hardware connection
;   drawing (page 8). Each host word is one byte of a transfer. The first
;   byte after a STOP (or after the start of the thread) gets a START: DIO
;   falls while CLK is high. A write byte goes out LSB first: DIO changes Q
;   cycles after each CLK fall and the chip takes it at the CLK rise. A read
;   byte (key scan data after the read-key command 0x42) is clocked out of
;   the chip: DIO is read HI1 cycles after each CLK rise, LSB first, and the
;   byte goes to the host. After each byte a ninth clock with DIO released
;   reads the chip's ACK (DIO low). A STOP (DIO rises while CLK is high)
;   follows a byte word that asks for it; between the bytes of a transfer
;   CLK stays low until the next host word.
;   Clock: CLK low Q + SU = 125 cycles and high HI1 + HI2 = 125 cycles, a
;   period of 4.167 us = 240 kHz at 60 MHz (the data sheet: key reads below
;   250 kHz, writes up to 500 kHz; PWCLK >= 400 ns, tSETUP and tHOLD >=
;   100 ns, tWAIT >= 1 us). DIO is read in the middle of the CLK high time,
;   long after the chip's output delay (tPLZ <= 300 ns after the CLK fall).
;
; Why:
;   The bus looks like I2C but is not (no address, LSB first, ACK from the
;   chip only, its own command set): the controller must make the START,
;   the STOP and the ninth clock itself and keep the key-read clock below
;   250 kHz.
;
; Host words to this thread, one for each byte: bits 7..0 the byte to write
;   (not used for a read), bit 8 STOP after the byte, bit 9 read a byte from
;   the chip.
; Words to the host: the byte of each read word; at each STOP the number of
;   bytes of the transfer that had no ACK (0 = every byte acknowledged).
; Registers: r0 host word, r1 data and work, r2 bit counter, r3 bytes
;   without ACK.

        .equ    CLK, UIO0
        .equ    DIO, UIO1
        .equ    Q, 62           ; CLK fall to the DIO change (tHOLD after the rise before it: HI1 + HI2 + Q)
        .equ    SU, 63          ; DIO change to the CLK rise (tSETUP 100 ns = 6 cycles)
        .equ    HI1, 62         ; CLK rise to the DIO read
        .equ    HI2, 63         ; DIO read to the CLK fall

get:    TSYNC   16              ; keep T near now while there is no word (isa 7)
        PULL.NB r0
        BEC     get
        TSYNC   48
        LDI     r3, 0           ; bytes without ACK in this transfer
        OUT     DIO, 0          ; START: DIO low while CLK is high
        TADD    HI2
        OUT     CLK, 0
byte:   MOV     r1, r0          ; T = the last CLK fall
        SHR     r1, 9
        ANDI    r1, 1
        BNE     read
        MOV     r1, r0
        LDI     r2, 8
wbit:   TADD    Q
        OUT     DIO, r1>>       ; bit 0 first (1 = released)
        TADD    SU
        OUT     CLK, 1
        TADD    HI1
        TADD    HI2
        OUT     CLK, 0
        DJNZ    r2, wbit
        JMP     ack
read:   LDI     r2, 8
rbit:   TADD    Q
        TADD    SU
        OUT     CLK, 1
        TADD    HI1
        SAMPLE  DIO, r1>>, 2    ; pin level HI1 after the rise (isa 6.2: t = p + 2)
        TADD    HI2
        OUT     CLK, 0
        DJNZ    r2, rbit
        SHR     r1, 8           ; the first bit read (LSB) to bit 0
        PUSH    r1
ack:    TADD    Q
        OUT     DIO, 1          ; release DIO for the chip's ACK
        TADD    SU
        OUT     CLK, 1
        TADD    HI1
        SAMPLE  DIO, r1>>, 2
        BEC     acked
        ADDI    r3, 1           ; no ACK
acked:  TADD    HI2
        OUT     CLK, 0
        MOV     r1, r0
        SHR     r1, 8
        ANDI    r1, 1
        BNE     stop
        PULL    r0              ; next byte of the transfer; CLK stays low
        TSYNC   32
        JMP     byte
stop:   TADD    Q
        OUT     DIO, 0
        TADD    SU
        OUT     CLK, 1
        TADD    HI1
        TADD    HI2
        OUT     DIO, 1          ; STOP: DIO high while CLK is high
        PUSH    r3
        TADD    HI1
        TADD    HI2             ; bus free time before the next START
        JMP     get
