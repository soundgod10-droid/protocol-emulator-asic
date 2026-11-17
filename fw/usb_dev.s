; USB 2.0 low-speed device on the USB line helper (docs/helpers.md): a HID boot keyboard with the LED
; output report. One thread (thread 0) does the device side of the protocol (packet decode, token address
; and endpoint, data toggles, control transfers, the chapter 9 and HID requests, bus reset, suspend and
; resume); the helper does the bit level (SYNC, NRZI, bit stuffing, EOP, CRC5 and CRC16 checks, CRC16
; generation, line timers and the timed reply start).
;
; Pins (host configuration, docs/host_port.md): uio0 VPO (helper TXA), uio1 VMO (TXB), uio2 OE# (not OE)
; to a low-speed transceiver; ui_in[0] RCV, ui_in[1] VP, ui_in[2] VM from it (SEL below).
; Data memory: descriptors, the request table and the reply stages of fw/usb_dev_data.py, written by the
; host before START. The device stays silent until the first bus reset.
; Host FIFO of thread 0: host to thread, 4 words = one 8-byte keyboard report for endpoint 1; thread to
; host, event words (low byte code, high byte value): 1 bus reset, 2 suspend, 3 resume, 4 configuration,
; 5 LED report, 6 endpoint 1 report taken by the host, 7 address.
;
; Reply time: the helper stamps the end of the host EOP (TSTAMP); the first symbol of a reply is on the
; pin REPLY + 1 cycles later. REPLY = 224 cycles gives 5.7 bit times at the pins (USB 2.0 7.1.18.1: at
; least 2, less than 6.5 with a detachable and 7.5 with a captive cable). The start time is armed as soon
; as the end of packet is seen; a packet that needs no reply leaves the buffer empty and is aborted.
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
        .equ TABLE, 700          ; request table: K1, flags, wValue, pointer, length (5 words an entry)
        .equ SENT, 815           ; sentinel entry after the 23 entries: any wValue, Request Error
        .equ RXBUF, 960          ; received words of one packet (8)
        .equ ST0, 970            ; reply stages: words, SYNC+PID word, TXCTRL, data bytes (-1: handshake),
        .equ ST1, 980            ;   data words
        .equ STACK, 990
        .equ STNAK, 994
        .equ STSTALL, 998
        .equ STZLP1, 1002
        .equ VARBUF, 1006

        ; variables (program memory)
        .equ V_FPTR, 479         ; stage pointer variable of a matched IN token
        .equ V_ADDR, 480         ; device address
        .equ V_PEND, 481         ; address of a SET_ADDRESS, used after its status stage (-1: none)
        .equ V_CONF, 482
        .equ V_STATE, 483        ; endpoint 0: 0 idle, 1 data in, 2 status in, 3 data out, 4 stalled
        .equ V_TOG, 484          ; endpoint 0 data toggle of the next IN data packet
        .equ V_PTR, 485          ; data word of the next IN data packet
        .equ V_REM, 486          ; bytes left in the data stage
        .equ V_ZLP, 487          ; 1: the data stage ends with a zero-length packet
        .equ V_TOK, 488          ; last token to endpoint 0 at our address: 1 SETUP, 2 OUT
        .equ V_IN0A, 489         ; received words of an IN token to endpoint 0 and 1 at our address
        .equ V_IN0B, 490
        .equ V_IN1A, 491
        .equ V_IN1B, 492
        .equ V_HALT1, 493
        .equ V_TOG1, 494
        .equ V_K1, 495           ; bmRequestType + 256 bRequest
        .equ V_K2, 496           ; wValue
        .equ V_WLEN, 497
        .equ V_RET, 498
        .equ V_LASTEP, 499       ; endpoint of the last data packet sent (255: none)
        .equ V_READY, 500        ; 0 until the first bus reset
        .equ V_WVAR, 501
        .equ V_SUSP, 502
        .equ V_IDLE, 503
        .equ V_PROTO, 504
        .equ V_LED, 505
        .equ V_ALT, 506
        .equ V_HW, 507
        .equ V_RET2, 508
        .equ V_FLD, 509
        .equ V_ST0P, 510         ; reply stage for an IN to endpoint 0
        .equ V_ST1P, 511         ; reply stage for an IN to endpoint 1

        .org 0
