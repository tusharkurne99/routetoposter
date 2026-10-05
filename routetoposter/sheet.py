"""A contact sheet: many poster previews side by side, each captioned with its theme name."""
from pathlib import Path

from matplotlib import font_manager
from PIL import Image, ImageDraw, ImageFont  # Pillow comes with matplotlib


def contact_sheet(posters: list[tuple[str, Path]], out: Path, columns: int = 6, thumb_width: int = 400) -> Path:
    """Save one image with every poster in `posters` ([(caption, image path)]) in a grid; return `out`."""
    first = Image.open(posters[0][1])
    thumb_h = round(thumb_width * first.height / first.width)
    pad, caption_h = 24, 34
    rows = -(-len(posters) // columns)  # ceiling division
    sheet = Image.new("RGB", (columns * (thumb_width + pad) + pad, rows * (thumb_h + caption_h + pad) + pad), "#ECE8E1")
    font = ImageFont.truetype(font_manager.findfont("DejaVu Sans"), 20)
    draw = ImageDraw.Draw(sheet)
    for i, (caption, path) in enumerate(posters):
        x = pad + (i % columns) * (thumb_width + pad)
        y = pad + (i // columns) * (thumb_h + caption_h + pad)
        sheet.paste(Image.open(path).convert("RGB").resize((thumb_width, thumb_h), Image.LANCZOS), (x, y))
        draw.text((x + thumb_width / 2, y + thumb_h + 8), caption.replace("_", " "), fill="#333333", font=font, anchor="ma")
    sheet.save(out)
    return out
