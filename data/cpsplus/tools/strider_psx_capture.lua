-- Drives the Strider (PlayStation) sound test for the CPS+ pack builder.
--
-- MAME records the audio itself (-wavwrite); this script only plays a fixed
-- input schedule and selects each sound-test slot, so the same disc, BIOS
-- and MAME build produce the same recording every time.  Everything it
-- needs arrives in the environment, set by capture_strider_psx.py:
--   STRIDER_SCHEDULE   frame ranges during which an input is held,
--                      "first-last:TOKEN;..."
--   STRIDER_POKES      "frame:address=value;..." 32-bit writes that select
--                      the sound-test slot before each play
--   STRIDER_END_FRAME  the run ends after this many frames
local m = manager.machine
local cpu = m.devices[':maincpu']
local sp = cpu.spaces['program']

local fields = {}
for _, port in pairs(m.ioport.ports) do
  for _, field in pairs(port.fields) do
    fields[m.ioport:input_type_to_token(field.type, field.player)] = field
  end
end

local schedule, pokes = {}, {}
for a, b, tok in string.gmatch(os.getenv('STRIDER_SCHEDULE') or '', '(%d+)%-(%d+):([^;]+)') do
  assert(fields[tok], 'unknown input ' .. tok)
  schedule[#schedule + 1] = { tonumber(a), tonumber(b), tok }
end
for fr, addr, val in string.gmatch(os.getenv('STRIDER_POKES') or '', '(%d+):([^=;]+)=([^;]+)') do
  pokes[tonumber(fr)] = { tonumber(addr), tonumber(val) }
end
local end_frame = os.getenv('STRIDER_END_FRAME')
assert(end_frame, 'STRIDER_END_FRAME not set')
local last = tonumber(end_frame)

local f, active = 0, {}
emu.register_frame_done(function()
  f = f + 1
  local p = pokes[f]
  if p then sp:write_u32(p[1], p[2]) end
  local wanted = {}
  for _, r in ipairs(schedule) do
    if f >= r[1] and f <= r[2] then wanted[r[3]] = true end
  end
  for t in pairs(wanted) do
    fields[t]:set_value(1)
    active[t] = true
  end
  for t in pairs(active) do
    if not wanted[t] then
      fields[t]:clear_value()
      active[t] = nil
    end
  end
  if f % 9000 == 0 then print(string.format('frame %d of %d', f, last)) end
  if f > last then m:exit() end
end)
