"""gw_addon_tool remap-textures (09-content-addons-vfs.md 4.1 B, C7; 14-addon-resource-scoping.md 5).

The textures of an addon whose names the base has too ("textures\\wpn\\wpn_ak74.dds" for a new model) recolor the base
models of these names. The engine gives an addon its own name space - the alias root textures\\@<id>\\ - so the fix
needs no move of files (a folder "@..." inside the addon is not mounted at all):
    1. the texture is listed in addon.ltx [private]: the base keeps its own file, the addon's one is seen only as
       textures\\@<id>\\<name> (and by the models of the addon: the engine looks under @<id>\\ first, 14 section 3.2);
    2. the models of the addon name it explicitly: OGF_TEXTURE "wpn\\wpn_ak74" -> "@<id>\\wpn\\wpn_ak74" (optional - the
       engine does the same at load; explicit names survive a copy of the model into another addon).
The .thm of the texture follows it into [private]; bump and detail names of a .thm under @<id>\\ are resolved by the
engine (ApplyTHM), the .thm is not rewritten. Configs, UI xml and scripts look a name up globally: a target they name
stays public (a warning lists them) - rewrite those references to @<id>\\<name> by hand, then run again.
"""

import os
import re
import tempfile

from . import limits, ltx, manifest as manifest_mod, ogf, package, resources
from .report import Report

TEXT_EXT = (".ltx", ".xml", ".script", ".lua", ".txt", ".json")


def _texture_keys(layers):
    """{texture key (lower case, '\\', no .dds): [vpaths of its files: .dds and .thm]} of every layer of the addon"""
    keys, thm = {}, set()
    for lf in layers:
        for vpath in list(lf["public"]) + list(lf["private"]):
            if not vpath.startswith("textures\\"):
                continue
            if vpath.endswith(".dds"):
                keys.setdefault(vpath[len("textures\\"):-4], set()).add(vpath)
            elif vpath.endswith(".thm"):
                thm.add(vpath)
    for key, paths in keys.items():
        if "textures\\%s.thm" % key in thm:
            paths.add("textures\\%s.thm" % key)
    return {k: sorted(v) for k, v in keys.items()}


def _name_re(key):
    """A texture name in a text: its parts through '\\' or '/', not a part of a longer name"""
    body = r"[\\/]+".join(re.escape(p) for p in key.split("\\"))
    return re.compile(r"(?<![a-z0-9_.@\\/])" + body + r"(?![a-z0-9_#\\/])")


def _text_refs(root, layers, keys):
    """{key: [package paths of the text files that name it]}"""
    out = {}
    if not keys:
        return out
    tails = {}
    for key in keys:
        tails.setdefault(key.rsplit("\\", 1)[-1], []).append((key, _name_re(key)))  # compiled once: re caches 512
    for lf in layers:
        for vpath, pkg in sorted(list(lf["public"].items()) + list(lf["private"].items())):
            if not vpath.endswith(TEXT_EXT):
                continue
            with open(os.path.join(root, *pkg.split("/")), "rb") as f:
                text = f.read().decode("utf-8", "replace").lower()
            for tail, group in tails.items():
                if tail not in text:
                    continue
                for key, name_re in group:
                    if name_re.search(text):
                        out.setdefault(key, []).append(pkg)
    return out


def add_private_masks(path, masks, comment=None):
    """Appends masks to [private] of addon.ltx (a new section at the end when there is none), keeping the encoding
    (UTF-8, a BOM if it had one) and the line breaks. -> the new bytes (not written)"""
    with open(path, "rb") as f:
        data = f.read()
    bom = data.startswith(b"\xef\xbb\xbf")
    text = data[3:].decode("utf-8") if bom else data.decode("utf-8")
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    header_re = re.compile(r"^\s*\[\s*([^\]]*?)\s*\]")
    sections = [(i, m.group(1).lower()) for i, m in ((i, header_re.match(l)) for i, l in enumerate(lines)) if m]
    own = [i for i, name in sections if name == "private"]
    block = ([("; " + comment)] if comment else []) + list(masks)
    if own:
        start = own[-1]
        nxt = [i for i, _ in sections if i > start]
        at = nxt[0] if nxt else len(lines)
        while at - 1 > start and not lines[at - 1].strip():
            at -= 1
        lines[at:at] = block
    else:
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ["", "[private]"] + block + [""]
    out = nl.join(lines)
    if not out.endswith(nl):
        out += nl
    return (b"\xef\xbb\xbf" if bom else b"") + out.encode("utf-8")


