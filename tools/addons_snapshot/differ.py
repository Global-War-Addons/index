"""'diff': two snapshots -> markdown "Изменения для аддонов" (what an addon of the old version may lose).

Reports removals and changes that break references (12-addon-breakage-without-api.md section 4.2, "В патч-нотах"):
files, ltx sections / parents / effective keys, strings, textures_descr ids, UI xml paths, Lua modules / functions /
parameter counts / tables, events / schemas, cvars, engine binding lines. Additions are only counted.
"""

import json

from common import load_json_gz
from ltxparse import effective_keys


def _all_sections(snap):
    """{section: (tree root, section data, tree)} over all trees; engine roots win over standalone files."""
    out = {}
    trees = snap["ltx"]["trees"]
    order = list(snap["ltx"]["engine_roots"]) + [r for r in trees if r not in snap["ltx"]["engine_roots"]]
    for root in order:
        tree = trees.get(root)
        if not tree:
            continue
        for name, sec in tree["sections"].items():
            out.setdefault(name, (root, sec, tree))
    return out


def module_names(mod):
    names = set(mod.get("tables", ())) | set(mod.get("vars", ())) | set(mod.get("classes", ()))
    names |= set(mod.get("functions", {})) | set(mod.get("runtime", ()))
    return names


def compute(old, new):
    r = {}
    of, nf = old["files"], new["files"]
    r["files_removed"] = sorted(set(of) - set(nf))
    r["files_added"] = len(set(nf) - set(of))
    r["files_changed"] = sorted(p for p in set(of) & set(nf)
                                if of[p][1] and nf[p][1] and of[p][1] != nf[p][1] or of[p][0] != nf[p][0])

    # ltx: per tree, engine roots and standalone files
    ltx = {"sections_removed": [], "parents_changed": [], "keys_removed": [], "moved": [], "trees_removed": [],
           "sections_added": 0}
    ot, nt = old["ltx"]["trees"], new["ltx"]["trees"]
    new_all = None
    for root in sorted(ot):
        if root not in nt:
            # A standalone root also disappears when another file of configs\ starts to include it: the file and
            # its sections are still there, in another tree. Only a deleted root file is a removed tree; the
            # sections are looked up in all trees of the new snapshot.
            if root not in nf:
                ltx["trees_removed"].append(root)
            if new_all is None:
                new_all = _all_sections(new)
            ofiles = ot[root]["files"]
            for name in sorted(ot[root]["sections"]):
                if name not in new_all:
                    ltx["sections_removed"].append((root, name, ofiles[ot[root]["sections"][name]["file"]]))
            continue
        os_, ns_ = ot[root]["sections"], nt[root]["sections"]
        ofiles, nfiles = ot[root]["files"], nt[root]["files"]
        ocache, ncache = {}, {}
        for name in sorted(os_):
            o = os_[name]
            n = ns_.get(name)
            if n is None:
                ltx["sections_removed"].append((root, name, ofiles[o["file"]]))
                continue
            if o.get("parents", []) != n.get("parents", []):
                ltx["parents_changed"].append((root, name, o.get("parents", []), n.get("parents", [])))
            lost = effective_keys(os_, name, ocache) - effective_keys(ns_, name, ncache)
            if lost:
                ltx["keys_removed"].append((root, name, sorted(lost)))
            if ofiles[o["file"]] != nfiles[n["file"]]:
                ltx["moved"].append((root, name, ofiles[o["file"]], nfiles[n["file"]]))
        ltx["sections_added"] += len(set(ns_) - set(os_))
    r["ltx"] = ltx

    strings = {}
    for lang, ids in old["strings"].items():
        gone = sorted(set(ids) - set(new["strings"].get(lang, {})))
        if gone:
            strings[lang] = gone
    r["strings_removed"] = strings

    r["textures_removed"] = sorted(set(old["textures_descr"]) - set(new["textures_descr"]))

    xml = {}
    for f, item in old["xml"].items():
        if f not in new["xml"]:
            continue  # the file itself is in files_removed
        gone = sorted(set(item["paths"]) - set(new["xml"][f]["paths"]))
        if gone:
            xml[f] = gone
    r["xml_paths_removed"] = xml

    om, nm = old["lua"]["modules"], new["lua"]["modules"]
    lua = {"modules_removed": sorted(set(om) - set(nm)), "names_removed": [], "signature_changed": []}
    for mod in sorted(set(om) & set(nm)):
        o, n = om[mod], nm[mod]
        ninj = set(new["lua"].get("injected", {}).get(mod, {}))
        gone = sorted(module_names(o) - module_names(n) - ninj)
        if gone:
            lua["names_removed"].append((mod, gone))
        for fname, f in sorted(o["functions"].items()):
            g = n["functions"].get(fname)
            if g and (g["params"] != f["params"] or bool(g.get("vararg")) != bool(f.get("vararg"))):
                lua["signature_changed"].append((mod, fname, f, g))
    r["lua"] = lua

    def events_of(snap, sources):
        """{name: schema or None} from the given sources only."""
        ev = snap.get("events") or {}
        out = {}
        if "meta" in sources:
            for name in ((ev.get("meta") or {}).get("events") or {}):
                out[name] = None
        for src in ("builtin", "script"):
            if src in sources:
                for name, schema in (ev.get(src) or {}).items():
                    out[name] = schema
        return out

    def has_source(snap, src):
        ev = snap.get("events") or {}
        if src == "meta":
            return bool((ev.get("meta") or {}).get("events"))
        return ev.get(src) is not None

    # Only the sources both snapshots have are compared: a snapshot built without events_meta.json or without
    # --engine-src would otherwise "lose" every event of that source.
    sources = [s for s in ("meta", "builtin", "script") if has_source(old, s) and has_source(new, s)]
    oe, ne = events_of(old, sources), events_of(new, sources)
    r["events_removed"] = sorted(set(oe) - set(ne))
    r["events_schema_changed"] = sorted((e, oe[e], ne[e]) for e in set(oe) & set(ne)
                                        if oe[e] is not None and ne[e] is not None and oe[e] != ne[e])
    r["events_compared"] = bool(sources)
    r["events_sources"] = sources
    r["events_sources_skipped"] = [s for s in ("meta", "builtin", "script")
                                   if (has_source(old, s) or has_source(new, s)) and s not in sources]

    oc, nc = old.get("console"), new.get("console")
    r["console_compared"] = oc is not None and nc is not None
    r["console_removed"] = sorted(set(oc) - set(nc)) if r["console_compared"] else []

    ob, nb = old.get("engine_bindings"), new.get("engine_bindings")
    r["bindings_compared"] = ob is not None and nb is not None
    r["bindings_removed"] = sorted(set(ob["lines"]) - set(nb["lines"]) - {""}) if r["bindings_compared"] else []
    return r


