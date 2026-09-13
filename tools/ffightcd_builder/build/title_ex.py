"""Render the authored EX on the topmost title layer, using the CD swirl paths."""
from functools import lru_cache
from pathlib import Path
import argparse
import json
import struct

import numpy as np
from PIL import Image

ART = Path(__file__).resolve().parents[1] / 'assets/arcade_ex_alternative'
MOTION = Path(__file__).resolve().parents[1] / 'assets/native_ex'
GFX_OFFSET = 0x480000
GFX_LIMIT = 0x5E2000  # stop before the mapper-neutral continue fill at $5E2400
DATA = 0x1E0000
PALETTE = 29
MARKER = 0x913FF0
MAGIC = 0x45585454
COUNTER = MARKER + 4
SCENE = MARKER + 6
WAIT = 45
STEPS = 60
# The authored canvas origin.  E and X are full-canvas layers that interlock
# across 18 columns, so each one carries the whole 72x52 frame and they share
# this origin; the composite is intact once both have settled on it.
HOME = (226, 107)


def _pens(image, colors):
    im = np.asarray(image.convert('RGBA'))
    assert im.shape == (52, 72, 4)
    assert set(np.unique(im[:, :, 3])) <= {0, 255}
    pens = np.full((52, 72), 15, dtype=np.uint8)
    for index, color in enumerate(colors):
        pens[np.all(im[:, :, :3] == color, axis=2) & (im[:, :, 3] == 255)] = index
    assert np.array_equal(pens != 15, im[:, :, 3] == 255)
    return pens


@lru_cache(None)
def artwork():
    """The two independent letter layers, E first, on the shared 72x52 canvas."""
    spec = json.loads((ART / 'ex_pixels.json').read_text())
    assert tuple(spec['size']) == (72, 52)
    colors = [tuple(bytes.fromhex(c)) for c in spec['palette']]
    letters = tuple(_pens(Image.open(ART / f'{name}.png'), colors) for name in ('e', 'x'))
    # Each layer holds one whole letter with its own contour and shadow, so
    # they overlap.  E is the upper layer, matching the authored composite.
    grid = np.array([[15 if c == spec['transparent'] else int(c, 16) for c in row]
                     for row in spec['rows']], dtype=np.uint8)
    assert np.array_equal(_settled(letters), grid)
    assert np.array_equal(grid, _pens(Image.open(ART / 'ex.png'), colors))
    return letters, colors


def _settled(letters):
    return np.where(letters[0] == 15, letters[1], letters[0])


def pixels():
    """The settled composite, as the authored grid renders it."""
    letters, colors = artwork()
    return _settled(letters), colors


@lru_cache(None)
def motion():
    data = json.loads((MOTION / 'cd_title_motion.json').read_text())
    poses = data['poses']
    assert len(poses) == STEPS
    assert [p['frame'] for p in poses] == list(range(9744, 9804))
    return poses


def positions(index):
    """Index 0 is the logo's entrance; 1..60 are the CD's measured poses."""
    if index == 0:
        return None
    pose, final = motion()[min(index, STEPS)-1], motion()[-1]
    return tuple((HOME[0]+pose[k][0]-final[k][0],
                  HOME[1]+pose[k][1]-final[k][1]) for k in ('c', 'd'))


def frame(index):
    result = np.full((224, 384), 15, dtype=np.uint8)
    pos = positions(index)
    if pos is None:
        return result
    letters, _ = artwork()
    # C precedes D in the Sega CD sprite list, so E covers X if they overlap.
    for letter in (1, 0):
        part = letters[letter]
        x, y = pos[letter]
        yy, xx = np.where(part != 15)
        valid = (xx+x >= 0) & (xx+x < 384) & (yy+y >= 0) & (yy+y < 224)
        result[(yy+y)[valid], (xx+x)[valid]] = part[yy[valid], xx[valid]]
    return result


def planar(tile):
    out = bytearray()
    for y in range(32):
        for half in range(4):
            for plane in range(4):
                out.append(sum(((int(tile[y, half*8+x]) >> plane) & 1) << (7-x)
                               for x in range(8)))
    return bytes(out)


