// Terminal 0's type and colours in the Thoughtfield (doomtickle/passdesk-ops#213). Loaded before term.js, which reads this and does
// everything else: this file holds data only, makes no request and never touches the key.
//
// Readability decides each value:
// - An opaque background, so glyphs get the browser's full antialiasing and every contrast below is exact, with no sky behind the text.
// - minimumContrastRatio: the Coordinator picks many of its own colours (truecolor reaches past this palette), so xterm lifts any
//   text under this ratio against its real cell background. It halves the ratio for dimmed text, so 9 keeps that at 4.5. It leaves box and
//   block characters alone, as drawings. term-field.css covers the dimmed text xterm's check misses.
// - lineHeight 1: JetBrains Mono's own line is already 1.32em, which is roomy, and box-drawing lines still meet from row to row.
"use strict";
window.TermSkin = {
  // In the Thoughtfield the Coordinator is LOIS (doomtickle/passdesk-ops#216). The classic page keeps its own words.
  who: "LOIS",
  // The pane is always on screen beside the page, so a state message must never move the keys into the Coordinator: only a click does.
  focusOnState: false,
  // Tells the Thoughtfield's page three fixed words (Enter pressed, output arriving, output stopped), never text or sizes. term.js sends them only in a frame the server gave the main page's port.
  tell: true,
  reasons: {
    elsewhere: ["Open elsewhere", "LOIS is open in `factory chat`. Close it there, then power on here."],
    "no-sandbox": ["LOIS isn't set up yet", "Run `factory start` first, then power on."],
    error: ["LOIS couldn't start", "Power on to try again."],
    exited: ["LOIS is off", "The session ended. Power on starts a fresh session; Resume continues the last one."],
    service: ["Restarting or updating", "The factory is restarting or updating. LOIS comes back when it's done."],
  },
  faces: ['400 16px "JetBrains Mono"', '700 16px "JetBrains Mono"'],
  // A cell is 0.6em wide. It aims for 10, 9 or 8 pixels (about 113 columns in the widest panel, over 40 on a phone), then lands on a whole
  // number of the screen's real pixels, whatever its scaling or the browser's zoom: a fractional cell softens text and shows seams through block characters.
  fontSize: () => {
    const scale = window.devicePixelRatio || 1, aim = window.innerWidth >= 900 ? 10 : window.innerWidth >= 620 ? 9 : 8;
    return Math.round(aim * scale) / scale / 0.6;
  },
  options: {
    // After JetBrains Mono: the symbols it lacks (⏺ ⎿ ✻) and a fallback if the font fails.
    fontFamily: '"JetBrains Mono", "Cascadia Mono", Consolas, Menlo, "Segoe UI Symbol", "Apple Symbols", "DejaVu Sans Mono", monospace',
    fontWeight: "400",
    fontWeightBold: "700",
    lineHeight: 1,
    allowTransparency: false,
    minimumContrastRatio: 9,
    theme: {
      background: "#070a12", foreground: "#e8eef8", cursor: "#7fe0ff", cursorAccent: "#070a12",
      selectionBackground: "rgba(124, 196, 255, 0.38)", selectionInactiveBackground: "rgba(124, 196, 255, 0.2)", selectionForeground: "#ffffff",
      // Black is mostly used as a background. As text, the contrast floor lifts it.
      black: "#3b4454", red: "#ff9aa5", green: "#7fe0a0", yellow: "#ffd479", blue: "#8cc8ff", magenta: "#d8b4fe", cyan: "#7fe0ff", white: "#cbdaee",
      brightBlack: "#a3b7d1", brightRed: "#ffb8c0", brightGreen: "#a6eebd", brightYellow: "#ffe49e", brightBlue: "#b3dbff", brightMagenta: "#e7cfff", brightCyan: "#aeeeff", brightWhite: "#f5f9ff",
    },
  },
};
