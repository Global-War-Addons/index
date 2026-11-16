"""The references of an OGF model to other resources, and the rewrite of its texture names (09-content-addons-vfs.md
4.1 B, 14-addon-resource-scoping.md 5).

An OGF is a list of chunks: u32 id (bit 31 - the chunk is compressed), u32 size, the data (xrCore/FS.cpp,
xrCore/FMesh.hpp). What the engine reads by name:
    OGF_TEXTURE (2)         "<textures, comma list>\\0<shader>\\0" - relative to textures\\ without .dds
                            (Layers/xrRender/FBasicVisual.cpp, dxRender_Visual::Load)
    OGF_CHILDREN (9)        the children: chunks 0, 1, ... each a whole OGF (FHierrarhyVisual.cpp)
    OGF_S_LODS (23)         the name of the LOD model, a line (SkeletonCustom.cpp)
    OGF_S_MOTION_REFS (19)  "<refs, comma list>\\0"; OGF_S_MOTION_REFS2 (24): u32 count + count names \\0
                            (SkeletonAnimated.cpp): meshes\\<ref>.omf, "<folder>\\*.omf" - a set
Strings are bytes in the code page of the author: kept as bytes (latin-1 round trip).
"""

import struct

OGF_TEXTURE = 2
OGF_CHILDREN = 9
OGF_S_MOTION_REFS = 19
OGF_S_LODS = 23
OGF_S_MOTION_REFS2 = 24
COMPRESSED = 0x80000000
TEXTURE_LIST_MAX = 255  # string256 fnT of dxRender_Visual::Load, the terminating zero aside
MAX_DEPTH = 16          # children inside children: a broken file must not recurse forever
MAX_CHUNKS = 1 << 16    # all the chunks of one model (real ones have hundreds): a file of empty chunks must not cost
                        # a tuple per 8 bytes (memory, time) - the packages the CI checks are untrusted


class OgfError(ValueError):
    pass


def chunks(data, start=0, end=None, clamp=True, budget=None):
    """[(id without the compression bit, compressed, payload start, payload end)] of data[start:end], and where the
    chunks end (the rest is a tail). As IReader::find_chunk the reading stops at a cut header; a chunk longer than
    what is left is read up to the end (clamp) or left in the tail (not clamp: a rewrite keeps such bytes as they are).
    Real models have such tails (junk after the last chunk). budget - [chunks left], shared by the calls for one
    model: OgfError when it runs out."""
    end = len(data) if end is None else end
    out = []
    pos = start
    while end - pos >= 8:
        if budget is not None:
            budget[0] -= 1
            if budget[0] < 0:
                raise OgfError("more than %d chunks: not a model" % MAX_CHUNKS)
        cid, size = struct.unpack_from("<II", data, pos)
        if size > end - pos - 8:
            if clamp:
                out.append((cid & ~COMPRESSED, bool(cid & COMPRESSED), pos + 8, end))
                pos = end
            break
        out.append((cid & ~COMPRESSED, bool(cid & COMPRESSED), pos + 8, pos + 8 + size))
        pos += 8 + size
    return out, pos


def _z(data, start, end):
    """A zero-terminated string at data[start:end] -> (str, the position after its zero)"""
    zero = data.find(b"\0", start, end)
    if zero < 0:
        zero = end
    return data[start:zero].decode("latin-1"), min(zero + 1, end)


def split_list(value):
    """_GetItem of the engine: the items of a comma list, spaces around them cut (_Trim cuts ' ' only)"""
    return [item.strip(" ") for item in value.split(",")] if value.strip(" ") else []


