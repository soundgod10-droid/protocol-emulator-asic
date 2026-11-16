// SPDX-License-Identifier: Apache-2.0
// Firmware editor: CodeMirror 6 with the assembler syntax, assembler error markers and the PC line of
// each thread.

import { EditorState, StateField, StateEffect, Compartment, RangeSet } from "@codemirror/state";
import {
  EditorView, keymap, lineNumbers, highlightActiveLineGutter, Decoration, gutter, GutterMarker, drawSelection,
} from "@codemirror/view";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { StreamLanguage, syntaxHighlighting, HighlightStyle } from "@codemirror/language";
import { tags as t } from "@lezer/highlight";
import { lintGutter, setDiagnostics } from "@codemirror/lint";

const MNEMONICS = new Set(
  ("HALT NOP JR LDX STX LDI ADDI MOV ADD SUB AND OR XOR CMP TST NOT ANDI ORI XORI CMPI SHL SHR ASR ROR TADD " +
    "TADDR TSET TGET NOW TNOW TSYNC TWAIT JMP BEQ BNE BCS BCC BMI BES BEC DJNZ JAL LD ST OUT SAMPLE SAMPLEL " +
    "SAMPLER WAIT PULL PUSH PULLNB PUSHNB IOR IOW").split(" "),
);

const asm = StreamLanguage.define({
  name: "pe-asm",
  token(stream) {
    if (stream.eatSpace()) return null;
    if (stream.peek() === ";") {
      stream.skipToEnd();
      return "comment";
    }
    if (stream.match(/^\.[A-Za-z]+/)) return "meta";
    if (stream.match(/^0[xX][0-9A-Fa-f]+|^0[bB][01]+|^\d+/)) return "number";
    const m = stream.match(/^[A-Za-z_]\w*/);
    if (m) {
      const w = m[0];
      if (stream.peek() === ":") return "def";
      if (/^[rR][0-3]$/.test(w)) return "atom";
      if (/^(UIO[0-7]|UO[0-6]|UI[0-4]|STATUS|FLAGS|PINS|THREAD|FALL|RISE|ANY|CT|NB|TO)$/i.test(w)) return "string";
      if (MNEMONICS.has(w.toUpperCase())) return "keyword";
      return "variable";
    }
    stream.next();
    return null;
  },
});

const style = HighlightStyle.define([
  { tag: t.comment, color: "var(--code-comment)", fontStyle: "italic" },
  { tag: t.keyword, color: "var(--code-keyword)", fontWeight: "600" },
  { tag: t.number, color: "var(--code-number)" },
  { tag: t.meta, color: "var(--code-meta)" },
  { tag: [t.definition(t.variableName), t.labelName], color: "var(--code-label)", fontWeight: "600" },
  { tag: t.atom, color: "var(--code-reg)" },
  { tag: t.string, color: "var(--code-pin)" },
]);

// PC lines: [{line, thread}]
const setPc = StateEffect.define();

class TagMarker extends GutterMarker {
  constructor(threads) {
    super();
    this.threads = threads;
  }
  eq(o) {
    return o.threads.join() === this.threads.join();
  }
  toDOM() {
    const el = document.createElement("span");
    el.className = "pc-tags";
    for (const n of this.threads) {
      const s = document.createElement("span");
      s.className = `pc-tag t${n}`;
      s.textContent = "T" + n;
      el.appendChild(s);
    }
    return el;
  }
}

function buildPc(doc, list) {
  const byLine = new Map();
  for (const { line, thread } of list) {
    if (line < 1 || line > doc.lines) continue;
    if (!byLine.has(line)) byLine.set(line, []);
    byLine.get(line).push(thread);
  }
  const lines = [...byLine.keys()].sort((a, b) => a - b);
  const deco = [];
  const marks = [];
  for (const ln of lines) {
    const th = byLine.get(ln).sort();
    const from = doc.line(ln).from;
    deco.push(Decoration.line({ class: `cm-pc ${th.map((n) => "cm-pc-t" + n).join(" ")}` }).range(from));
    marks.push(new TagMarker(th).range(from));
  }
  return { deco: Decoration.set(deco), marks: RangeSet.of(marks) };
}

const pcField = StateField.define({
  create: () => ({ deco: Decoration.none, marks: RangeSet.empty, list: [] }),
  update(v, tr) {
    for (const e of tr.effects) if (e.is(setPc)) return { ...buildPc(tr.state.doc, e.value), list: e.value };
    if (tr.docChanged) return { ...buildPc(tr.state.doc, []), list: [] };
    return v;
  },
  provide: (f) => EditorView.decorations.from(f, (v) => v.deco),
});

const pcGutter = gutter({
  class: "cm-pc-gutter",
  markers: (view) => view.state.field(pcField).marks,
  initialSpacer: () => new TagMarker([0]),
});

export function createEditor(parent, { onChange } = {}) {
  const ro = new Compartment();
  const states = new Map(); // file name -> EditorState
  let current = null;
  const extensions = (readOnly) => [
    lineNumbers(),
    highlightActiveLineGutter(),
    pcGutter,
    lintGutter(),
    history(),
    drawSelection(),
    EditorView.lineWrapping,
    keymap.of([...defaultKeymap, ...historyKeymap]),
    asm,
    syntaxHighlighting(style),
    pcField,
    ro.of([EditorState.readOnly.of(readOnly), EditorView.editable.of(!readOnly)]),
    EditorView.updateListener.of((u) => {
      if (u.docChanged && onChange) onChange(current, u.state.doc.toString());
    }),
    EditorView.theme({}, {}),
  ];
  const view = new EditorView({ parent, state: EditorState.create({ doc: "", extensions: extensions(true) }) });
  return {
    view,
    open(name, text, readOnly) {
      if (current) states.set(current, view.state);
      current = name;
      let st = states.get(name);
      if (!st || st.doc.toString() !== text) st = EditorState.create({ doc: text, extensions: extensions(readOnly) });
      view.setState(st);
    },
    reset() {
      states.clear();
      current = null;
    },
    current: () => current,
    text: () => view.state.doc.toString(),
    setErrors(list) {
      const doc = view.state.doc;
      const diags = list
        .filter((e) => e.line >= 1 && e.line <= doc.lines)
        .map((e) => ({ from: doc.line(e.line).from, to: doc.line(e.line).to, severity: "error", message: e.message }));
      view.dispatch(setDiagnostics(view.state, diags));
    },
    setPcLines(list, follow) {
      const prev = view.state.field(pcField).list;
      if (JSON.stringify(prev) === JSON.stringify(list)) return;
      const effects = [setPc.of(list)];
      if (follow !== undefined && follow >= 1 && follow <= view.state.doc.lines) {
        effects.push(EditorView.scrollIntoView(view.state.doc.line(follow).from, { y: "nearest", yMargin: 40 }));
      }
      view.dispatch({ effects });
    },
  };
}
