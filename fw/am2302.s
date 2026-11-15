; am2302.s: AM2302 (DHT22) humidity and temperature sensor reader for one thread.
;
; What it does:
;   A protocol that the chip was not designed for: written from the AM2302 data
;   sheet only, on the unchanged instruction set and assembler.
;   Each host word (any value) starts one reading:
;     start signal: the bus low for (NBE + 1) x STEP cycles, then released;
;     the sensor answers (low, then high) and sends 40 bits, MSB first:
;     16 bits humidity, 16 bits temperature, 8 bits check sum.
;   Reply: a status word, then (if the status is 0) three data words:
;     0x0000, humidity (bits 15..0), temperature (bits 15..0), check sum (bits 7..0).
;     A status with bit 15 = 1 is a timeout and no data words follow:
;     bits 5..0 = 41 if the sensor did not answer, else the number of bits not
;     received in the group (16, 16, 8) that was being read.
;   The host checks the check sum (the low 8 bits of the sum of the four data
;   bytes) and waits at least 2 s between two readings (data sheet).
;
; Timing (AM2302 data sheet, pages 3 to 5; the values are typical, no limits given):
;   start signal: the bus low for "1ms minimum" (figure page 4; the text says
;   "at least 1~10ms"); here 40 x 3,000 cycles = 2.0 ms at 60 MHz, 2.4 ms at 50 MHz.
;   The sensor waits 20 to 40 us after the release, pulls the bus low for 80 us,
;   releases it for 80 us, then sends each bit as 50 us low and 26 to 28 us high
;   ("0") or 70 us high ("1").
;   The firmware takes T from each rising edge (WAIT ... CT) and samples the bus
;   48 us later: a "0" is low again by then, a "1" is still high. The high time of
;   a bit ends with the low of the next bit (figures, page 4), so after the last
;   bit the sensor also pulls the bus low. 48 us is between 28 us and 70 us, so a
;   sensor whose times are 0.69 to 1.71 times the data sheet values still reads
;   correctly (test_holdout_am2302 runs 0.8 and 1.2).
;   Every WAIT has a time limit (TO): a missing or a stopped sensor gives a timeout
;   status, not a thread that waits for ever.
; The .equ values below are cycles at 60 MHz (60 cycles for each us); at 50 MHz the
; host assembles with T48 2400 (the other values are the same).
; Each time comes from T (TADD), not from the time that the instructions take.
; A TADD adds at most 4,095 cycles (68 us at 60 MHz, docs/isa.md 7), so each time
; limit for a sensor edge that can be 80 us x 1.2 away is two TADD TR2 (136 us at
; 60 MHz, 164 us at 50 MHz).
;
; Host configuration before RUN: output pin DHT belongs to this thread, open-drain.
; The bus needs an external pull-up resistor; the sensor works from 3.3 V (data
; sheet page 5: supply 3.3 to 6 V).
; Registers: r0 = host word and link, r1 = bit counter, r2 = bits, r3 = edge times.

        .equ    DHT, UIO5       ; sensor data pin (output and input)
        .equ    STEP, 3000      ; start signal: the bus low for (NBE + 1) x STEP cycles
        .equ    NBE, 39         ; 40 x 3,000 = 120,000 cycles
        .equ    TRESP, 4000     ; 67 us (80 us at 50 MHz): time limit for the answer, which comes after 20 to 40 us
        .equ    TR2, 4095       ; 68 us (82 us at 50 MHz): half of the time limit for a next edge
        .equ    T48, 2880       ; 48 us: bit sample after the rising edge
        .equ    MARGIN, 16      ; the first OUT is 3 instructions after TSYNC: 3 x 4 + 4 = 16

start:  TNOW    MARGIN          ; T = now + MARGIN
idle:   TSYNC   MARGIN          ; keep T near now while the FIFO is empty (isa 7)
        PULL.NB r0              ; any host word starts a reading
        BEC     idle
        OUT     DHT, 0          ; start signal: bus low at T
        LDI     r1, NBE
low:    TADD    STEP
        TWAIT                   ; keeps T inside the event horizon (isa 4.2)
        DJNZ    r1, low
        TADD    STEP
        OUT     DHT, Z          ; release after (NBE + 1) x STEP
        LDI     r1, 41          ; timeout status if the sensor does not answer
        TADD    TRESP
        WAIT    DHT, FALL, r3, TO, CT   ; answer: the sensor pulls the bus low
        BEC     fail
        TADD    TR2
        TADD    TR2
        WAIT    DHT, RISE, r3, TO, CT   ; end of the 80 us low
        BEC     fail
        TADD    TR2
        TADD    TR2
        WAIT    DHT, FALL, r3, TO, CT   ; end of the 80 us high: the first bit starts
        BEC     fail
        TADD    TR2                     ; 68 us for the 50 us low of the first bit
        LDI     r1, 16
        JAL     r0, bits        ; humidity
        ST      r2, hum
        LDI     r1, 16
        JAL     r0, bits        ; temperature
        ST      r2, tmp
        LDI     r1, 8
        JAL     r0, bits        ; check sum
        ANDI    r2, 255
        LDI     r0, 0
        PUSH    r0              ; status 0: three data words follow
        LD      r0, hum
        PUSH    r0
        LD      r0, tmp
        PUSH    r0
        PUSH    r2
        JMP     idle
bits:   WAIT    DHT, RISE, r3, TO, CT   ; T = rising edge of the bit, after its 50 us low
        BEC     fail
        TADD    T48
        SAMPLE  DHT, r2<<, 0    ; 48 us after the rise: "0" is low again, "1" is still high
        TADD    TR2
        TADD    TR2             ; time limit for the next rising edge: 184 us after this one
        DJNZ    r1, bits
        JR      r0
fail:   LDI     r2, 1
        SHL     r2, 15
        OR      r2, r1          ; 0x8000 + bits not received (41: no answer)
        PUSH    r2
        JMP     idle
hum:    .word   0
tmp:    .word   0