start:  LDI  r0, 8
        IOW  r0, CFGADR
        LDI  r0, 0xA9
        SHL  r0, 4
        ORI  r0, 8
        IOW  r0, CFGDAT          ; SEL: DIN = input 8 (RCV), AUX0 = input 9 (VP), AUX1 = input 10 (VM)
        LDI  r0, 0
        ST   r0, V_READY
        ST   r0, V_IN0A
        ST   r0, V_IN1A
        ST   r0, V_HW
        LDI  r0, STNAK
        ST   r0, V_ST0P
        ST   r0, V_ST1P
        LDI  r0, 0xFF
        ST   r0, V_LASTEP
        LDI  r3, 0

; ---- idle: words of a packet, end of packet, line conditions, host reports -------------------------
idle:   IOR  r1, USTAT
        MOV  r0, r1
        ANDI r0, 0x28            ; RXEOP, RXWORD
        BNE  rxany
        SHR  r1, 10              ; LINEA (bus reset) -> bit 0, LINEB (suspend) -> bit 1
        BNE  linec
        PULL.NB r0
        BES  hostw
        JMP  idle
rxany:  ANDI r0, 0x20
        BNE  pkend
        IOR  r0, RXDATA
        CMPI r3, 8
        BCS  idle                ; longer than any low-speed packet: drop the rest
        LDI  r2, RXBUF
        ADD  r2, r3
        STX  r0, [r2]
        ADDI r3, 1
        CMPI r3, 2
        BEQ  rxw2
        JMP  idle

; Second word: the helper gives the last (8-bit) word of a token at the qualified SE0, about 60
; cycles before the end of packet. An IN token to us is matched here, so that after the end of packet
; only the start time and the first reply word remain (7.1.18.1 reply time).
rxw2:   LD   r1, V_IN0B
        CMP  r0, r1
        BEQ  rxw0
        LD   r1, V_IN1B
        CMP  r0, r1
        BNE  idle
        LDI  r2, V_ST1P
        LDI  r1, V_IN1A
        JMP  rxw3
rxw0:   LDI  r2, V_ST0P
        LDI  r1, V_IN0A
rxw3:   LDX  r1, [r1]
        LDI  r0, RXBUF
        LDX  r0, [r0]
        CMP  r0, r1
        BNE  idle
        ST   r2, V_FPTR
fw:     IOR  r1, USTAT           ; IN token to us: wait for its end of packet
        MOV  r0, r1
        ANDI r0, 0x28
        BEQ  fw
        ANDI r0, 0x20
        BEQ  idle                ; one more word: not a token
        IOR  r0, TSTAMP
        ADDI r0, REPLY
        IOW  r0, TXTIME
        MOV  r0, r1
        SHR  r0, 8
        ANDI r0, 3               ; STUFFERR, RXOVER: no reply
        BNE  drop
        LDI  r0, 0x330
        IOW  r0, USTAT
        LDX  r1, [r2]            ; data, zero-length packet, NAK or STALL (8.4.6.1)
        JAL  r3, reply
        LD   r2, V_FPTR
        LDX  r1, [r2]
        LDI  r0, V_ST0P
        SUB  r2, r0              ; endpoint 0 or 1
        JMP  sent

; ---- end of a packet ----------------------------------------------------------------------------
pkend:  IOR  r0, TSTAMP
        ADDI r0, REPLY
        IOW  r0, TXTIME          ; reply time (7.1.18.1)
