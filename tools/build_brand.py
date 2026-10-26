"""Generate the original, simple bidirectional brand mark (requires Pillow)."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

DEST = Path(__file__).resolve().parents[1] / "custom_components/h3x_modbus_translator/brand"


def main():
    image = Image.new("RGBA", (512, 512))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((8, 8, 504, 504), radius=52, fill="#20262c")
    draw.line((100, 145, 400, 145), fill="#44d1ac", width=36)
    draw.polygon(((345, 84), (420, 145), (345, 206)), fill="#44d1ac")
    draw.line((112, 275, 412, 275), fill="#f4cc5d", width=36)
    draw.polygon(((167, 214), (92, 275), (167, 336)), fill="#f4cc5d")
    font = ImageFont.load_default(size=110)
    draw.text((256, 413), "H3X", fill="white", font=font, anchor="mm")
    DEST.mkdir(parents=True, exist_ok=True)
    image.save(DEST / "icon@2x.png")
    image.resize((256, 256), Image.Resampling.LANCZOS).save(DEST / "icon.png")


if __name__ == "__main__":
    main()
