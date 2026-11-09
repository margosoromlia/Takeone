"""Рисует иконку Takeone (телефон с красной точкой записи) в icon.png / icon.ico / icon.icns.
Использование: python make_icon.py <папка-вывода>"""
import os
import sys

from PIL import Image, ImageDraw

out = sys.argv[1] if len(sys.argv) > 1 else "."
os.makedirs(out, exist_ok=True)

S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
# фон — тёмный скруглённый квадрат
d.rounded_rectangle([40, 40, S - 40, S - 40], radius=220, fill=(28, 32, 44, 255))
# телефон
d.rounded_rectangle([300, 170, 724, 854], radius=70, fill=(236, 240, 247, 255))
d.rounded_rectangle([330, 230, 694, 794], radius=24, fill=(58, 66, 88, 255))
d.rounded_rectangle([462, 192, 562, 210], radius=9, fill=(58, 66, 88, 255))
# точка записи
cx, cy, r = 700, 720, 170
d.ellipse([cx - r - 22, cy - r - 22, cx + r + 22, cy + r + 22], fill=(28, 32, 44, 255))
d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(235, 64, 52, 255))

img.save(os.path.join(out, "icon.png"))
img.save(os.path.join(out, "icon.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
img.save(os.path.join(out, "icon.icns"))
print("icon written to", out)
