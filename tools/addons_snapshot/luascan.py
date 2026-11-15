"""Light lexical scan of Lua 5.1 / LuaJIT scripts (no full parser).

Gives, per file:
    functions  top-level definitions:  function a(...)   function a.b(...)   function a:b(...)
               a = function(...)        a.b = function(...)
               -> name ("a", "a.b", "a:b"), number of declared parameters, vararg flag, line; a name defined
               twice keeps the LAST definition (the one in effect after the file has run)
    tables     top-level  a = { ... }
    vars       other top-level  a = <expr>  (not local)
    classes    luabind  class "name" (base)
    locals     every name declared local, as a parameter or a for variable anywhere in the file
    refs       a.b  pairs (a is not preceded by '.' or ':'), with line - used by 'check'
    requires   require("x") with a string literal
    runtime    names assigned without 'local' inside functions (globals of the module created at runtime)
    runtime_fields  'a.b = <value>' at top level or inside functions (fields set into a table / another module)

"Top level" = outside any function/do/if/repeat block and outside (), {}, []. Block depth is counted by keywords:
function, do, if, repeat open; end, until close (while/for open through their 'do').

Limits (documented in README): no scopes; 'a, b = 1, 2' is not recorded; an assignment inside a function that sits
in a table constructor ({ f = function() x = 1 end }) is not seen; setfenv/rawset/_G[...] and names built at
runtime are not seen; locals are per file, not per scope, so 'check' may miss a reference that a local of the same
name shadows elsewhere in the file.
"""

import re

KEYWORDS = {
    "and", "break", "do", "else", "elseif", "end", "false", "for", "function", "goto", "if", "in", "local", "nil",
    "not", "or", "repeat", "return", "then", "true", "until", "while",
}

TOKEN = re.compile(r"""
 (?P<ws>[ \t\r\f\v]+)
|(?P<nl>\n)
|(?P<lcomment>--\[(?P<lceq>=*)\[)
|(?P<comment>--[^\n]*)
|(?P<lstr>\[(?P<lseq>=*)\[)
|(?P<str>"(?:[^"\\\n]|\\.|\\\n)*"|'(?:[^'\\\n]|\\.|\\\n)*')
|(?P<name>[A-Za-z_][A-Za-z0-9_]*)
|(?P<num>0[xX][0-9a-fA-F]*(?:\.[0-9a-fA-F]*)?(?:[pP][+-]?\d+)?|\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)
|(?P<op>\.\.\.|\.\.|==|~=|<=|>=|::|[-+*/%^\#&~|<>=(){}\[\];:,.])
""", re.X)


def tokenize(text):
    """List of (kind, value, line). kind: name | kw | str | num | op. Comments and whitespace dropped."""
    out = []
    pos = 0
    line = 1
    n = len(text)
    match = TOKEN.match
    while pos < n:
        m = match(text, pos)
        if m is None:
            # stray char (non-ASCII outside strings, unterminated quote): skip it
            if text[pos] == "\n":
                line += 1
            pos += 1
            continue
        kind = m.lastgroup
        if kind in ("lceq", "lseq"):
            kind = "lcomment" if m.group("lcomment") else "lstr"
        val = m.group(0)
        end = m.end()
        if kind == "ws":
            pass
        elif kind == "nl":
            line += 1
        elif kind == "comment":
            pass
        elif kind in ("lcomment", "lstr"):
            eq = m.group("lceq") if kind == "lcomment" else m.group("lseq")
            close = "]" + eq + "]"
            stop = text.find(close, end)
            stop = n if stop < 0 else stop + len(close)
            body = text[pos:stop]
            if kind == "lstr":
                out.append(("str", body[len(eq) + 2: -len(close)] if stop <= n else body, line))
            line += body.count("\n")
            end = stop
        elif kind == "str":
            out.append(("str", val[1:-1], line))
            line += val.count("\n")
        elif kind == "name":
            out.append(("kw" if val in KEYWORDS else "name", val, line))
        else:
            out.append((kind, val, line))
        pos = end
    return out


OPEN_BLOCK = {"function", "do", "if", "repeat"}
CLOSE_BLOCK = {"end", "until"}
OPEN_BR = {"(", "{", "["}
CLOSE_BR = {")", "}", "]"}


def _params(toks, i):
    """toks[i] == '(' of a parameter list -> (count, vararg, index after ')'), names list."""
    names = []
    vararg = False
    j = i + 1
    while j < len(toks) and toks[j][1] != ")":
        k, v, _ = toks[j]
        if k == "name":
            names.append(v)
        elif v == "...":
            vararg = True
        j += 1
    return len(names), vararg, j + 1, names


def _dotted(toks, i):
    """Name chain a.b.c or a.b:c starting at toks[i] -> (name, index after)."""
    if toks[i][0] != "name":
        return None, i
    parts = [toks[i][1]]
    j = i + 1
    while j + 1 < len(toks) and toks[j][1] in (".", ":") and toks[j + 1][0] == "name":
        parts.append(toks[j][1])
        parts.append(toks[j + 1][1])
        sep = toks[j][1]
        j += 2
        if sep == ":":
            break
    return "".join(parts), j


