# Aura brand assets

The identity is drawn from Aura herself: silver-lavender hair that fades into
ice blue, the crystal halo that circles her in the portrait, and the little
four-pointed sparkles scattered through her sticker pack. Nothing here is a
generated illustration of the character — that art belongs to the sticker pack.

## Files

| File | Use |
| --- | --- |
| `logo.svg` / `logo.png` | The app icon: mark on a deep-indigo rounded square. 512 px PNG |
| `logo-192.png`, `logo-64.png` | Ready-made sizes for avatars and favicons |
| `mark.svg` / `mark.png` | The same sparkle-and-halo mark on transparency, 1024 px |
| `banner.svg` / `banner.png` | 1200×400 hero for the README |

SVG is the source of truth. The PNGs are rendered from it so they stay in step.

## Palette

| Name | Hex | Where it is used |
| --- | --- | --- |
| Night | `#08060F` → `#171333` → `#2E2760` | Icon background gradient, banner |
| Halo lavender | `#D3C9FF` | Start of every halo stroke |
| Aura violet | `#9B87F2` | The mark's mid-tone, accents |
| Ice | `#3FC9F4` | The mark's cool tip, secondary highlights |
| Mist | `#DED3FF` | The pale inner edge of the sparkle |
| Ink text | `#EDE9FF` | Wordmark on dark |

The violet-to-ice ramp is the whole brand in one line: her hair on one end, the
architecture behind her on the other.

## Re-rendering

`resvg-py` is already a runtime dependency, and `svg_image.py` knows where the
bundled Vazirmatn fonts are, so a render needs nothing extra:

```bash
python - <<'PY'
import pathlib, resvg_py
from svg_image import font_files, TEXT_STYLE_SHEET

for name, w, h in [("logo", 512, 512), ("mark", 1024, 1024), ("banner", 1200, 400)]:
    svg = pathlib.Path(f"assets/brand/{name}.svg").read_text(encoding="utf-8")
    data = resvg_py.svg_to_bytes(
        svg_string=svg, width=w, height=h, background=None,
        font_files=list(font_files()) or None, style_sheet=TEXT_STYLE_SHEET,
    )
    pathlib.Path(f"assets/brand/{name}.png").write_bytes(bytes(data))
PY
```

The banner carries text, so it stays a PNG: a browser has no Vazirmatn and would
fall back to a different face, which matters most for the Persian line.

## Usage

The mark is MIT licensed along with the rest of the project (see [NOTICE](../../NOTICE)).
If you run your own instance, you are welcome to keep the name and mark, or to
replace them — nothing in the code depends on either.