def _cap(items, limit):
    if limit and len(items) > limit:
        return items[:limit], len(items) - limit
    return items, 0


def render(old, new, r, limit):
    out = []
    w = out.append
    w("# Изменения для аддонов: %s → %s" % (old["game"]["tag"], new["game"]["tag"]))
    w("")
    w("Что было в базе `%s` и пропало или изменилось в `%s`. Аддон, который ссылается на это, может перестать "
      "работать (часто молча). Сгенерировано `tools/addons_snapshot/gw_snapshot.py diff`." % (
          old["game"]["tag"], new["game"]["tag"]))
    w("")

    ltx = r["ltx"]
    lua = r["lua"]
    w("## Сводка")
    w("")
    w("| Что | Удалено / изменено |")
    w("|---|---|")
    w("| Файлы | удалено %d, изменено %d, добавлено %d |" % (len(r["files_removed"]), len(r["files_changed"]),
                                                            r["files_added"]))
    w("| Секции ltx | удалено %d, сменили родителей %d, потеряли ключи %d, перенесены в другой файл %d "
      "(добавлено %d) |" % (len(ltx["sections_removed"]), len(ltx["parents_changed"]), len(ltx["keys_removed"]),
                            len(ltx["moved"]), ltx["sections_added"]))
    w("| Строки | удалено %s |" % (", ".join("%s: %d" % (k, len(v)) for k, v in sorted(r["strings_removed"].items()))
                                   or "0"))
    w("| Текстуры UI (`textures_descr`) | удалено %d |" % len(r["textures_removed"]))
    w("| Пути UI xml | удалены в %d файлах |" % len(r["xml_paths_removed"]))
    w("| Lua | модулей удалено %d, имён удалено %d, сигнатур изменено %d |" % (
        len(lua["modules_removed"]), sum(len(g) for _, g in lua["names_removed"]), len(lua["signature_changed"])))
    skipped = r.get("events_sources_skipped") or []
    w("| События | %s%s |" % ("удалено %d, сменили схему %d" % (len(r["events_removed"]),
                                                                len(r["events_schema_changed"]))
                             if r["events_compared"] else "нет данных в одном из снимков",
                             "; не сравнивались (есть только в одном снимке): %s" % ", ".join(skipped)
                             if skipped and r["events_compared"] else ""))
    w("| cvar | %s |" % ("удалено %d" % len(r["console_removed"]) if r["console_compared"] else "нет дампа консоли"))
    w("| Экспорты движка | %s |" % ("удалено строк %d" % len(r["bindings_removed"]) if r["bindings_compared"]
                                    else "нет дампа `-dump_bindings`"))
    w("")

    def section(title, items, fmt):
        if not items:
            return
        items, more = _cap(items, limit)
        w("## " + title)
        w("")
        for it in items:
            w("- " + fmt(it))
        if more:
            w("- … и ещё %d" % more)
        w("")

    section("Удалённые Lua-модули", lua["modules_removed"], lambda m: "`%s`" % m)
    section("Удалённые функции и таблицы Lua-модулей", lua["names_removed"],
            lambda it: "`%s`: %s" % (it[0], ", ".join("`%s`" % g for g in it[1])))
    section("Изменилось число параметров", lua["signature_changed"],
            lambda it: "`%s.%s`: %d%s → %d%s" % (it[0], it[1], it[2]["params"], "+…" if it[2].get("vararg") else "",
                                                  it[3]["params"], "+…" if it[3].get("vararg") else ""))
    section("Удалённые секции ltx", ltx["sections_removed"], lambda it: "`[%s]` (`%s`)" % (it[1], it[2]))
    section("Секции сменили родителей", ltx["parents_changed"],
            lambda it: "`[%s]`: `%s` → `%s`" % (it[1], ", ".join(it[2]) or "—", ", ".join(it[3]) or "—"))
    section("Секции потеряли ключи (с учётом наследования)", ltx["keys_removed"],
            lambda it: "`[%s]`: %s" % (it[1], ", ".join("`%s`" % k for k in it[2])))
    section("Секции перенесены в другой файл (замена старого файла не подействует)", ltx["moved"],
            lambda it: "`[%s]`: `%s` → `%s`" % (it[1], it[2], it[3]))
    section("Удалённые деревья ltx (корневые файлы)", ltx["trees_removed"], lambda f: "`%s`" % f)
    for lang, ids in sorted(r["strings_removed"].items()):
        section("Удалённые строки (%s)" % lang, ids, lambda s: "`%s`" % s)
    section("Удалённые id текстур UI", r["textures_removed"], lambda s: "`%s`" % s)
    section("Удалённые пути в UI xml (XMLOverride по ним ничего не найдёт)", sorted(r["xml_paths_removed"].items()),
            lambda it: "`%s`: %s" % (it[0], ", ".join("`%s`" % p for p in _cap(it[1], 20)[0])
                                     + (" … ещё %d" % (len(it[1]) - 20) if len(it[1]) > 20 else "")))
    section("Удалённые события", r["events_removed"], lambda e: "`%s`" % e)
    section("События сменили схему аргументов", r["events_schema_changed"],
            lambda it: "`%s`: `%s` → `%s`" % it)
    section("Удалённые cvar", r["console_removed"], lambda c: "`%s`" % c)
    section("Удалённые строки дампа экспортов движка", r["bindings_removed"], lambda c: "`%s`" % c)
    section("Удалённые файлы", r["files_removed"], lambda f: "`%s`" % f)
    return "\n".join(out) + "\n"


def run(args):
    old, new = load_json_gz(args.old), load_json_gz(args.new)
    r = compute(old, new)
    if args.format == "json":
        text = json.dumps(r, ensure_ascii=False, indent=1, default=list)
    else:
        text = render(old, new, r, args.limit)
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    else:
        print(text, end="")
    return 0
