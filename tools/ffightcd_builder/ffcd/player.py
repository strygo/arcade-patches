"""Pure disc-driven scene player.

Drives ENDING chunk5's OWN dispatcher (0xC0008) per frame in m68k. The
disc's handler code supplies all opcode semantics; MAINSYS (loaded at its
0xFF0000 link base) supplies the resident library. Anything unresolvable
is trapped, logged, and stubbed explicitly.
"""
import struct, collections
from . import disc, m68k

SENTINEL = 0x00DEAD00

class FrameWait(Exception):
    def __init__(self, frames, resume_pc):
        self.frames = frames; self.resume_pc = resume_pc

PROFILE = {
    "jp": dict(timer=0xFF9854, state=0xFF985C, stage=0xFF985A,
               req=0xFF9CF4, wait_frames=0xFF0744, wait_vbl=0xFF1DD8,
               done_wait=0xFF108E, req_wait=0xFF1E60,
               sub_ack=0xFF09F4, tc_recv=0xFF1014),
    "us": dict(timer=0xFF9856, state=0xFF985E, stage=0xFF985C,
               req=0xFF9CF6, wait_frames=0xFF0744, wait_vbl=0xFF1DF2,
               done_wait=0xFF108E, req_wait=0xFF1E7A,
               sub_ack=0xFF09F4, tc_recv=0xFF1014),
}

# ---- the resident main library, sourced FROM THE DISC.
# Sourcing it from the disc keeps every VM-derived artifact free of captured
# data; a captured 64 KB main-RAM image would make each one capture-derived.
# The boot sector's IP is its own loader -- its tail is
# lea $FFFF0600,a1; move.w #$1FFF,d6; move.l (a0)+,(a1)+; dbf
# jmp $FFFF0600
# so the IP sits at 0xFF0000 and MAINSYS.BIN is copied to 0xFF0600.
# ---- CD clock.  Two terms, a SLOPE and an OFFSET:
#
# RATE.  Ticking `cd_frames += 1.25` is 75 CD-frames/s over a NOMINAL 60 Hz
# field.  The Mega Drive's real field rate is 53693175/(3420*262) =
# 59.922743 Hz, so the physical tick is 75/59.922743 = 1.2516115875 and 1.25
# runs slow by 0.1289%/frame.  Measured densely, the capture-to-VM offset
# WALKS across every sweep at 1.25 (jp O 3709->3700, us O 1434->1426, jp E
# 3727->3724) and goes uniform at the physical rate.
#
# The literal is 1.2516115875, not 1.2516617: 1.2516617 is a digit
# transposition (it reads as "75/59.92275" but that quotient is 1.2516116),
# 40 ppm fast -- +0.31 vblanks across a whole 7700-frame sweep.
#
# That sub-frame error is NOT invisible, because it is quantised -- an event
# sitting near a frame boundary tips to the other side, and each tip changes
# a run of frames until the next re-sync.  The correction moves: jp ending 0
# frames (bit-identical), jp opening 65, us opening 788, us ending 254.  The
# us ending also runs one frame shorter (see clock_hold).  Steve reviewed the
# us ending against tr24 and approved it.
#
# CD_PHASE.  The timecode's sub-frame alignment at clock zero: the audio track
# does not start on a vblank boundary, so this term is REAL, but its VALUE is
# NOT settled and is deliberately left at 0.  A measured 0.65 makes CODY
# clean against the Guy/Cody track, but that objective is ENTANGLED with an
# unexplained one-frame sprite offset: 0.65 makes Cody exact but puts Guy's
# mouth wrong over 20 rows, and only an unexplained sprites-from-k+1 pairing
# makes both clean at once.  A phase fitted through that entanglement is not
# evidence.
# Re-determine it against an objective that does not involve sprites.
#
# CHANGING EITHER INVALIDATES every clock-gated constant downstream --
# vmconvert's ALIGN_OFFSET, make_esnaps' first-full-bright, export_layers'
# FRAME_OFF, the ending lip-sync anchors.  Re-derive, never carry over.
CD_PHASE = 0.0