pkdr:   IOR  r0, RXINFO
        ANDI r0, 0x60
        BEQ  pkgo
        IOR  r0, RXDATA
        CMPI r3, 8
        BCS  pkdr
        LDI  r2, RXBUF
        ADD  r2, r3
        STX  r0, [r2]
        ADDI r3, 1
        JMP  pkdr
pkgo:   MOV  r0, r1
        SHR  r0, 8
        ANDI r0, 3               ; STUFFERR (7.1.9), RXOVER
        BNE  drop
        LDI  r0, 0x330
        IOW  r0, USTAT           ; clear RXSOP, RXEOP, STUFFERR, RXOVER
        JMP  slow
sent:   ADDI r1, 3               ; r1 = stage sent, r2 = endpoint
        LDX  r0, [r1]            ; data bytes; negative: a handshake
        MOV  r0, r0
        BMI  senths
        ST   r2, V_LASTEP
        JMP  txwait
senths: LDI  r0, 0xFF
        ST   r0, V_LASTEP
        JMP  txwait

; ---- other packets: PID with its check field (8.3.1) ------------------------------------------------
slow:   LDI  r2, RXBUF
        LDX  r0, [r2]
        ANDI r0, 0xFF
        CMPI r0, 0xC3            ; DATA0
        BEQ  gdata
        CMPI r0, 0x4B            ; DATA1
        BEQ  gdata
        CMPI r0, 0xD2            ; ACK
        BEQ  gack
        CMPI r0, 0x2D            ; SETUP
        BEQ  gsetup
        CMPI r0, 0xE1            ; OUT
        BEQ  gout
        JMP  tokno
gsetup: LDI  r1, 1
        JMP  gtok
gout:   LDI  r1, 2
gtok:   CMPI r3, 2               ; a token has 3 bytes (8.4.1)
        BNE  tokno
        IOR  r0, USTAT
        ANDI r0, 0x80            ; CRC5 residue (8.3.5.1)
        BEQ  tokno
        LD   r0, V_READY
        CMPI r0, 0
        BEQ  tokno
        LDX  r0, [r2]
        SHR  r0, 8               ; ADDR, ENDP bit 0
        ADDI r2, 1
        LDX  r2, [r2]
        ANDI r2, 7               ; ENDP bits 3..1
        SHL  r2, 8
        OR   r0, r2
        LD   r2, V_ADDR          ; endpoint 0 at our address
        CMP  r0, r2
        BNE  tokno
        ST   r1, V_TOK
        JMP  drop
tokno:  LDI  r1, 0
        ST   r1, V_TOK
drop:   LDI  r0, 0x140
        SHL  r0, 2
        IOW  r0, TXCTRL          ; abort the armed start, empty the transmit buffer
        LDI  r0, 0x334
        IOW  r0, USTAT           ; clear TXUNDER and the receive flags
        LDI  r3, 0
        JMP  idle

; ---- data packets after a SETUP or OUT token -----------------------------------------------------------
gdata:  LD   r1, V_TOK
        CMPI r1, 0
        BEQ  drop
        IOR  r2, USTAT
        ANDI r2, 0x40            ; CRC16 residue (8.3.5.2); a bad packet gets no handshake (8.4.6.4)
        BEQ  tokno
        CMPI r1, 1
        BNE  odata
        CMPI r0, 0xC3            ; setup data: DATA0 with 8 bytes (8.5.3)
        BNE  tokno
        CMPI r3, 6
        BNE  tokno
        IOR  r2, RXINFO
        ANDI r2, 31
        CMPI r2, 8
        BNE  tokno
        LDI  r1, STACK           ; a SETUP is always acknowledged (8.4.6.4)
        JAL  r3, reply
        LDI  r0, 0
        ST   r0, V_TOK
        JMP  dosetup
odata:  LD   r1, V_STATE
        CMPI r1, 1
        BEQ  odst                ; status stage of a control read
        CMPI r1, 3
        BEQ  odwr                ; data stage of a control write
        LDI  r1, STSTALL
        JAL  r3, reply
        JMP  odend
