; 10BASE-T (IEEE 802.3 clause 14) station on the 10BASE-Te line helper (docs/helpers.md), half duplex.
; Thread 0 does the MAC and the link side; the helper does Manchester coding at line rate, receive clock
; recovery, SFD detection, CRC-32 generation and check, the TP_IDL hold, link test pulses, the squelch
; and link-pulse classifiers, collision detection with the 32-bit jam, and jabber.
;
; Firmware functions (802.3 subclauses):
;   - link test pulses every 16 ms when no frame is sent, restarting after each frame (14.2.1.1);
;   - Link Integrity Test function, Figure 14-6 (14.2.1.7): link_loss 96 ms, link_test_min 4 ms,
;     link_test_max 64 ms, lc_max 4; frames are neither sent nor delivered in the LINK TEST FAIL states;
;   - transmit: preamble and SFD from the buffer, pad to 60 octets (4.2.3.3), defer while carrier
;     (4.2.3.2.1), interframe gap 96 BT (4.2.3.2.2), on collision truncated binary exponential backoff,
;     backoffLimit 10, attemptLimit 16, slot time 512 BT (4.2.3.2.5, Table 4-2);
;   - receive: frame delivered to the host when its FCS is good and it has at least 64 octets
;     (4.2.4.2.2); dribble bits dropped by truncation to the octet (4.2.4.2.1); others discarded with a
;     reason.
;
; Pins (host configuration): uio0 TD+ (helper TXA), uio1 TD- (TXB), uio2 OE (helper OE) to the transmit
; network; ui_in[0] receive slicer, ui_in[1] squelch comparator (SEL below).
; Host FIFO of thread 0, host to thread: a frame to send = its length in octets (DA to the end of the data,
; no FCS; at most 408), then the octets in words, low byte first. Thread to host: a received frame =
; 0x8000 + its length in octets (no FCS), then the words; events (low byte code, high byte value):
; 1 link pass, 2 link fail, 3 frame sent, 4 frame not sent (attempt limit, link fail or too long),
; 5 frame discarded (reason 1 runt, 2 FCS error, 3 longer than the buffer, 4 link failed, 5 buffer busy).
; Timers count units of 128 cycles (2.133 us at 60 MHz); their lengths are data words from
; fw/eth_10bt_data.py (a test can load shorter ones; the firmware is the same).

        .equ E, 32
        .equ TXDATA, E+0
        .equ RXDATA, E+1
        .equ ESTAT, E+2
        .equ TXCTRL, E+4
        .equ RXINFO, E+6
        .equ TSTAMP, E+10
        .equ CFGADR, E+12
        .equ CFGDAT, E+13
        .equ TXLAST, E+14
        .equ IPG, 576            ; 96 BT in cycles (9.6 us at 60 MHz)

        ; data memory (fw/eth_10bt_data.py)
        .equ INIT, 512           ; start values of the variables V_BASE to 511
        .equ PHT, 550            ; addresses of the housekeeping steps A to F
        .equ TXBUF, 560          ; 3 preamble words + preamble/SFD word, then the frame (204 words)
        .equ RXBUF, 776          ; received frame (760 words, to 1535); RXBUF-1: the header word

        ; variables (program memory); V_TNLP to V_TBO in this order
        .equ V_BASE, 475
        .equ V_TL, 475           ; time of the last timer update (now, 16 bits)
        .equ V_PEND, 476         ; units not yet subtracted from the timers
        .equ V_TNLP, 477         ; timers, counting down: next link pulse,
        .equ V_TMIN, 478         ;   link_test_min, link_test_max, link_loss, backoff
        .equ V_TMAX, 479
        .equ V_TLOSS, 480
        .equ V_TBO, 481
        .equ V_LINK, 482         ; 0 LINK TEST PASS, 1 a LINK TEST FAIL state
        .equ V_CNT, 483          ; link_count
        .equ V_TXS, 484          ; transmit: 0 idle, 1 waiting to start, 3 backoff, 4 loading from the host
        .equ V_TXN, 485          ; words to send with the preamble
        .equ V_TXC, 486          ; TXCTRL for the frame
        .equ V_ATT, 487          ; collisions of this frame
        .equ V_MASK, 488         ; backoff range - 1
        .equ V_RND, 489          ; random number (16-bit LFSR)
        .equ V_IDLEAT, 490       ; end of the last activity on the line (gap reference)
        .equ V_TMP, 491
        .equ V_PH, 492           ; housekeeping step
        .equ V_UPN, 493          ; words to the host (frame upload), next word
        .equ V_UPP, 494
        .equ V_HLN, 495          ; words still to come from the host, next address, octets
        .equ V_HLP, 496
        .equ V_HLO, 497
        .equ V_EVQ, 498          ; events waiting: bit k = event k + 1
        .equ V_EVR, 499          ; reason of a discarded frame
        .equ C_NLP, 500          ; link pulse period (units)
        .equ C_LMIN, 501         ; link_test_min, link_test_max, link_loss (units), lc_max
        .equ C_LMAX, 502
        .equ C_LLOSS, 503
        .equ C_LCMAX, 504
        .equ C_POLY, 505         ; LFSR polynomial
        .equ C_RXEND, 506        ; end of the receive buffer
        .equ C_LPBIT, 507        ; STATUS LINKPULSE
        .equ C_RXCLR, 508        ; STATUS RXSOP, RXEOP, RXOVER, LINKPULSE
        .equ C_TXCLR, 509        ; STATUS TXDONE, TXLATE, TXUNDER, COLLISION
        .equ C_TXEND, 510        ; end of the transmit buffer
        .equ C_TXMAXO, 511       ; longest frame to send (octets)

        .org 0
