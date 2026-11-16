"""Limits of the catalog. 11-catalog-distribution.md 3.1, 3.4; 00-roadmap.md 5a decision 25 (2026-11-05): parts of
2 GiB without a total limit, up to 10 screenshots, up to 1920 px and 1 MB. The ones marked (gwcat) are this validator's
own proposal (not in the plan): they keep index.json compact (R22) and are easy to change here."""

GIB = 1024 ** 3
MIB = 1024 ** 2
KIB = 1024

PART_MAX_BYTES = 2 * GIB - 1  # a release asset of GitHub is "under 2 GiB"
ICON_SIDE = 256
ICON_MAX_BYTES = 256 * KIB
SCREENSHOTS_MAX = 10
SCREENSHOT_MAX_SIDE = 1920
SCREENSHOT_MAX_BYTES = 1 * MIB
DESCRIPTION_MAX_BYTES = 64 * KIB      # (gwcat) one description*.md
NAME_MAX_CHARS = 80                   # (gwcat) name / name_<lang>
SUMMARY_MAX_CHARS = 200               # (gwcat) summary / summary_<lang>
TAGS_MAX = 10                         # (gwcat)
AUTHORS_MAX_CHARS = 300               # (gwcat) the whole authors line
MANIFEST_MAX_BYTES = 64 * KIB         # (gwcat) addon.ltx, catalog.ltx
DELTA_TEXTS_WARN_BYTES = 1 * MIB      # 15 section 7.2: "the file is replaced almost whole" (gw_snapshot warns too)
ID_MAX = 64
REPLACES_LIST_MAX = 200             # (gwcat) files/<id>/<ver>.json "replaces" files: the first N paths
PRIVATE_WILDCARD_MAX = 256          # (gwcat) [private] masks with * or ?: each is matched against every file
MODEL_READ_MAX_BYTES = 64 * MIB     # (gwcat) an .ogf above this is not parsed for its references