odst:   LDI  r1, STACK
        JAL  r3, reply
        LDI  r0, 0
        ST   r0, V_STATE
        LDI  r0, STNAK
        ST   r0, V_ST0P
        JMP  odend
odwr:   LDI  r1, STACK
        JAL  r3, reply
        LDI  r2, RXBUF
        LDX  r0, [r2]
        SHR  r0, 8               ; first data byte (the LED report)
        LD   r1, V_WVAR
        STX  r0, [r1]
        SHL  r0, 8
        ORI  r0, 5
        PUSH.NB r0
        LDI  r0, 2
        ST   r0, V_STATE
        LDI  r0, STZLP1
        ST   r0, V_ST0P
odend:  LDI  r0, 0
        ST   r0, V_TOK
        JMP  txwait

; ---- ACK from the host after our data packet (8.6.2) ---------------------------------------------------
gack:   LD   r0, V_LASTEP
        CMPI r0, 1
        BEQ  ack1
        CMPI r0, 0
        BNE  drop
        LDI  r0, 0xFF
        ST   r0, V_LASTEP
        LD   r0, V_STATE
        CMPI r0, 2
        BEQ  ackst
        CMPI r0, 1
        BNE  drop
        LDI  r2, ST0+3
        LDX  r1, [r2]            ; data stage: the host took the bytes of ST0
        LD   r0, V_REM
        SUB  r0, r1
        ST   r0, V_REM
        ADDI r1, 1
        SHR  r1, 1
        LD   r0, V_PTR
        ADD  r0, r1
        ST   r0, V_PTR
        LD   r0, V_TOG
        XORI r0, 1
        ST   r0, V_TOG
        LD   r0, V_REM
        CMPI r0, 0
        BNE  ackmr
        LD   r0, V_ZLP
        CMPI r0, 0
        BEQ  ackend
        LDI  r0, 0
        ST   r0, V_ZLP
ackmr:  JAL  r3, stage0
        JMP  drop
ackend: LDI  r0, STNAK
        ST   r0, V_ST0P
        JMP  drop
ackst:  LDI  r0, 0           ; status stage done
        ST   r0, V_STATE
        LDI  r0, STNAK
        ST   r0, V_ST0P
        LD   r0, V_PEND
        MOV  r0, r0
        BMI  drop
        ST   r0, V_ADDR          ; the new address takes effect after the status stage (9.4.6)
        LDI  r1, 0
        NOT  r1, r1
        ST   r1, V_PEND
        SHL  r0, 8
        ORI  r0, 7
        PUSH.NB r0
        JAL  r3, mktok
        JMP  drop
ack1:   LDI  r0, 0xFF
        ST   r0, V_LASTEP
        LD   r0, V_TOG1
        XORI r0, 1
        ST   r0, V_TOG1
        LDI  r0, STNAK
        ST   r0, V_ST1P
        LDI  r0, 6
        PUSH.NB r0
        JMP  drop

; ---- send a reply stage (r1) at the armed time; return to r3 -------------------------------------
reply:  LDX  r2, [r1]            ; words
        ADDI r1, 1
        LDX  r0, [r1]            ; SYNC and PID
        ADDI r2, -1
        BNE  rp2
        IOW  r0, TXLAST          ; one-word packet: handshake or zero-length data
        ADDI r1, 1
        LDX  r0, [r1]
        IOW  r0, TXCTRL
        JR   r3
rp2:    IOW  r0, TXDATA
        ADDI r1, 1
        LDX  r0, [r1]
        IOW  r0, TXCTRL          ; append CRC16, bits of the last word
        ADDI r1, 2
rp3:    IOR  r0, TXDATA          ; free words in the transmit buffer
        CMPI r0, 0
        BEQ  rp3
        LDX  r0, [r1]
        ADDI r1, 1
        ADDI r2, -1
        BEQ  rp4
        IOW  r0, TXDATA
        JMP  rp3
