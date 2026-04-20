"""
V2 → Layer-1 shared services bridges (PRD §10.5).

V2 code must ONLY touch V1 through these adapters. The point of the
indirection is that when V1 is eventually retired the bridges can be
re-pointed at a V2-native registry/manager with zero business-code
change. See ``scripts/check_v2_isolation.py`` for the enforcing
pre-commit check.
"""