IP_LBA, IP_OFF, IP_LEN = 0, 0x200, 0x600     # boot sector, Initial Program
LIB_BASE = 0xFF0600                          # the copy loop's destination
VDP_SHADOW = 0xFFFDB4                        # the register block the ISR replays
# ISO9660 directory entries for MAINSYS.BIN (same LBA both regions)
MAINSYS = {"jp": (2273, 31338), "us": (2273, 31420)}
# MAIN1.BIN -- the resident MAIN-side module the stage dispatcher reads at
# 0x207800.  FF12A8 fetches its handler pointer from there for
# every POSITIVE TICK_LIST entry; only the bit7 entries come from the code
# chunk at 0x200000.  Entering at $985A = 4 used just the chunk dispatcher,
# which is why the player got this far without it -- and why leaving stage 4
# for stage 5 at the ending's first scene boundary jumped through garbage.
# MAIN1, not MAIN2: both are based at 0x207800, but MAIN2's stage-5 handler
# runs off its own end (207E9A jsr $20b88a, past 0x209EA4), while MAIN1's
# entry 0x14 -> 0x208AEA lands inside its own span.  Verified by running it.
MAIN1 = {"jp": (2433, 5972), "us": (2433, 6190)}
# MAINSYS's own VDP-register installer, whose operands name its default table:
# lea (pc,+d),a0; lea $FFFFFDB4.w,a1; lea $00C00004,a2; moveq #n,d0
# move.w (a0)+,d1; move.w d1,(a1)+; move.w d1,(a2); dbf d0
REG_INSTALLER = bytes.fromhex("43f8fdb445f900c00004")


def mainsys_regtable(ms: bytes) -> bytes:
    """MAINSYS's boot-default VDP register block, read out of its installer.

    Parsed from the routine's own operands rather than hardcoded, so the
    table's location and length come from the disc image in hand.
    """
    i = ms.find(REG_INSTALLER)
    if i < 0 or ms.count(REG_INSTALLER) != 1:
        raise RuntimeError("MAINSYS VDP-register installer not found")
    disp = int.from_bytes(ms[i - 2:i], "big")       # the lea (pc,+d),a0 operand
    tbl = (i - 4) + 2 + disp
    n = ms[i + len(REG_INSTALLER) + 1] + 1          # moveq #n,d0 -> n+1 words
    blk = ms[tbl:tbl + 2 * n]
    if len(blk) != 2 * n or any(blk[2 * k] != 0x80 + k for k in range(n)):
        raise RuntimeError("MAINSYS VDP-register table did not decode")
    return blk

