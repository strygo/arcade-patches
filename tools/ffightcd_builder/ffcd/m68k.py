"""Minimal 68000 interpreter for the FF CD scene player.

Executes the DISC'S OWN player code (bundle code chunks + MAIN* overlays,
loaded at the game's own loader-table addresses). Our code supplies only the
machine boundary: memory bus, VDP ports (address latch, VRAM/CRAM/VSRAM,
DMA), vblank timing, and grow-on-demand stubs for main-RAM library calls
(0xFFFFxxxx) -- every stub logged, semantics validated against ground truth.

Instruction coverage is a grow-on-demand subset: unknown opcodes raise
Unimplemented(pc, opcode) and are added as encountered.
"""
import struct, collections

# Operand size masks.  Module-level because the expressions that used
# them sit in the interpreter's inner loop and rebuilt the dict on
# every operand access.
_MASK = {1: 0xFF, 2: 0xFFFF, 4: 0xFFFFFFFF}      # value kept
_KEEP = {1: 0xFFFFFF00, 2: 0xFFFF0000, 4: 0}     # register bits preserved


class Unimplemented(Exception):
    def __init__(self, pc, op, note=""):
        super().__init__(f"pc={pc:06x} op={op:04x} {note}")
        self.pc, self.op = pc, op

class StubCall(Exception):
    def __init__(self, pc, target):
        super().__init__(f"stub call {target:06x} from {pc:06x}")
        self.pc, self.target = pc, target

class VDP:
    """Model of the MD VDP: control/data ports, VRAM/CRAM/VSRAM, regs, DMA."""
    def __init__(self, bus):
        self.bus = bus
        self.vram = bytearray(0x10000)
        self.cram = bytearray(128)
        self.vsram = bytearray(80)
        self.regs = [0]*24
        self.addr = 0; self.code = 0
        self.pending = None      # first half of a 32-bit control write
        self.dma_fill = False
        self.writes = 0
    def _target(self):
        t = self.code & 0xF
        if t in (1, 0): return self.vram, 0xFFFF     # VRAM write/read
        if t in (3, 8): return self.cram, 0x7F
        if t in (5, 4): return self.vsram, 0x4F
        return self.vram, 0xFFFF
    def ctrl_write16(self, v):
        if self.pending is not None:
            first, v2 = self.pending, v
            self.addr = ((first & 0x3FFF) | ((v2 & 3) << 14)) & 0xFFFF
            self.code = ((first >> 14) & 3) | ((v2 >> 2) & 0x3C)
            self.pending = None
            if self.code & 0x20:
                self._dma()
            return
        if (v & 0xC000) == 0x8000:
            r = (v >> 8) & 0x1F
            if r < 24: self.regs[r] = v & 0xFF
        else:
            self.pending = v
    def data_write16(self, v):
        mem, mask = self._target()
        a = self.addr & mask
        if a + 1 < len(mem):
            mem[a] = (v >> 8) & 0xFF; mem[a+1] = v & 0xFF
        self.addr = (self.addr + (self.regs[15] or 2)) & 0xFFFF
        self.writes += 1
    def _dma(self):
        mode = self.regs[23] >> 6
        length = (self.regs[19] | (self.regs[20] << 8)) or 0x10000
        if mode < 2:   # 68k -> VDP
            src = ((self.regs[21] | (self.regs[22] << 8) |
                    ((self.regs[23] & 0x7F) << 16)) << 1) & 0xFFFFFF
            wordram = 0x200000 <= src < 0x240000
            if wordram:
                # Mega CD Word-RAM DMA quirk: fetch lags one word; the
                # game's setup code pre-adds 2 to compensate
                src = (src - 2) & 0xFFFFFF
            mem, mask = self._target()
            inc = self.regs[15] or 2
            a = self.addr
            # DMA reads the ART UNDERNEATH any resident-module overlay: on
            # hardware the module and the scene art are in different Word-RAM
            # banks, and only the CPU side sees the module.
            rd = self.bus.read16_dma if wordram else self.bus.read16
            for i in range(length):
                w = rd(src + i*2)
                aa = a & mask
                if aa + 1 < len(mem):
                    mem[aa] = (w >> 8) & 0xFF; mem[aa+1] = w & 0xFF
                a = (a + inc) & 0xFFFF
            self.addr = a
        # fill/copy modes unused by the player so far; add when hit
    def status_read(self):
        return 0x3400 | 0x0008   # fifo empty | vblank flag set

