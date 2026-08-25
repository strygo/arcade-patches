"""ffcd — Final Fight CD direct-from-disc scene composition.

Pipeline:
  disc  — ISO/bundle IO, decompression, palette detection (JP + US discs)
  megadrive — full Mega Drive compositor (planes, priority, per-line hscroll,
             per-plane vscroll, 512px wrap, window-plane clipping, sprites)
  usmap    — piecewise-aligned JP->US tile/palette mapping (unique-tile anchors)
  scenes   — scene registry + render_scene()/gallery()/sweep() entry points
"""