class ScriptPlayer:
    def __init__(self, region="jp", bundle="E", parts=None, scenes=None):
        self.P = PROFILE[region]
        img = disc.JP_IMG if region == "jp" else disc.US_IMG
        raw = disc.read_iso_file(img, *disc.EXTENTS[region][bundle])
        self.chunks = disc.load_chunks(raw)
        self.table = disc.chunk_table(raw)
        # ---- part-entry Sub-CPU handshake latency (clock phase).
        # At INIT the Sub reads the whole scene bundle off the disc (1x
        # drive, 75 x 2048B sectors/s) BEFORE it can seek/PLAY the audio
        # track -- until then the timecode receiver (0xFF1014) has no valid
        # status and $9854 holds 0, so every clock-gated event (fades, cel
        # records, scene-out gates) waits.  Ticking the clock from f0 instead
        # runs all clock-gated content early by the bundle-read time while
        # frame-driven anims keep length -- the measured clock-phase drift the
        # hold corrects.  Cost per part entry, no free parameters:
        # hold = ceil(file_bytes / 2048) sectors * (60/75) vblanks
        # (us E 125 sect -> 100 vbl; jp E 123 -> 98; jp O 149 -> 119;
        # us O 146 -> 116).  Evidence: US ending VO fit needs 95 +/- 4 vbl
        # (blocks 2-6 land within +/-4 at H=95, +/-9 at H=100); the live e6
        # ladder (ramdump_e6 $9854 vs frame) is
        # strictly linear at 1.25 cdf/vbl mid-part (clock-zero f3824 +/- 2
        # from f4100-f6200), so the whole cost sits at the part head; the
        # jp opening capture-vs-VM DTW ladder is net-flat (~3820 start to
        # end), so scene-chunk rotations add no cumulative cost (they are
        # Sub PRG-RAM -> Word RAM stagings, no disc access).
        # CD frames -> vblanks at the TRUE field rate, not `* 4 // 5` (75/60,
        # the same nominal-60 Hz assumption): integer truncation absorbs the
        # 0.13% error for three of the four discs, but us E crosses a boundary
        # (100 -> 99 vblanks), which would start the whole US ending timeline
        # one frame earlier.
        self.clock_hold = int((-(-len(raw) // 2048)) * (53693175 / (3420 * 262)) / 75)
        self.bus = m68k.Bus()
        self.cpu = m68k.CPU(self.bus)
        # part code chunks + scene-chunk rotation (bundle layouts measured
        # from the live machines: E = code c5, scenes c1-c3;
        # O = code parts c11..c14, scenes c3..c10)
        self.PART_CODE = parts if parts else ([5] if bundle == "E" else [11, 12, 13, 14])
        # scene-chunk rotation order, proven against the capture oracles:
        # JP opening plays map(c3) duo(c4) bar(c5) sketch(c10) ...;
        # US swaps c4/c5 (bar second, Damnd-wipe third)
        if scenes:
            self.SCENES = scenes
        elif bundle == "E":
            self.SCENES = [1, 2, 3]
        else:
            self.SCENES = ([3, 4, 5, 10, 6, 7, 8, 9] if region == "jp"
                           else [3, 5, 4, 10, 6, 7, 8, 9])
        self.part = 0
        self.bus.load(0x0C0000, self.chunks[self.PART_CODE[0]])
        self.staged = -1         # index into SCENES; fn-0x0C requests advance
        self.bus.load(0x0C3000, self.chunks[self.SCENES[0]])
        # boot-time VDP registers (from the game's init; ground-truth values)
        for i, v in enumerate([4,0x64,0,4,1,12,0,0,0,0,0,0,0x81,7,0,2,1,0,0,0,0,0,0,0]):
            self.bus.vdp.regs[i] = v
        # ENDING pre-roll art (chunk 0 = the ending's INIT module, staged at
        # 0xC0000 before the c5 dispatcher; its init uploads shared actor art
        # once and nothing rewrites it — verified identical in the live e6
        # VRAM at scene 1 and scene 3).  Our direct list-B boot skips c0, so
        # replay its uploads here (disc bytes only; dests from the live
        # oracle).  Without these the guy/cody two-shot's phase-2 cels point
        # at blank tiles.
        if bundle == "E":
            vr = self.bus.vdp.vram
            c0, c1 = self.chunks[0], self.chunks[1]
            vr[0x3000:0x3E00] = c0[0x0000:0x0E00]
            vr[0x1400:0x1800] = c0[0x0E00:0x1200]
            vr[0x1B00:0x1C00] = c0[0x1280:0x1380]
            vr[0x1000:0x1400] = c1[0x5350:0x5450] * 4   # letterbox filler tiles
        self.fade_ctr = 0
        self.timer_f = 0.0   # $9854 ticks at ~750Hz (12.5/frame, CD-clock)
        # ---- resident main library, FROM THE DISC (was a captured RAM image).
        # Validated against that image: the IP is byte-identical over
        # 0xFF0000..0xFF0600, and MAINSYS at 0xFF0600 differs in exactly the
        # four bytes of the vblank-hook vector at 0xFF0AD0 -- which we
        # overwrite below anyway, and where the FILE holds the pristine value
        # and the dump held the opening's stale hook.
        self.bus.load(0xFF0000, disc.read_iso_file(
            img, IP_LBA, IP_OFF + IP_LEN)[IP_OFF:])
        ms = disc.read_iso_file(img, *MAINSYS[region])
        self.bus.load(LIB_BASE, ms)
        # The resident MAIN-side module (see MAIN1 above).  Held as a CPU-side
        # OVERLAY, not written into word RAM: on hardware it sits in MAIN's
        # 1M-mode bank while the Sub fills its own with the scene art, and
        # this Bus folds both onto one buffer -- writing it would punch a hole
        # through the staged scene chunk (measured: exactly the corrupt band
        # in the farewell's second half).  VDP DMA reads the art beneath it.
        self.bus.load_module(0x207800,
                             disc.read_iso_file(img, *MAIN1[region]))
        # The VDP register shadow the vblank ISR replays every frame: MAINSYS
        # installs its own default block here at boot (0xFF069C).  Nothing
        # reads the rest of 0xFFFD00..0xFFFE00 in any cutscene run -- the
        # exception-vector jmp table there is BIOS-built and dead for us.
        self.bus.load(VDP_SHADOW, mainsys_regtable(ms))
        # ...then DISP.  This is a MACHINE BOUNDARY, not disc data: we enter
        # at the scene stage, where the boot and opening have already turned
        # the display on, while MAINSYS's boot default leaves reg1 bit 6
        # clear.  It is the ONLY bit of the 64 KB main-RAM image that the disc
        # does not supply (reg3's window base and reg18 also differ live, and
        # both are verified not to matter: byte-identical sweeps without them).
        self.bus.write8(VDP_SHADOW + 3, self.bus.read8(VDP_SHADOW + 3) | 0x40)
        # vblank-hook var (inside lib space): the library's own default hook
        # points at code the running part replaces; park it on a low gate
        # address (pc<0x10000 auto-RTS in m68k)
        # until the running part's init installs its own hook.  (0x20013E is
        # only an RTS when c5 sits at 0xC0000 — parts=[0,5] breaks that.)
        self.bus.write32(0xFF0AD0, 0x000BBA)
        # 0xFF0744 = wait-d0-frames (yields to the main loop): suspend/resume
        self.bus.stubs[self.P['wait_frames']] = self._wait_stub
        self.bus.stubs[self.P['wait_vbl']] = self._wait_vbl_stub
        # service-completion wait (tst.b (a0); beq .-4) at its real address:
        # our VDP applies services synchronously, so complete instantly
        self.bus.peeks[self.P['done_wait']] = lambda c: c.b.write8(c.a[0], 1)
        # $9CF4 work-request wait: yield a frame while THE BIT is pending.
        # it is one bit, not any bit.  The real code is
        # FF1E5A  bset.b #$1,$9cf4.w      raise the request
        # FF1E60  btst.b #$1,$9cf4.w      spin until THAT bit clears
        # serviced by the ISR at FF2FEA (bclr.b #$1,$9cf4.w); US is the same
        # shape on $9CF6.  Testing the whole byte behaved through the ending's
        # first scene, where no other bit stays up, and hung the main loop
        # forever after the scene boundary -- state 2, nothing painted, VDP
        # writes down from ~21/frame to ~3.
        req = self.P["req"]
        def _yield_9cf4(c):
            if c.b.read8(req) & 0x02:
                raise FrameWait(1, c.pc)
        self.bus.peeks[self.P["req_wait"]] = _yield_9cf4
        # 0xFF09F4: wait-for-Sub-ack through a reg slot the running system
        # patches (the library ships it unpatched): instant-complete
        self.bus.stubs[self.P['sub_ack']] = lambda c: None
        # 0xFF1014: CD-timecode receiver (reads the Sub's status words).
        # There is no Sub CPU here, so the status block is never valid; our
        # virtual CD clock owns $9854 instead.
        self.bus.stubs[self.P['tc_recv']] = lambda c: None
        self.bus.bios_stubs[0x35C] = self._gate_35c
        self.suspended = None   # (wake_frame, resume_pc)
        self.script_resume = None   # cutscene-only part-end handling
        self.main_pc = None      # persistent mainloop context
        self.main_wake = 0
        self.frame = 0
        self.dma_log = []
        self.pal_log = []
        self.fade_log = []
    def _gate_35c(self, cpu):
        """BIOS gate 0x35C: post (fn, arg) into the ISR mailbox $FDF4/$FDF6.
        fn 0xFFFF = idle (timecode update); non-table fns are BIOS-internal.
        fn 0x0C = next-scene resource load (Sub side): rotate the scene
        chunk staged at 0xC3000 (c1 -> c2 -> c3, live-machine-proven)."""
        fn = cpu.d[0] & 0xFFFF
        if fn == 0x0C:
            nxt = self.staged + 1
            if nxt < len(self.SCENES):
                self.bus.load(0x0C3000, self.chunks[self.SCENES[nxt]])
                self.staged = nxt
        if fn == 0xFFFF or ((fn & 3) == 0 and fn <= 0x3C):
            self.bus.write16(0xFFFDF4, fn)
            self.bus.write16(0xFFFDF6, cpu.d[1] & 0xFFFF)
        if (fn & 3) == 0 and 4 <= fn <= 0x40:
            # request bits consumed by the ISR stanzas / vblank hook
            bit = (fn >> 2) - 1
            addr = self.P["req"] + (bit >> 3)
            self.bus.write8(addr, self.bus.read8(addr) | (1 << (bit & 7)))
    def _wait_stub(self, cpu):
        frames = max(1, cpu.d[0] & 0xFFFF)
        raise FrameWait(frames, cpu.pop32())
    def _wait_vbl_stub(self, cpu):
        # 0xFF1DD8: spin until $9CF2 changes, then `move.w (a7)+,d0; rts`
        d0w = cpu.b.read16(cpu.a[7]); cpu.a[7] = (cpu.a[7] + 2) & 0xFFFFFFFF
        cpu.d[0] = (cpu.d[0] & 0xFFFF0000) | d0w
        raise FrameWait(1, cpu.pop32())
    def _load_req_stub(self, cpu):
        """Async resource-load request (d0 = load code). The Sub stages the
        resources; completion clears $8EA8. Our staging is already resident,
        so complete immediately (codes logged for refinement)."""
        self.pal_log.append(("loadreq", self.frame, cpu.d[0] & 0xFFFF))
        self.bus.write8(0xFF8EA8, 0)
    def _install_stub(self, cpu):
        src = cpu.a[0] & 0xFFFFFF; dst = cpu.a[1] & 0xFFFFFF
        for i in range(0x60):
            self.bus.write8(dst + i, self.bus.read8(src + i))
        self.pal_log.append(("install", self.frame, src, dst))
    def _fade_stub(self, cpu):
        mode = self.bus.read8(0x202130)
        self.fade_log.append((self.frame, cpu.d[0] & 0xFFFF,
                              mode, self.bus.read8(0x202134)))
        self.fade_ctr = 0
        if not (mode & 0x80):
            # fade-IN: target snapshot is at 0xFF9474; start live shadow black
            for i in range(64): self.bus.write16(0xFFFB80 + i*2, 0)
    def _fade_step(self):
        """Palette fade engine: active while $202130 bit5; every $202134
        frames each live-shadow channel steps toward black; clears bit5 done."""
        mode = self.bus.read8(0x202130)
        if not (mode & 0x20): return
        speed = max(1, self.bus.read8(0x202134))
        self.fade_ctr += 1
        if self.fade_ctr < speed: return
        self.fade_ctr = 0
        fade_in = not (mode & 0x80)
        all_done = True
        for i in range(64):
            w = self.bus.read16(0xFFFB80 + i*2)
            tgt = self.bus.read16(0xFF9474 + i*2) if fade_in else 0
            nw = 0
            for shift in (1, 5, 9):
                ch = (w >> shift) & 7
                tc = (tgt >> shift) & 7
                if ch < tc: ch += 1
                elif ch > tc: ch -= 1
                nw |= ch << shift
            if nw != (tgt & 0x0EEE): all_done = False
            self.bus.write16(0xFFFB80 + i*2, nw)
        if all_done:
            self.bus.write8(0x202130, mode & ~0x20)
    def _dma_stub(self, cpu):
        cmd = cpu.d[0] & 0xFFFFFFFF
        src = cpu.d[1] & 0xFFFFFF
        length = cpu.d[2] & 0xFFFF
        v = self.bus.vdp
        v.regs[1] |= 0x10; v.regs[15] = 2
        v.regs[19] = length & 0xFF; v.regs[20] = (length >> 8) & 0xFF
        sw = src >> 1
        v.regs[21] = sw & 0xFF; v.regs[22] = (sw >> 8) & 0xFF
        v.regs[23] = (sw >> 16) & 0x7F
        v.ctrl_write16((cmd >> 16) & 0xFFFF)
        v.ctrl_write16((cmd & 0xFFFF) | 0x80)
        self.dma_log.append((self.frame, cmd, src, length))
    def _pal_stub(self, cpu):
        src = cpu.a[0] & 0xFFFFFF
        dst = cpu.a[1] & 0xFFFFFF
        self.pal_log.append((self.frame, src, dst))
        for i in range(64):
            w = self.bus.read16(src + i*2)
            if not (w & 0x8000):
                self.bus.write16(dst + i*2, w)
        self.stubs_hit = collections.Counter()
        self.ga_writes = []
    def call(self, entry, max_steps=400_000):
        c = self.cpu
        c.push32(SENTINEL)
        c.pc = entry
        steps = 0
        while c.pc != SENTINEL:
            c.step()
            steps += 1
            if steps > max_steps:
                raise RuntimeError(
                    f"runaway pc={c.pc:06x} state={self.bus.read16(0xFF985C):04x}")
        return steps
    # per-frame tick router (MAINSYS boot task, list B at lib file 0xFE8):
    # d1 = list[$985A]; positive -> MAIN2 header fn, bit7 -> chunk header fn
    TICK_LIST = [0x00, 0x0C, 0x10, 0x08, 0x80, 0x14, 0x08, 0x84, 0x18, 0x04]
    def tick(self, max_steps=400_000):
        # Bank select for the resident module.  TICK_LIST entries
        # with bit 7 set dispatch through the CODE CHUNK at 0x200000 and those
        # scenes read their own art out of 0x207800..; every other entry
        # dispatches through the module there.  Follow $985A rather than
        # leaving the overlay permanently on, which diverges the ending's first
        # scene from frame 3.
        st = self.bus.read16(self.P["stage"])
        self.bus.module_on = not (self.TICK_LIST[st] & 0x80) \
            if 0 <= st < len(self.TICK_LIST) else False
        # resume a frame-waiting context first
        if self.suspended is not None:
            wake, pc = self.suspended
            if self.frame < wake:
                return 0
            self.suspended = None
            try:
                return self._run_from(pc, max_steps)
            except FrameWait as w:
                self.suspended = (self.frame + w.frames, w.resume_pc)
                return 0
        # mainloop: the game's task scheduler (0xFF0894) runs preemptively —
        # persistent CPU context, sliced per frame; FrameWait fast-forwards
        c = self.cpu
        if self.main_pc is None:
            # enter the list-B stage loop directly at the scene stage:
            # $985A=4 -> chunk header[0] = chunk5's dispatcher; $985C=0 so
            # chunk5's own state-0 init (script ptr, cel table, hook) runs
            self.bus.write16(self.P["stage"], 4)
            self.bus.write16(self.P["state"], 0)
            c.pc = 0xFF15D0
            c.a[7] = 0xFFF700          # mainloop stack
        else:
            if self.frame < self.main_wake:
                return 0
            c.pc = self.main_pc
        steps = 0
        hard_cap = max_steps * 8
        try:
            while steps < max_steps or ((c.sr & 0x0700) >= 0x0600
                                        and steps < hard_cap):
                # never preempt inside an interrupt-masked section (VDP
                # copies etc.) — run to a safe point first
                c.step()
                steps += 1
        except FrameWait as w:
            self.main_wake = self.frame + w.frames
            self.main_pc = w.resume_pc
            return steps
        self.main_wake = 0
        self.main_pc = c.pc
        return steps
    def after_tick(self):
        """Cutscene-only mode: skip the game's stage walk at part ends.
        state 4 = part-end -> swap in the NEXT PART's code chunk at 0xC0000
        and re-init (state 0): the new part runs its own script/cels."""
        b = self.bus
        st = b.read16(self.P["state"])
        if st == 4:
            self.part += 1
            if self.part < len(self.PART_CODE):
                b.load(0x0C0000, self.chunks[self.PART_CODE[self.part]])
            # the old part's vblank hook points into the replaced code:
            # park it (low-gate auto-RTS) until the new part installs its own
            b.write32(0xFF0AD0, 0x000BBA)
            b.write16(self.P["state"], 0)
            b.write16(self.P["stage"], 4)   # stay on the code-chunk dispatcher
    def _run_from(self, pc, max_steps=400_000):
        c = self.cpu
        c.pc = pc
        steps = 0
        while c.pc != SENTINEL:
            c.step()
            steps += 1
            if steps > max_steps:
                raise RuntimeError(f"runaway pc={c.pc:06x}")
        return steps
    def _tick_chunk_direct(self, max_steps=400_000):
        c = self.cpu
        c.push32(SENTINEL)
        c.pc = 0x0C0008
        steps = 0
        while c.pc != SENTINEL:
            c.step()
            steps += 1
            if steps > max_steps:
                raise RuntimeError(
                    f"runaway pc={c.pc:06x} state={self.bus.read16(0xFF985C):04x}")
        return steps
    # vblank ISR transfer list (descriptors at lib file 0x2D5C):
    # SAT shadow 0xFFA07E -> VRAM, palette shadow 0xFFFB80 -> CRAM,
    # hscroll shadow 0xFF8ED4 -> VRAM
    ISR_XFERS = [(0x58000080, 0xFFA07E, 0x140),
                 (0xC0000080, 0xFFFB80, 0x40),
                 (0x5C000080, 0xFF8ED4, 0x1E0)]
    def vblank(self):
        self.frame += 1
        # CD clock: $9854 is the CD timecode in BCD MM:SS:FF (75 fps frame
        # field) — proven by the live-dump deltas (500 video frames == 8s24f
        # BCD). Track the position in CD frames (75/60 = +1.25 per vblank)
        # and honor the game's own clears (track restarts).
        if self.frame <= self.clock_hold and not getattr(self, "cd_frames", 0):
            # part-entry handshake window: the Sub is still
            # reading the bundle; no valid timecode status yet — $9854
            # holds 0 and every clock-gated event waits.  (Resume
            # harnesses that pre-seed cd_frames from a live snapshot skip
            # the hold — the load already happened on their machine.)
            self.bus.write32(self.P["timer"], 0)
            self.cd_frames = 0.0
        else:
            if self.bus.read32(self.P["timer"]) == 0:
                self.cd_frames = 0
            self.cd_frames = getattr(self, "cd_frames", 0) + 1.2516115875
            cf = int(self.cd_frames + CD_PHASE)
            mm = cf // 4500; ss = (cf // 75) % 60; ff = cf % 75
            bcd = lambda v: ((v // 10) << 4) | (v % 10)
            self.bus.write32(self.P["timer"],
                             (bcd(mm) << 16) | (bcd(ss) << 8) | bcd(ff))
        # the game's own vblank ISR (real library code) on its own stack,
        # with the interrupted context saved/restored (an interrupt frame).
        # Respect the interrupted context's interrupt mask (VDP copies run
        # under sr=2700; preempting them clobbers the VDP address latch).
        c = self.cpu
        saved = (list(c.d), list(c.a), c.pc, c.sr)
        c.a[7] = 0xFFF580   # clear of the palette shadow at 0xFFFB80
        try:
            self.call(0xFF0A0A)
        except FrameWait:
            pass    # ISR-side code must not block; drop if it tries
        c.d, c.a, c.pc, c.sr = list(saved[0]), list(saved[1]), saved[2], saved[3]
        # service any pending async load request (the sub side)
        if self.bus.read8(0xFF8EA8):
            self._service_load()
    def _service_load(self):
        code = self.bus.read16(0xFF9AE2)
        self.pal_log.append(("loadreq", self.frame, code))
        # stage the ending part-1 set: chunk1 verbatim at 0xC3000
        # (the live E6 machine shows exactly this layout at scene time)
        self.bus.load(0x0C3000, self.chunks[1])
        self.staged = 1
        self.bus.write8(0xFF8EA8, 0)
    def state(self):
        return self.bus.read16(self.P["state"])
    def snapshot(self):
        v = self.bus.vdp
        vram = list(struct.unpack(">32768H", bytes(v.vram)))
        cram = list(struct.unpack(">64H", bytes(v.cram)))
        vsram = list(struct.unpack(">40H", bytes(v.vsram)))
        return vram, cram, list(v.regs), vsram

def probe(frames=600):
    p = ScriptPlayer()
    last_state = -1
    for f in range(frames):
        p.vblank()
        try:
            steps = p.tick()
        except m68k.Unimplemented as e:
            print(f"f{f}: UNIMPLEMENTED {e}"); return p
        except m68k.StubCall as e:
            print(f"f{f}: STUB 0x{e.target:06X} called from {e.pc:06x}"); return p
        except RuntimeError as e:
            print(f"f{f}: {e}"); return p
        st = p.state()
        if st != last_state:
            print(f"f{f}: state {last_state:#x} -> {st:#x} "
                  f"(steps {steps}, vdpw {p.bus.vdp.writes})")
            last_state = st
        if p.bus.log:
            for e in p.bus.log[:6]: print("   buslog:", e)
            del p.bus.log[:]
    return p

if __name__ == "__main__":
    probe(int(sys.argv[1]) if len(sys.argv) > 1 else 600)
