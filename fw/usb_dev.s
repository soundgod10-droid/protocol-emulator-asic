; USB 2.0 low-speed device on the USB line helper (docs/helpers.md): a HID boot keyboard with the LED
; output report. One thread (thread 0) does the device side of the protocol: packet decode, token address
; and endpoint, handshakes, data toggles, control transfers, the chapter 9 and HID requests, bus reset,
; suspend and resume. The helper does the bit level: SYNC, NRZI, bit stuffing, EOP, the CRC5 and CRC16
; checks, CRC16 generation, the line timers and the timed reply start.
;
; Pins (host configuration, docs/host_port.md): uio0 VPO (helper TXA), uio1 VMO (TXB), uio2 OE# (not OE)
; to a low-speed transceiver; ui_in[0] RCV, ui_in[1] VP, ui_in[2] VM from it (SEL below).
; Data memory: the request table, descriptors as packet chunks, the CRC5 table of token words, the reply
; stages and the variable start values, all from fw/usb_dev_data.py, written by the host before START.
; The device stays silent until the first bus reset.
; Host FIFO of thread 0: host to thread, 4 words = one 8-byte keyboard report for endpoint 1; thread to
; host, event words (low byte code, high byte value): 1 bus reset, 2 suspend, 3 resume, 4 configuration,
; 5 LED report, 6 endpoint 1 report taken by the host, 7 address.
;
; Reply time: the helper stamps the end of the host EOP (TSTAMP); the first symbol of a reply is on the pin
; REPLY + 1 cycles later: 225 cycles = 5.6 bit times (USB 2.0 7.1.18.1: at least 2, less than 6.5 with a
; detachable and 7.5 with a captive cable). The reply is in the transmit buffer before the end of packet:
; for an IN token to the device it is loaded at the token's first word, and its second word (CRC5) is
; compared before the end of packet; for a data packet the handshake chosen at its token is loaded at the
; first word. At the end of packet the start time is armed; a packet that needs no reply, or has an error,
; aborts it.
; References: USB 2.0 section numbers.

        .equ U, 16
        .equ TXDATA, U+0
        .equ RXDATA, U+1
        .equ USTAT, U+2
        .equ TXCTRL, U+4
        .equ TXTIME, U+5
        .equ RXINFO, U+6
        .equ TSTAMP, U+10
        .equ ULINE, U+11
        .equ CFGADR, U+12
        .equ CFGDAT, U+13
        .equ TXLAST, U+14
        .equ REPLY, 224

        ; data memory (fw/usb_dev_data.py)
        .equ CRCT, 512           ; word 1 of a token: endpoint 0 at CRCT + address, endpoint 1 at + 128
        .equ TABLE, 768          ; requests: K1, handler (+ 0x8000: any wValue), wValue, pointer, length
        .equ NREQ, 23
        .equ SENT, TABLE+115     ; sentinel entry: any request, Request Error
        .equ RXBUF, 900          ; received words of one packet (8)
        .equ ST1, 910            ; reply stages: TXCTRL, words, SYNC and PID, data words
        .equ STNAK, 918
        .equ STSTALL, 921
        .equ STZLP1, 924
        .equ VARBUF, 927         ; a packet chunk for one variable
        .equ OUTHS, 932          ; handshake word for an OUT data packet in each endpoint 0 state
        .equ INIT, 940           ; start values of the variables V_BASE to 511

        ; variables (program memory); V_IN0B, V_IN1B, V_ST0P, V_ST1P in this order
        .equ V_BASE, 482
        .equ V_ADDR, 482         ; device address
        .equ V_PEND, 483         ; address of a SET_ADDRESS, used after its status stage (-1: none)
        .equ V_CONF, 484
        .equ V_STATE, 485        ; endpoint 0: 0 idle, 1 data in, 2 status in, 3 data out, 4 stalled
        .equ V_PTR, 486          ; packet chunk of the next IN data packet
        .equ V_REM, 487          ; bytes left in the data stage
        .equ V_TOK, 488          ; last token to endpoint 0 at our address: 1 SETUP, 2 OUT
        .equ V_HSW, 489          ; handshake word for its data packet (0: no token)
        .equ V_AW, 490           ; device address << 8
        .equ V_HALT1, 491
        .equ V_TOG1, 492
        .equ V_K2, 493           ; wValue
        .equ V_WLEN, 494
        .equ V_LASTEP, 495       ; endpoint of the last data packet sent (255: none)
        .equ V_WVAR, 496
        .equ V_IDLE, 497
        .equ V_PROTO, 498
        .equ V_LED, 499
        .equ V_HW, 500
        .equ V_FPTR, 501         ; for an IN token to us: V_IN0B or V_IN1B; else V_NONE
        .equ V_FP1, 502          ; reply loaded at the first word: last word address, words left
        .equ V_FC, 503
        .equ V_ACKW, 504         ; SYNC + ACK
        .equ V_PID0, 505         ; SYNC + DATA0, SYNC + DATA1
        .equ V_PID1, 506
        .equ V_NONE, 507         ; -1
        .equ V_IN0B, 508         ; word 1 of a token to our address: endpoint 0, endpoint 1 (-1: none)
        .equ V_IN1B, 509
        .equ V_ST0P, 510         ; reply stage for an IN to endpoint 0, endpoint 1
        .equ V_ST1P, 511

        .org 0
