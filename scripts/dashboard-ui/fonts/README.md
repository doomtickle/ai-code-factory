# Dashboard fonts

The dashboard works offline, so its typefaces are vendored here as the
Latin subsets Google Fonts serves (woff2). Each is under the SIL Open Font
License 1.1; its license text sits beside it. `SHA256SUMS` pins the files, and
`scripts/test_dashboard.py` checks them against it.

| File | Typeface | Used for | License |
|---|---|---|---|
| `vt323.woff2` | VT323 | CRT and LCD text | `OFL-VT323.txt` |
| `barlow-condensed-600.woff2`, `barlow-condensed-700.woff2` | Barlow Condensed | silkscreen labels, keys | `OFL-BarlowCondensed.txt` |
| `dotgothic16.woff2` | DotGothic16 | event printer | `OFL-DotGothic16.txt` |
| `cormorant-garamond-variable.woff2` | Cormorant Garamond, variable, `wght` 500 to 600 | the Thoughtfield's voice | `OFL-CormorantGaramond.txt` |
| `hanken-grotesk-variable.woff2` | Hanken Grotesk, variable, `wght` 400 to 700 | the Thoughtfield's data | `OFL-HankenGrotesk.txt` |
| `jetbrains-mono-400.woff2`, `jetbrains-mono-700.woff2` | JetBrains Mono 2.304, regular and bold | the Thoughtfield's Terminal 0 | `OFL-JetBrainsMono.txt` |

Google serves those last two as one variable file per family, cut to the weights
asked for, so each `@font-face` must declare exactly that range
(`font-weight: 500 600` and `font-weight: 400 700`). They have no `→` (U+2192): it
falls back to another font.

Source: `https://fonts.googleapis.com/css2?family=VT323&family=Barlow+Condensed:wght@600;700&family=DotGothic16`
(the `/* latin */` faces), fetched 19 Oct 2026, and
`https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@500;600&family=Hanken+Grotesk:wght@400;500;600;700&display=swap`
(the `/* latin */` faces, asked with a Chrome User-Agent so it answers with woff2),
fetched 5 Nov 2026. Those two licence texts are from `github.com/google/fonts`
(`ofl/cormorantgaramond/OFL.txt`, `ofl/hankengrotesk/OFL.txt`). JetBrains Mono is not the Google subset, because a terminal needs its box-drawing and
block characters: the two woff2 and `OFL.txt` are from the v2.304 release zip (`fonts/webfonts/`,
`github.com/JetBrains/JetBrainsMono/releases`, zip SHA-256
`6f6376c6ed2960ea8a963cd7387ec9d76e3f629125bc33d1fdcd7eb7012f7bbf`), fetched 5 Nov 2026. It has no
`⏺ ⎿ ✻` and a few other symbols: those fall back to a system font, and xterm keeps them on the grid. To replace one,
download it, update `SHA256SUMS` (`sha256sum *.woff2 > SHA256SUMS`) and this table.