rp4:    IOW  r0, TXLAST
        JR   r3

; ---- wait for the end of our packet; drop its echo from the transceiver ----------------------------
txwait: IOR  r0, USTAT
        IOR  r1, RXINFO
        ANDI r1, 0x60
        BEQ  tw1
        IOR  r1, RXDATA
tw1:    MOV  r1, r0
        ANDI r1, 1               ; TXDONE
        BNE  tw2
        ANDI r0, 4               ; TXUNDER with the transmitter stopped: the start was missed
        BEQ  txwait
        IOR  r0, TXCTRL
        ANDI r0, 15
        BNE  txwait
tw2:    LDI  r0, 0x337
        IOW  r0, USTAT           ; clear TXDONE, TXLATE, TXUNDER and the receive flags
        IOR  r0, RXDATA
        IOR  r0, RXDATA
        LDI  r0, 1
        SHL  r0, 10
        IOW  r0, TXCTRL          ; empty the buffer; last word 16 bits, no CRC
        LDI  r3, 0
        JMP  idle

; ---- SETUP: decode the request with the table (9.3, 9.4; HID 7.2) ----------------------------------
dosetup: LDI r0, 0
        ST   r0, V_STATE
        ST   r0, V_ZLP
        LDI  r0, STNAK           ; NAK until the request is decoded
        ST   r0, V_ST0P
        LDI  r2, RXBUF
        LDX  r0, [r2]            ; PID, bmRequestType
        SHR  r0, 8
        ADDI r2, 1
        LDX  r1, [r2]            ; bRequest, wValue low
        MOV  r3, r1
        SHL  r3, 8
        OR   r0, r3
        ST   r0, V_K1
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
        STX  r0, [r3]            ; the sentinel matches every request: Request Error (9.2.7)
        MOV  r1, r0
        LDI  r3, TABLE
ds1:    LDX  r0, [r3]
        CMP  r0, r1
        BEQ  ds3
        ADDI r3, 5
        JMP  ds1
ds3:    ADDI r3, 1
        LDX  r0, [r3]            ; flags: action, bit 15 = any wValue
        MOV  r0, r0
        BMI  ds4
        ADDI r3, 1
        LDX  r2, [r3]
        LD   r0, V_K2
        CMP  r0, r2
        BEQ  ds5
        ADDI r3, 3
        JMP  ds1
ds5:    ADDI r3, -1
        LDX  r0, [r3]
ds4:    ADDI r3, 2
        LDX  r1, [r3]            ; pointer
        ADDI r3, 1
        LDX  r2, [r3]            ; length
        ANDI r0, 15
        CMPI r0, 1
        BEQ  aread
        CMPI r0, 2
        BEQ  aaddr
        CMPI r0, 3
        BEQ  aconf
        CMPI r0, 4
        BEQ  aset
        CMPI r0, 5
        BEQ  aget
        CMPI r0, 6
        BEQ  awr
        CMPI r0, 7
        BEQ  afeat
        CMPI r0, 8
        BEQ  aseth
        CMPI r0, 9
        BEQ  aok
astall: LDI  r0, 4               ; Request Error: STALL in the data or status stage (9.2.7)
        ST   r0, V_STATE
        LDI  r0, STSTALL
        ST   r0, V_ST0P
        JMP  txwait
aget:   LDX  r0, [r1]            ; a variable: copy it to VARBUF and read it
        LDI  r1, VARBUF
        STX  r0, [r1]
aread:  LD   r0, V_WLEN          ; bytes = min(length, wLength) (9.4.3)
        CMP  r2, r0
        BCC  ar1
        MOV  r2, r0
        LDI  r0, 0
        JMP  ar3
ar1:    MOV  r0, r2
        ANDI r0, 7
        BNE  ar2
        LDI  r0, 1               ; shorter than wLength and a whole packet: ends with a ZLP (5.5.3)
        JMP  ar3
