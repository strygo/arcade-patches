import struct, sys

def decompress(data, sp=0, max_out=64*1024*1024):
    src = sp
    out = bytearray()
    ctrl = 0; mask = 0
    n = len(data)
    while src + 1 < n and len(out) < max_out:
        if mask == 0:
            ctrl = data[src] | (data[src+1] << 8); src += 2; mask = 0x8000
            if src+1 >= n: break
        if (ctrl & mask) == 0:
            out += data[src:src+2]; src += 2
        else:
            w = data[src] | (data[src+1] << 8)
            len5 = w >> 11
            if len5 == 0:
                if src+3 >= n: break
                length = data[src+2] | (data[src+3] << 8); off = w & 0x7ff; src += 4
            else:
                length = len5; off = w & 0x7ff; src += 2
            if off == 0:
                if length == 0:
                    return bytes(out), src   # terminator
                out += b'\x00\x00' * length
            else:
                start = len(out) - off*2
                if start < 0: return None, src
                for _ in range(length):
                    out += out[start:start+2]; start += 2
        mask >>= 1
    return bytes(out), src

if __name__ == '__main__':
    b = open(sys.argv[1],'rb').read()
    # brute-force the start offset; report ones that produce an ELF
    for sp in range(0, 0x40, 2):
        res = decompress(b, sp, max_out=4*1024*1024)
        if res[0] and len(res[0])>0x1000:
            o = res[0]
            tag = 'ELF!' if o[:4]==b'\x7fELF' else o[:4].hex()
            if o[:4]==b'\x7fELF' or o[:2]==b'\x7fE':
                print(f"sp=0x{sp:x}: out={len(o)} bytes head={o[:16].hex()} {tag}")
    # also just try sp=0 and report head
    o,_=decompress(b,0,max_out=1024*1024)
    print(f"sp=0 head={o[:16].hex() if o else None} len={len(o) if o else 0}")