start:  LDI  r0, 8
        IOW  r0, CFGADR
        LDI  r0, 0xD9
        SHL  r0, 4
        ORI  r0, 8
        IOW  r0, CFGDAT          ; SEL: DIN = input 8 (slicer), AUX0 = input 9 (squelch), AUX1 none
        LDI  r0, 0
        NOT  r0, r0
        IOW  r0, ESTAT           ; flags of the time before SEL
        LDI  r1, INIT
        LDI  r2, V_BASE
vi1:    LDX  r0, [r1]
        STX  r0, [r2]
        ADDI r1, 1
        ADDI r2, 1
        MOV  r0, r2
        SHR  r0, 9               ; up to address 511
        BEQ  vi1
        NOW  r0
        ST   r0, V_TL

; ---- main loop: carrier and link pulses in every pass, then one housekeeping step (A to F) ---------
; One pass takes at most about 80 slots (320 cycles), less than the preamble after the carrier starts.
main:   IOR  r1, ESTAT
        MOV  r0, r1
        SHR  r0, 12
        ANDI r0, 1               ; CARRIER
        BNE  carw
        SHR  r1, 13
        ANDI r1, 1               ; LINKPULSE (the helper sets it only without carrier)
        BNE  lpr
        LD   r0, V_PH
        ADDI r0, 1
        CMPI r0, 6
        BCC  mn1
        LDI  r0, 0
mn1:    ST   r0, V_PH
        LDI  r1, PHT
        ADD  r1, r0
        LDX  r1, [r1]
        JR   r1

; ---- receive ------------------------------------------------------------------------------------
carw:   IOR  r1, ESTAT           ; carrier: wait for the SFD
        MOV  r0, r1
        ANDI r0, 0x10            ; RXSOP
        BNE  rxf
        SHR  r1, 12
        ANDI r1, 1
        BNE  carw
        NOW  r0                  ; activity without a frame start (e.g. a collision fragment):
        ST   r0, V_IDLEAT        ;   its end is the gap reference
        LD   r0, C_RXCLR
        IOW  r0, ESTAT
        JAL  r2, rdact
        JMP  main
rxf:    LDI  r3, RXBUF
        LD   r0, V_UPN
        CMPI r0, 0
        BEQ  rf1
        LD   r3, C_RXEND         ; the host still reads the last frame: receive without storing
rf1:    LD   r2, C_RXEND
rxl:    IOR  r1, ESTAT
        MOV  r0, r1
        ANDI r0, 8               ; RXWORD
        BEQ  rxe
        IOR  r0, RXDATA
        CMP  r3, r2
        BCS  rxov                ; buffer full
        STX  r0, [r3]
        ADDI r3, 1
        MOV  r0, r3
        ANDI r0, 0xFF
        BNE  rxl
        JAL  r2, tacc            ; every 256 words: keep the time base (a frame lasts up to 73,000 cycles)
        LD   r2, C_RXEND
        JMP  rxl
rxov:   MOV  r3, r2              ; a word was lost: pointer one past the end
        ADDI r3, 1
        JMP  rxl