ar2:    LDI  r0, 0
ar3:    ST   r0, V_ZLP
        ST   r1, V_PTR
        ST   r2, V_REM
        LDI  r0, 1
        ST   r0, V_TOG           ; the data stage starts with DATA1 (8.5.3)
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
        BCS  astall
        ST   r0, V_CONF
        LDI  r1, 0
        ST   r1, V_TOG1          ; endpoint 1 restarts with DATA0, not halted (9.4.5, 9.4.7)
        ST   r1, V_HALT1
        LDI  r1, STNAK
        ST   r1, V_ST1P
        SHL  r0, 8
        ORI  r0, 4
        PUSH.NB r0
        JAL  r3, mktok
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
afeat:  LD   r0, V_K1
        SHR  r0, 8               ; 1 CLEAR_FEATURE, 3 SET_FEATURE (ENDPOINT_HALT)
        CMPI r0, 3
        BEQ  af1
        LDI  r0, 0
        ST   r0, V_HALT1
        ST   r0, V_TOG1
        LDI  r0, STNAK
        ST   r0, V_ST1P
        JMP  aok
af1:    LDI  r0, 1
        ST   r0, V_HALT1
        LDI  r0, STSTALL
        ST   r0, V_ST1P
aok:    LDI  r0, 2               ; no data stage: status stage IN, DATA1 zero length
        ST   r0, V_STATE
        LDI  r0, STZLP1
        ST   r0, V_ST0P
        JMP  txwait

; ---- ST0 = next IN data packet from V_PTR, V_REM, V_TOG; return to r3 --------------------------------
stage0: ST   r3, V_RET
        LD   r0, V_REM
        CMPI r0, 8
        BCC  sg1
        LDI  r0, 8               ; low-speed maximum packet size (5.5.3)
sg1:    LDI  r2, ST0+3
        STX  r0, [r2]
        MOV  r1, r0
        ANDI r1, 1
        SHL  r1, 3
        ORI  r1, 0x20            ; append CRC16; the last word has 8 bits for an odd count
        ADDI r2, -1
        STX  r1, [r2]
        LDI  r1, 0x4B
        LD   r3, V_TOG
        CMPI r3, 0
        BNE  sg2
        LDI  r1, 0xC3
sg2:    SHL  r1, 8
        ORI  r1, 0x80            ; SYNC (7.1.10)
        ADDI r2, -1
        STX  r1, [r2]
        ADDI r0, 1
        SHR  r0, 1               ; data words
        MOV  r1, r0
        ADDI r1, 1
        ADDI r2, -1
        STX  r1, [r2]
        CMPI r0, 0
        BEQ  sg4
        LD   r3, V_PTR
        ADDI r2, 4
sg3:    LDX  r1, [r3]
        STX  r1, [r2]
        ADDI r3, 1
        ADDI r2, 1
        DJNZ r0, sg3
sg4:    LDI  r0, ST0
        ST   r0, V_ST0P
        LD   r3, V_RET
        JR   r3

; ---- IN token words for endpoints 0 and 1 at V_ADDR (8.4.1, CRC5 8.3.5.1); return to r3 -------------
mktok:  ST   r3, V_RET2
        LD   r0, V_ADDR
        JAL  r3, crc5tok
        ST   r0, V_IN0A
        ST   r1, V_IN0B
        LD   r0, V_CONF
        CMPI r0, 0
        BEQ  mk1
        LD   r0, V_ADDR
        ORI  r0, 0x80            ; ENDP = 1
        JAL  r3, crc5tok
        ST   r0, V_IN1A
        ST   r1, V_IN1B
        JMP  mk2
mk1:    LDI  r0, 0               ; endpoint 1 does not answer before SET_CONFIGURATION
        ST   r0, V_IN1A
mk2:    LD   r3, V_RET2
        JR   r3