@lru_cache(None)
def packed():
    tiles, unique, records = [], {}, []
    for index in range(STEPS+1):
        canvas = frame(index)
        entries = []
        for row in range(7):
            for col in range(12):
                tile = canvas[row*32:(row+1)*32, col*32:(col+1)*32]
                if np.all(tile == 15):
                    continue
                key = tile.tobytes()
                if key not in unique:
                    unique[key] = len(tiles)
                    tiles.append(planar(tile))
                code = GFX_OFFSET//512 + unique[key]
                address = 0x910000 + (col*8+row)*4
                entries.append(struct.pack('>II', address, (code << 16) | PALETTE))
        records.append(struct.pack('>H', len(entries)) + b''.join(entries))
    data = bytearray()
    cursor = DATA + len(records)*4
    for record in records:
        data.extend(struct.pack('>I', cursor))
        cursor += len(record)
    data.extend(b''.join(records))
    gfx = b''.join(tiles)
    assert GFX_OFFSET + len(gfx) <= GFX_LIMIT, 'EX frames overlap reserved continue fill'
    assert len(data) <= 0x10000
    return gfx, bytes(data), len(tiles)


def graphics():
    return packed()[0]


def init_code():
    b = bytearray()
    def words(*args):
        b.extend(struct.pack('>' + 'H'*len(args), *args))
    def move_long(value, address):
        words(0x23FC, value >> 16, value & 0xFFFF, address >> 16, address & 0xFFFF)
    move_long(MAGIC, MARKER)
    # Remember the scene the title was entered from.  A credited title keeps
    # the scene word of whatever it interrupted -- the opening, a profile
    # card, the fighting demo, and a different one per region -- so the tick
    # cannot test a fixed list.  It draws while the scene is the one this
    # init saw, and stops as soon as the scene moves on.
    words(0x33ED, 0x9288, SCENE >> 16, SCENE & 0xFFFF)
    # Credited title starts settled; only the attract entrance starts at zero.
    words(0x33FC, WAIT+STEPS, COUNTER >> 16, COUNTER & 0xFFFF)
    words(0x0C79, 0x51DE, 0x00FF, 0xFFF0, 0x6608)
    words(0x33FC, 0, COUNTER >> 16, COUNTER & 0xFFFF)
    _, colors = pixels()
    palette = [0xF000 | (r//17 << 8) | (g//17 << 4) | b//17 for r,g,b in colors]
    palette += [0xF000]*(16-len(palette))
    for i in range(0, 16, 2):
        move_long((palette[i] << 16) | palette[i+1], 0x914C00 + PALETTE*32 + i*2)
    for offset, value in ((0x32,0),(0x36,0),(0x34,0x710),(0x38,0x710),
                          (0x6E,0x360E),(0x70,0x360E)):
        words(0x3B7C, value, offset)
    words(0x207C, 0x000C, 0xA040, 0x4E75)
    return bytes(b)


def emit_tick(emit, bxx, lab, b):
    """A bounded title-only SCR3 update before the normal video-register copy."""
    lab['extick'] = len(b)
    emit(0x0CB9, MAGIC >> 16, MAGIC & 0xFFFF, MARKER >> 16, MARKER & 0xFFFF)
    bxx(0x6600, 'exret')
    # Off-title frames only SKIP.  Disarming here cost the EX mark on every
    # title the init does not re-enter afterwards, and the marker is cleared
    # anyway as soon as the next screen loads its own map over these cells.
    #
    # tst.B: the game mode is the byte at $FF8000.  A word test also reads
    # the credit byte at $FF8001, which is non-zero once a coin is in, so it
    # failed on every credited title -- the EX mark only ever appeared on
    # the attract one, and the title came up stock after a coin.
    emit(0x4A39, 0x00FF, 0x8000)  # game mode must be attract/credited title
    bxx(0x6600, 'exret')
    emit(0x3239, SCENE >> 16, SCENE & 0xFFFF)   # move.w SCENE,d1
    emit(0xB26D, 0x9288)                        # cmp.w (scene word),d1
    bxx(0x6600, 'exret')
    emit(0x4A39, 0x00FF, 0x8001)  # a credit in hand means the credited title
    bxx(0x6600, 'excredited')
    emit(0x323C, 0x348E)           # preserve S2/OBJ/S1 order, then EX
    bxx(0x6000, 'exactive')
    lab['excredited'] = len(b)
    # A coin can also interrupt an existing attract swirl without reinitializing.
    emit(0x33FC, WAIT+STEPS, COUNTER >> 16, COUNTER & 0xFFFF)
    emit(0x323C, 0x360E)           # preserve OBJ/S2/S1 order, then EX
    lab['exactive'] = len(b)
    emit(0x3B41, 0x006E, 0x3B41, 0x0070)
    for offset, value in ((0x32,0),(0x36,0),(0x34,0x710),(0x38,0x710)):
        emit(0x3B7C, value, offset)
    # Clear the previous overlay. SCR3 is blank on the stock title; 12x8
    # contiguous cells cover the entire visible canvas, including partial edges.
    emit(0x45F9, 0x0091, 0x0000)
    emit(0x203C, 0x0980, 0x0000, 0x343C, 95)
    lab['exclear'] = len(b)
    emit(0x24C0)
    bxx(0x51CA, 'exclear')
    emit(0x3039, COUNTER >> 16, COUNTER & 0xFFFF)
    emit(0x0C40, WAIT+STEPS)
    bxx(0x6400, 'excounted')
    emit(0x5279, COUNTER >> 16, COUNTER & 0xFFFF)
    lab['excounted'] = len(b)
    emit(0x0440, WAIT)
    bxx(0x6400, 'expose')
    emit(0x7000)
    bxx(0x6000, 'exdraw')
    lab['expose'] = len(b)
    emit(0x5240, 0x0C40, STEPS)
    bxx(0x6300, 'exdraw')
    emit(0x303C, STEPS)
    lab['exdraw'] = len(b)
    emit(0xE548)                  # lsl.w #2,d0
    emit(0x43F9, DATA >> 16, DATA & 0xFFFF)
    emit(0x2271, 0x0000)          # movea.l (a1,d0.w),a1
    emit(0x3419)                  # move.w (a1)+,d2
    bxx(0x6700, 'exret')
    emit(0x5342)
    lab['excells'] = len(b)
    emit(0x2459, 0x2499)          # movea.l (a1)+,a2; move.l (a1)+,(a2)
    bxx(0x51CA, 'excells')
    bxx(0x6000, 'exret')
    lab['exret'] = len(b)
    emit(0x4E75)


def self_test():
    gfx, data, count = packed()
    assert len(gfx) == count*512
    for index in range(STEPS+1):
        offset = struct.unpack_from('>I', data, index*4)[0] - DATA
        n = struct.unpack_from('>H', data, offset)[0]
        decoded = np.full((224,384),15,dtype=np.uint8)
        for k in range(n):
            address, cell = struct.unpack_from('>II',data,offset+2+k*8)
            row, col = ((address-0x910000)//4)%8, ((address-0x910000)//4)//8
            start = ((cell >> 16)-GFX_OFFSET//512)*512
            assert cell & 0xFFFF == PALETTE
            for y in range(32):
                for x in range(32):
                    decoded[row*32+y,col*32+x] = sum(((gfx[start+y*16+x//8*4+p] >> (7-x%8)) & 1) << p for p in range(4))
        assert np.array_equal(decoded,frame(index))
    assert positions(STEPS) == (HOME, HOME)
    x, y = HOME
    assert np.array_equal(frame(STEPS)[y:y+52,x:x+72],pixels()[0])
    assert init_code()[-8:] == bytes.fromhex('207c000ca0404e75')
    print(f'CD paths and all 61 foreground frame round trips: PASS ({count} tiles)')


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-test', action='store_true', required=True)
    parser.parse_args()
    self_test()
