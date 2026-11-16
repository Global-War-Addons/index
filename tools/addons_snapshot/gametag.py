"""Release tags of the game: parse and compare them the way the engine does (xray-16/src/xrGame/addon_merge.cpp,
MergeParseTag / ReleaseTagOlder). The one implementation for the Python tools: GlobalWar/utils/release/write_gw_release.py,
gw_snapshot.py and the catalog validator (catalog/validator, through gwcat/snapshot_tools.py) import this module.

The scheme of a tag:

    [v|V] <N>(.<N>)* [-<suffix>]

    <N>       ASCII digits, at most 4294967295 (u32 of the engine), leading zeros allowed (04 == 4); a missing part
              counts as 0 when tags are compared (v1.4 == 1.4.0)
    -patchN   ("patch" in any case, N - digits, at most 4294967295) the patch number of the release
    -<other>  ASCII letters, digits, dots: a pre-release (-Beta, -rc.1, -patch, -patch4b, -patch4.1)

The whole string is the tag: no spaces around (the callers trim an ltx value first, as the engine does).

For the same numbers: pre-release < release < -patch0 < -patch1 < ...; two pre-releases are compared as strings
ignoring the case ("rc.10" < "rc.2").
Examples: 0.5.5-Beta < 0.5.5 < 0.5.5-patch1 < 0.5.5-patch4 < 0.5.6-Beta < v0.5.6.

This is the scheme of the GAME releases only: the release tags of the addons of the catalog stay v<MAJOR.MINOR.PATCH>
(catalog/validator/gwcat/index_build.py TAG_RE).
"""

import re

TAG_RE = re.compile(r"^[vV]?([0-9]+(?:\.[0-9]+)*)(?:-([A-Za-z0-9.]+))?\Z")
PATCH_RE = re.compile(r"^patch([0-9]+)\Z", re.IGNORECASE)

MAX_NUMBER = 4294967295  # a number of a tag above it: not a tag (the engine keeps them in u32)
PRE, RELEASE, PATCH = 0, 1, 2  # the kinds of a tag, in their order for the same numbers


class GameTag(tuple):
    """A parsed tag: (numbers, kind, patch, pre). numbers - the parts without the trailing zeros (1.4.0 -> (1, 4)), so
    that the tuples compare as the tags do; kind - PRE / RELEASE / PATCH; patch - N of -patchN; pre - the pre-release
    suffix in lower case. Two tags compare (<, ==) as GameTag objects"""

    __slots__ = ()

    def __new__(cls, numbers, kind, patch=0, pre=""):
        return tuple.__new__(cls, (tuple(numbers), kind, patch, pre))

    numbers = property(lambda self: self[0])
    kind = property(lambda self: self[1])
    patch = property(lambda self: self[2])
    pre = property(lambda self: self[3])


def _number(digits):
    """ASCII digits -> int, None above MAX_NUMBER (the length is checked first: int() of a huge string is slow or
    refused)"""
    if len(digits.lstrip("0")) > len(str(MAX_NUMBER)):
        return None
    value = int(digits)
    return value if value <= MAX_NUMBER else None


def parse(tag):
    """A tag -> GameTag, None - not a tag of the scheme (spaces around are not trimmed: the caller trims, as the
    engine does for an ltx value)"""
    if not isinstance(tag, str):
        return None
    m = TAG_RE.match(tag)
    if not m:
        return None
    numbers = [_number(x) for x in m.group(1).split(".")]
    if None in numbers:
        return None
    while numbers and numbers[-1] == 0:
        numbers.pop()
    suffix = m.group(2)
    if suffix is None:
        return GameTag(numbers, RELEASE)
    patch = PATCH_RE.match(suffix)
    if patch:
        number = _number(patch.group(1))
        return GameTag(numbers, PATCH, number) if number is not None else None
    return GameTag(numbers, PRE, pre=suffix.lower())


def is_tag(tag):
    return parse(tag) is not None


def compare(a, b):
    """-1 / 0 / 1 for two tags of the scheme; ValueError when one of them is not such a tag"""
    pa, pb = parse(a), parse(b)
    if pa is None or pb is None:
        raise ValueError("not a release tag of the game: %r" % (a if pa is None else b))
    return (pa > pb) - (pa < pb)


def same(a, b):
    """The same release: equal strings, or two tags of the scheme that compare equal (v1.4 and 1.4.0)"""
    if a == b:
        return True
    pa, pb = parse(a), parse(b)
    return pa is not None and pa == pb


def is_older(game_version, tag):
    """[addon] game_version is older than the release tag of the game, as the engine decides it (ReleaseTagOlder): an
    empty value or the same tag - no; a value that is not a tag of the scheme - any difference counts as older"""
    if not game_version or not tag or game_version == tag:
        return False
    pa, pb = parse(game_version), parse(tag)
    if pa is None or pb is None:
        return True
    return pa < pb


def sort_key(tag):
    """sorted(tags, key=sort_key): the tags of the scheme in their order, the other strings before them (by string)"""
    parsed = parse(tag)
    return (1, parsed, "") if parsed is not None else (0, GameTag((), PRE), tag)