start:  LDI  r0, 8
        IOW  r0, CFGADR
        LDI  r0, 0xA9
        SHL  r0, 4
        ORI  r0, 8
        IOW  r0, CFGDAT          ; SEL: DIN = input 8 (RCV), AUX0 = input 9 (VP), AUX1 = input 10 (VM)
        JAL  r3, vinit           ; also clears the flags of the time before SEL (no pins: SE0)

; ---- idle: words of a packet, end of packet, line conditions, host reports ---------------------------
idle0:  LDI  r3, 0               ; r3 = words of the packet
idle:   IOR  r1, USTAT
        MOV  r0, r1
        ANDI r0, 0x28            ; RXEOP, RXWORD
        BNE  rxany
        SHR  r1, 10              ; LINEA (bus reset) -> bit 0, LINEB (suspend) -> bit 1
        BNE  linec
        PULL.NB r0
        BES  hostw
        JMP  idle
rxany:  ANDI r0, 8               ; words first: at the end of packet every word is taken
        BEQ  pkend
        IOR  r0, RXDATA
        CMPI r3, 8
        BCS  idle                ; longer than any low-speed packet: drop the rest
        LDI  r2, RXBUF
        ADD  r2, r3
        STX  r0, [r2]
        ADDI r3, 1
        CMPI r3, 1
        BEQ  rxw0
        CMPI r3, 2
        BNE  idle
        LD   r2, V_FPTR          ; word 1 (8 bits) of an IN token to us: ENDP and CRC5 must match
        LDX  r1, [r2]
        CMP  r0, r1
        BEQ  idle
        LDI  r0, V_NONE
        CMP  r2, r0
        BEQ  idle
        ST   r0, V_FPTR          ; a damaged IN token: empty the loaded reply
        JMP  txfl

; Word 0: a data packet gets the handshake chosen at its token; an IN token to us gets its reply stage.
rxw0:   MOV  r1, r0
        ANDI r1, 0x77            ; DATA0 (C3) and DATA1 (4B) give 43
        CMPI r1, 0x43
        BNE  rxw0b
        LD   r1, V_HSW
        MOV  r1, r1
        BEQ  idle
        IOW  r1, TXLAST          ; ACK or STALL (TXCTRL is 0 here: 16 bits, no CRC)
        JMP  idle
rxw0b:  LD   r2, V_AW
        XOR  r0, r2
        XORI r0, 0x69            ; IN to us: 0 (endpoint 0) or 0x8000 (endpoint 1)
        SHL  r0, 1
        BNE  rxno
        LDI  r2, V_IN0B
        BCC  rxw0c
        LDI  r2, V_IN1B