class Bus:
    def __init__(self):
        self.wram = bytearray(0x40000)   # word RAM window @0x200000
        self.ram  = bytearray(0x10000)   # main RAM @0xFF0000 (mirror FFFF8000+)
        self.vdp = VDP(self)
        self.stubs = {}                  # addr -> python fn(cpu)
        self.bios_stubs = {}             # gate addr -> fn(cpu)
        self.bios_calls = []
        self.peeks = {}                  # pc -> fn(cpu), fires then continues
        self.log = []
        # ---- resident MAIN-side module overlay.
        # MAINSYS's stage dispatcher (FF12A8) fetches a handler pointer from
        # 0x207800 for every positive TICK_LIST entry; that module is MAIN1.BIN
        # for the ending.  In 1M mode it lives in MAIN's Word-RAM bank while
        # the Sub fills ITS bank with the scene art, so the two never collide
        # on hardware -- but this Bus folds both sides onto one buffer, where
        # the module's 0x207800.. would sit inside the scene chunk staged at
        # 0x203000 (the farewell's c3 spans 0x203000..0x213D8C).  Modelling it
        # as a CPU-side overlay keeps both: instruction fetch and data reads
        # see the module, VDP DMA (read16_dma) sees the art beneath it.
        # `module_on` is the bank select.  It must be OFF while the CODE-CHUNK
        # stages run: those scenes read their own art and nametables straight
        # out of 0x207800.., and shadowing it there diverges the ending's first
        # scene from frame 3 (measured).  ScriptPlayer.tick() sets it from
        # $985A each frame -- on for the MAIN2-header stages, off for the two
        # chunk-header ones.
        self.module = None               # (base, end, bytes)
        self.module_on = False
    def load_module(self, base, data):
        self.module = (base, base + len(data), bytes(data))
    def load(self, addr, data):
        if 0x0C0000 <= addr < 0x100000:
            addr = addr - 0x0C0000 + 0x200000
        if 0x200000 <= addr < 0x240000:
            self.wram[addr-0x200000:addr-0x200000+len(data)] = data
        elif addr >= 0xFF0000:
            a = addr - 0xFF0000
            self.ram[a:a+len(data)] = data
        else:
            raise ValueError(hex(addr))
    def read8_dma(self, a):
        """Word-RAM read that IGNORES the module overlay (see load_module)."""
        a &= 0xFFFFFF
        if 0x200000 <= a < 0x240000: return self.wram[a-0x200000]
        if 0x0C0000 <= a < 0x100000: return self.wram[a-0x0C0000]
        return self.read8(a)
    def read16_dma(self, a):
        a &= 0xFFFFFF
        return (self.read8_dma(a) << 8) | self.read8_dma(a+1)
    def read8(self, a):
        a &= 0xFFFFFF
        m = self.module
        if m is not None and self.module_on and m[0] <= a < m[1]:
            return m[2][a-m[0]]
        if 0x200000 <= a < 0x240000: return self.wram[a-0x200000]
        if 0x0C0000 <= a < 0x100000: return self.wram[a-0x0C0000]
        if a >= 0xFF0000: return self.ram[a-0xFF0000]
        if a == 0xC00004 or a == 0xC00006: return (self.vdp.status_read() >> 8) & 0xFF
        if a == 0xC00005 or a == 0xC00007: return self.vdp.status_read() & 0xFF
        if a == 0xA12003: return 0x01   # Word RAM: RET=1 (main owns), DMNA=0
        if 0xA00000 <= a < 0xA20000: return 0   # z80/io/gate-array: quiet
        return 0
    def read16(self, a):
        # Fetches both bytes directly for the three plain memory regions
        # instead of calling read8 twice.  read16 ran 21.1 M times per US
        # farewell render and was the source of ~42 M of read8's 43.8 M
        # calls; the pair is ~25% of the 68K interpreter's cost.
        #
        # The fast path is only taken when BOTH bytes fall inside one
        # region and the module overlay cannot claim either of them -- a
        # 16-bit read straddling the overlay edge, or the 0xFFFFFF wrap,
        # still goes the long way through read8 so the answer is identical
        # rather than merely usually identical.
        a &= 0xFFFFFF
        if a == 0xC00004 or a == 0xC00006: return self.vdp.status_read()
        m = self.module
        if m is not None and self.module_on:
            if m[0] <= a and a + 1 < m[1]:
                o = a - m[0]; b = m[2]
                return (b[o] << 8) | b[o+1]
            if a + 1 >= m[0] and a < m[1]:            # straddles the overlay
                return (self.read8(a) << 8) | self.read8(a+1)
        if 0x200000 <= a and a + 1 < 0x240000:
            o = a - 0x200000; w = self.wram
            return (w[o] << 8) | w[o+1]
        if 0x0C0000 <= a and a + 1 < 0x100000:
            o = a - 0x0C0000; w = self.wram
            return (w[o] << 8) | w[o+1]
        if 0xFF0000 <= a and a + 1 <= 0xFFFFFF:
            o = a - 0xFF0000; r = self.ram
            return (r[o] << 8) | r[o+1]
        return (self.read8(a) << 8) | self.read8(a+1)
    def read32(self, a):
        return (self.read16(a) << 16) | self.read16(a+2)
    def write8(self, a, v):
        a &= 0xFFFFFF; v &= 0xFF
        if 0x200000 <= a < 0x240000: self.wram[a-0x200000] = v; return
        if 0x0C0000 <= a < 0x100000: self.wram[a-0x0C0000] = v; return
        if a >= 0xFF0000: self.ram[a-0xFF0000] = v; return
        if a in (0xC00000,0xC00001,0xC00002,0xC00003):
            self.vdp.data_write16((v<<8)|v); return
        if a in (0xC00004,0xC00005,0xC00006,0xC00007):
            return  # byte ctrl writes unused
        self.log.append(("w8", a, v))
    def write16(self, a, v):
        a &= 0xFFFFFF; v &= 0xFFFF
        if 0x200000 <= a < 0x240000:
            o=a-0x200000; self.wram[o]=(v>>8)&0xFF; self.wram[o+1]=v&0xFF; return
        if 0x0C0000 <= a < 0x100000:
            o=a-0x0C0000; self.wram[o]=(v>>8)&0xFF; self.wram[o+1]=v&0xFF; return
        if a >= 0xFF0000:
            o=a-0xFF0000; self.ram[o]=(v>>8)&0xFF; self.ram[o+1]=v&0xFF; return
        if a in (0xC00000, 0xC00002): self.vdp.data_write16(v); return
        if a in (0xC00004, 0xC00006): self.vdp.ctrl_write16(v); return
        self.log.append(("w16", a, v))
    def write32(self, a, v):
        self.write16(a, (v>>16)&0xFFFF); self.write16(a+2, v&0xFFFF)

