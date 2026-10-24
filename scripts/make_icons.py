"""מייצר את קבצי הלוגו של האתר מ-images/logo_transparent.png. מריצים אחרי כל החלפת לוגו:

python scripts/make_icons.py

app/static/logo.webp            הלוגו באתר ובמסך הכניסה (560px רוחב, מספיק לתצוגה צפופה)
app/static/favicon.png          64x64 שקוף: הלוגו המלא ברוחב מלא, ממורכז לגובה
app/static/apple-touch-icon.png 180x180 על רקע לבן (ב-iPhone שקיפות נצבעת בשחור)
"""
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "static"

src = Image.open(ROOT / "images" / "logo_transparent.png").convert("RGBA")
src = src.crop(src.getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox())  # בלי שוליים שקופים


def fit(size, pad=0, bg=(0, 0, 0, 0)):
    """הלוגו ממורכז בריבוע, בלי לחתוך ממנו."""
    canvas = Image.new("RGBA", (size, size), bg)
    scale = (size - 2 * pad) / max(src.size)
    w, h = round(src.width * scale), round(src.height * scale)
    canvas.alpha_composite(src.resize((w, h), Image.LANCZOS), ((size - w) // 2, (size - h) // 2))
    return canvas


src.resize((560, round(560 * src.height / src.width)), Image.LANCZOS).save(STATIC / "logo.webp", quality=90, method=6)
fit(64).save(STATIC / "favicon.png", optimize=True)
fit(180, 20, (255, 255, 255, 255)).convert("RGB").save(STATIC / "apple-touch-icon.png", optimize=True)
print(f"הלוגו {src.width}x{src.height} (יחס {src.width / src.height:.2f}). ב-HTML: height = width / {src.width / src.height:.2f}")