rxw0c:  ST   r2, V_FPTR
        ADDI r2, 2
        LDX  r1, [r2]            ; reply stage: data, zero-length packet, NAK or STALL (8.4.6.1)
        LDX  r0, [r1]
        IOW  r0, TXCTRL
        ADDI r1, 1
        LDX  r2, [r1]            ; words
        ADDI r1, 1
        LDX  r0, [r1]
        ADDI r2, -1
        BEQ  pl2
        IOW  r0, TXDATA
        ADDI r1, 1
        LDX  r0, [r1]
        ADDI r2, -1
        BEQ  pl2
        IOW  r0, TXDATA          ; the buffer holds 2 words; the rest after the end of packet
        JMP  pl3
pl2:    IOW  r0, TXLAST
pl3:    ST   r1, V_FP1
        ST   r2, V_FC
        JMP  idle
rxno:   LDI  r2, V_NONE
        ST   r2, V_FPTR
        JMP  idle

; ---- end of a packet ----------------------------------------------------------------------------
pkend:  IOR  r0, TSTAMP
        ADDI r0, REPLY
        IOW  r0, TXTIME          ; reply time (7.1.18.1)
        MOV  r0, r1
        SHR  r0, 8
        ANDI r0, 3               ; STUFFERR (7.1.9), RXOVER: no reply
        BNE  tokno
        LDI  r0, 0x330
        IOW  r0, USTAT           ; clear RXSOP, RXEOP, STUFFERR, RXOVER
        LD   r2, V_FPTR
        LDI  r0, V_NONE
        CMP  r2, r0
        BEQ  pk1
        CMPI r3, 2               ; an IN token to us has 3 bytes
        BNE  tokno
        LD   r1, V_FP1           ; the rest of the reply
        LD   r2, V_FC
        MOV  r2, r2
        BEQ  in2
        JAL  r3, rp1
in2:    LD   r2, V_FPTR
        ADDI r2, 2
        LDX  r1, [r2]
        LDI  r0, V_ST0P
        SUB  r2, r0              ; endpoint 0 or 1
        LDX  r0, [r1]            ; TXCTRL of the stage: CRC appended = a data packet
        ANDI r0, 0x20
        BNE  snt1
        LDI  r2, 0xFF
snt1:   ST   r2, V_LASTEP

; ---- wait until the transmitter is idle (sent, or a late start dropped); drop the own packet heard back
txwait: IOR  r0, TXCTRL
        ANDI r0, 15
        BNE  txwait
        LDI  r0, 0x337
        IOW  r0, USTAT
        IOR  r0, RXDATA
        IOR  r0, RXDATA
txfl:   LDI  r0, 1
        SHL  r0, 10
        IOW  r0, TXCTRL          ; empty the buffer; last word 16 bits, no CRC (a handshake by TXLAST)
        JMP  idle0

; feed words r1 + 1 ... while r2 > 0 as the buffer frees; return to r3
rp1:    IOR  r0, TXDATA
        CMPI r0, 0
        BEQ  rp1
        ADDI r1, 1
        LDX  r0, [r1]
        ADDI r2, -1
        BEQ  rp2
        IOW  r0, TXDATA
        JMP  rp1
rp2:    IOW  r0, TXLAST
        JR   r3

; data packet: its handshake is in the buffer when it had a token (V_HSW)
pk1:    LDI  r2, RXBUF
        LDX  r0, [r2]
        ANDI r0, 0x77
        CMPI r0, 0x43
        BNE  slow
        LD   r1, V_HSW
        MOV  r1, r1
        BEQ  drop
        IOR  r0, USTAT
        ANDI r0, 0x40            ; CRC16 (8.3.5.2): a bad packet gets no handshake (8.4.6.4)
        BEQ  tokno
        LDX  r0, [r2]
        ANDI r0, 0xFF
        LD   r1, V_TOK
        CMPI r1, 1
        BNE  odata
        CMPI r0, 0xC3            ; setup data: DATA0 with 8 bytes (8.5.3)
        BNE  tokno
        CMPI r3, 6
        BNE  tokno
        IOR  r1, RXINFO
        ANDI r1, 31
        CMPI r1, 8
        BNE  tokno

