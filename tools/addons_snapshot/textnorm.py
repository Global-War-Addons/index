"""Text normalization of gamedata text files (.script, .ltx, .xml).

The snapshot keeps every text file in one canonical form, so that the catalog CI can compute an addon's delta
against it (plans/addons_api_support/13-extension-model.md, section 4.6) and the parsers see the same text:

    decode   UTF-8 (strict); if that fails - Windows-1251 (as the engine does, see CLAUDE.md "Кодировки");
             a byte that is undefined even in cp1251 (0x98) is replaced with U+FFFD, the file is marked "cp1251-lossy"
    BOM      a leading UTF-8 BOM is removed
    EOL      CRLF -> LF; a lone CR is kept as it is (the bytes of the file are not changed beyond CRLF). Note that
             the engine ltx reader (IReader::r_string) and the Lua lexer take a lone CR as a line break too:
             ltxparse.split_lines does the same

The original form (encoding, BOM, line endings) is recorded next to the text, so a merged file can be written back
in the encoding the base used (the rule "do not change the encoding of an existing file").
"""

import hashlib

BOM = b"\xef\xbb\xbf"


def normalize_bytes(data):
    """bytes -> (text, info). info: enc ("ascii" | "utf-8" | "cp1251" | "cp1251-lossy"), bom (bool), eol."""
    bom = data.startswith(BOM)
    if bom:
        data = data[3:]
    if data.isascii():
        enc = "ascii"
        text = data.decode("ascii")
    else:
        try:
            text = data.decode("utf-8")
            enc = "utf-8"
        except UnicodeDecodeError:
            try:
                text = data.decode("cp1251")
                enc = "cp1251"
            except UnicodeDecodeError:
                text = data.decode("cp1251", errors="replace")
                enc = "cp1251-lossy"
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf and lf:
        eol = "mixed"
    elif crlf:
        eol = "crlf"
    elif lf:
        eol = "lf"
    else:
        eol = "none"
    if crlf:
        text = text.replace("\r\n", "\n")
    info = {"enc": enc, "bom": bom, "eol": eol}
    return text, info


def text_sha256(text):
    """sha256 of the normalized text, encoded as UTF-8 (the form stored in the texts blob)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_normalized(path):
    with open(path, "rb") as f:
        return normalize_bytes(f.read())