; r0 = ADDR + 128 ENDP (11 bits) -> r0, r1 = the two received words of an IN token; return to r3
crc5tok: ST  r0, V_FLD
        LDI  r1, 0x1F
        LDI  r2, 11
c5l:    SHR  r0, 1
        BCC  c5a
        XORI r1, 1
c5a:    SHR  r1, 1
        BCC  c5b
        XORI r1, 0x14            ; x^5 + x^2 + 1, reflected
c5b:    DJNZ r2, c5l
        XORI r1, 0x1F            ; inverted, first bit in bit 0
        LD   r0, V_FLD
        MOV  r2, r0
        SHR  r2, 8
        SHL  r1, 3
        OR   r1, r2              ; ENDP bits 3..1, CRC5
        ANDI r0, 0xFF
        SHL  r0, 8
        ORI  r0, 0x69            ; IN PID
        JR   r3

; ---- line conditions: bus reset (7.1.7.5), suspend and resume (7.1.7.6, 7.1.7.7) -----------------------
linec:  MOV  r0, r1
        ANDI r0, 1
        BNE  busrst
        ANDI r1, 2
        BNE  susp
        JMP  idle
busrst: LDI  r0, 1
        SHL  r0, 10
        IOW  r0, USTAT
        LDI  r0, 0
        ST   r0, V_ADDR
        ST   r0, V_CONF
        ST   r0, V_STATE
        ST   r0, V_TOK
        ST   r0, V_HALT1
        ST   r0, V_TOG1
        ST   r0, V_SUSP
        ST   r0, V_PROTO
        ST   r0, V_IDLE
        ST   r0, V_ALT
        LDI  r0, 1
        ST   r0, V_READY
        ST   r0, V_PROTO         ; report protocol after reset (HID 7.2.6)
        LDI  r0, 0xFF
        ST   r0, V_LASTEP
        LDI  r0, 0
        NOT  r0, r0
        ST   r0, V_PEND
        LDI  r0, STNAK
        ST   r0, V_ST0P
        ST   r0, V_ST1P
        JAL  r3, mktok
        LDI  r0, 1
        PUSH.NB r0
        LDI  r0, 0x330
        IOW  r0, USTAT
        LDI  r3, 0
        JMP  idle
susp:   LDI  r0, 1
        SHL  r0, 11
        IOW  r0, USTAT
        LDI  r0, 1
        ST   r0, V_SUSP
        LDI  r0, 2
        PUSH.NB r0
sus1:   IOR  r0, ULINE           ; resume: K on the bus (VP 1, VM 0)
        ANDI r0, 3
        CMPI r0, 1
        BEQ  resum
        IOR  r0, USTAT
        SHR  r0, 10
        ANDI r0, 1
        BNE  busrst              ; a bus reset also ends the suspend
        JMP  sus1
resum:  LDI  r0, 0
        ST   r0, V_SUSP
        LDI  r0, 3
        PUSH.NB r0
        LDI  r0, 0x330
        IOW  r0, USTAT
        LDI  r3, 0
        JMP  idle

; ---- host words: an 8-byte keyboard report for endpoint 1 (HID boot report) ----------------------------
hostw:  LD   r1, V_HW
        LDI  r2, ST1+4
        ADD  r2, r1
        STX  r0, [r2]
        ADDI r1, 1
        CMPI r1, 4
        BNE  hw1
        LDI  r1, 0x4B
        LD   r2, V_TOG1
        CMPI r2, 0
        BNE  hw2
        LDI  r1, 0xC3
hw2:    SHL  r1, 8
        ORI  r1, 0x80
        LDI  r2, ST1+1
        STX  r1, [r2]
        LD   r0, V_HALT1
        CMPI r0, 0
        BNE  hw3
        LDI  r0, ST1
        ST   r0, V_ST1P
hw3:    LDI  r1, 0
hw1:    ST   r1, V_HW
        JMP  idle
