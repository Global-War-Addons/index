"""Lenient XML tag scanner for the X-Ray UI / text xml.

The engine xml (XMLDocument) is not strict XML: files may have no single root (the engine wraps them), use
'#include "file.xml"' lines, unknown entities, a wrong encoding declaration. ElementTree fails on part of them, so the
tag structure is read with a regex: comments, CDATA, <? ?> and <! > are skipped, attributes are read from start tags,
unclosed tags are closed by the parent's end tag. Tag names keep their case (tinyxml is case-sensitive), attribute
names are lowercased. Text content is not needed here (the texts blob has it).
"""

import re

TAG = re.compile(r"<!--.*?-->|<!\[CDATA\[.*?\]\]>|<\?.*?\?>|<![^>]*>|<(/?)([A-Za-z_][\w:.\-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*?)(/?)>",
                 re.S)
ATTR = re.compile(r"([A-Za-z_][\w:.\-]*)\s*=\s*(\"([^\"]*)\"|'([^']*)')")
INCLUDE = re.compile(r'^[ \t]*#include[ \t]+"([^"]+)"', re.M)


def includes(text):
    return [m.group(1) for m in INCLUDE.finditer(text)]


def iter_elements(text):
    """Yields (path_tuple_of_(tag, attrs), line) for every start tag, path from the document top."""
    stack = []
    line = 1
    last = 0
    for m in TAG.finditer(text):
        if m.group(2) is None:
            continue
        line += text.count("\n", last, m.start())
        last = m.start()
        closing, tag, attrs_text, selfclose = m.group(1), m.group(2), m.group(3), m.group(4)
        if closing:
            # pop up to the matching tag (tolerates unclosed children)
            for k in range(len(stack) - 1, -1, -1):
                if stack[k][0] == tag:
                    del stack[k:]
                    break
            continue
        attrs = {}
        for a in ATTR.finditer(attrs_text):
            attrs[a.group(1).lower()] = a.group(3) if a.group(3) is not None else a.group(4)
        stack.append((tag, attrs))
        yield tuple(stack), line
        if selfclose:
            stack.pop()


def segment(tag, attrs):
    if "id" in attrs:
        return "%s[id=%s]" % (tag, attrs["id"])
    if "name" in attrs:
        return "%s[name=%s]" % (tag, attrs["name"])
    return tag


def ui_paths(text):
    """Unique tag paths 'w/main_wnd/background[id=x]/texture'; segments carry id= or name= when present."""
    paths = set()
    for stack, _ in iter_elements(text):
        paths.add("/".join(segment(t, a) for t, a in stack))
    return sorted(paths)


def _under_root(text):
    """iter_elements limited to the first top-level element: XMLDocument's root is m_Doc.FirstChildElement(), the
    engine readers below never look at a second top-level element."""
    roots = 0
    for stack, line in iter_elements(text):
        if len(stack) == 1:
            roots += 1
        if roots == 1:
            yield stack, line


def string_ids(text):
    """id of every <string id="..."> that is a child of the root (CStringTable::Load: GetNodesNum(GetRoot(),
    "string")), the string table of configs/text/<lang>/*.xml. A <string> deeper in the tree is not a string."""
    out = []
    for stack, line in _under_root(text):
        if len(stack) != 2:
            continue
        tag, attrs = stack[-1]
        if tag == "string" and "id" in attrs:
            out.append((attrs["id"], line))
    return out


def texture_ids(text):
    """(id, texture file) of every <texture id="..."> that is a child of a <file name="..."> child of the root
    (CUITextureMaster::ParseShTexInfo), configs/ui/textures_descr."""
    out = []
    for stack, line in _under_root(text):
        if len(stack) != 3:
            continue
        tag, attrs = stack[-1]
        ftag, fattrs = stack[1]
        if tag == "texture" and "id" in attrs and ftag == "file":
            out.append((attrs["id"], fattrs.get("name", ""), line))
    return out
