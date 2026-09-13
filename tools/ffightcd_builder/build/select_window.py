"""Repair isolated transparent pixels in the select-screen window border."""
import argparse

# SCR3 32x32 tiles: two single-pixel border holes and one two-pixel pane
# hole. Repeated tile placements expose nine pixels across the three windows.
PIXELS = ((0x09D0, 24, 30), (0x09D1, 8, 30), (0x09D1, 8, 24), (0x09D1, 8, 25))


def pen(space, tile, x, y):
    offset = tile*512 + y*16 + (x//8)*4
    return sum(((space[offset+p] >> (7-x%8)) & 1) << p for p in range(4))


def patch(space):
    for tile, x, y in PIXELS:
        assert pen(space, tile, x, y) == 15, f"window hole moved: {tile:04x}"
        # The two-pixel hole is in the pane beside a reflection edge. Its
        # left neighbors are flat gray; the right edge transitions to pen 10.
        neighbors = (x-2, x-1, x+1, x+2) if y == 30 else (x-2, x-1)
        assert all(pen(space, tile, xx, y) == 9 for xx in neighbors)
        offset = tile*512 + y*16 + (x//8)*4
        mask = 1 << (7-x%8)
        for plane in range(4):
            space[offset+plane] = ((space[offset+plane] & ~mask)
                                   | (((9 >> plane) & 1) * mask))


def self_test():
    space = bytearray(0x200000)
    for tile, x, y in PIXELS:
        offset = tile*512 + y*16 + (x//8)*4
        # Solid pen-9 row, with exactly the original pen-15 hole.
        start = tile*512+y*16
        space[start:start+16] = bytes.fromhex("ff0000ff")*4
        space[offset+1] |= 1 << (7-x%8)
        space[offset+2] |= 1 << (7-x%8)
    old = bytes(space)
    patch(space)
    assert sum((a ^ b).bit_count() for a, b in zip(old, space)) == 8
    assert all(pen(space, *p) == 9 for p in PIXELS)
    try:
        patch(space)
    except AssertionError:
        pass
    else:
        raise AssertionError("changed source pixels must fail the guard")
    print("Select window: four source pixels repaired; source guard PASS")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", required=True)
    parser.parse_args()
    self_test()
