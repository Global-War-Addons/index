"""'check' (stub of S2, 12-addon-breakage-without-api.md section 6): references of an addon to what the snapshot lacks.

Checked now (the simplest cases):
    ltx    parents of sections '[a]:b,c', DLTX targets '![a]' / '!![a]' (and their parents), '#include' targets;
           known = all sections of all trees of the snapshot + sections the addon declares itself
    lua    'module.name' where module is a base script module (or one the snapshot of the author's version had):
           name must be a function / table / variable / class of the module, a function another base script injects
           into it, or something the addon defines itself (its own copy of the module or 'function module.name()')
    events RegisterScriptCallback("name") / UnregisterScriptCallback - name must be a declared event
    info   files of the addon that replace base files (A7), with "same as base" / "differs"

TODO (not checked yet): string ids and textures_descr ids used by the addon, XMLOverride paths, ltx values that
reference sections / models / textures / sounds, obj_on_job / smart names in logic, bare calls of _g.script globals,
get_console_cmd / execute cvars, shader includes, engine exports (needs a -dump_bindings snapshot).
"""

import json
import os

import ltxparse
import luascan
from builder import LUA_STD
from common import load_json_gz, vnorm
from differ import module_names
from textnorm import read_normalized, text_sha256


def addon_root(path):
    inner = os.path.join(path, "gamedata")
    return inner if os.path.isdir(inner) else path


def addon_files(root):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            full = os.path.join(dirpath, name)
            out[vnorm(os.path.relpath(full, root))] = full
    return out


def sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def replaced_atoms(args, snap, files, replaced):
    """The atoms a replaced text file changes against the base (gw_merge atoms, 15-merge-implementation.md 7.2):
    "atoms" of an item of info.replaced, when gw_merge and the texts of the snapshot (base_texts.tar.gz) are there"""
    import gwmerge
    todo = [it for it in replaced if not it["same_as_base"] and gwmerge.kind_of(it["file"])]
    if not todo:
        return
    exe = gwmerge.find_gw_merge(getattr(args, "gw_merge", None))
    tar = gwmerge.texts_tar_of(args.snapshot, snap)
    if not exe or not tar:
        return
    with gwmerge.TextsDir(tar, [it["file"] for it in todo]) as base:
        for it in todo:
            base_file = os.path.join(base.path, *it["file"].split("\\"))
            if os.path.isfile(base_file):
                it["atoms"] = gwmerge.atoms(exe, gwmerge.kind_of(it["file"]), base_file, files[it["file"]], it["file"])