; ---- SETUP: decode the request with the table (9.3, 9.4; HID 7.2) ----------------------------------
        LDI  r0, 0
        ST   r0, V_TOK
        ST   r0, V_HSW
        ST   r0, V_STATE
        LDI  r0, STNAK           ; NAK an IN until the request is decoded
        ST   r0, V_ST0P
        LDX  r0, [r2]            ; PID, bmRequestType
        SHR  r0, 8
        ADDI r2, 1
        LDX  r1, [r2]            ; bRequest, wValue low
        MOV  r3, r1
        SHL  r3, 8
        OR   r0, r3              ; K1 = bmRequestType + 256 bRequest
        SHR  r1, 8
        ADDI r2, 1
        LDX  r3, [r2]            ; wValue high, wIndex low
        SHL  r3, 8
        OR   r1, r3
        ST   r1, V_K2
        ADDI r2, 1
        LDX  r1, [r2]            ; wIndex high, wLength low
        SHR  r1, 8
        ADDI r2, 1
        LDX  r3, [r2]            ; wLength high, CRC
        SHL  r3, 8
        OR   r1, r3
        ST   r1, V_WLEN
        LDI  r3, SENT
        STX  r0, [r3]            ; the sentinel matches any request: Request Error (9.2.7)
        MOV  r1, r0
        LDI  r3, TABLE
ds1:    LDX  r0, [r3]
        CMP  r0, r1
        BEQ  ds2
        ADDI r3, 5
        JMP  ds1
ds2:    ADDI r3, 1
        LDX  r0, [r3]            ; handler; bit 15: any wValue
        MOV  r0, r0
        BMI  ds4
        ADDI r3, 1
        LDX  r2, [r3]
        LD   r0, V_K2
        CMP  r0, r2
        BEQ  ds3
        ADDI r3, 3
        JMP  ds1
ds3:    ADDI r3, -1
        LDX  r0, [r3]
ds4:    ADDI r3, 2
        LDX  r1, [r3]            ; pointer
        ADDI r3, 1
        LDX  r2, [r3]            ; length
        JR   r0                  ; handler (address = r0 mod 512)

; Request handlers: r1 = pointer, r2 = length.
astall: LDI  r0, 4               ; Request Error: STALL in the data or status stage (8.5.3.4)
        ST   r0, V_STATE
        LDI  r0, STSTALL
        ST   r0, V_ST0P
        JMP  txwait
aget:   LDX  r0, [r1]            ; a variable: send it from the VARBUF chunk
        LDI  r1, VARBUF+3
        STX  r0, [r1]
        LDI  r1, VARBUF
aread:  LD   r0, V_WLEN          ; at most wLength bytes (9.4.3)
        CMP  r2, r0
        BCC  ar1
        MOV  r2, r0
ar1:    ST   r1, V_PTR
        ST   r2, V_REM
        LDI  r0, 1               ; data stage, DATA1 first (8.5.3; the chunks carry the toggles)
        ST   r0, V_STATE
        JAL  r3, stage0
        JMP  txwait
aaddr:  LD   r0, V_K2
        ANDI r0, 0x7F
        ST   r0, V_PEND
        JMP  aok
aconf:  LD   r0, V_K2
        ANDI r0, 0xFF
        CMPI r0, 2
        BCS  astall              ; only configuration 0 or 1 (9.4.7)
        ST   r0, V_CONF
        SHL  r0, 8
        ORI  r0, 4
        PUSH.NB r0
        JAL  r3, mktok
aclrh:  LDI  r0, 0               ; endpoint 1: not halted, DATA0 next (9.4.1, 9.4.5)
        ST   r0, V_HALT1
        ST   r0, V_TOG1
        LDI  r0, STNAK
        ST   r0, V_ST1P
        JMP  aok
aset:   LD   r0, V_K2
        STX  r0, [r1]
        JMP  aok
aseth:  LD   r0, V_K2
        SHR  r0, 8
        STX  r0, [r1]
        JMP  aok
awr:    ST   r1, V_WVAR          ; control write: data stage next
        LDI  r0, 3
        ST   r0, V_STATE
        JMP  txwait
ahalt:  LDI  r0, 1               ; SET_FEATURE(ENDPOINT_HALT): endpoint 1 STALLs (9.4.9)
        ST   r0, V_HALT1
        LDI  r0, STSTALL
        ST   r0, V_ST1P
