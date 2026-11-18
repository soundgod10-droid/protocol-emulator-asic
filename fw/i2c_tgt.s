; fw/i2c_tgt.s - I2C target: 7-bit address, a 256-byte register map with an
; address pointer, write, random read and current-address read.
;
; What it does:
;   Pins (open drain, external pull-ups): SCL = uio[6], SDA = uio[7]. The
;   thread waits for a START (SDA falls while SCL is high), receives the
;   address byte and ACKs it when the 7-bit address is ADDR.
;   Write (R/W = 0): the first data byte sets the pointer, each next byte is
;   written to map[pointer] and the pointer counts up (it wraps at 256); every
;   byte is ACKed. A STOP or a repeated START ends the write.
;   Read (R/W = 1): the thread sends map[pointer], pointer + 1, as long as the
;   controller ACKs; after a NACK it waits for the STOP or a repeated START.
;   The pointer then points after the last byte sent.
;   The map is data memory words MEM to MEM + 255 (one byte in each), the
;   pointer is word PTR, the state of a write is word STATE. The thread
;   clears all of them at the start.
;
; Timing (docs/isa.md 6.1: an edge of SCL or SDA reaches the thread 7 cycles
; after the pin, the time of a WAIT edge already includes them):
;   - Receive: after each SCL rise the thread reads SDA with IOR PINS (5 to 8
;     cycles after the pin edge), then reads SCL and SDA every 20 cycles until
;     SCL falls. An SDA change while SCL is still high is a START (SDA fell)
;     or a STOP (SDA rose). This holds for a high time of 36 cycles or more
;     (Fast-mode tHIGH 0.6 us = 36 cycles at 60 MHz, NXP UM10204 Rev. 7.0
;     Table 11): a START or STOP condition lasts at least tSU;STO or tHD;STA
;     (0.6 us), longer than the 20-cycle read period.
;   - After the 8th bit of each received byte the thread holds SCL low (clock
;     stretching, UM10204 3.1.9) at most 67 cycles after the SCL fall on the
;     pin (7 cycles to the thread, up to 19 to the next read of the pins, 41
;     to the pin event), before the controller can end its LOW period (tLOW
;     1.3 us = 78 cycles). While SCL is low it decides on the ACK, writes the byte, sets
;     SDA, and releases SCL TSU cycles after the SDA change. Table 11 note [3]:
;     a device that stretches must have the data valid by the set-up time
;     before it releases the clock; TSU 80 cycles (1.33 us) covers note [4]
;     (tr(max) + tSU;DAT = 1,250 ns for Standard-mode).
;   - Transmit: each bit is put on SDA HOLD cycles after the filtered SCL fall
;     (the pin changes 7 + HOLD + 1 = 26 cycles = 433 ns after the SCL pin
;     edge: more than the 300 ns fall time tf(max) of Table 11, note [2], and
;     less than tVD;DAT 0.9 us).
;     After the 8th bit the thread releases SDA for the ACK and holds SCL low
;     while it loads the next byte.
;
; Host words: none. The host loads the program and starts the thread.
; Registers: r0 byte, r1 bit counter, r2 sampled SDA or pointer, r3 PINS or
;   an address.

        .equ    SCL, UIO6
        .equ    SDA, UIO7
        .equ    ADDR, 0x50      ; 7-bit target address
        .equ    MEM, 512        ; register map: data memory words MEM to MEM + 255
        .equ    PTR, 768        ; data memory word of the pointer
        .equ    STATE, 769      ; 0: address byte next, 1: pointer byte next, 2: data bytes
        .equ    HOLD, 18        ; SDA change after the filtered SCL fall
        .equ    TSU, 80         ; SDA change to the SCL release while the thread stretches

start:  LDI     r0, MEM
        LDI     r1, 258         ; map, PTR and STATE
        LDI     r2, 0
clr:    STX     r2, [r0]
        ADDI    r0, 1
        DJNZ    r1, clr
        TNOW    8
        OUT     SCL, 1          ; release both lines
        OUT     SDA, 1
idle:   WAIT    SDA, FALL, r2   ; an SDA fall (no time limit)
        IOR     r2, PINS
        ANDI    r2, 0x40        ; SCL high: a START
        BEQ     idle
startc: LDI     r0, 0
        LDI     r3, STATE
        STX     r0, [r3]        ; address byte next