def run(args):
    snap = load_json_gz(args.snapshot)
    old = load_json_gz(args.old_snapshot) if args.old_snapshot else None
    root = addon_root(os.path.abspath(args.addon))
    files = addon_files(root)
    texts = {}
    for vp, full in files.items():
        if vp.endswith((".ltx", ".script")):
            texts[vp], _ = read_normalized(full)

    problems = []  # {kind, file, line, ref, why}
    info = {"replaced": [], "unknown_globals": []}

    # -- replaced base files ------------------------------------------------------------------------------
    base_texts = (snap.get("texts") or {}).get("files") or {}
    for vp, full in sorted(files.items()):
        base = snap["files"].get(vp)
        if base:
            same = base[1] is not None and base[1] == sha256(full)
            if not same and vp in base_texts:
                # a text that differs only in CRLF/LF or BOM (git autocrlf, another editor) is the same text
                text, _ = read_normalized(full)
                same = text_sha256(text) == base_texts[vp].get("sha256")
            info["replaced"].append({"file": vp, "same_as_base": same})
    replaced_atoms(args, snap, files, info["replaced"])

    # -- ltx ----------------------------------------------------------------------------------------------
    known_sections = set()
    for tree in snap["ltx"]["trees"].values():
        known_sections.update(tree["sections"])
    headers = {}
    for vp in sorted(texts):
        if vp.endswith(".ltx"):
            headers[vp] = list(ltxparse.scan_headers(texts[vp]))
    own_sections = {h["name"] for hs in headers.values() for h in hs if h["kind"] in ("section", "safe")}
    all_sections = known_sections | own_sections
    for vp, hs in headers.items():
        for h in hs:
            if h["kind"] == "include":
                target = vnorm(vp.rsplit("\\", 1)[0] + "\\" + h["name"]) if "\\" in vp else vnorm(h["name"])
                if "*" in h["name"]:
                    continue
                if target not in snap["files"] and target not in files:
                    problems.append({"kind": "ltx_include", "file": vp, "line": h["line"], "ref": target,
                                     "why": "файла нет ни в базе, ни в аддоне"})
                continue
            if h["kind"] in ("override", "delete") and h["name"] not in all_sections:
                problems.append({"kind": "dltx_target", "file": vp, "line": h["line"], "ref": h["name"],
                                 "why": "DLTX %s: секции нет в базе" % ("![…]" if h["kind"] == "override" else "!![…]")})
            for parent in h["parents"]:
                if parent.startswith("!"):
                    continue  # DLTX: remove this parent
                if parent and parent not in all_sections:
                    problems.append({"kind": "ltx_parent", "file": vp, "line": h["line"], "ref": parent,
                                     "why": "родитель секции [%s] не найден" % h["name"]})

    # -- lua ----------------------------------------------------------------------------------------------
    base_mods = {n: m for n, m in snap["lua"]["modules"].items() if m["kind"] == "global"}
    injected = {m: set(v) for m, v in snap["lua"].get("injected", {}).items()}
    # What OTHER base scripts put into a module: still there when the addon replaces the module's own file
    injected_ext = {m: {k for k, it in v.items() if it.get("from") != m}
                    for m, v in snap["lua"].get("injected", {}).items()}
    old_mods = set(n for n, m in old["lua"]["modules"].items() if m["kind"] == "global") if old else set()
    external = set(snap["lua"].get("external_globals", ()))
    scans = {vp: luascan.scan(texts[vp]) for vp in sorted(texts) if vp.endswith(".script")}
    addon_mods = {}
    for vp, sc in scans.items():
        rel = vp[len("scripts\\"):-len(".script")] if vp.startswith("scripts\\") else None
        if rel and "\\" not in rel:
            addon_mods[rel] = sc
    # what the addon defines in other modules: function sim_smart.foo() / sim_smart.foo = ...
    addon_injected = {}
    for sc in scans.values():
        for field in sc["runtime_fields"]:
            head, rest = field.split(".", 1)
            addon_injected.setdefault(head, set()).add(rest.split(".", 1)[0])
        for fname in sc["functions"]:
            parts = fname.replace(":", ".").split(".")
            if len(parts) > 1:
                addon_injected.setdefault(parts[0], set()).add(parts[1])
    for vp, sc in scans.items():
        own_mod = vp[len("scripts\\"):-len(".script")] if vp.startswith("scripts\\") else ""
        own = set(sc["tables"]) | set(sc["vars"]) | set(sc["classes"]) | \
            {f for f in sc["functions"] if "." not in f and ":" not in f}
        seen = set()
        for a, b, line in sc["refs"]:
            if a in sc["locals"] or a in own or a in LUA_STD or (a, b) in seen:
                continue
            seen.add((a, b))
            if a == own_mod:
                continue  # the module refers to itself: its own definitions
            if a in addon_mods:
                am = addon_mods[a]
                names = set(am["tables"]) | set(am["vars"]) | set(am["classes"]) | set(am["functions"]) | \
                    set(am["runtime"])
                if b not in names and b not in addon_injected.get(a, ()) and b not in injected_ext.get(a, ()):
                    problems.append({"kind": "lua_name", "file": vp, "line": line, "ref": "%s.%s" % (a, b),
                                     "why": "нет в модуле аддона `%s`" % a})
                continue
            if a in base_mods:
                if b in module_names(base_mods[a]) or b in injected.get(a, ()) or b in addon_injected.get(a, ()):
                    continue
                problems.append({"kind": "lua_name", "file": vp, "line": line, "ref": "%s.%s" % (a, b),
                                 "why": "в модуле базы `%s` нет `%s`" % (a, b)})
                continue
            if a in old_mods:
                problems.append({"kind": "lua_module", "file": vp, "line": line, "ref": "%s.%s" % (a, b),
                                 "why": "модуль `%s` был в версии автора и удалён" % a})
                continue
            if a not in external:
                info["unknown_globals"].append({"file": vp, "line": line, "ref": "%s.%s" % (a, b)})

    # -- events -------------------------------------------------------------------------------------------
    ev = snap.get("events") or {}
    declared = set((ev.get("meta") or {}).get("events") or {}) | set(ev.get("script") or {}) | \
        set(ev.get("builtin") or {}) | set(ev.get("used_by_base") or ())
    if declared:
        for vp in sorted(texts):
            if vp.endswith(".script"):
                for name, line in luascan.callbacks(texts[vp]):
                    if name not in declared:
                        problems.append({"kind": "event", "file": vp, "line": line, "ref": name,
                                         "why": "событие не объявлено в базе"})

    result = {"game": snap["game"]["tag"], "addon": os.path.abspath(args.addon), "broken": len(problems),
              "problems": problems, "info": info,
              "todo": ["string ids", "textures_descr ids", "XMLOverride paths", "ltx value references",
                       "smarts/jobs in logic", "_g.script globals", "cvars", "shader includes", "engine exports"]}
    if args.format == "json":
        text = json.dumps(result, ensure_ascii=False, indent=1)
    else:
        text = render(result)
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    else:
        print(text, end="")
    return 1 if problems else 0