aok:    LDI  r0, 2               ; no data stage: status stage IN, DATA1 with no data (8.5.3)
        ST   r0, V_STATE
        LDI  r0, STZLP1
        ST   r0, V_ST0P
        JMP  txwait

; ---- write TXCTRL and the word count of the next IN data packet into its chunk; return to r3 ---------
stage0: LD   r0, V_REM           ; min(8, bytes left) (5.5.3); 0 gives a zero-length packet
        CMPI r0, 8
        BCC  sg1
        LDI  r0, 8
sg1:    MOV  r1, r0
        ANDI r1, 1
        SHL  r1, 3
        ORI  r1, 0x20            ; append CRC16; 8 bits in the last word for an odd count
        LD   r2, V_PTR
        STX  r1, [r2]
        ADDI r0, 1
        SHR  r0, 1
        ADDI r0, 1               ; words: SYNC and PID, data
        ADDI r2, 1
        STX  r0, [r2]
        ADDI r2, -1
        ST   r2, V_ST0P
        JR   r3

; ---- data packet after an OUT token (status of a control read, or data of a control write) -----------
odata:  CMPI r0, 0x4B
        BEQ  od1
        CMPI r0, 0xC3
        BNE  tokno
od1:    LD   r0, V_STATE
        CMPI r0, 3
        BEQ  odwr
        CMPI r0, 1
        BNE  odx                 ; STALL was sent
        LDI  r0, 0               ; status stage of a control read: done
        LDI  r1, STNAK
        JMP  odset
odwr:   LDX  r0, [r2]            ; first data byte (the LED report)
        SHR  r0, 8
        LD   r1, V_WVAR
        STX  r0, [r1]
        SHL  r0, 8
        ORI  r0, 5
        PUSH.NB r0
        LDI  r0, 2
        LDI  r1, STZLP1
odset:  ST   r0, V_STATE
        ST   r1, V_ST0P
odx:    LDI  r0, 0
        ST   r0, V_TOK
        ST   r0, V_HSW
        JMP  txwait

; ---- other packets: ACK, and SETUP / OUT tokens to endpoint 0 at our address (8.4.1) ------------------
slow:   LDX  r0, [r2]
        MOV  r1, r0
        ANDI r1, 0xFF
        CMPI r1, 0xD2            ; ACK
        BEQ  gack
        LD   r1, V_AW
        XOR  r0, r1              ; to us, endpoint 0: only the PID is left
        LDI  r1, 1
        CMPI r0, 0x2D            ; SETUP
        BEQ  gtok
        LDI  r1, 2
        CMPI r0, 0xE1            ; OUT
        BNE  tokno
gtok:   CMPI r3, 2               ; 3 bytes
        BNE  tokno
        ADDI r2, 1
        LDX  r0, [r2]
        LD   r2, V_IN0B          ; ENDP bits 3..1 and CRC5 of our address, endpoint 0
        CMP  r0, r2
        BNE  tokno
        ST   r1, V_TOK
        LD   r0, V_ACKW
        CMPI r1, 1
        BEQ  gt2                 ; SETUP: always ACK (8.4.6.4)
        LD   r2, V_STATE         ; OUT: ACK in a status or write data stage, else STALL
        LDI  r0, OUTHS
        ADD  r0, r2
        LDX  r0, [r0]
gt2:    ST   r0, V_HSW
        JMP  drop
tokno:  LDI  r1, 0
        ST   r1, V_TOK
        ST   r1, V_HSW
drop:   LDI  r0, 0x140
        SHL  r0, 2
        IOW  r0, TXCTRL          ; abort the armed start, empty the transmit buffer
        LDI  r0, 0x334
        IOW  r0, USTAT           ; clear TXUNDER and the receive flags
        JMP  idle0

; ---- ACK from the host after our data packet (8.6.2) --------------------------------------------------
gack:   LD   r0, V_LASTEP
        LDI  r1, 0xFF
        ST   r1, V_LASTEP
        CMPI r0, 1
        BEQ  ack1
        CMPI r0, 0
        BNE  drop
        LD   r0, V_STATE
        CMPI r0, 2
        BEQ  ackst
        CMPI r0, 1
        BNE  drop
        LD   r0, V_REM           ; data stage: the host took up to 8 bytes
        ADDI r0, -8
        ST   r0, V_REM
        BMI  acke                ; a short packet was the last one
        LD   r0, V_PTR           ; next chunk; after a whole last packet, a zero-length one (5.5.3)
        ADDI r0, 7
        ST   r0, V_PTR
        JAL  r3, stage0
        JMP  drop
