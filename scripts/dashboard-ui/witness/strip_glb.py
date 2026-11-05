#!/usr/bin/env python3
"""Remove the embedded preview images from a glTF binary (.glb).

    python3 strip_glb.py SOURCE.glb OUTPUT.glb

The Thoughtfield page replaces every material on LOIS's bust, so the previews in
the export are dead weight. They are also a problem: GLTFLoader reads an embedded
image through a blob: URL (fetch, so connect-src, in three r147), and the dashboard's
CSP allows blob: nowhere.

Drops `images`, `textures`, `samplers` and every texture reference in a material,
keeping the material and its plain factors. An extension is dropped from
`extensionsUsed` only if nothing uses it any more. The BIN chunk is rebuilt with only
the bufferViews an accessor still reads, in their original order, so the file really
shrinks; each view's bytes are copied unchanged. Nodes, meshes, morph targets,
extras and accessors are untouched apart from the bufferView numbers they point at.

Standard library only, Python 3.9. Anything it doesn't understand is refused rather
than guessed at. It never writes to the source.
"""

import json
import os
import struct
import sys

GLB_MAGIC = b"glTF"
JSON_CHUNK = 0x4E4F534A
BIN_CHUNK = 0x004E4942
ALIGN = 4  # the widest component a glTF accessor has, and the GLB chunk alignment


class Refused(Exception):
    pass


def read_glb(data):
    """The (json dict, BIN bytes) of a GLB, checking every length against the file."""
    if len(data) < 12:
        raise Refused("too short to be a GLB")
    magic, version, total = struct.unpack_from("<4sII", data, 0)
    if magic != GLB_MAGIC or version != 2:
        raise Refused("not a glTF 2 binary")
    if total != len(data):
        raise Refused(f"header says {total} bytes, the file has {len(data)}")
    chunks, off = [], 12
    while off < total:
        if off + 8 > total:
            raise Refused("truncated chunk header")
        size, kind = struct.unpack_from("<II", data, off)
        if size % ALIGN or off + 8 + size > total:
            raise Refused("bad chunk length")
        chunks.append((kind, data[off + 8:off + 8 + size]))
        off += 8 + size
    if len(chunks) != 2 or chunks[0][0] != JSON_CHUNK or chunks[1][0] != BIN_CHUNK:
        raise Refused("expected exactly one JSON chunk and one BIN chunk")
    return json.loads(chunks[0][1].decode("utf-8")), chunks[1][1]


def write_glb(doc, bin_data):
    text = json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    text += b" " * (-len(text) % ALIGN)
    if len(bin_data) % ALIGN:
        raise Refused("BIN chunk is not aligned")
    total = 12 + 8 + len(text) + 8 + len(bin_data)
    return b"".join((
        struct.pack("<4sII", GLB_MAGIC, 2, total),
        struct.pack("<II", len(text), JSON_CHUNK), text,
        struct.pack("<II", len(bin_data), BIN_CHUNK), bin_data,
    ))


def drop_texture_refs(node):
    """Remove every `...Texture` object below a material, extensions included."""
    if isinstance(node, dict):
        for key in [k for k in node if k.endswith("Texture")]:
            del node[key]
        for value in node.values():
            drop_texture_refs(value)
    elif isinstance(node, list):
        for value in node:
            drop_texture_refs(value)


def accessor_view_refs(accessor):
    """The objects inside one accessor that hold a `bufferView` number."""
    refs = []
    if "bufferView" in accessor:
        refs.append(accessor)
    sparse = accessor.get("sparse")
    if sparse is not None:
        refs += [sparse["indices"], sparse["values"]]
    return refs


def strip(doc, bin_data):
    """A new (json dict, BIN bytes) without images. `doc` is changed in place."""
    if doc.get("extensionsRequired"):
        raise Refused(f"requires extensions this tool can't vouch for: {doc['extensionsRequired']}")
    buffers = doc.get("buffers", [])
    if len(buffers) != 1 or "uri" in buffers[0]:
        raise Refused("expected one buffer, the BIN chunk")
    views = doc.get("bufferViews", [])
    for view in views:
        if view.get("buffer") != 0:
            raise Refused("a bufferView names another buffer")
        start = view.get("byteOffset", 0)
        if start + view["byteLength"] > buffers[0]["byteLength"] or buffers[0]["byteLength"] > len(bin_data):
            raise Refused("a bufferView runs past the BIN chunk")

    image_views = {image["bufferView"] for image in doc.get("images", []) if "bufferView" in image}
    if any("uri" in image for image in doc.get("images", [])):
        raise Refused("an image points outside the file")
    for material in doc.get("materials", []):
        drop_texture_refs(material)
    for key in ("images", "textures", "samplers"):
        doc.pop(key, None)

    # Only accessors keep a view alive. Anything else naming one (a compression
    # extension, say) is outside what this tool understands.
    refs = [ref for accessor in doc.get("accessors", []) for ref in accessor_view_refs(accessor)]
    kept = sorted({ref["bufferView"] for ref in refs})
    if image_views & set(kept):
        raise Refused("an accessor reads the same bufferView as an image")
    if "bufferView" in json.dumps({k: v for k, v in doc.items() if k not in ("accessors", "bufferViews")}):
        raise Refused("something other than an accessor names a bufferView")
    for extension in doc.get("extensionsUsed", []):
        if not extension.startswith("KHR_materials_"):
            raise Refused(f"unexpected extension: {extension}")

    # Rebuild the BIN chunk: each kept view's bytes, unchanged, on a 4-byte boundary.
    # An accessor's own byteOffset is relative to its view, so it stays valid only if
    # the view's start keeps its alignment; 4 covers every glTF component type.
    out, renumber, new_views = bytearray(), {}, []
    for old in kept:
        view = views[old]
        out += b"\0" * (-len(out) % ALIGN)
        start = view.get("byteOffset", 0)
        moved = dict(view)
        moved["byteOffset"] = len(out)
        out += bin_data[start:start + view["byteLength"]]
        renumber[old] = len(new_views)
        new_views.append(moved)
    out += b"\0" * (-len(out) % ALIGN)
    for ref in refs:
        ref["bufferView"] = renumber[ref["bufferView"]]
    doc["bufferViews"] = new_views
    buffers[0]["byteLength"] = len(out)

    # An extension stays declared only while a material still uses it.
    if "extensionsUsed" in doc:
        text = json.dumps(doc.get("materials", []))
        used = [name for name in doc["extensionsUsed"] if f'"{name}"' in text]
        if used:
            doc["extensionsUsed"] = used
        else:
            del doc["extensionsUsed"]
    return doc, bytes(out)


def main(argv):
    if len(argv) != 3:
        print(__doc__.strip().split("\n\n")[1].strip(), file=sys.stderr)
        return 2
    source, target = argv[1], argv[2]
    if os.path.exists(target) and os.path.samefile(source, target):
        print("strip_glb: refusing to write over the source", file=sys.stderr)
        return 2
    with open(source, "rb") as f:
        data = f.read()
    try:
        doc, bin_data = strip(*read_glb(data))
        result = write_glb(doc, bin_data)
    except (Refused, KeyError, TypeError, ValueError) as e:
        print(f"strip_glb: {source}: {e}", file=sys.stderr)
        return 1
    with open(target, "wb") as f:
        f.write(result)
    print(f"{source}: {len(data)} -> {len(result)} bytes, {len(doc['bufferViews'])} bufferViews kept")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