def render(r):
    out = ["# Проверка аддона по снимку `%s`" % r["game"], "", "Аддон: `%s`" % r["addon"], "",
           "**Битых ссылок: %d**" % r["broken"], ""]
    if r["problems"]:
        out += ["| Где | Ссылка | Почему |", "|---|---|---|"]
        for p in r["problems"]:
            out.append("| `%s:%d` | `%s` | %s |" % (p["file"], p["line"], p["ref"], p["why"]))
        out.append("")
    rep = r["info"]["replaced"]
    if rep:
        out += ["## Заменяет файлы базы (%d)" % len(rep), ""]
        for it in rep:
            out.append("- `%s` — %s" % (it["file"], "совпадает с базой" if it["same_as_base"] else "отличается"))
            atoms = it.get("atoms")
            if atoms:
                # gw_merge atoms: what the merge of the engine sees as the changes of the addon
                ops = {"add": "+", "modify": "~", "delete": "-"}
                shown = ["%s`%s`" % (ops.get(a["op"], "?"), a["atom"]) for a in atoms if not a.get("container")]
                out.append("  - атомы (%d): %s%s" % (len(shown), ", ".join(shown[:30]),
                                                   " …" if len(shown) > 30 else ""))
        out.append("")
    unk = r["info"]["unknown_globals"]
    if unk:
        out += ["## Неизвестные глобальные имена (`X.y`, не модуль базы и не встречаются в базе)", "",
                "Не ошибка: это может быть экспорт движка, глобал из `_g.script` или модуль, удалённый раньше. "
                "Передайте `--old-snapshot`, чтобы отличить удалённые модули.", ""]
        for it in unk[:200]:
            out.append("- `%s:%d` `%s`" % (it["file"], it["line"], it["ref"]))
        if len(unk) > 200:
            out.append("- … и ещё %d" % (len(unk) - 200))
        out.append("")
    out += ["## Не проверяется (TODO)", ""] + ["- " + t for t in r["todo"]]
    return "\n".join(out) + "\n"
