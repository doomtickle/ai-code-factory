# Vendored xterm.js

Terminal 0 (the Coordinator's terminal in `factory dashboard`) draws with
xterm.js. The dashboard works offline and loads nothing from the network, so
the files it needs are vendored here, unmodified, from the npm registry
tarballs (no source maps). `SHA256SUMS` pins them, and
`scripts/test_dashboard_term.py` checks them against it.

| File | Package | Version | License |
|---|---|---|---|
| `xterm.js`, `xterm.css` | `@xterm/xterm` (`lib/xterm.js`, `css/xterm.css`) | 5.5.0 | MIT, `LICENSE-xterm.txt` |
| `addon-fit.js` | `@xterm/addon-fit` (`lib/addon-fit.js`) | 0.10.0 | MIT, `LICENSE-addon-fit.txt` |

Source: `https://registry.npmjs.org/@xterm/xterm/-/xterm-5.5.0.tgz` and
`https://registry.npmjs.org/@xterm/addon-fit/-/addon-fit-0.10.0.tgz`, fetched
19 Oct 2026. No other addons are used: no clipboard (OSC 52), links or images.
To update, replace the files from the new tarballs, then
`sha256sum xterm.js xterm.css addon-fit.js > SHA256SUMS` and update this table.
The files have LF line endings and must keep them (the checksums cover the bytes).