def scan(text):
    toks = tokenize(text)
    functions = {}
    tables = {}
    vars_ = {}
    classes = {}
    locals_ = set()
    refs = []
    requires = []
    dyn = []
    depth = 0
    br = 0
    n = len(toks)
    i = 0
    while i < n:
        kind, val, line = toks[i]
        prev = toks[i - 1][1] if i > 0 else None
        if kind == "kw":
            if val == "local":
                # local a, b ... / local function f
                j = i + 1
                if j < n and toks[j][1] == "function":
                    if j + 1 < n and toks[j + 1][0] == "name":
                        locals_.add(toks[j + 1][1])
                else:
                    while j < n and toks[j][0] == "name":
                        locals_.add(toks[j][1])
                        if j + 1 < n and toks[j + 1][1] == ",":
                            j += 2
                        else:
                            break
            elif val == "for":
                j = i + 1
                while j < n and toks[j][0] == "name":
                    locals_.add(toks[j][1])
                    if j + 1 < n and toks[j + 1][1] == ",":
                        j += 2
                    else:
                        break
            elif val == "function":
                # parameters of every function are locals of the file
                j = i + 1
                if j < n and toks[j][0] == "name":
                    name, j = _dotted(toks, j)
                    if (depth == 0 and br == 0 and prev != "local" and j < n and toks[j][1] == "("):
                        cnt, va, _, pn = _params(toks, j)
                        functions[name] = {"params": cnt, "vararg": va, "line": line}  # the last definition wins
                if j < n and toks[j][1] == "(":
                    _, _, _, pn = _params(toks, j)
                    locals_.update(pn)
                    if ":" in (toks[j - 2][1] if j >= 2 else ""):
                        locals_.add("self")
            if val in OPEN_BLOCK:
                depth += 1
            elif val in CLOSE_BLOCK:
                depth = max(0, depth - 1)
            i += 1
            continue
        if kind == "op":
            if val in OPEN_BR:
                br += 1
            elif val in CLOSE_BR:
                br = max(0, br - 1)
            i += 1
            continue
        if kind == "name":
            if depth > 0 and br == 0 and prev not in (".", ":", "local", ",", "function"):
                # assignment inside a function: a global (or a field of another module) set at runtime
                name, j = _dotted(toks, i)
                if j < n and toks[j][1] == "=" and ":" not in name:
                    dyn.append(name)
            if prev not in (".", ":"):
                # a.b reference
                if i + 2 < n and toks[i + 1][1] == "." and toks[i + 2][0] == "name":
                    refs.append((val, toks[i + 2][1], line))
                if val == "require" and i + 1 < n:
                    j = i + 1
                    if toks[j][1] == "(" and j + 1 < n:
                        j += 1
                    if toks[j][0] == "str":
                        requires.append(toks[j][1])
                if val == "class" and i + 1 < n and toks[i + 1][0] == "str" and depth == 0:
                    classes.setdefault(toks[i + 1][1], toks[i + 1][2])
                # top-level assignment a = ... / a.b = ...
                if depth == 0 and br == 0 and prev not in ("local", ",", "function"):
                    name, j = _dotted(toks, i)
                    if j < n and toks[j][1] == "=" and ":" not in name:
                        rhs = toks[j + 1] if j + 1 < n else ("", "", 0)
                        if rhs[1] == "function" and j + 2 < n and toks[j + 2][1] == "(":
                            cnt, va, _, _ = _params(toks, j + 2)
                            functions[name] = {"params": cnt, "vararg": va, "line": line}  # the last definition wins
                        elif "." in name:
                            dyn.append(name)  # a.b = <value> at top level: a field set into a table/module
                        else:
                            if rhs[1] == "{":
                                tables.setdefault(name, line)
                            else:
                                vars_.setdefault(name, line)
                        i = j
                        continue
        i += 1
    runtime = sorted({d for d in dyn if "." not in d and d not in locals_})
    runtime_fields = sorted({d for d in dyn if "." in d and d.split(".", 1)[0] not in locals_})
    return {
        "runtime": runtime,
        "runtime_fields": runtime_fields,
        "functions": functions,
        "tables": tables,
        "vars": vars_,
        "classes": classes,
        "locals": locals_,
        "refs": refs,
        "requires": requires,
    }


def callbacks(text):
    """Event names of RegisterScriptCallback("name", ...) / UnregisterScriptCallback with a string literal."""
    toks = tokenize(text)
    out = []
    for i in range(len(toks) - 2):
        if toks[i][0] == "name" and toks[i][1] in ("RegisterScriptCallback", "UnregisterScriptCallback") \
                and toks[i + 1][1] == "(" and toks[i + 2][0] == "str":
            out.append((toks[i + 2][1], toks[i][2]))
    return out
