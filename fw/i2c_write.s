; i2c_write.s: OUTLINE firmware for an I2C controller write (milestone M2a).
;
; What it does:
;   The thread writes bytes from its host FIFO on an I2C bus (SCL on UIO6,
;   SDA on UIO7, both open drain). One bit is LOW1 + LOW2 + HIGH1 + HIGH2 =
;   126 cycles: 396.8 kbit/s at 50 MHz (Fast-mode, maximum 400 kbit/s).
;   Host word to the thread: bits 7 to 0 = byte, bit 8 = 1: send START
;   before the byte, bit 9 = 1: send STOP after the byte.
;   Word to the host for each byte: the ACK bit (0 = ACK, 1 = NACK).
;
; Why these times:
;   SCL low = LOW1 + LOW2 = 70 cycles = 1.4 us. SCL high = HIGH1 + HIGH2 =
;   56 cycles = 1.12 us. SDA changes LOW1 = 35 cycles (700 ns) after SCL
;   falls, so the data set-up time before SCL rises is 700 ns. The
;   Fast-mode minimum values are tLOW 1.3 us, tHIGH 0.6 us, tSU;DAT 100 ns
;   (A1 section 3). The START hold time, the STOP set-up time and the bus
;   free time are 70 cycles (1.4 us); A1 does not give their minimum
;   values [OPEN].
;   SAMPLE reads SDA in the middle of each SCL high time. For the 9th bit
;   this is the ACK of the target. For the data bits, r1 gets the bus
;   value, which a later version can use for arbitration.
;
; Why it is an outline:
;   M2a must count the memory words for I2C (decision D2). This firmware
;   has no clock stretching (it does not read SCL), no arbitration, no
;   repeated START (START needs SCL high) and no read.
;
; Host configuration before RUN: output pins UIO6 (SCL) and UIO7 (SDA)
; belong to this thread, open-drain mode. External pull-up resistors.
; Registers: r0 = bits to send, r1 = bus values read, r2 = bit counter,
; r3 = control bits of the host word.

        .equ    SCL, UIO6       ; clock, open drain  [ENGINEER: pin choice]
        .equ    SDA, UIO7       ; data, open drain
        .equ    LOW1, 35        ; SCL fall to SDA change
        .equ    LOW2, 35        ; SDA change to SCL rise (data set-up time)
        .equ    HIGH1, 28       ; SCL rise to SDA sample
        .equ    HIGH2, 28       ; SDA sample to SCL fall
        .equ    HOLD, 70        ; START hold, STOP set-up, bus free time
        .equ    SDLY, 2         ; sample offset: SAMPLE at T + 2 reads the pin level of cycle T
        .equ    MARGIN, 32      ; the first OUT after TSYNC is 6 instructions later: 6 x 4 + 4 = 28, rounded up

start:  TNOW    MARGIN          ; T starts near now
        OUT     SCL, 1          ; release SCL (open drain: the pull-up makes it high)
        OUT     SDA, 1          ; release SDA
idle:   TSYNC   MARGIN          ; keep T near now while the FIFO is empty
        PULL.NB r0              ; get a host word; E = 1 if there was one
        BEC     idle            ; FIFO empty: try again
        MOV     r3, r0          ; r3 = host word
        SHR     r3, 9           ; C = bit 8 (START); bit 0 of r3 = bit 9 (STOP)
        BCC     data            ; no START: send the byte
        OUT     SDA, 0          ; START: SDA falls while SCL is high
        TADD    HOLD            ; START hold time
        OUT     SCL, 0          ; SCL low: the first data bit can change
data:   SHL     r0, 8           ; byte to bits 15 to 8 (MSB first)
        ORI     r0, 0x80        ; bit 7 = 1: the 9th bit releases SDA for the ACK
        LDI     r2, 9           ; 8 data bits and the ACK bit
ibit:   TADD    LOW1            ; T = SDA change time
        OUT     SDA, r0<<       ; SDA = next bit (1 = released), r0 shifts left
        TADD    LOW2            ; T = SCL rise time
        OUT     SCL, 1          ; release SCL: the target reads SDA
        TADD    HIGH1           ; T = middle of SCL high
        SAMPLE  SDA, r1<<, SDLY ; read SDA, bit into bit 0 of r1
        TADD    HIGH2           ; T = SCL fall time
        OUT     SCL, 0          ; SCL low
        DJNZ    r2, ibit        ; next bit if r2 is not 0
        ANDI    r1, 1           ; the last bit read is the ACK bit
        PUSH    r1              ; give the ACK bit to the host
        SHR     r3, 1           ; C = STOP bit
        BCC     idle            ; no STOP: SCL stays low, wait for the next byte
        TADD    LOW1            ; T = SDA change time
        OUT     SDA, 0          ; SDA low while SCL is low
        TADD    LOW2            ; T = SCL rise time
        OUT     SCL, 1          ; release SCL
        TADD    HOLD            ; STOP set-up time
        OUT     SDA, 1          ; STOP: SDA rises while SCL is high
        TADD    HOLD            ; bus free time before the next START
        JMP     idle            ; wait for the next host word
