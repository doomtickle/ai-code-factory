# Dashboard fonts

The dashboard works offline, so its three typefaces are vendored here as the
Latin subsets Google Fonts serves (woff2). Each is under the SIL Open Font
License 1.1; its license text sits beside it. `SHA256SUMS` pins the files, and
`scripts/test_dashboard.py` checks them against it.

| File | Typeface | Used for | License |
|---|---|---|---|
| `vt323.woff2` | VT323 | CRT and LCD text | `OFL-VT323.txt` |
| `barlow-condensed-600.woff2`, `barlow-condensed-700.woff2` | Barlow Condensed | silkscreen labels, keys | `OFL-BarlowCondensed.txt` |
| `dotgothic16.woff2` | DotGothic16 | event printer | `OFL-DotGothic16.txt` |

Source: `https://fonts.googleapis.com/css2?family=VT323&family=Barlow+Condensed:wght@600;700&family=DotGothic16`
(the `/* latin */` faces), fetched 19 Oct 2026. To replace one, download it,
update `SHA256SUMS` (`sha256sum *.woff2 > SHA256SUMS`) and this table.