def read_refs(data):
    """-> {"textures": [names], "shaders": [names], "motions": [refs], "lods": [names]} of the model and its children.
    Compressed chunks of these ids are not read (OgfError)."""
    out = {"textures": [], "shaders": [], "motions": [], "lods": []}
    budget = [MAX_CHUNKS]

    def walk(start, end, depth):
        if depth > MAX_DEPTH:
            raise OgfError("children nested deeper than %d" % MAX_DEPTH)
        for cid, packed, a, b in chunks(data, start, end, budget=budget)[0]:
            if cid in (OGF_TEXTURE, OGF_CHILDREN, OGF_S_MOTION_REFS, OGF_S_MOTION_REFS2, OGF_S_LODS) and packed:
                raise OgfError("chunk %d is compressed: not supported" % cid)
            if cid == OGF_TEXTURE:
                textures, pos = _z(data, a, b)
                shader, _ = _z(data, pos, b)
                out["textures"].extend(split_list(textures))
                if shader:
                    out["shaders"].append(shader)
            elif cid == OGF_CHILDREN:
                for _, child_packed, ca, cb in chunks(data, a, b, budget=budget)[0]:
                    if child_packed:
                        raise OgfError("a compressed child: not supported")
                    walk(ca, cb, depth + 1)
            elif cid == OGF_S_MOTION_REFS:
                refs, _ = _z(data, a, b)
                out["motions"].extend(split_list(refs))
            elif cid == OGF_S_MOTION_REFS2:
                if b - a < 4:
                    raise OgfError("a cut OGF_S_MOTION_REFS2")
                count = struct.unpack_from("<I", data, a)[0]
                pos = a + 4
                for _ in range(count):
                    if pos >= b:
                        raise OgfError("OGF_S_MOTION_REFS2: %d names announced, the chunk ends" % count)
                    name, pos = _z(data, pos, b)
                    out["motions"].append(name)
            elif cid == OGF_S_LODS:
                # IReader::r_string: to the end of the line (or of the chunk)
                line = data[a:b].split(b"\n", 1)[0].split(b"\0", 1)[0].rstrip(b"\r")
                if line:
                    out["lods"].append(line.decode("latin-1").strip())
    walk(0, len(data), 0)
    return out


def rewrite_textures(data, rename):
    """rename(name) -> new name or None, for every item of every OGF_TEXTURE of the model and its children.
    -> (new bytes, [(old, new)] changed, [problems]). The chunks around are copied as they are; the sizes of
    OGF_CHILDREN and its children are recomputed. A list longer than the engine reads (255 bytes) is left as is
    (a problem)."""
    changed, problems = [], []
    budget = [MAX_CHUNKS]

    def build(start, end, depth):
        if depth > MAX_DEPTH:
            raise OgfError("children nested deeper than %d" % MAX_DEPTH)
        parts = []
        found, tail = chunks(data, start, end, clamp=False, budget=budget)
        for cid, packed, a, b in found:
            raw_id = cid | (COMPRESSED if packed else 0)
            payload = data[a:b]
            if cid == OGF_TEXTURE and not packed:
                textures, pos = _z(data, a, b)
                items = split_list(textures)
                new_items = []
                pairs = []
                for item in items:
                    new = rename(item)
                    if new and new != item:
                        pairs.append((item, new))
                        new_items.append(new)
                    else:
                        new_items.append(item)
                if pairs:
                    new_list = ",".join(new_items).encode("latin-1")
                    if len(new_list) > TEXTURE_LIST_MAX:
                        problems.append("the texture list '%s' would be %d bytes, the engine reads %d: left as is" %
                                        (textures, len(new_list), TEXTURE_LIST_MAX))
                    else:
                        payload = new_list + b"\0" + data[pos:b]
                        changed.extend(pairs)
            elif cid == OGF_CHILDREN and not packed:
                kids = []
                kid_items, kid_tail = chunks(data, a, b, clamp=False, budget=budget)
                for kid, kid_packed, ka, kb in kid_items:
                    body = data[ka:kb] if kid_packed else build(ka, kb, depth + 1)
                    kids.append(struct.pack("<II", kid | (COMPRESSED if kid_packed else 0), len(body)) + body)
                payload = b"".join(kids) + data[kid_tail:b]
            elif cid in (OGF_TEXTURE, OGF_CHILDREN):
                problems.append("chunk %d is compressed: its textures are not rewritten" % cid)
            parts.append(struct.pack("<II", raw_id, len(payload)) + payload)
        return b"".join(parts) + data[tail:end]

    out = build(0, len(data), 0)
    return (out if changed else data), changed, problems
