; spi_mode0.s: OUTLINE firmware for an SPI controller, mode 0 (milestone M2a).
;
; What it does:
;   The thread sends each byte from its host FIFO on MOSI, MSB first, and
;   receives one byte on MISO at the same time. Mode 0: SCK is low when
;   idle (CPOL = 0). MOSI changes at the falling edge of SCK, and both
;   sides read the data at the rising edge (CPHA = 0). SCK = 50 MHz /
;   (2 x H) = 1 MHz. CS is low during the transfer.
;   Host word to the thread: bits 7 to 0 = byte to send. Bit 8 = 1: set
;   CS high after this byte (end of the transfer). Bit 8 = 0: CS stays low.
;   Word to the host: bits 7 to 0 = byte received on MISO.
;
; Why it is an outline:
;   M2a must count the memory words for SPI (decision D2). This firmware
;   has the full bit timing, but no command words for the mode, the clock
;   rate or the sample delay. The model test (test/test_isa.py) checks it
;   with MISO connected to MOSI only, not with a real SPI device.
;
; Why H is 25:
;   After SAMPLE completes (up to 4 cycles after the sample time), three
;   instructions (TADD, OUT SCK, OUT MOSI) must execute before the falling
;   edge, and the last one needs a lead of 4 cycles (docs/isa.md section
;   4.2). That needs H >= 2 + 4 + 3 x 4 + 4 = 22 cycles. H = 25 gives 1 MHz.
;   A higher rate needs the line-code helper (A1 section 3, block D4).
;
; Host configuration before RUN: output pins UIO0 (CS), UIO1 (MOSI) and
; UIO3 (SCK) belong to this thread, push-pull. UIO2 (MISO) is an input.
; Registers: r0 = bits to send, r1 = bits received, r2 = bit counter,
; r3 = host word (for bit 8).

        .equ    CS, UIO0        ; chip select, low = active  [ENGINEER: pin choice]
        .equ    MOSI, UIO1      ; data from the controller
        .equ    MISO, UIO2      ; data to the controller (input)
        .equ    SCK, UIO3       ; clock
        .equ    H, 25           ; half SCK period in cycles: 50 MHz / 50 = 1 MHz
        .equ    SDLY, 2         ; sample offset: SAMPLE at T + 2 reads the pin level of cycle T
        .equ    MARGIN, 32      ; OUT MOSI at time T is 6 instructions after TSYNC: 6 x 4 + 4 = 28, rounded up

start:  TNOW    MARGIN          ; T starts near now
        OUT     CS, 1           ; CS high: no transfer
        OUT     SCK, 0          ; SCK low: CPOL = 0
idle:   TSYNC   MARGIN          ; keep T near now while the FIFO is empty
        PULL.NB r0              ; get a host word; E = 1 if there was one
        BEC     idle            ; FIFO empty: try again
        MOV     r3, r0          ; keep the host word for bit 8
        SHL     r0, 8           ; byte to bits 15 to 8 (MSB first)
        OUT     CS, 0           ; CS low at T (no change if it is low already)
        OUT     MOSI, r0<<      ; first data bit at T, half a period before the first rising edge
        LDI     r2, 8           ; 8 bits
sbit:   TADD    H               ; T = next rising edge
        OUT     SCK, 1          ; rising edge: the device reads MOSI
        SAMPLE  MISO, r1<<, SDLY ; read MISO at the rising edge, bit into bit 0 of r1
        TADD    H               ; T = next falling edge
        OUT     SCK, 0          ; falling edge: the device changes MISO
        OUT     MOSI, r0<<      ; next data bit at the falling edge
        DJNZ    r2, sbit        ; next bit if r2 is not 0
        ANDI    r1, 0xFF        ; keep the 8 bits of this byte
        PUSH    r1              ; give the received byte to the host
        SHR     r3, 9           ; C = bit 8 of the host word
        BCC     idle            ; bit 8 = 0: CS stays low for the next byte
        TADD    H               ; half a period after the last falling edge
        OUT     CS, 1           ; CS high: end of the transfer
        JMP     idle            ; wait for the next host word
