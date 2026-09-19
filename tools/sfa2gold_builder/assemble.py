"""Shared reconstruction core for the SFA2 Gold builder.

Both the generator (my side, has the verified target) and the shipped applier
(has only the user's ISO + arcade romset + the recipes) build the SAME ordered
source pools here, so recipe source indices line up. The heavy bytes always
come from the user's own files via mechanical transforms; recipes carry only
copy ops + our small authored literals.
"""
import gfx
import recipe as rc
from extract import wordswap

PROG_SIZE = 0x80000  # the six maincpu program ROMs (names differ per set)
GFX_PRIMARY = ("sza.13m", "sza.15m", "sza.17m", "sza.19m")
GFX_SECONDARY = ("sza.14m", "sza.16m", "sza.18m", "sza.20m")
SAMPLES = ("sza.11m", "sza.12m")
Z80 = ("sza.01", "sza.02")


def missing_inputs(arc: dict) -> list:
    """Members this reconstruction reads that the romset does not have.

    Checked before anything is assembled: a split set (one that leaves the
    shared graphics and samples in its parent) otherwise died with a KeyError
    deep in the graphics pool, which tells a user nothing about what to fetch.
    """
    need = list(GFX_PRIMARY) + list(GFX_SECONDARY) + list(SAMPLES) + list(Z80)
    missing = [m for m in need if m not in arc]
    if len([m for m, b in arc.items() if len(b) == PROG_SIZE]) < 6:
        missing.append("the six 512 KB program ROMs")
    return missing


def prepare_inputs(arc: dict, extracted: dict) -> dict:
    """arc: {member: bytes} of the stock arcade romset.
    extracted: {'entry531': [tiles], 'comp2': bytes, 'audio': {sza.0x: bytes}}.
    Returns the derived pools used by both encode and apply."""
    enc531 = b"".join(gfx.encode_true_tile(t) for t in extracted["entry531"])
    return {
        "arc": arc,
        "prog": sorted(m for m, b in arc.items() if len(b) == PROG_SIZE),
        "comp2": extracted["comp2"],
        "audio": extracted["audio"],
        "enc531": enc531,
        "arc_planar": {
            "primary": gfx.planar([arc[n] for n in GFX_PRIMARY]),
            "secondary": gfx.planar([arc[n] for n in GFX_SECONDARY]),
        },
    }


# --- fixed ordered source pools (identical in generator and applier) ---

def program_pool(inp, member):
    # comp2 twice: as stored, and word-swapped, because some tables the
    # revision imports (the Gold quote-selector rows among them) sit in the
    # program ROMs in the other byte order.  The swapped copy goes LAST so
    # every existing source index stays put.
    return ([inp["arc"][member]] + [inp["arc"][m] for m in inp["prog"]]
            + [inp["comp2"], wordswap(inp["comp2"])])


def gfx_pool(inp, bank):
    return [inp["arc_planar"][bank], inp["enc531"], inp["comp2"]]


def sample_pool(inp):
    # The corpus is repacked in RAW (un-word-swapped) sample space, so the
    # recipe runs there too; members are word-swapped back on output.
    a, au = inp["arc"], inp["audio"]
    return [wordswap(a["sza.11m"]), wordswap(a["sza.12m"]),
            wordswap(au["zero6"]["sza.11m"]), wordswap(au["zero6"]["sza.12m"]),
            wordswap(au["zero4"]["sza.11m"]), wordswap(au["zero4"]["sza.12m"]),
            inp["comp2"]]


def z80_pool(inp):
    au = inp["audio"]
    return [au["zero6"]["sza.01"], au["zero6"]["sza.02"],
            au["zero4"]["sza.01"], au["zero4"]["sza.02"], inp["comp2"]]


# --- generator: build recipes from a verified target ---

def build_recipes(inp, target: dict) -> dict:
    """Return {member_or_bank: serialized_ops} for everything that differs from
    the arcade base. Verifies each unit reconstructs byte-exact before storing."""
    out = {}
    # program
    for m in inp["prog"]:
        if target[m] != inp["arc"][m]:
            pool = program_pool(inp, m)
            ops = rc.encode_member(target[m], _named(pool), base_id=0)
            assert rc.apply_member(ops, pool) == target[m], m
            out["prog:" + m] = rc.dump_ops(ops)
    # graphics (planar level)
    for bank, names in (("primary", GFX_PRIMARY), ("secondary", GFX_SECONDARY)):
        tgt_planar = gfx.planar([target[n] for n in names])
        if tgt_planar != inp["arc_planar"][bank]:
            pool = gfx_pool(inp, bank)
            ops = rc.encode_member(tgt_planar, _named(pool), base_id=0)
            assert rc.apply_member(ops, pool) == tgt_planar, bank
            out["gfx:" + bank] = rc.dump_ops(ops)
    # audio
    for m in SAMPLES:
        if target[m] != inp["arc"][m]:
            pool = sample_pool(inp)
            tgt_raw = wordswap(target[m])          # recipe in raw sample space
            ops = rc.encode_member(tgt_raw, _named(pool))
            assert wordswap(rc.apply_member(ops, pool)) == target[m], m
            out["samp:" + m] = rc.dump_ops(ops)
    for m in Z80:
        if target[m] != inp["arc"][m]:
            pool = z80_pool(inp)
            ops = rc.encode_member(target[m], _named(pool))
            assert rc.apply_member(ops, pool) == target[m], m
            out["z80:" + m] = rc.dump_ops(ops)
    return out


def _named(pool):
    # encode_member takes [(id, bytes)]; ids only used for base preference by index
    return [(i, b) for i, b in enumerate(pool)]


# --- applier: reconstruct a full romset from inputs + recipes ---

def reconstruct(inp, recipes: dict) -> dict:
    """Return {member: bytes} for the full romset. Members with no recipe are
    copied verbatim from the arcade romset."""
    members = dict(inp["arc"])
    for m in inp["prog"]:
        key = "prog:" + m
        if key in recipes:
            members[m] = rc.apply_member(rc.load_ops(recipes[key]), program_pool(inp, m))
    # graphics: apply planar recipes, then split each bank into 4 ROMs
    for bank, names in (("primary", GFX_PRIMARY), ("secondary", GFX_SECONDARY)):
        key = "gfx:" + bank
        planar = inp["arc_planar"][bank]
        if key in recipes:
            planar = rc.apply_member(rc.load_ops(recipes[key]), gfx_pool(inp, bank))
        for name, rom in zip(names, gfx.planar_to_roms(planar)):
            members[name] = rom
    for m in SAMPLES:
        key = "samp:" + m
        if key in recipes:
            raw = rc.apply_member(rc.load_ops(recipes[key]), sample_pool(inp))
            members[m] = wordswap(raw)
    for m in Z80:
        key = "z80:" + m
        if key in recipes:
            members[m] = rc.apply_member(rc.load_ops(recipes[key]), z80_pool(inp))
    return members