rx:     LDI     r1, 8
        LDI     r0, 0
rbit:   WAIT    SCL, RISE, r2
        SHL     r0, 1
        IOR     r2, PINS
        ANDI    r2, 0x80        ; SDA at the start of the high period
        BEQ     hold
        ORI     r0, 1
hold:   IOR     r3, PINS        ; every 20 cycles while SCL is high and SDA does not change
        ANDI    r3, 0xC0
        XOR     r3, r2
        CMPI    r3, 0x40
        BEQ     hold
        ANDI    r3, 0x40        ; SCL still high: SDA changed, a START or a STOP
        BNE     cond
        DJNZ    r1, rbit
        TNOW    8
        OUT     SCL, 0          ; byte complete: hold SCL low
        LDI     r3, STATE
        LDX     r2, [r3]
        MOV     r2, r2
        BNE     data
        MOV     r1, r0          ; address byte
        ANDI    r1, 1           ; R/W
        SHR     r0, 1
        CMPI    r0, ADDR
        BNE     nomatch
        TNOW    8
        OUT     SDA, 0          ; ACK
        MOV     r1, r1
        BNE     rd
        LDI     r2, 1           ; write: pointer byte next
        STX     r2, [r3]
        JMP     ack
nomatch: TNOW   8
        OUT     SCL, 1          ; not this target: no ACK
        JMP     idle
cond:   MOV     r2, r2
        BNE     startc          ; SDA fell: repeated START
        JMP     idle            ; SDA rose: STOP
data:   CMPI    r2, 1
        BNE     wr
        LDI     r3, PTR
        STX     r0, [r3]        ; pointer = first data byte
        LDI     r2, 2
        LDI     r3, STATE
        STX     r2, [r3]
        JMP     ackd
wr:     LDI     r3, PTR
        LDX     r2, [r3]
        LDI     r1, MEM
        ADD     r1, r2
        STX     r0, [r1]        ; map[pointer] = byte
        ADDI    r2, 1
        ANDI    r2, 0xFF
        STX     r2, [r3]
ackd:   TNOW    8
        OUT     SDA, 0          ; ACK
ack:    TADD    TSU
        OUT     SCL, 1          ; release SCL: the ACK clock pulse
        WAIT    SCL, FALL, r2, CT
        TADD    HOLD
        OUT     SDA, 1          ; release SDA after the ACK clock pulse
        JMP     rx
rd:     LDI     r3, PTR         ; read: SCL is held low and SDA is the ACK
        LDX     r2, [r3]
        LDI     r1, MEM
        ADD     r1, r2
        LDX     r0, [r1]        ; first byte: map[pointer]
        ADDI    r2, 1
        ANDI    r2, 0xFF
        STX     r2, [r3]        ; pointer + 1
        SHL     r0, 8
        LDI     r1, 8
        TADD    TSU
        TSYNC   8
        OUT     SCL, 1          ; release SCL: the ACK clock pulse of the address
        JMP     tbit
txack:  WAIT    SCL, RISE, r3   ; ACK clock pulse of the controller
        IOR     r3, PINS
        ANDI    r3, 0x80
        BNE     nack
        LDI     r1, 8
tbit:   WAIT    SCL, FALL, r3, CT ; end of the clock pulse before this bit
        TADD    HOLD
        OUT     SDA, r0<<       ; data bit (1: released)
        DJNZ    r1, tbit
        WAIT    SCL, FALL, r3, CT ; end of the 8th bit
        TADD    HOLD
        OUT     SDA, 1          ; release SDA for the ACK of the controller
        OUT     SCL, 0, 4       ; hold SCL low while the next byte is loaded
        LDI     r3, PTR
        LDX     r2, [r3]
        LDI     r1, MEM
        ADD     r1, r2
        LDX     r0, [r1]        ; next byte: map[pointer]
        ADDI    r2, 1
        ANDI    r2, 0xFF
        STX     r2, [r3]        ; pointer + 1 (taken back after a NACK)
        SHL     r0, 8
        TADD    TSU
        TSYNC   8
        OUT     SCL, 1
        JMP     txack
nack:   ADDI    r2, -1          ; the byte loaded after the last one is not sent
        ANDI    r2, 0xFF
        LDI     r3, PTR
        STX     r2, [r3]
        JMP     idle
