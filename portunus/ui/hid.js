// USB HID keyboard usage IDs (HID Usage Tables, page 0x07), keyed by
// KeyboardEvent.code so the mapping follows physical keys, not layout.

const table = {};

"ABCDEFGHIJKLMNOPQRSTUVWXYZ".split("").forEach((letter, i) => {
  table[`Key${letter}`] = 0x04 + i;
});
"1234567890".split("").forEach((digit, i) => {
  table[`Digit${digit}`] = 0x1e + i;
});
for (let n = 1; n <= 12; n += 1) table[`F${n}`] = 0x39 + n;
"123456789".split("").forEach((digit, i) => {
  table[`Numpad${digit}`] = 0x59 + i;
});

Object.assign(table, {
  Enter: 0x28, Escape: 0x29, Backspace: 0x2a, Tab: 0x2b, Space: 0x2c,
  Minus: 0x2d, Equal: 0x2e, BracketLeft: 0x2f, BracketRight: 0x30,
  Backslash: 0x31, Semicolon: 0x33, Quote: 0x34, Backquote: 0x35,
  Comma: 0x36, Period: 0x37, Slash: 0x38, CapsLock: 0x39,
  Insert: 0x49, Home: 0x4a, PageUp: 0x4b, Delete: 0x4c, End: 0x4d, PageDown: 0x4e,
  ArrowRight: 0x4f, ArrowLeft: 0x50, ArrowDown: 0x51, ArrowUp: 0x52,
  NumpadDivide: 0x54, NumpadMultiply: 0x55, NumpadSubtract: 0x56, NumpadAdd: 0x57,
  NumpadEnter: 0x58, Numpad0: 0x62, NumpadDecimal: 0x63, IntlBackslash: 0x64,
  ControlLeft: 0xe0, ShiftLeft: 0xe1, AltLeft: 0xe2, MetaLeft: 0xe3,
  ControlRight: 0xe4, ShiftRight: 0xe5, AltRight: 0xe6, MetaRight: 0xe7,
});

export const USAGE = Object.freeze(table);
export const CMD_LEFT = 0xe3;
export const KEY_C = 0x06;
export const KEY_V = 0x19;

// iOS editing shortcuts use Command. With ctrlAsCmd on, the PC's Ctrl keys
// act as Command so Ctrl+C / Ctrl+V / Ctrl+Z behave like they do on a Mac.
export function usageFor(code, ctrlAsCmd) {
  if (ctrlAsCmd) {
    if (code === "ControlLeft") return 0xe3;
    if (code === "ControlRight") return 0xe7;
  }
  return USAGE[code];
}