acke:   LDI  r0, STNAK
        ST   r0, V_ST0P
        JMP  drop
ackst:  LDI  r0, 0               ; status stage done
        ST   r0, V_STATE
        LDI  r0, STNAK
        ST   r0, V_ST0P
        LD   r0, V_PEND
        MOV  r0, r0
        BMI  drop
        ST   r0, V_ADDR          ; the new address takes effect after the status stage (9.4.6)
        LD   r1, V_NONE
        ST   r1, V_PEND
        SHL  r0, 8
        ORI  r0, 7
        PUSH.NB r0
        JAL  r3, mktok
        JMP  drop
ack1:   LD   r0, V_TOG1
        XORI r0, 1
        ST   r0, V_TOG1
        LDI  r0, STNAK
        ST   r0, V_ST1P
        LDI  r0, 6
        PUSH.NB r0
        JMP  drop

; ---- token words of our address from the CRC5 table (8.3.5.1); return to r3 ----------------------------
mktok:  LD   r0, V_ADDR
        MOV  r1, r0
        SHL  r1, 8
        ST   r1, V_AW
        LDI  r1, CRCT
        ADD  r1, r0
        LDX  r2, [r1]
        ST   r2, V_IN0B
        LD   r2, V_NONE
        LD   r0, V_CONF
        CMPI r0, 0
        BEQ  mk1                 ; endpoint 1 answers only in the configured state (9.1.1.5)
        ADDI r1, 128
        LDX  r2, [r1]
mk1:    ST   r2, V_IN1B
        JR   r3

; ---- variables from their start values, all helper flags cleared; return to r3 ------------------------
vinit:  LDI  r0, 0
        NOT  r0, r0
        IOW  r0, USTAT
        LDI  r1, INIT
        LDI  r2, V_BASE
vi1:    LDX  r0, [r1]
        STX  r0, [r2]
        ADDI r1, 1
        ADDI r2, 1
        MOV  r0, r2
        SHR  r0, 9               ; up to address 511
        BEQ  vi1
        JR   r3

; ---- line conditions: bus reset (7.1.7.5), suspend and resume (7.1.7.6, 7.1.7.7) -----------------------
linec:  SHR  r1, 1
        BCS  busrst
        ANDI r1, 1
        BEQ  idle
        LDI  r0, 1
        SHL  r0, 11
        IOW  r0, USTAT
        LDI  r0, 2
        PUSH.NB r0
sus1:   IOR  r0, ULINE           ; resume: K on the bus (VP 1, VM 0)
        ANDI r0, 3
        CMPI r0, 1
        BEQ  resum
        IOR  r0, USTAT
        SHR  r0, 11              ; C = LINEA: a bus reset also ends the suspend
        BCS  busrst
        JMP  sus1
resum:  LDI  r0, 3
        PUSH.NB r0
        LDI  r0, 0x330
        IOW  r0, USTAT
        JMP  idle0
busrst: LDI  r0, 1
        PUSH.NB r0
        JAL  r3, vinit           ; default state, address 0 (9.1.1.3)
        JAL  r3, mktok
        JMP  idle0

; ---- host words: an 8-byte keyboard report for endpoint 1 (HID boot report) ----------------------------
hostw:  LD   r1, V_HW
        LDI  r2, ST1+3
        ADD  r2, r1
        STX  r0, [r2]
        ADDI r1, 1
        CMPI r1, 4
        BNE  hw1
        LD   r1, V_TOG1
        ADDI r1, V_PID0
        LDX  r1, [r1]
        LDI  r2, ST1+2
        STX  r1, [r2]
        LD   r0, V_HALT1
        CMPI r0, 0
        BNE  hw2
        LDI  r0, ST1
        ST   r0, V_ST1P
hw2:    LDI  r1, 0
hw1:    ST   r1, V_HW
        JMP  idle