def _write_atomic(path, data):
    fd, tmp = tempfile.mkstemp(prefix=".gw_remap_", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def remap_textures(folder, base_files=None, all_textures=False, only=None, make_private=True, rewrite_models=True,
                   dry_run=False, report=None):
    """-> Report. base_files - the snapshot of the base ({vpath: ...}); needed unless all_textures."""
    report = report or Report("remap-textures " + folder)
    files = package.folder_files(folder, report)
    path = os.path.join(folder, "addon.ltx")
    if not os.path.isfile(path):
        report.error("repo_no_manifest", "no addon.ltx in the folder", "addon.ltx")
        return report
    with open(path, "rb") as f:
        m = manifest_mod.check_manifest(f.read(), Report("addon.ltx"), "addon.ltx")
    if not m or not m.get("id"):
        report.error("manifest_syntax", "addon.ltx is not usable: run validate first", "addon.ltx")
        return report
    own = m["id"]
    if not all_textures and base_files is None:
        report.error("remap_no_snapshot", "the textures that collide with the base need the snapshot of the base: "
                                          "--snapshots DIR [--game TAG], --index (snapshot_url) or --base-snapshot "
                                          "FILE; or --all for every texture of the addon")
        return report
    layers, masks, _ = resources.scan_layers(files, m)
    keys = _texture_keys(layers)
    if all_textures:
        targets = sorted(keys)
    else:
        targets = sorted(k for k in keys if "textures\\%s.dds" % k in base_files)
    if only:
        wanted = [resources.normalize_mask(x) for x in only if resources.normalize_mask(x)]
        targets = [k for k in targets if any(resources.pattern_match("textures\\%s.dds" % k, w) or
                                             resources.pattern_match(k, w) for w in wanted)]
    if not targets:
        report.note("remap_nothing", "no texture of the addon %s" % ("matches" if only else
                                                                     "has a name the base has" if not all_textures
                                                                     else "is found"))
        return report

    # 1. [private]
    text_refs = _text_refs(root=folder, layers=layers, keys=targets)
    public_kept = sorted(k for k in targets if k in text_refs)
    for key in public_kept:
        report.warning("remap_text_reference", "textures\\%s: named by %s - configs, UI and scripts look it up "
                                               "globally and would get the base texture; it stays public. Rewrite "
                                               "these references to @%s\\%s and run again" %
                       (key, ", ".join(text_refs[key][:5]), own, key), text_refs[key][0])
    new_masks = []
    old_set = resources.MaskSet(masks)
    if make_private:
        seen = set()
        for key in targets:
            if key in text_refs:
                continue
            for vpath in keys[key]:
                if not old_set.match(vpath) and vpath not in seen:
                    seen.add(vpath)
                    new_masks.append(vpath)
    new_set = resources.MaskSet(masks + new_masks)
    private_now = set(k for k in targets if all(new_set.match(v) for v in keys[k]))
    rename_set = set(targets) if not make_private else private_now

    # The edit of addon.ltx is checked before anything is written: the models must not name @<id>\ textures that
    # stay public because addon.ltx was left as is
    ltx_data = None
    if new_masks:
        report.note("remap_private", "[private] += %d mask(s): %s" % (len(new_masks), ", ".join(new_masks[:20]) +
                                                                     (" (+%d more)" % (len(new_masks) - 20)
                                                                      if len(new_masks) > 20 else "")), "addon.ltx")
        ltx_data = add_private_masks(path, new_masks, "gw_addon_tool remap-textures: seen only as textures\\@%s\\..."
                                     % own)
        try:
            parsed = ltx.parse(ltx.decode(ltx_data, "addon.ltx")).keys("private")
            broken = None if parsed[-len(new_masks):] == new_masks else "the masks are not the last keys of [private]"
        except ltx.LtxError as e:
            broken = str(e)
        if not broken and len(ltx_data) > limits.MANIFEST_MAX_BYTES:
            broken = "%d bytes, the catalog takes at most %d: list folder masks (textures\\<folder>\\*) by hand, or " \
                     "narrow the run with --only" % (len(ltx_data), limits.MANIFEST_MAX_BYTES)
        if broken:
            report.error("remap_manifest_broken", "addon.ltx would not be usable after the edit, nothing is written: "
                                                  "%s" % broken, "addon.ltx")
            return report

    # 2. the models
    changed_models = 0
    if rewrite_models and rename_set:
        def rename(item):
            if not item or item.startswith(("@", "$")):
                return None
            # "\wpn\x" and "wpn\x" are one texture (_texture_key): "@<id>\" + the name without a leading separator
            return "@%s\\%s" % (own, item.lstrip("\\/")) if resources._texture_key(item) in rename_set else None
        for lf in layers:
            for vpath, pkg in sorted(list(lf["public"].items()) + list(lf["private"].items())):
                if not (vpath.startswith("meshes\\") and vpath.endswith(".ogf")):
                    continue
                full = os.path.join(folder, *pkg.split("/"))
                with open(full, "rb") as f:
                    data = f.read()
                try:
                    new, pairs, problems = ogf.rewrite_textures(data, rename)
                except ogf.OgfError as e:
                    report.warning("model_unreadable", "the model is not read: %s" % e, pkg)
                    continue
                for p in problems:
                    report.warning("remap_model_problem", p, pkg)
                if pairs:
                    changed_models += 1
                    report.note("remap_model", "%d texture name(s): %s" % (len(pairs), ", ".join(
                        "%s -> %s" % (a, b) for a, b in sorted(set(pairs))[:10])), pkg)
                    if not dry_run:
                        _write_atomic(full, new)

    # 3. addon.ltx
    if ltx_data is not None and not dry_run:
        _write_atomic(path, ltx_data)
    report.note("remap_summary", "%d target texture(s): %d private, %d kept public; %d model(s) rewritten%s" %
                (len(targets), len([k for k in targets if k in private_now]) if make_private else 0,
                 len(public_kept), changed_models, " (dry run: nothing written)" if dry_run else ""))
    report.data["remap"] = {"targets": targets, "private_masks": new_masks, "public_kept": public_kept,
                            "models": changed_models}
    return report
