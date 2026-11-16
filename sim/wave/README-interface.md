# web/wave: waveform view interface

The page shell loads `wave/waveview.js` (an ES module) and uses only this interface.

```js
import { WaveView } from './wave/waveview.js';

const view = WaveView.mount(element);          // draws into element; returns the view
view.setSignals([{ name, width, kind }]);      // kind: 'bit' | 'bus'; clears the old data
view.append(cycle, values);                    // streaming from the engine: values[i] for signal i
view.loadVCD(text, { title });                 // replay of a recorded VCD
view.addDecoder(name, { protocol, pins });     // decoder row(s) under the signals
view.setTimebase(ns_per_cycle);                // 60 MHz = 16.667 ns
view.setCursor(cycle);                         // cursor A at a cycle (the shell's current cycle)
```

## Details

- `values[i]` in `append`: a number (bit: 0 or 1; bus: the integer value), or `'z'` (released, not driven),
  or `'x'` (unknown); `null` or `undefined` count as unknown. Cycles must not go backwards; a value is stored only when it changes.
- `addDecoder(name, { protocol, pins, options })`: `protocol` is one of `uart`, `spi`, `i2c`, `can`, `jtag`,
  `swd`, `ps2`, `onewire`. `pins` maps each role of the protocol to a signal name, for example
  `{ protocol: 'i2c', pins: { scl: 'scl', sda: 'sda' } }`. `options` is optional (for example
  `{ baud: 115200 }` for UART; `{ cpol: 0, cpha: 0 }` for SPI; `{ bitrate: 250000 }` for CAN). The roles and
  options of each protocol are listed in `wave/decoders/index.js` (`PROTOCOLS`).
- `loadVCD(text, { title, nsPerCycle })`: `nsPerCycle` is optional and sets the timebase of the replay (for
  example 16.668 for the 60 MHz test clock of the chip tests).
- Extras, not needed by the shell: `view.setTiming(name | null)` shows the timing limits of one decoder on the
  waveform; `view.addArrows([{ fromCycle, toCycle, signal, label }])` draws an arrow from an instruction cycle
  to the edge it scheduled; `view.setTheme('light' | 'dark' | 'auto')`; `view.fit()`; `view.destroy()`.
