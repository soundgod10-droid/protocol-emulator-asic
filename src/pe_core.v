/*
 * Copyright (c) 2026 Steven Anthony
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// pe_core: 4-thread barrel processor of the protocol emulator (A1 block D1).
//
// What it does:
//   The core executes the instruction set of docs/isa.md. Thread n
//   executes one instruction in each clock cycle c with c mod 4 = n
//   (isa 3.1), so now[1:0] is the number of the thread that has the slot.
//   The core writes pin events into the output event slots (pe_timed_out,
//   A1 block D2), reads the input history (pe_timed_in, block D3) for
//   SAMPLE and WAIT, and uses the host FIFOs (pe_host_fifo, block D6).
//
// Why it has this structure:
//   1. Thread state in a ring. The state of each thread (r0 to r3, T, PC,
//      flags, LATE, ERR, RUN and the state of a blocking instruction,
//      CW = 106 bits) is in a ring of 4 registers st0 to st3. st0 is the
//      state of the thread that has the slot. In each cycle the ring
//      turns: the new state of the slot thread goes into st3 and comes
//      back to st0 after 4 cycles, in the next slot of that thread. Each
//      flip-flop loads in each cycle from one source, so it needs no
//      load-enable multiplexer. R1_uart_tx.md 9.4 item 3: the library has
//      no flip-flop with enable, so a loadable bit costs 67.13 um2 and a
//      ring bit 48.99 um2. Also, the register read multiplexers select
//      only 1 of 4 registers (st0), not 1 of 16. M2b_core.md section 2
//      gives the synthesis areas of this choice and of the alternatives.
//   2. Instruction prefetch. The memory read for the slot at cycle c
//      occurs in cycle c-1, with the PC in st1 (the next slot thread; its
//      state is complete because its last slot was at c-4). The word goes
//      into the instruction register ir. Thus, the execute logic has a
//      full cycle, and the latch read path ends at a flip-flop.
//      docs/isa.md 3.3 does not define a read 1 to 3 cycles after a store,
//      so the one-cycle early read is not visible to correct firmware.
//   3. Blocking instructions. An instruction that must wait leaves PC as
//      it is, so the same word is read again for the next slot (isa 3.2).
//      Only a small state (ps) tells what is pending. LD and LDX keep the
//      address and the destination register in the state, because in the
//      second slot the read port reads the data word, not the instruction.
//   4. Pin events. The event time is T + offset. The lead d = t - now
//      decides on time (4 <= d <= 32,767) or late (event at now + 4, LATE
//      set), isa 4.2 and 4.3. The core writes the event into the slot of
//      the pin at the end of the cycle, so the slot needs only 1 cycle of
//      lead (pe_timed_out) and the 4-cycle LEAD of the instruction set is
//      kept with 3 cycles of margin.
//   5. SAMPLE and WAIT read the shared input history in the slot after the
//      sample time or the edge, not a per-thread capture unit (see
//      pe_timed_in). T does not change while the instruction waits, so
//      the sample time and the timeout come again from T.
//
// Reference: model/pe_model.py. The lockstep test (test/test_core.py)
// compares this RTL with the model in each clock cycle.

module pe_core (
    input  wire        clk,
    input  wire        rst_n,          // active low, asynchronous assert
    input  wire [15:0] now,            // low 16 bits of the cycle counter
    // Host configuration (pe_config) and start
    input  wire        start,          // 1 = threads start; the counter is 0 in the next cycle
    input  wire [27:0] start_pc,       // start address of thread n at [7n+6:7n]
    input  wire [3:0]  run_mask,       // RUN of thread n at start
    input  wire [29:0] pin_owner,      // owner thread of output pin p at [2p+1:2p]
    input  wire [14:0] pin_en,         // 1 = output pin p has an owner
    input  wire [14:0] pin_od,         // 1 = output pin p is open drain
    // Program and data memory (pe_latch_ram)
    output wire [6:0]  mem_raddr,      // read address in this cycle, for the next slot
    input  wire [15:0] mem_rdata,
    output reg         mem_we,         // store in this cycle
    output reg  [6:0]  mem_waddr,
    output wire [15:0] mem_wdata,
    // Output event slots (pe_timed_out, one for each output pin 0 to 14)
    input  wire [14:0] ev_ready,       // 1 = slot of pin p is empty
    output wire [14:0] ev_load,        // 1 = write the event into the slot of pin p
    output wire [15:0] ev_time,
    output wire        ev_value,
    output wire        ev_oe,
    // Timed input unit (pe_timed_in)
    input  wire [12:0] s_now,          // s(c)
    input  wire [64:0] s_hist,         // s(c-1-i) at [13*i +: 13]
    // Host FIFOs of the slot thread (pe_host_fifo)
    output reg         fifo_pull,
    output reg         fifo_push,
    output wire [15:0] fifo_wdata,
    input  wire        fifo_in_valid,
    input  wire [15:0] fifo_in_data,
    input  wire        fifo_out_full
);

  // ---------------------------------------------------------------------
  // Thread state vector (one entry of the ring)
  // ---------------------------------------------------------------------
  localparam integer F_T    = 64;   // [79:64]  time register T
  localparam integer F_PC   = 80;   // [86:80]  PC
  localparam integer F_Z    = 87;
  localparam integer F_C    = 88;
  localparam integer F_N    = 89;
  localparam integer F_E    = 90;
  localparam integer F_LATE = 91;
  localparam integer F_ERR  = 92;
  localparam integer F_RUN  = 93;
  localparam integer F_PS   = 94;   // [96:94]  pending state
  localparam integer F_LDRD = 97;   // [98:97]  LD/LDX destination register
  localparam integer F_LDA  = 99;   // [105:99] LD/LDX address
  localparam integer CW     = 106;  // r0 to r3 are [63:0], 16 bits each

  // Pending state of a blocking instruction (isa 3.2)
  localparam [2:0] PS_NONE  = 3'd0;  // no instruction pending
  localparam [2:0] PS_LD    = 3'd1;  // LD/LDX: data word read for this slot
  localparam [2:0] PS_SMP   = 3'd2;  // SAMPLE on time: complete in the first slot after T + offset
  localparam [2:0] PS_SMPL1 = 3'd3;  // SAMPLE late: sample cycle is this slot cycle (c0 + 4)
  localparam [2:0] PS_SMPL2 = 3'd4;  // SAMPLE late: complete now with s(c - 4)
  localparam [2:0] PS_WAIT1 = 3'd5;  // WAIT, first slot after the start (edge at c - 4 does not count)
  localparam [2:0] PS_WAIT  = 3'd6;  // WAIT, later slots
  localparam [2:0] PS_WEXP  = 3'd7;  // WAIT with TO and T already past: time out now

  reg [CW-1:0] st0, st1, st2, st3;
  reg [15:0]   ir;

  wire [1:0]  tid = now[1:0];

  wire [15:0] h_r0     = st0[15:0];
  wire [15:0] h_r1     = st0[31:16];
  wire [15:0] h_r2     = st0[47:32];
  wire [15:0] h_r3     = st0[63:48];
  wire [15:0] h_t      = st0[F_T+:16];
  wire [6:0]  h_pc     = st0[F_PC+:7];
  wire        h_z      = st0[F_Z];
  wire        h_c      = st0[F_C];
  wire        h_n      = st0[F_N];
  wire        h_e      = st0[F_E];
  wire        h_late   = st0[F_LATE];
  wire        h_err    = st0[F_ERR];
  wire        h_run    = st0[F_RUN];
  wire [2:0]  h_ps     = st0[F_PS+:3];
  wire [1:0]  h_ldrd   = st0[F_LDRD+:2];
  wire [6:0]  h_ldaddr = st0[F_LDA+:7];

  // ---------------------------------------------------------------------
  // Fetch: read the word for the next slot thread (st1) in this cycle.
  // At start, st1 is not yet the state of thread 0, so use its start PC.
  // ---------------------------------------------------------------------
  assign mem_raddr = start ? start_pc[6:0] :
                     (st1[F_PS+:3] == PS_LD) ? st1[F_LDA+:7] : st1[F_PC+:7];

  // ---------------------------------------------------------------------
  // Instruction fields (isa 11)
  // ---------------------------------------------------------------------
  wire [3:0] op    = ir[15:12];
  wire [3:0] pin   = ir[11:8];   // OUT, SAMPLE, WAIT
  wire [2:0] okind = ir[5:3];    // OUT/SAMPLE kind
  wire [1:0] wkind = ir[5:4];    // WAIT edge
  wire       wto   = ir[3];      // WAIT TO
  wire       wct   = ir[2];      // WAIT CT
  wire [2:0] tfn   = ir[11:9];   // TIME function
  wire [1:0] hfn   = ir[11:10];  // HOST/IO function
  wire       hnb   = ir[7];      // .NB
  wire [5:0] port  = ir[5:0];
  wire [3:0] afn   = ir[7:4];    // ALU register function
  wire [1:0] ifn   = ir[9:8];    // ALU immediate and SHIFT function
  wire [3:0] shn   = ir[7:4];    // SHIFT amount
  wire [6:0] pc1   = h_pc + 7'd1;

  // ---------------------------------------------------------------------
  // Register read ports. Port A is the register that the instruction
  // writes or uses first; its field position depends on the opcode.
  // Port B is rs of the ALU register instructions and ra of LDX/STX.
  // ---------------------------------------------------------------------
  reg [1:0] sa;
  always @* begin
    case (op)
      4'h0, 4'hC, 4'hD: sa = ir[7:6];
      4'h7:             sa = ir[8:7];
      4'hB:             sa = ir[10:9];
      4'hE:             sa = ir[9:8];
      default:          sa = ir[11:10];
    endcase
  end
  wire [1:0] sb = (op == 4'h3) ? ir[9:8] : ir[5:4];

  reg [15:0] ra, rb;
  always @* begin
    case (sa)
      2'd0:    ra = h_r0;
      2'd1:    ra = h_r1;
      2'd2:    ra = h_r2;
      default: ra = h_r3;
    endcase
    case (sb)
      2'd0:    rb = h_r0;
      2'd1:    rb = h_r1;
      2'd2:    rb = h_r2;
      default: rb = h_r3;
    endcase
  end

  // ---------------------------------------------------------------------
  // Adder: ADDI, ADD, SUB, CMP, CMPI and DJNZ use one 16-bit adder.
  // Subtract is A + not B + 1; then the carry is 1 when there is no
  // borrow (isa 9.1).
  // ---------------------------------------------------------------------
  reg [15:0] add_b;
  reg        add_cin;
  always @* begin
    add_b   = rb;
    add_cin = 1'b0;
    case (op)
      4'h2: add_b = {{6{ir[9]}}, ir[9:0]};  // ADDI: sign-extended immediate
      4'h3: begin
        if ((afn == 4'd2) || (afn == 4'd6)) begin  // SUB, CMP
          add_b   = ~rb;
          add_cin = 1'b1;
        end
      end
      4'h4: begin  // CMPI (the only ALU immediate instruction that adds)
        add_b   = ~{8'h00, ir[7:0]};
        add_cin = 1'b1;
      end
      4'h9:    add_b = 16'hFFFF;  // DJNZ: rd - 1
      default: ;
    endcase
  end
  wire [16:0] add_s = {1'b0, ra} + {1'b0, add_b} + {16'd0, add_cin};

  // ---------------------------------------------------------------------
  // Shifter: one rotate-right unit and masks give SHL, SHR, ASR and ROR.
  // SHL by n is rotate right by 16 - n with the low n bits cleared. The
  // carry is the last bit that goes out: rot[0] for SHL, rot[15] for the
  // right shifts and ROR (isa 9.1).
  // ---------------------------------------------------------------------
  wire [3:0]  rot_n  = (ifn == 2'd0) ? (4'd0 - shn) : shn;
  wire [31:0] rot_w  = {ra, ra} >> rot_n;
  wire [15:0] rot    = rot_w[15:0];
  wire [15:0] mask_r = 16'hFFFF >> shn;
  wire [15:0] mask_l = 16'hFFFF << shn;
  reg  [15:0] sh_res;
  always @* begin
    if (shn == 4'd0) begin
      sh_res = ra;
    end else begin
      case (ifn)
        2'd0:    sh_res = rot & mask_l;                                // SHL
        2'd1:    sh_res = rot & mask_r;                                // SHR
        2'd2:    sh_res = (rot & mask_r) | (ra[15] ? ~mask_r : 16'h0);  // ASR
        default: sh_res = rot;                                         // ROR
      endcase
    end
  end
  wire sh_c = (ifn == 2'd0) ? rot[0] : rot[15];

  // ---------------------------------------------------------------------
  // Time unit (isa 4, 6, 7).
  // tsum:  T + imm12 (TADD), T + rs (TADD rs), now + imm7 (TNOW, TSYNC),
  //        T + offset (OUT, SAMPLE: the event time t).
  // tdiff: t - now for OUT and SAMPLE (the lead d), else T - now.
  // ---------------------------------------------------------------------
  wire       base_now = (op == 4'h7) && ((tfn == 3'd4) || (tfn == 3'd5));
  reg [15:0] tadd_b;
  always @* begin
    case (op)
      4'h6:    tadd_b = {4'h0, ir[11:0]};
      4'h7:    tadd_b = (tfn == 3'd0) ? ra : {9'h000, ir[6:0]};
      4'hC:    tadd_b = {13'h0000, ir[2:0]};
      default: tadd_b = 16'h0000;
    endcase
  end
  wire [15:0] tsum  = (base_now ? now : h_t) + tadd_b;
  wire [15:0] tdiff = ((op == 4'hC) ? tsum : h_t) - now;

  // isa 4.2: on time if 4 <= d <= 32,767. Else the event is late and
  // occurs at now + 4 (isa 4.3).
  wire        ontime = !tdiff[15] && (tdiff[14:2] != 13'd0);
  wire [15:0] now_p4 = now + 16'd4;
  wire [15:0] ev_at  = ontime ? tsum : now_p4;

  // TSYNC: (T - now) as a signed number is less than imm7.
  wire tsync_move = tdiff[15] || ((tdiff[14:7] == 8'd0) && (tdiff[6:0] < ir[6:0]));

  // ---------------------------------------------------------------------
  // Input history of the pin of this instruction: hp[i] = s(c-1-i).
  // Inputs 13 to 15 read 0 (isa 5.1).
  // ---------------------------------------------------------------------
  wire [4:0] hp;
  genvar gi;
  generate
    for (gi = 0; gi < 5; gi = gi + 1) begin : g_hp
      wire [15:0] hv = {3'b000, s_hist[13*gi+:13]};
      assign hp[gi] = hv[pin];
    end
  endgenerate

  // SAMPLE: the instruction completes in the first slot after t, so
  // c - t = -tdiff is 1 to 4, and the bit is s(t) = hp[c - t - 1].
  wire       smp_due = (tdiff[15:2] == 14'h3FFF);
  wire [1:0] smp_k1  = ~tdiff[1:0];  // c - t - 1
  wire       smp_bit = (h_ps == PS_SMPL2) ? hp[3] : hp[smp_k1];

  // WAIT: an edge at cycle c - k (k = 1 to 4) is between s(c-k-1) =
  // hp[k] and s(c-k) = hp[k-1] (isa 6.3). With TO, the edge counts only
  // up to the timeout cycle T: T - (c - k) = tdiff + k >= 0. While the
  // thread waits, tdiff is -4 or more (it times out in the first slot
  // after T), so only tdiff = -1 to -4 removes edges.
  reg  [4:1] e_hit;
  integer k;
  always @* begin
    for (k = 1; k <= 4; k = k + 1) begin
      case (wkind)
        2'd0:    e_hit[k] = hp[k] && !hp[k-1];  // FALL
        2'd1:    e_hit[k] = !hp[k] && hp[k-1];  // RISE
        default: e_hit[k] = hp[k] != hp[k-1];   // ANY
      endcase
      if (wto && tdiff[15] && !((tdiff[15:2] == 14'h3FFF) && ((k - 1) >= {30'd0, ~tdiff[1:0]})))
        e_hit[k] = 1'b0;
    end
    // The edge at c - 4 is the cycle of the WAIT start; only later edges count.
    if (h_ps == PS_WAIT1) e_hit[4] = 1'b0;
  end
  // The first edge in time is the largest k.
  wire [2:0]  e_k      = e_hit[4] ? 3'd4 : e_hit[3] ? 3'd3 : e_hit[2] ? 3'd2 : 3'd1;
  wire        e_any    = |e_hit;
  wire [15:0] e_time   = now - {13'd0, e_k};
  wire        w_tout   = wto && tdiff[15];  // the timeout cycle T is before this slot
  // At the start: with TO, T now or in the past (d = 0 or d >= 32,768).
  wire        w_expired = wto && ((tdiff == 16'd0) || tdiff[15]);

  // ---------------------------------------------------------------------
  // OUT (isa 4.4, 4.5, 5.2)
  // ---------------------------------------------------------------------
  wire [15:0] pin_en16 = {1'b0, pin_en};
  wire [15:0] pin_od16 = {1'b0, pin_od};
  wire [15:0] ready16  = {1'b0, ev_ready};
  wire [31:0] owner32  = {2'b00, pin_owner};
  wire        owned    = pin_en16[pin] && (owner32[2*pin+:2] == tid);  // pin 15: never
  wire        obit     = (okind == 3'd0) ? 1'b0 : (okind == 3'd1) ? 1'b1 :
                         (okind == 3'd5) ? ra[15] : ra[0];
  wire        oz       = (okind == 3'd2) || (obit && pin_od16[pin]);
  assign ev_value = oz ? 1'b1 : obit;
  assign ev_oe    = !oz;
  assign ev_time  = ev_at;

  // ---------------------------------------------------------------------
  // Execute: next state of the slot thread and the side effects.
  // ---------------------------------------------------------------------
  reg        wen;      // write register wsel
  reg [1:0]  wsel;
  reg [15:0] wval;
  reg [15:0] n_t;
  reg [6:0]  n_pc;
  reg        n_z, n_c, n_n, n_e, n_late, n_err, n_run;
  reg [2:0]  n_ps;
  reg [1:0]  n_ldrd;
  reg [6:0]  n_ldaddr;
  reg        o_load;   // write the event into the slot of "pin"

  assign mem_wdata  = ra;
  assign fifo_wdata = ra;
  assign ev_load    = o_load ? (15'd1 << pin) : 15'd0;

  reg [15:0] alu_res;  // result of the ALU instructions that set Z and N

  always @* begin
    wen      = 1'b0;
    wsel     = sa;
    wval     = 16'h0000;
    n_t      = h_t;
    n_pc     = h_pc;
    n_z      = h_z;
    n_c      = h_c;
    n_n      = h_n;
    n_e      = h_e;
    n_late   = h_late;
    n_err    = h_err;
    n_run    = h_run;
    n_ps     = h_ps;
    n_ldrd   = h_ldrd;
    n_ldaddr = h_ldaddr;
    mem_we    = 1'b0;
    mem_waddr = ir[6:0];
    o_load    = 1'b0;
    fifo_pull = 1'b0;
    fifo_push = 1'b0;
    alu_res   = 16'h0000;

    if (h_run) begin
      case (h_ps)
        // Second slot of LD/LDX: ir holds the data word (isa 3.3).
        PS_LD: begin
          wen  = 1'b1;
          wsel = h_ldrd;
          wval = ir;
          n_ps = PS_NONE;
          n_pc = pc1;
        end

        PS_SMP, PS_SMPL1, PS_SMPL2: begin
          if (h_ps == PS_SMPL1) begin
            // The late sample occurs at the end of this cycle (c0 + 4).
            n_ps = PS_SMPL2;
          end else if ((h_ps == PS_SMPL2) || smp_due) begin
            wen  = 1'b1;
            wval = okind[0] ? {ra[14:0], smp_bit} : {smp_bit, ra[15:1]};  // r<< or r>>
            n_e  = smp_bit;
            n_ps = PS_NONE;
            n_pc = pc1;
          end
        end

        PS_WAIT1, PS_WAIT, PS_WEXP: begin
          if ((h_ps != PS_WEXP) && e_any) begin
            wen  = 1'b1;
            wval = e_time;
            if (wct) n_t = e_time;
            n_e  = 1'b1;
            n_ps = PS_NONE;
            n_pc = pc1;
          end else if ((h_ps == PS_WEXP) || w_tout) begin
            n_e  = 1'b0;
            n_ps = PS_NONE;
            n_pc = pc1;
          end else begin
            n_ps = PS_WAIT;
          end
        end

        default: begin  // PS_NONE: execute the instruction in ir
          case (op)
            4'h0: begin
              case (ir[11:8])
                4'h0: n_run = 1'b0;  // HALT: PC stays at the HALT
                4'h1: n_pc = pc1;    // NOP
                4'h2: n_pc = ra[6:0];  // JR
                4'h3: begin  // LDX: read in the next slot
                  n_ps     = PS_LD;
                  n_ldrd   = ir[7:6];
                  n_ldaddr = rb[6:0];
                end
                4'h4: begin  // STX
                  mem_we    = 1'b1;
                  mem_waddr = rb[6:0];
                  n_pc      = pc1;
                end
                default: begin  // illegal (isa 12)
                  n_run = 1'b0;
                  n_err = 1'b1;
                end
              endcase
            end

            4'h1: begin  // LDI
              wen  = 1'b1;
              wval = {6'd0, ir[9:0]};
              n_pc = pc1;
            end

            4'h2: begin  // ADDI
              wen     = 1'b1;
              wval    = add_s[15:0];
              alu_res = add_s[15:0];
              n_c     = add_s[16];
              n_z     = (alu_res == 16'd0);
              n_n     = alu_res[15];
              n_pc    = pc1;
            end

            4'h3: begin  // ALU register
              if (afn > 4'd8) begin
                n_run = 1'b0;
                n_err = 1'b1;
              end else begin
                case (afn)
                  4'd0:    alu_res = rb;           // MOV
                  4'd3,
                  4'd7:    alu_res = ra & rb;      // AND, TST
                  4'd4:    alu_res = ra | rb;      // OR
                  4'd5:    alu_res = ra ^ rb;      // XOR
                  4'd8:    alu_res = ~rb;          // NOT
                  default: alu_res = add_s[15:0];  // ADD, SUB, CMP
                endcase
                if ((afn == 4'd1) || (afn == 4'd2) || (afn == 4'd6)) n_c = add_s[16];
                n_z  = (alu_res == 16'd0);
                n_n  = alu_res[15];
                wen  = (afn != 4'd6) && (afn != 4'd7);  // CMP and TST keep rd
                wval = alu_res;
                n_pc = pc1;
              end
            end

            4'h4: begin  // ALU immediate
              case (ifn)
                2'd0:    alu_res = ra & {8'h00, ir[7:0]};  // ANDI
                2'd1:    alu_res = ra | {8'h00, ir[7:0]};  // ORI
                2'd2:    alu_res = ra ^ {8'h00, ir[7:0]};  // XORI
                default: alu_res = add_s[15:0];            // CMPI
              endcase
              if (ifn == 2'd3) n_c = add_s[16];
              n_z  = (alu_res == 16'd0);
              n_n  = alu_res[15];
              wen  = (ifn != 2'd3);
              wval = alu_res;
              n_pc = pc1;
            end

            4'h5: begin  // SHIFT
              wen  = 1'b1;
              wval = sh_res;
              n_z  = (sh_res == 16'd0);
              n_n  = sh_res[15];
              if (shn != 4'd0) n_c = sh_c;  // n = 0: C does not change
              n_pc = pc1;
            end

            4'h6: begin  // TADD imm12
              n_t  = tsum;
              n_pc = pc1;
            end

            4'h7: begin  // TIME
              n_pc = pc1;
              case (tfn)
                3'd0, 3'd4: n_t = tsum;  // TADD rs, TNOW
                3'd1:       n_t = ra;    // TSET
                3'd2: begin  // TGET
                  wen  = 1'b1;
                  wval = h_t;
                end
                3'd3: begin  // NOW
                  wen  = 1'b1;
                  wval = now;
                end
                3'd5: if (tsync_move) n_t = tsum;  // TSYNC
                3'd6: if (!tdiff[15]) n_pc = h_pc;  // TWAIT: wait while T is not past
                default: begin
                  n_pc  = h_pc;
                  n_run = 1'b0;
                  n_err = 1'b1;
                end
              endcase
            end

            4'h8: begin  // Bcc (isa 9.2)
              case (ir[11:9])
                3'd0:    n_pc = ir[6:0];
                3'd1:    n_pc = h_z ? ir[6:0] : pc1;
                3'd2:    n_pc = h_z ? pc1 : ir[6:0];
                3'd3:    n_pc = h_c ? ir[6:0] : pc1;
                3'd4:    n_pc = h_c ? pc1 : ir[6:0];
                3'd5:    n_pc = h_n ? ir[6:0] : pc1;
                3'd6:    n_pc = h_e ? ir[6:0] : pc1;
                default: n_pc = h_e ? pc1 : ir[6:0];
              endcase
            end

            4'h9: begin  // DJNZ: flags do not change
              wen  = 1'b1;
              wval = add_s[15:0];
              n_pc = (add_s[15:0] != 16'd0) ? ir[6:0] : pc1;
            end

            4'hA: begin  // JAL
              wen  = 1'b1;
              wval = {9'd0, pc1};
              n_pc = ir[6:0];
            end

            4'hB: begin
              if (ir[11]) begin  // ST
                mem_we = 1'b1;
                n_pc   = pc1;
              end else begin  // LD: read in the next slot
                n_ps     = PS_LD;
                n_ldrd   = ir[10:9];
                n_ldaddr = ir[6:0];
              end
            end

            4'hC: begin
              if (okind[2:1] == 2'b11) begin  // SAMPLE: start, complete later (isa 6.2)
                if (!ontime) n_late = 1'b1;
                n_ps = ontime ? PS_SMP : PS_SMPL1;
              end else if (!(owned && !ready16[pin])) begin
                // OUT completes, unless the pin is owned and its slot is
                // full (then it waits, isa 4.4).
                n_pc = pc1;
                if (okind == 3'd4) begin  // OUT r>>
                  wen  = 1'b1;
                  wval = {1'b0, ra[15:1]};
                end
                if (okind == 3'd5) begin  // OUT r<<
                  wen  = 1'b1;
                  wval = {ra[14:0], 1'b0};
                end
                if (!owned) begin
                  n_err = 1'b1;  // isa 5.2: no pin change
                end else begin
                  o_load = 1'b1;
                  if (!ontime) n_late = 1'b1;
                end
              end
            end

            4'hD: begin  // WAIT: start (isa 6.3)
              if (wkind == 2'd3) begin
                n_run = 1'b0;
                n_err = 1'b1;
              end else begin
                n_ps = w_expired ? PS_WEXP : PS_WAIT1;
              end
            end

            4'hE: begin  // HOST and I/O ports (isa 8.5, 10)
              case (hfn)
                2'd0: begin  // PULL, PULL.NB
                  if (fifo_in_valid) begin
                    fifo_pull = 1'b1;
                    wen       = 1'b1;
                    wval      = fifo_in_data;
                    n_z       = (fifo_in_data == 16'd0);
                    n_n       = fifo_in_data[15];
                    n_e       = 1'b1;
                    n_pc      = pc1;
                  end else if (hnb) begin
                    n_e  = 1'b0;
                    n_pc = pc1;
                  end
                end
                2'd1: begin  // PUSH, PUSH.NB
                  if (!fifo_out_full) begin
                    fifo_push = 1'b1;
                    n_e       = 1'b1;
                    n_pc      = pc1;
                  end else if (hnb) begin
                    n_e  = 1'b0;
                    n_pc = pc1;
                  end
                end
                2'd2: begin  // IOR
                  wen  = 1'b1;
                  n_pc = pc1;
                  case (port)
                    6'd0:    wval = {14'd0, h_err, h_late};       // STATUS
                    6'd1:    wval = {12'd0, h_e, h_n, h_c, h_z};  // FLAGS
                    6'd2:    wval = {3'd0, s_now};                // PINS
                    6'd3:    wval = {14'd0, tid};                 // THREAD
                    default: wval = 16'h0000;
                  endcase
                end
                default: begin  // IOW
                  n_pc = pc1;
                  if (port == 6'd0) begin
                    if (ra[0]) n_late = 1'b0;
                    if (ra[1]) n_err = 1'b0;
                  end
                  if (port == 6'd1) begin
                    n_z = ra[0];
                    n_c = ra[1];
                    n_n = ra[2];
                    n_e = ra[3];
                  end
                end
              endcase
            end

            default: begin  // opcode 15: illegal
              n_run = 1'b0;
              n_err = 1'b1;
            end
          endcase
        end
      endcase
    end
  end

  // ---------------------------------------------------------------------
  // Next state and the ring
  // ---------------------------------------------------------------------
  wire [15:0] n_r0 = (wen && (wsel == 2'd0)) ? wval : h_r0;
  wire [15:0] n_r1 = (wen && (wsel == 2'd1)) ? wval : h_r1;
  wire [15:0] n_r2 = (wen && (wsel == 2'd2)) ? wval : h_r2;
  wire [15:0] n_r3 = (wen && (wsel == 2'd3)) ? wval : h_r3;

  wire [CW-1:0] nc = {n_ldaddr, n_ldrd, n_ps, n_run, n_err, n_late, n_e, n_n, n_c, n_z,
                      n_pc, n_t, n_r3, n_r2, n_r1, n_r0};

  // At start, each ring entry gets the start PC and RUN of the thread
  // that it holds in cycle 0 (entry i holds thread i). The other fields
  // are 0 from the chip reset (see pe_test_port).
  function [CW-1:0] at_start(input [CW-1:0] x, input [6:0] pc, input run);
    begin
      at_start          = x;
      at_start[F_PC+:7] = pc;
      at_start[F_RUN]   = run;
      at_start[F_PS+:3] = PS_NONE;
    end
  endfunction

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      st0 <= {CW{1'b0}};
      st1 <= {CW{1'b0}};
      st2 <= {CW{1'b0}};
      st3 <= {CW{1'b0}};
      ir  <= 16'h0000;
    end else begin
      // Each entry loads in every cycle from one source: no enable mux.
      st3 <= start ? at_start(nc, start_pc[27:21], run_mask[3]) : nc;
      st2 <= start ? at_start(st3, start_pc[20:14], run_mask[2]) : st3;
      st1 <= start ? at_start(st2, start_pc[13:7], run_mask[1]) : st2;
      st0 <= start ? at_start(st1, start_pc[6:0], run_mask[0]) : st1;
      ir  <= mem_rdata;
    end
  end

endmodule