class CPU:
    def __init__(self, bus):
        self.b = bus
        self.d = [0]*8; self.a = [0]*8
        self.pc = 0; self.sr = 0x2700
        self.a[7] = 0xFFFD00
        self.stub_ret = None
        self.trace = collections.deque(maxlen=48)
    # ---- flags
    def setnz(self, v, size):
        m = {1:0x80,2:0x8000,4:0x80000000}[size]
        v &= _MASK[size]
        self.sr = (self.sr & ~0x0C) | (0x08 if v & m else 0) | (0x04 if v==0 else 0)
    def set_vc(self, v=0, c=0):
        self.sr = (self.sr & ~0x03) | (0x02 if v else 0) | (0x01 if c else 0)
    def set_x(self, x):
        self.sr = (self.sr & ~0x10) | (0x10 if x else 0)
    # ---- memory via size
    def rd(self, a, s): return {1:self.b.read8,2:self.b.read16,4:self.b.read32}[s](a)
    def wr(self, a, v, s): {1:self.b.write8,2:self.b.write16,4:self.b.write32}[s](a, v)
    def fetch16(self):
        v = self.b.read16(self.pc); self.pc += 2; return v
    def fetch32(self):
        v = self.b.read32(self.pc); self.pc += 4; return v
    # ---- effective address resolution: returns ('d'|'a'|'m', index_or_addr)
    def ea(self, mode, reg, size):
        if mode == 0: return ('d', reg)
        if mode == 1: return ('a', reg)
        if mode == 2: return ('m', self.a[reg])
        if mode == 3:
            addr = self.a[reg]
            inc = size if not (reg == 7 and size == 1) else 2
            self.a[reg] = (self.a[reg] + inc) & 0xFFFFFFFF
            return ('m', addr)
        if mode == 4:
            dec = size if not (reg == 7 and size == 1) else 2
            self.a[reg] = (self.a[reg] - dec) & 0xFFFFFFFF
            return ('m', self.a[reg])
        if mode == 5:
            d = self.fetch16()
            if d & 0x8000: d -= 0x10000
            return ('m', (self.a[reg] + d) & 0xFFFFFFFF)
        if mode == 6:
            ext = self.fetch16()
            d = ext & 0xFF
            if d & 0x80: d -= 0x100
            xr = (ext >> 12) & 7
            xv = self.a[xr] if ext & 0x8000 else self.d[xr]
            if not (ext & 0x800):
                xv &= 0xFFFF
                if xv & 0x8000: xv -= 0x10000
            else:
                xv = xv if xv < 0x80000000 else xv - 0x100000000
            return ('m', (self.a[reg] + d + xv) & 0xFFFFFFFF)
        if mode == 7:
            if reg == 0:
                v = self.fetch16()
                if v & 0x8000: v |= 0xFFFF0000
                return ('m', v & 0xFFFFFFFF)
            if reg == 1: return ('m', self.fetch32() & 0xFFFFFFFF)
            if reg == 2:
                base = self.pc; d = self.fetch16()
                if d & 0x8000: d -= 0x10000
                return ('m', (base + d) & 0xFFFFFFFF)
            if reg == 3:
                base = self.pc; ext = self.fetch16()
                d = ext & 0xFF
                if d & 0x80: d -= 0x100
                xr = (ext >> 12) & 7
                xv = self.a[xr] if ext & 0x8000 else self.d[xr]
                if not (ext & 0x800):
                    xv &= 0xFFFF
                    if xv & 0x8000: xv -= 0x10000
                return ('m', (base + d + xv) & 0xFFFFFFFF)
            if reg == 4:
                if size == 1: v = self.fetch16() & 0xFF
                elif size == 2: v = self.fetch16()
                else: v = self.fetch32()
                return ('i', v)
        raise Unimplemented(self.pc, 0, f"ea mode {mode}/{reg}")
    def get(self, t, x, size):
        if t == 'd':
            return self.d[x] & _MASK[size]
        if t == 'a':
            return self.a[x] & 0xFFFFFFFF
        if t == 'i': return x
        return self.rd(x, size)
    def put(self, t, x, v, size):
        v &= _MASK[size]
        if t == 'd':
            m = _KEEP[size]
            self.d[x] = (self.d[x] & m) | v
        elif t == 'a':
            if size == 2 and v & 0x8000: v |= 0xFFFF0000
            self.a[x] = v & 0xFFFFFFFF
        else:
            self.wr(x, v, size)
    # ---- condition codes
    def cond(self, c):
        n=(self.sr>>3)&1; z=(self.sr>>2)&1; v=(self.sr>>1)&1; cc=self.sr&1
        return [True, False, not (cc or z), cc or z, not cc, cc, not z, z,
                not v, v, not n, n, n==v, n!=v, (n==v) and not z,
                z or (n!=v)][c]
    def push32(self, v):
        self.a[7] = (self.a[7]-4) & 0xFFFFFFFF; self.b.write32(self.a[7], v)
    def pop32(self):
        v = self.b.read32(self.a[7]); self.a[7] = (self.a[7]+4) & 0xFFFFFFFF
        return v
    # ---- the step
    def step(self):
        self.pc &= 0xFFFFFF
        pc0 = self.pc
        self.trace.append(pc0)
        # BIOS call gates (low memory): log fn, default no-op return
        if self.pc < 0x10000:
            self.b.bios_calls.append((self.pc, self.d[0] & 0xFFFF, self.d[1] & 0xFFFF))
            fn = self.b.bios_stubs.get(self.pc)
            if fn: fn(self)
            self.pc = self.pop32()
            return
        if self.pc in self.b.peeks:
            self.b.peeks[self.pc](self)
        # library stubs
        if (self.pc & 0xFF0000) == 0xFF0000 and self.pc in self.b.stubs:
            self.b.stubs[self.pc](self)
            self.pc = self.pop32()
            return
        op = self.fetch16()
        try:
            self.exec_op(op, pc0)
        except Unimplemented:
            raise
        except StubCall:
            raise
    def exec_op(self, op, pc0):
        C = self
        hi = op >> 12
        # MOVE / MOVEA
        if hi in (1, 2, 3):
            size = {1:1, 3:2, 2:4}[hi]
            sm=(op>>3)&7; sr=op&7; dm=(op>>6)&7; dr=(op>>9)&7
            st,sx = C.ea(sm,sr,size); v = C.get(st,sx,size)
            dt,dx = C.ea(dm,dr,size)
            if dt=='a':
                C.put(dt,dx,v,size)
            else:
                C.put(dt,dx,v,size); C.setnz(v,size); C.set_vc(0,0)
            return
        if hi == 0:
            if op in (0x003C, 0x007C, 0x023C, 0x027C, 0x0A3C, 0x0A7C):
                imm = C.fetch16()
                if op & 0x40:   # to SR
                    if op & 0x0200: C.sr &= imm if (op & 0x0800)==0 else 0xFFFF
                    if (op & 0x0F00) == 0x0000: C.sr |= imm
                    elif (op & 0x0F00) == 0x0200: C.sr &= imm
                    else: C.sr ^= imm
                else:           # to CCR (low byte)
                    if (op & 0x0F00) == 0x0000: C.sr |= (imm & 0xFF)
                    elif (op & 0x0F00) == 0x0200: C.sr &= (imm & 0xFF) | 0xFF00
                    else: C.sr ^= (imm & 0xFF)
                return
            # ORI/ANDI/EORI/SUBI/ADDI/CMPI/BTST etc.
            if (op & 0xFF00) in (0x0000,0x0200,0x0A00,0x0400,0x0600,0x0C00):
                kind = (op>>9)&7
                size = [1,2,4][(op>>6)&3]
                if size==1: imm=C.fetch16()&0xFF
                elif size==2: imm=C.fetch16()
                else: imm=C.fetch32()
                t,x = C.ea((op>>3)&7,op&7,size); v=C.get(t,x,size)
                if kind==0: r=v|imm; C.put(t,x,r,size); C.setnz(r,size); C.set_vc(0,0)
                elif kind==1: r=v&imm; C.put(t,x,r,size); C.setnz(r,size); C.set_vc(0,0)
                elif kind==5: r=v^imm; C.put(t,x,r,size); C.setnz(r,size); C.set_vc(0,0)
                elif kind==2:  # SUBI
                    r=(v-imm)&_MASK[size]
                    C.put(t,x,r,size); C.setnz(r,size); C.set_vc(0, v<imm); C.set_x(v<imm)
                elif kind==3:  # ADDI
                    m=_MASK[size]
                    r=(v+imm)&m; C.put(t,x,r,size); C.setnz(r,size)
                    C.set_vc(0,(v+imm)>m); C.set_x((v+imm)>m)
                elif kind==6:  # CMPI
                    r=(v-imm)&_MASK[size]
                    C.setnz(r,size); C.set_vc(0, v<imm)
                return
            if (op & 0xF1C0) == 0x0100 or (op & 0xFFC0) == 0x0800:
                # BTST dn/#imm
                if (op & 0xFFC0) == 0x0800: bit = C.fetch16() & 0xFF
                else: bit = C.d[(op>>9)&7] & 0xFF
                m=(op>>3)&7; r=op&7
                if m==0:
                    v=C.d[r]; bit&=31
                else:
                    t,x=C.ea(m,r,1); v=C.get(t,x,1); bit&=7
                C.sr = (C.sr & ~0x04) | (0 if (v>>bit)&1 else 0x04)
                return
            if (op & 0xF1C0) in (0x01C0,0x0180,0x0140) or (op & 0xFFC0) in (0x08C0,0x0880,0x0840):
                # BSET/BCLR/BCHG
                if (op & 0xFF00) == 0x0800: bit = C.fetch16() & 0xFF; kind=(op>>6)&3
                else: bit = C.d[(op>>9)&7] & 0xFF; kind=(op>>6)&3
                m=(op>>3)&7; r=op&7
                if m==0:
                    bit&=31; v=C.d[r]
                    old=(v>>bit)&1
                    if kind==3: v|=(1<<bit)
                    elif kind==2: v&=~(1<<bit)
                    else: v^=(1<<bit)
                    C.d[r]=v&0xFFFFFFFF
                else:
                    t,x=C.ea(m,r,1); v=C.get(t,x,1); bit&=7
                    old=(v>>bit)&1
                    if kind==3: v|=(1<<bit)
                    elif kind==2: v&=~(1<<bit)
                    else: v^=(1<<bit)
                    C.put(t,x,v,1)
                C.sr=(C.sr&~0x04)|(0 if old else 0x04)
                return
        if hi == 4:
            if (op & 0xFFC0) == 0x4E80:   # JSR
                t,x = C.ea((op>>3)&7, op&7, 4)
                C.push32(C.pc)
                C.pc = x; return
            if (op & 0xFFC0) == 0x4EC0:   # JMP
                t,x = C.ea((op>>3)&7, op&7, 4)
                C.pc = x; return
            if (op & 0xFFF0) == 0x4E40:   # TRAP #n: BIOS syscall, log + continue
                C.b.bios_calls.append((0x10000 | (op & 0xF), C.d[0] & 0xFFFF,
                                       C.d[1] & 0xFFFF))
                return
            if op == 0x4E75: C.pc = C.pop32(); return           # RTS
            if op == 0x4E73: C.pc = C.pop32(); return           # RTE (approx)
            if op == 0x4E71: return                              # NOP
            if (op & 0xFFF0) == 0x4E60: return                   # MOVE USP
            if op == 0x4AFC: raise Unimplemented(pc0, op, "ILLEGAL")
            # MOVEM (ea mode >= 2 only — mode 0 is EXT, which this pattern
            # also matches; misrouting EXT here fetches a bogus mask word and
            # swallows the next instruction)
            if (op & 0xFB80) == 0x4880 and (op>>6)&3 in (2,3) and ((op>>3)&7) >= 2:
                size = 2 if ((op>>6)&1)==0 else 4
                mask = C.fetch16()
                to_mem = not (op & 0x400)
                m=(op>>3)&7; r=op&7
                if to_mem and m == 4:
                    addr = C.a[r]
                    for i in range(16):
                        if mask & (1<<i):
                            src = C.a[15-i-8] if (15-i)>=8 else C.d[15-i]
                            addr -= size; C.wr(addr, src, size)
                    C.a[r]=addr & 0xFFFFFFFF
                    return
                if not to_mem:
                    if m == 3:
                        addr=C.a[r]
                        for i in range(16):
                            if mask & (1<<i):
                                v=C.rd(addr,size)
                                if size==2 and v&0x8000: v|=0xFFFF0000
                                if i>=8: C.a[i-8]=v&0xFFFFFFFF
                                else: C.d[i]=v&0xFFFFFFFF
                                addr+=size
                        C.a[r]=addr & 0xFFFFFFFF
                        return
                    t,addr = C.ea(m,r,size)
                    for i in range(16):
                        if mask & (1<<i):
                            v=C.rd(addr,size)
                            if size==2 and v&0x8000: v|=0xFFFF0000
                            if i>=8: C.a[i-8]=v&0xFFFFFFFF
                            else: C.d[i]=v&0xFFFFFFFF
                            addr+=size
                    return
                if to_mem:
                    t,addr=C.ea(m,r,size)
                    for i in range(16):
                        if mask & (1<<i):
                            src=C.a[i-8] if i>=8 else C.d[i]
                            C.wr(addr,src,size); addr+=size
                    return
            if (op & 0xF1C0) == 0x41C0:   # LEA
                t,x = C.ea((op>>3)&7, op&7, 4)
                C.a[(op>>9)&7] = x & 0xFFFFFFFF; return
            if (op & 0xFF00) == 0x4A00:   # TST
                size=[1,2,4][(op>>6)&3]
                t,x=C.ea((op>>3)&7,op&7,size); v=C.get(t,x,size)
                C.setnz(v,size); C.set_vc(0,0); return
            if (op & 0xFF00) == 0x4200:   # CLR
                size=[1,2,4][(op>>6)&3]
                t,x=C.ea((op>>3)&7,op&7,size); C.put(t,x,0,size)
                C.setnz(0,size); C.set_vc(0,0); return
            if (op & 0xFFC0) == 0x4840 and (op>>3)&7==0:  # SWAP
                r=op&7; v=C.d[r]
                C.d[r]=((v>>16)&0xFFFF)|((v&0xFFFF)<<16)
                C.setnz(C.d[r],4); C.set_vc(0,0); return
            if (op & 0xFFC0) == 0x4840:   # PEA
                t,x=C.ea((op>>3)&7,op&7,4); C.push32(x); return
            if (op & 0xFFC0) == 0x44C0:   # MOVE <ea>,CCR
                t,x=C.ea((op>>3)&7,op&7,2); v=C.get(t,x,2)
                C.sr=(C.sr&0xFF00)|(v&0xFF); return
            if (op & 0xFFC0) == 0x46C0:   # MOVE <ea>,SR
                t,x=C.ea((op>>3)&7,op&7,2); C.sr=C.get(t,x,2); return
            if (op & 0xFFC0) == 0x40C0:   # MOVE SR,<ea>
                t,x=C.ea((op>>3)&7,op&7,2); C.put(t,x,C.sr,2); return
            if (op & 0xFF00) == 0x4400:   # NEG
                size=[1,2,4][(op>>6)&3]
                t,x=C.ea((op>>3)&7,op&7,size); v=C.get(t,x,size)
                m=_MASK[size]
                r=(-v)&m; C.put(t,x,r,size); C.setnz(r,size)
                C.set_vc(0, v!=0); C.set_x(v!=0); return
            if (op & 0xFF00) == 0x4600:   # NOT
                size=[1,2,4][(op>>6)&3]
                t,x=C.ea((op>>3)&7,op&7,size); v=C.get(t,x,size)
                m=_MASK[size]
                r=(~v)&m; C.put(t,x,r,size); C.setnz(r,size); C.set_vc(0,0); return
            if (op & 0xFFB8) == 0x4880:   # EXT
                r=op&7
                if (op>>6)&1:
                    v=C.d[r]&0xFFFF
                    if v&0x8000: v|=0xFFFF0000
                    C.d[r]=v; C.setnz(v,4)
                else:
                    v=C.d[r]&0xFF
                    if v&0x80: v|=0xFF00
                    C.d[r]=(C.d[r]&0xFFFF0000)|v; C.setnz(v,2)
                C.set_vc(0,0); return
            if (op & 0xFFF8) == 0x4E50:   # LINK
                r=op&7; d=C.fetch16()
                if d&0x8000: d-=0x10000
                C.push32(C.a[r]); C.a[r]=C.a[7]; C.a[7]=(C.a[7]+d)&0xFFFFFFFF; return
            if (op & 0xFFF8) == 0x4E58:   # UNLK
                r=op&7; C.a[7]=C.a[r]; C.a[r]=C.pop32(); return
        if hi == 5:
            if (op & 0xF0C0) == 0x50C0:
                m=(op>>3)&7
                if m == 1:  # DBcc
                    c=(op>>8)&0xF; r=op&7; disp=C.fetch16()
                    if disp&0x8000: disp-=0x10000
                    if not C.cond(c):
                        cnt=(C.d[r]&0xFFFF)
                        cnt=(cnt-1)&0xFFFF
                        C.d[r]=(C.d[r]&0xFFFF0000)|cnt
                        if cnt != 0xFFFF:
                            C.pc = (C.pc - 2 + disp) & 0xFFFFFFFF
                    return
                # Scc
                c=(op>>8)&0xF; t,x=C.ea(m,op&7,1)
                C.put(t,x,0xFF if C.cond(c) else 0,1); return
            # ADDQ/SUBQ
            data=(op>>9)&7 or 8
            size=[1,2,4][(op>>6)&3]
            m=(op>>3)&7; r=op&7
            t,x=C.ea(m,r,size); v=C.get(t,x,size)
            if t=='a':
                if op & 0x100: C.a[r]=(C.a[r]-data)&0xFFFFFFFF
                else: C.a[r]=(C.a[r]+data)&0xFFFFFFFF
                return
            msk=_MASK[size]
            if op & 0x100:
                rr=(v-data)&msk; c=v<data
            else:
                rr=(v+data)&msk; c=(v+data)>msk
            C.put(t,x,rr,size); C.setnz(rr,size); C.set_vc(0,c); C.set_x(c)
            return
        if hi == 6:
            c=(op>>8)&0xF; disp=op&0xFF
            if disp==0:
                disp=C.fetch16()
                if disp&0x8000: disp-=0x10000
                base=C.pc-2
            elif disp==0xFF:
                disp=C.fetch32(); base=C.pc-4
            else:
                if disp&0x80: disp-=0x100
                base=C.pc
            if c==1:  # BSR
                C.push32(C.pc)
                C.pc=(base+disp)&0xFFFFFFFF; return
            if c==0 or C.cond(c):
                C.pc=(base+disp)&0xFFFFFFFF
            return
        if hi == 7:   # MOVEQ
            r=(op>>9)&7; v=op&0xFF
            if v&0x80: v|=0xFFFFFF00
            C.d[r]=v&0xFFFFFFFF; C.setnz(v,4); C.set_vc(0,0); return
        if hi == 8:
            if (op & 0x1C0) == 0x0C0:  # DIVU
                t,x=C.ea((op>>3)&7,op&7,2); s=C.get(t,x,2)
                r=(op>>9)&7; v=C.d[r]
                if s:
                    q=v//s; rem=v%s
                    if q<=0xFFFF:
                        C.d[r]=((rem&0xFFFF)<<16)|(q&0xFFFF); C.setnz(q,2); C.set_vc(0,0)
                return
            # OR
            size=[1,2,4][(op>>6)&3]
            r=(op>>9)&7; m=(op>>3)&7; er=op&7
            if op & 0x100:
                t,x=C.ea(m,er,size); v=C.get(t,x,size)|C.d[r]
                C.put(t,x,v,size)
            else:
                t,x=C.ea(m,er,size); v=C.get(t,x,size)|(C.d[r]&_MASK[size])
                C.put('d',r,v,size)
            C.setnz(v,size); C.set_vc(0,0); return
        if hi == 9 or hi == 13:  # SUB/ADD
            sub = hi == 9
            r=(op>>9)&7; om=(op>>6)&7; m=(op>>3)&7; er=op&7
            if om in (3,7):   # ADDA/SUBA
                size = 2 if om==3 else 4
                t,x=C.ea(m,er,size); v=C.get(t,x,size)
                if size==2 and v&0x8000: v|=0xFFFF0000
                if sub: C.a[r]=(C.a[r]-v)&0xFFFFFFFF
                else: C.a[r]=(C.a[r]+v)&0xFFFFFFFF
                return
            size=[1,2,4][om&3]
            msk=_MASK[size]
            if om<3:
                t,x=C.ea(m,er,size); s=C.get(t,x,size); dv=C.d[r]&msk
                rr=(dv-s)&msk if sub else (dv+s)&msk
                c = dv<s if sub else (dv+s)>msk
                C.put('d',r,rr,size)
            else:
                t,x=C.ea(m,er,size); dv=C.get(t,x,size); s=C.d[r]&msk
                rr=(dv-s)&msk if sub else (dv+s)&msk
                c = dv<s if sub else (dv+s)>msk
                C.put(t,x,rr,size)
            C.setnz(rr,size); C.set_vc(0,c); C.set_x(c)
            return
        if hi == 11:  # CMP/CMPA/EOR
            r=(op>>9)&7; om=(op>>6)&7; m=(op>>3)&7; er=op&7
            if om in (3,7):
                size=2 if om==3 else 4
                t,x=C.ea(m,er,size); v=C.get(t,x,size)
                if size==2 and v&0x8000: v|=0xFFFF0000
                rr=(C.a[r]-v)&0xFFFFFFFF
                C.setnz(rr,4); C.set_vc(0,(C.a[r]&0xFFFFFFFF)<(v&0xFFFFFFFF)); return
            if om<3:  # CMP
                size=[1,2,4][om]
                msk=_MASK[size]
                t,x=C.ea(m,er,size); s=C.get(t,x,size); dv=C.d[r]&msk
                rr=(dv-s)&msk
                C.setnz(rr,size); C.set_vc(0,dv<s); return
            # EOR
            size=[1,2,4][om&3]
            t,x=C.ea(m,er,size); v=C.get(t,x,size)^(C.d[r]&_MASK[size])
            C.put(t,x,v,size); C.setnz(v,size); C.set_vc(0,0); return
        if hi == 12:
            if (op & 0x1C0) == 0x0C0:   # MULU
                t,x=C.ea((op>>3)&7,op&7,2); s=C.get(t,x,2)
                r=(op>>9)&7
                C.d[r]=((C.d[r]&0xFFFF)*s)&0xFFFFFFFF
                C.setnz(C.d[r],4); C.set_vc(0,0); return
            if (op & 0x1F8) == 0x140 or (op & 0x1F8) == 0x148 or (op & 0x1F8) == 0x188:
                # EXG
                r=(op>>9)&7; er=op&7; md=op&0xF8
                if md==0x40: C.d[r],C.d[er]=C.d[er],C.d[r]
                elif md==0x48: C.a[r],C.a[er]=C.a[er],C.a[r]
                else: C.d[r],C.a[er]=C.a[er],C.d[r]
                return
            # AND
            size=[1,2,4][(op>>6)&3]
            r=(op>>9)&7; m=(op>>3)&7; er=op&7
            msk=_MASK[size]
            if op & 0x100:
                t,x=C.ea(m,er,size); v=C.get(t,x,size)&(C.d[r]&msk)
                C.put(t,x,v,size)
            else:
                t,x=C.ea(m,er,size); v=C.get(t,x,size)&(C.d[r]&msk)
                C.put('d',r,v,size)
            C.setnz(v,size); C.set_vc(0,0); return
        if hi == 14:  # shifts
            if (op>>6)&3 == 3:   # memory shift (word, 1 bit)
                kind=(op>>9)&3; left=op&0x100
                t,x=C.ea((op>>3)&7,op&7,2); v=C.get(t,x,2)
                if left:
                    c=(v>>15)&1; v=(v<<1)&0xFFFF
                    if kind==3: v|=c
                else:
                    c=v&1; v>>=1
                    if kind==3 and c: v|=0x8000
                C.put(t,x,v,2); C.setnz(v,2); C.set_vc(0,c)
                if kind in (0,1): C.set_x(c)
                return
            size=[1,2,4][(op>>6)&3]
            cnt=(op>>9)&7
            if op & 0x20: cnt=C.d[cnt]&63
            else: cnt=cnt or 8
            r=op&7; v=C.d[r]&_MASK[size]
            kind=(op>>3)&3; left=op&0x100
            msk=_MASK[size]
            bits={1:8,2:16,4:32}[size]
            c=0
            for _ in range(cnt):
                if left:
                    c=(v>>(bits-1))&1; v=(v<<1)&msk
                    if kind==3: v|=c            # ROL
                else:
                    c=v&1; v>>=1
                    if kind==3 and c: v|=1<<(bits-1)   # ROR
                    if kind==0 and (C.d[r]>>(bits-1))&1 and False: pass
            C.put('d',r,v,size); C.setnz(v,size); C.set_vc(0,c)
            if kind in (0,1): C.set_x(c)
            return
        raise Unimplemented(pc0, op)
