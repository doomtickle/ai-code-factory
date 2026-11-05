# Vendored three.js

The Thoughtfield (LOIS's WebGL page in `factory dashboard`) draws with three.js.
The dashboard works offline and loads nothing from the network, so the files it
needs are vendored here, flat, keeping their base names. **Every byte is identical
to the package; nothing was changed.** `SHA256SUMS` pins them.

Version: **three 0.147.0** exactly (revision 147), MIT, `LICENSE` is the package's own.

| File | Path in the package |
|---|---|
| `three.min.js` | `build/three.min.js` |
| `GLTFLoader.js` | `examples/js/loaders/GLTFLoader.js` |
| `CopyShader.js` | `examples/js/shaders/CopyShader.js` |
| `LuminosityHighPassShader.js` | `examples/js/shaders/LuminosityHighPassShader.js` |
| `Pass.js` | `examples/js/postprocessing/Pass.js` |
| `EffectComposer.js` | `examples/js/postprocessing/EffectComposer.js` |
| `RenderPass.js` | `examples/js/postprocessing/RenderPass.js` |
| `ShaderPass.js` | `examples/js/postprocessing/ShaderPass.js` |
| `UnrealBloomPass.js` | `examples/js/postprocessing/UnrealBloomPass.js` |

These are classic scripts that attach to a global `THREE`, so `three.min.js` loads
first. This order works: `three.min.js`, `CopyShader.js`,
`LuminosityHighPassShader.js`, `Pass.js`, `EffectComposer.js`, `RenderPass.js`,
`ShaderPass.js`, `UnrealBloomPass.js`, `GLTFLoader.js`.

Source: `https://cdn.jsdelivr.net/npm/three@0.147.0/<path>`, fetched 5 Nov 2026 and
compared byte for byte with the registry tarball
`https://registry.npmjs.org/three/-/three-0.147.0.tgz`, whose own digest matched the
registry's record:

    integrity  sha512-LPTOslYQXFkmvceQjFTNnVVli2LaVF6C99Pv34fJypp8NbQLbTlu3KinZ0zURghS5zEehK+VQyvWuPZ/Sm8fzw==
    shasum     1974af9e8e0c1efb3a8561334d57c0b6c29a7951

## Re-fetching

In an empty scratch folder, outside this repo:

    curl -fsSLO https://registry.npmjs.org/three/-/three-0.147.0.tgz
    openssl dgst -sha512 -binary three-0.147.0.tgz | base64 -w0   # must equal the integrity value above
    tar -xzf three-0.147.0.tgz --no-same-owner package/LICENSE package/build/three.min.js package/examples/js

Copy the files in the table and `package/LICENSE` here, then, in this folder,
`sha256sum -c SHA256SUMS`. For another version, also update the version and digests
above and regenerate the file:

    sha256sum three.min.js GLTFLoader.js CopyShader.js LuminosityHighPassShader.js Pass.js EffectComposer.js RenderPass.js ShaderPass.js UnrealBloomPass.js > SHA256SUMS

Check any new version under the dashboard's CSP (no `unsafe-eval`, no `blob:`).
