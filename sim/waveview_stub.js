// SPDX-License-Identifier: Apache-2.0
// Placeholder waveform view (plain lines), used only if wave/waveview.js does not load. Same interface
// as wave/README-interface.md: mount, setSignals, append, loadVCD, addDecoder, setTimebase, setCursor.

const ROW_H = 22;
const LABEL_W = 96;

class View {
  constructor(element) {
    this.el = element;
    this.canvas = document.createElement("canvas");
    this.canvas.style.display = "block";
    this.canvas.style.width = "100%";
    element.appendChild(this.canvas);
    this.signals = [];
    this.changes = [];
    this.decoders = [];
    this.nsPerCycle = 16.667;
    this.last = 0;
    this.cursor = null;
    this._raf = 0;
    new ResizeObserver(() => this._draw()).observe(element);
  }

  setSignals(list) {
    this.signals = list.map((s) => ({ name: s.name, width: s.width || 1, kind: s.kind || "bit" }));
    this.changes = this.signals.map(() => []);
    this.decoders = [];
    this.last = 0;
    this.cursor = null;
    this._sched();
  }

  append(cycle, values) {
    for (let i = 0; i < this.signals.length; i++) {
      const ch = this.changes[i];
      const v = values[i] === undefined ? null : values[i];
      if (!ch.length || ch[ch.length - 1][1] !== v) ch.push([cycle, v]);
    }
    if (cycle > this.last) this.last = cycle;
    this._sched();
  }

  loadVCD() {}

  addDecoder(name, cfg) {
    this.decoders.push({ name, cfg });
    this._sched();
  }

  setTimebase(ns) {
    this.nsPerCycle = ns;
    this._sched();
  }

  setCursor(cycle) {
    this.cursor = cycle;
    if (cycle > this.last) this.last = cycle;
    this._sched();
  }

  _sched() {
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => {
      this._raf = 0;
      this._draw();
    });
  }

  _draw() {
    const rows = this.signals.length;
    const w = Math.max(200, this.el.clientWidth);
    const h = (rows + 1) * ROW_H + 8;
    const dpr = window.devicePixelRatio || 1;
    this.canvas.width = Math.round(w * dpr);
    this.canvas.height = Math.round(h * dpr);
    this.canvas.style.height = h + "px";
    const g = this.canvas.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    const css = getComputedStyle(this.el);
    const fg = css.getPropertyValue("--wave-fg").trim() || "#1d2733";
    const dim = css.getPropertyValue("--wave-dim").trim() || "#8a96a3";
    const line = css.getPropertyValue("--wave-line").trim() || "#1f6fb2";
    g.clearRect(0, 0, w, h);
    g.font = "12px ui-monospace, Consolas, monospace";
    g.textBaseline = "middle";
    const end = Math.max(this.last, this.cursor || 0, 1);
    const span = Math.max(400, Math.min(4000, end));
    const start = Math.max(0, end - span);
    const plotW = w - LABEL_W - 8;
    const x = (c) => LABEL_W + ((c - start) / span) * plotW;
    this.signals.forEach((s, i) => {
      const y0 = 4 + i * ROW_H;
      g.fillStyle = fg;
      g.fillText(s.name, 4, y0 + ROW_H / 2);
      const ch = this.changes[i];
      g.strokeStyle = line;
      g.lineWidth = 1.5;
      g.beginPath();
      let k = 0;
      while (k + 1 < ch.length && ch[k + 1][0] <= start) k++;
      let first = true;
      for (; k < ch.length; k++) {
        const c0 = Math.max(ch[k][0], start);
        const c1 = k + 1 < ch.length ? ch[k + 1][0] : end;
        const v = ch[k][1];
        const y = typeof v !== "number" ? y0 + ROW_H / 2 : v ? y0 + 4 : y0 + ROW_H - 4;
        if (first) g.moveTo(x(c0), y);
        else g.lineTo(x(c0), y);
        g.lineTo(x(Math.min(c1, end)), y);
        first = false;
      }
      g.stroke();
    });
    const us = (c) => ((c * this.nsPerCycle) / 1000).toFixed(1);
    g.fillStyle = dim;
    const y = 4 + rows * ROW_H + ROW_H / 2;
    g.fillText(`cycle ${start} (${us(start)} us)`, LABEL_W, y);
    const right = `cycle ${end} (${us(end)} us)`;
    g.fillText(right, w - 8 - g.measureText(right).width, y);
  }
}

export const WaveView = {
  mount(element) {
    return new View(element);
  },
};
