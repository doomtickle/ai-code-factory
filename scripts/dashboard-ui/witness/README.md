# LOIS's bust

The Witness: the head the Thoughtfield page draws. `SHA256SUMS` pins the model and
its seven textures.

| File | What it is |
|---|---|
| `witness.glb` | The model, made from `witness_v3.glb` by `strip_glb.py`. 9,186,376 bytes (the source is 11,476,512) |
| `normal_detail.png`, `roughness.png`, `thickness.png`, `curvature.png`, `ao.png`, `veins_mask.png`, `seam_mask.png` | 2048 px linear data maps, byte for byte the source's |
| `strip_glb.py` | The tool that removes the glb's embedded preview images. Standard library, Python 3.9. Not served |

## Provenance

Built 4 Nov 2026 in Blender 5.1.1 from the MakeHuman base mesh and shape targets, by
way of the MPFB add-on 2.0.18, with Khronos glTF Blender I/O v5.1.19. Face, mouth and
visemes come from MakeHuman's targets plus procedural changes. All surface detail is
a procedural bake: no scans of real people, nothing downloaded for it. The face is a
blend of generic targets, not modelled on anyone; no likeness search was run.

The source is the owner's `lois-assets/witness-v3/` folder (not in this repo), where
`REPORT.md` and `HANDOFF_WEB.md` give the full account, `witness_v3.blend` and
`scripts/` rebuild it. The source glb's SHA-256 is
`13c2f8a640346b3c60fc6ba4011f3d437ee18dcf05d5cddb888e39c1a024f5ae`.

Don't edit the glb or PNGs by hand. A change to geometry, morphs, UVs or bakes goes
back to the Blender scripts; then copy the PNGs and run:

    python3 strip_glb.py <source>/witness_v3.glb witness.glb
    sha256sum witness.glb *.png > SHA256SUMS

## Licence of the MakeHuman material

The source report had not re-read this. It was checked online on 5 Nov 2026 and the
claim holds: the assets are CC0 and generated output carries no GPL obligation.

MPFB's `LICENSE.md` at the tag of the version used,
<https://github.com/makehumancommunity/mpfb2/blob/v2.0.18/LICENSE.md>:

> MPFB uses the same license setup as MakeHuman, but with GPLv3 as source code license
> rather than AGPL

> The assets are defined as any data contributing to the output from MPFB.
> This includes:
>
> * The base mesh and proxies
> * Targets and modifiers
> [...]
>
> These assets have been released under CC0 1.0 Universal.

> It is the opinion of the MakeHuman team that no output from MPFB
> contains any trace of program logic. That is, regardless of whether you use
> the UI as such or if you call functions of MPFB via a script (such as
> an addon that extends MPFB), what you get is a combination of assets and your
> own creative input. As the assets have been released under CC0, there is no
> limitation on what you can do with this combined output.

The MakeHuman project's FAQ,
<https://static.makehumancommunity.org/oldsite/faq/what_changed_regarding_the_license_in_2020.html>:

> AFTER: Targets, proxies and the base mesh are considered graphical assets, covered by CC0

Limits of this finding:

- It is the project's own statement of its terms, read by an agent, not legal advice.
  On output it says "it is the opinion of" the team.
- It covers the assets bundled with MakeHuman and MPFB. The report lists no other
  third-party asset in this model.
- `www.makehumancommunity.org` could not be read: its TLS certificate names only
  `static.` and `files2.makehumancommunity.org`. The FAQ above is on `static.`.
- The MPFB code (GPLv3) is not part of any exported file, per the report; only
  geometry derived from the base mesh and targets is.

## What `strip_glb.py` changes

Three embedded previews (2.29 MB) are gone: the page replaces every material, and
GLTFLoader reads embedded images through `blob:` URLs, which the dashboard's CSP
refuses. `images`, `textures`, `samplers` and each material's texture references are
dropped, and the BIN chunk is rebuilt without those three bufferViews.

Materials keep their names and plain factors, and `KHR_materials_emissive_strength`
stays declared because `LoisSkin` and `LoisEye` still use it. So a mesh the page
forgets to re-material is obvious: `LoisSkin` is now a flat, bright white glow.

Everything else is unchanged: nodes, parents, meshes, 22 morph targets on `Head` and
`Mouth_Interior` with their normals and `extras.targetNames`, tangents, `COLOR_0`,
accessor bounds, and the bytes of every accessor, sparse ones included.

## Using it

- Units are metres, Y up, face toward +Z, base at y = 0. The `Head` pivot is the neck
  seam, world (0, 0.15448, 0.03181).
- Drive `Mouth_Interior` with the same morph weights as `Head`, every frame. Resting
  face: `lids_narrow` 0.22 + `smile` 0.1.
- Load every map with `flipY = false` as linear data. In three r147 that is
  `texture.encoding = THREE.LinearEncoding`, the default; `colorSpace` (which
  `HANDOFF_WEB.md` names) is a later API. Checked against the source's Blender render.
- `thickness.png` is literal: white is thick. Use `1 - thickness` for glow-through.

Known limits from the report: a thin line shows round the neck when the head turns
or nods far (18°, 8°), the lid margin dips into the cornea when `blink` is held
between about 0.25 and 0.75, the brow is soft, and gaze rotation and blended visemes
are untested.