rxe:    ANDI r1, 0x20            ; RXEOP: end of carrier; every word is taken
        BEQ  rxl
        IOR  r0, TSTAMP
        ST   r0, V_IDLEAT
        IOR  r1, ESTAT
        ST   r1, V_TMP           ; CRCA_OK of this frame
        LD   r0, C_RXCLR
        IOW  r0, ESTAT
        LD   r1, V_LINK
        JAL  r2, rdact
        LDI  r0, 4
        CMPI r1, 0
        BNE  rxd                 ; reason 4: received in a LINK TEST FAIL state (rcv disabled)
        LDI  r0, 5
        LD   r1, V_UPN
        CMPI r1, 0
        BNE  rxd                 ; reason 5: buffer busy
        LDI  r0, 3
        LD   r2, C_RXEND
        CMP  r2, r3
        BCC  rxd                 ; reason 3: longer than the buffer (a word was lost)
        LDI  r0, 2
        LD   r1, V_TMP
        ANDI r1, 0x40
        BEQ  rxd                 ; reason 2: FCS error (3.2.9)
        IOR  r0, RXINFO          ; bits in the last word (its word count field has 9 bits: not used)
        ANDI r0, 31
        LDI  r1, RXBUF
        SUB  r3, r1              ; words stored
        MOV  r1, r3
        SHL  r1, 1               ; 2 octets per word
        CMPI r0, 0
        BEQ  oc1
        ADDI r1, -2              ; a part word last: 8 to 15 bits give one octet, dribble bits dropped
        SHR  r0, 3
        ADD  r1, r0
oc1:
        LDI  r0, 1
        CMPI r1, 64
        BCC  rxd                 ; reason 1: shorter than minFrameSize (4.2.4.2.2)
        ADDI r1, -4              ; delivered without the FCS
        LDI  r0, 0x80
        SHL  r0, 8
        OR   r0, r1
        LDI  r3, RXBUF-1
        STX  r0, [r3]            ; header: 0x8000 + octets
        ST   r3, V_UPP
        ADDI r1, 1
        SHR  r1, 1
        ADDI r1, 1
        ST   r1, V_UPN           ; words to the host, with the header
        JMP  main
rxd:    ST   r0, V_EVR
        LD   r0, V_EVQ
        ORI  r0, 16              ; event 5: frame discarded
        ST   r0, V_EVQ
        JMP  main

; RD = active (Figure 14-6): a LINK TEST FAIL state goes to FAIL EXTEND and, with RD and DO idle, to
; LINK TEST PASS; in LINK TEST PASS the timers start again. Return to r2; r1 and r3 are kept.
rdact:  LD   r0, V_LINK
        CMPI r0, 0
        BEQ  lpass2
        LDI  r0, 0
        ST   r0, V_LINK
        LD   r0, V_EVQ
        ORI  r0, 1               ; event 1: link pass
        ST   r0, V_EVQ
lpass2: LD   r0, C_LLOSS
        ST   r0, V_TLOSS
        LD   r0, C_LMIN
        ST   r0, V_TMIN
        JR   r2

; link test pulse received (link_test_rcv, Figure 14-6)
lpr:    LD   r0, C_LPBIT
        IOW  r0, ESTAT
        LD   r1, V_LINK
        LD   r0, V_TMIN
        CMPI r0, 0
        BNE  lpe
        CMPI r1, 0
        BEQ  lpass               ; LINK TEST PASS, link_test_min_timer_done: timers start again
        LD   r0, V_CNT           ; LINK TEST FAIL COUNT
        ADDI r0, 1
        ST   r0, V_CNT
        LD   r1, C_LCMAX
        CMP  r0, r1
        BCC  ffail               ; link_count < lc_max: LINK TEST FAIL, its timers start again
        LDI  r0, 0               ; LINK TEST FAIL EXTEND -> LINK TEST PASS (RD and DO idle)
        ST   r0, V_LINK
        LD   r0, V_EVQ
        ORI  r0, 1
        ST   r0, V_EVQ
lpass:  LD   r0, C_LLOSS
        ST   r0, V_TLOSS
        LD   r0, C_LMIN
        ST   r0, V_TMIN
        JMP  main
lpe:    CMPI r1, 0
        BEQ  main                ; LINK TEST PASS: an early pulse does nothing
        JMP  frst                ; LINK TEST FAIL: link_test_min_timer_not_done -> FAIL RESET

; ---- housekeeping steps ---------------------------------------------------------------------------
; A: time base. Elapsed units of 128 cycles go to V_PEND; V_TL moves by whole units. Return to r2.
phA:    JAL  r2, tacc
        JMP  main
tacc:   NOW  r0
        LD   r1, V_TL
        SUB  r0, r1
        SHR  r0, 7
        ST   r0, V_TMP
        SHL  r0, 7
        ADD  r1, r0
        ST   r1, V_TL
        LD   r0, V_PEND
        LD   r1, V_TMP
        ADD  r0, r1
        ST   r0, V_PEND
        JR   r2

