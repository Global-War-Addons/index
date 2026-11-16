"""gwcat - the validator and the index builder of the GlobalWar addon catalog.

plans/addons_api_support/11-catalog-distribution.md (stages K2, K3), 15-merge-implementation.md section 7 (the delta).
Python standard library only. The merge delta is computed by tools/addons_snapshot (gw_snapshot.py delta, which runs
gw_merge): this package never reimplements it.
"""

VERSION = "0.1.0"