; B: subtract the elapsed units from the five timers (not below 0)
phB:    LD   r0, V_PEND
        CMPI r0, 0
        BEQ  main
        LDI  r1, 0
        ST   r1, V_PEND
        LDI  r2, V_TNLP
        LDI  r3, 5
pb1:    LDX  r1, [r2]
        SUB  r1, r0
        BCS  pb2
        LDI  r1, 0
pb2:    STX  r1, [r2]
        ADDI r2, 1
        DJNZ r3, pb1
        JMP  main

; C: link test pulse when its timer ends (14.2.1.1)
phC:    LD   r0, V_TNLP
        CMPI r0, 0
        BNE  main
        LDI  r0, 0x280           ; TXCTRL: link test pulse, start now
        IOW  r0, TXCTRL
        LD   r0, C_NLP
        ST   r0, V_TNLP
        JMP  main

; D: link timers (Figure 14-6)
phD:    LD   r0, V_LINK
        CMPI r0, 0
        BNE  pd2
        LD   r0, V_TLOSS         ; LINK TEST PASS: link_loss_timer_done (RD idle) -> FAIL RESET
        CMPI r0, 0
        BNE  main
        LDI  r0, 1
        ST   r0, V_LINK
        LD   r0, V_EVQ
        ORI  r0, 2               ; event 2: link fail
        ST   r0, V_EVQ
        JMP  frst
pd2:    LD   r0, V_TMAX          ; LINK TEST FAIL: link_test_max_timer_done (RD idle) -> FAIL RESET
        CMPI r0, 0
        BNE  main
frst:   LDI  r0, 0               ; LINK TEST FAIL RESET: link_count = 0
        ST   r0, V_CNT
ffail:  LD   r0, C_LMIN          ; LINK TEST FAIL: start link_test_min_timer and link_test_max_timer
        ST   r0, V_TMIN
        LD   r0, C_LMAX
        ST   r0, V_TMAX
        JMP  main

; E: transmit
phE:    LD   r0, V_TXS
        CMPI r0, 1
        BEQ  txgo
        CMPI r0, 3
        BNE  main
        LD   r0, V_TBO           ; backoff over: try again
        CMPI r0, 0
        BNE  main
        LDI  r0, 1
        ST   r0, V_TXS
        JMP  main
txgo:   LD   r0, V_LINK
        CMPI r0, 0
        BNE  txab                ; xmit disabled in the LINK TEST FAIL states (14.2.1.7)
        NOW  r0
        LD   r1, V_IDLEAT
        SUB  r0, r1
        LDI  r1, IPG
        CMP  r0, r1
        BCC  main                ; interframe gap (the carrier was checked at the start of this pass)
        LD   r0, C_TXCLR
        IOW  r0, ESTAT
        LDI  r0, 1
        SHL  r0, 10
        IOW  r0, TXCTRL          ; empty the buffer
        LDI  r3, TXBUF
        LD   r2, V_TXN
        LDX  r0, [r3]
        IOW  r0, TXDATA
        ADDI r3, 1
        LDX  r0, [r3]
        IOW  r0, TXDATA
        ADDI r3, 1
        ADDI r2, -2
        LD   r0, V_TXC
        IOW  r0, TXCTRL          ; append CRC-32, start now
txf:    IOR  r0, TXDATA          ; free words
        CMPI r0, 0
        BNE  txw
        IOR  r0, ESTAT
        ANDI r0, 1               ; TXDONE before the last word: a collision ended the frame
        BNE  txend
        JMP  txf
txw:    LDX  r0, [r3]
        ADDI r3, 1
        ADDI r2, -1
        BEQ  txl
        IOW  r0, TXDATA
        JMP  txf
txl:    IOW  r0, TXLAST
txd:    IOR  r0, ESTAT           ; TXDONE after the TP_IDL hold, or after the jam
        ANDI r0, 1
        BEQ  txd
txend:  NOW  r0
        ST   r0, V_IDLEAT
        LD   r0, C_NLP
        ST   r0, V_TNLP          ; link pulses again 16 ms after the frame (14.2.1.1)
        IOR  r0, ESTAT
        LD   r1, C_TXCLR
        IOW  r1, ESTAT
        LDI  r1, 1
        SHL  r1, 10
        IOW  r1, TXCTRL          ; empty the buffer (words left after a collision)
        SHR  r0, 14
        ANDI r0, 1               ; COLLISION
        BNE  txcol
        LDI  r0, 0
        ST   r0, V_TXS
        LD   r0, V_EVQ
        ORI  r0, 4               ; event 3: frame sent
        ST   r0, V_EVQ
        JMP  main
txcol:  LD   r0, V_ATT           ; truncated binary exponential backoff (4.2.3.2.5)
        ADDI r0, 1
        ST   r0, V_ATT
        CMPI r0, 16
        BCS  txab                ; attemptLimit
        LD   r0, V_MASK
        SHL  r0, 1
        ORI  r0, 1               ; 2^min(n, 10) - 1
        LDI  r1, 1023
        CMP  r1, r0
        BCS  bo0
        MOV  r0, r1
bo0:    ST   r0, V_MASK
        LD   r1, V_RND
        SHR  r1, 1
        BCC  bo1
        LD   r2, C_POLY
        XOR  r1, r2
bo1:    ST   r1, V_RND
        AND  r1, r0
        MOV  r0, r1
        SHL  r1, 4
        SHL  r0, 3
        ADD  r1, r0              ; r x 24 units = r slot times of 512 BT (3,072 cycles)
        ST   r1, V_TBO
        LDI  r0, 3
        ST   r0, V_TXS
        JMP  main
txab:   LDI  r0, 0
        ST   r0, V_TXS
        LD   r0, V_EVQ
        ORI  r0, 8               ; event 4: frame not sent
        ST   r0, V_EVQ
        JMP  main

; F: host FIFOs: received frame to the host, then events, then a frame to send from the host
phF:    LD   r1, V_UPN
        CMPI r1, 0
        BEQ  pf2
        LD   r3, V_UPP
pf1:    LDX  r0, [r3]
        PUSH.NB r0
        BEC  pf1x                ; FIFO full: next time
        ADDI r3, 1
        ADDI r1, -1
        BNE  pf1
pf1x:   ST   r3, V_UPP
        ST   r1, V_UPN
        JMP  main
pf2:    LD   r0, V_EVQ           ; events, lowest first
        CMPI r0, 0
        BEQ  pf4
        LDI  r1, 1               ; event code
        LDI  r2, 1               ; its bit
pf3:    MOV  r3, r0
        AND  r3, r2
        BNE  pf5
        ADDI r1, 1
        SHL  r2, 1
        JMP  pf3
pf5:    CMPI r1, 5
        BNE  pf6
        LD   r3, V_EVR           ; reason in the high byte
        SHL  r3, 8
        OR   r1, r3
pf6:    PUSH.NB r1
        BEC  main
        XOR  r0, r2
        ST   r0, V_EVQ
        JMP  main
pf4:    LD   r1, V_HLN           ; loading a frame from the host
        CMPI r1, 0
        BEQ  pf7
        LD   r3, V_HLP
pf8:    PULL.NB r0
        BEC  pf9
        LD   r2, C_TXEND
        CMP  r3, r2
        BCS  pf10                ; longer than the buffer: the rest is dropped
        STX  r0, [r3]
        ADDI r3, 1
pf10:   ADDI r1, -1
        BNE  pf8
pf9:    ST   r3, V_HLP
        ST   r1, V_HLN
        CMPI r1, 0
        BEQ  txrdy
        JMP  main
pf7:    LD   r0, V_TXS           ; a new frame (one at a time)
        CMPI r0, 0
        BNE  main
        PULL.NB r0
        BEC  main
        ST   r0, V_HLO           ; octets
        LDI  r1, 4
        ST   r1, V_TXS           ; loading
        LDI  r1, TXBUF+4
        ST   r1, V_HLP
        ADDI r0, 1
        SHR  r0, 1
        ST   r0, V_HLN           ; words to come
        BNE  main
        LD   r3, V_HLP
txrdy:  LDI  r0, 0               ; pad to 60 octets (4.2.3.3)
tr1:    LDI  r2, TXBUF+34
        CMP  r3, r2
        BCS  tr2
        STX  r0, [r3]
        ADDI r3, 1
        JMP  tr1
tr2:    LD   r1, V_HLO
        CMPI r1, 60
        BCS  tr3
        LDI  r1, 60
tr3:    LD   r2, C_TXMAXO
        CMP  r2, r1
        BCC  txab                ; longer than the buffer
        MOV  r0, r1
        ANDI r0, 1
        SHL  r0, 3               ; 8 bits in the last word for an odd length
        LDI  r2, 544             ; append CRC-32, start now
        OR   r0, r2
        ST   r0, V_TXC
        LDI  r0, TXBUF
        SUB  r3, r0
        ST   r3, V_TXN
        LDI  r0, 0
        ST   r0, V_ATT
        ST   r0, V_MASK
        LDI  r0, 1
        ST   r0, V_TXS
        JMP  main
