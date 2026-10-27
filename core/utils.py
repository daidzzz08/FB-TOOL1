import os
import re
import random
import uuid
import datetime
import zoneinfo
from typing import Optional
from PIL import Image, ImageEnhance, ImageOps, ImageDraw, ImageFont

VN_TZ = zoneinfo.ZoneInfo("Asia/Ho_Chi_Minh")
DESKTOP_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def get_vn_now() -> datetime.datetime:
    """Trả về đối tượng datetime hiện tại theo múi giờ Việt Nam (UTC+7)"""
    return datetime.datetime.now(VN_TZ)


def get_vn_time_str(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Trả về chuỗi thời gian Việt Nam định dạng chuẩn"""
    return get_vn_now().strftime(fmt)


def compute_jazoest(fb_dtsg: str) -> str:
    """Compute jazoest value from fb_dtsg token."""
    if not fb_dtsg:
        return "2957"
    return "2" + str(sum(ord(c) for c in fb_dtsg))


def extract_fb_dtsg_from_html(html: str) -> str:
    """Extracts fb_dtsg token from Facebook Comet HTML page."""
    if not html:
        return ""
    patterns = [
        r'"dtsg":\{"token":"([^"]+)"',
        r'\["DTSGInitialData",\[\],\{"token":"([^"]+)"\}',
        r'"DTSGInitialData",\[\],\{"token":"([^"]+)"\}',
        r'\["DTSGInitData",\[\],\{"token":"([^"]+)"',
        r'name="fb_dtsg"\s+value="([^"]+)"',
        r'"token":"(NAc[^"]+)"',
        r'"token":"(AQ[^"]+)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return match.group(1)
    return ""


# Alias
extract_dtsg_from_html = extract_fb_dtsg_from_html



def parse_spintax(text: str) -> str:
    """
    Recursively parses spintax format e.g. {A|B|{C|D}} into randomized text.
    Handles deeply nested spintax structures correctly.
    """
    if not text:
        return ""

    pattern = re.compile(r"\{([^{}]+)\}")
    
    # Loop until all nested spintax patterns are resolved
    while True:
        match = pattern.search(text)
        if not match:
            break
        
        # Replace each innermost match with a randomly selected option
        def replace_match(m):
            choices = m.group(1).split("|")
            return random.choice(choices)

        text = pattern.sub(replace_match, text)

    return text


def get_system_font(size: int):
    """
    Finds a clean TrueType font on macOS, Linux, or Windows.
    Falls back gracefully to ImageFont.load_default() if no TTF is found.
    """
    font_candidates = [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/SFNSText.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
        "Arial.ttf",
        "DejaVuSans.ttf"
    ]
    for path in font_candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    try:
        return ImageFont.truetype("arial.ttf", size)
    except Exception:
        return ImageFont.load_default()


def draw_watermark_text(
    draw: ImageDraw.ImageDraw,
    position: tuple,
    text: str,
    font,
    fill_color: tuple,
    outline_color: tuple
):
    """Draws text with a dark semi-transparent outline for high contrast on any background."""
    x, y = position
    # Draw outline in surrounding pixels for crisp shadow / border
    for dx, dy in [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1), (2, 2)]:
        draw.text((x + dx, y + dy), text, font=font, fill=outline_color)
    # Draw main text
    draw.text((x, y), text, font=font, fill=fill_color)


def apply_watermark_and_antidetect(
    input_path: str,
    output_dir: str = "/tmp",
    watermark_text: Optional[str] = None,
    opacity: float = 0.35
) -> str:
    """
    1. Removes EXIF & normalizes orientation.
    2. Applies branded watermark 'Zalo/Phone: <SĐT>' simultaneously at 5 key positions:
       - Top-Left, Top-Right, Bottom-Left, Bottom-Right, and Center (rotated).
    3. Alters image hash (crop 1-3px, subtle contrast/brightness modulation, variable compression).
    4. Returns path to the processed temporary antidetect image.
    """
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Source image not found: {input_path}")

    os.makedirs(output_dir, exist_ok=True)
    ext = os.path.splitext(input_path)[1].lower()
    if ext not in [".jpg", ".jpeg", ".png", ".webp"]:
        ext = ".jpg"

    output_filename = f"antidetect_{uuid.uuid4().hex[:12]}{ext}"
    output_path = os.path.join(output_dir, output_filename)

    with Image.open(input_path) as orig_img:
        # Normalize EXIF orientation
        img = ImageOps.exif_transpose(orig_img)
        img_rgba = img.convert("RGBA")
        width, height = img_rgba.size

        # --- Apply Watermark across 5 positions if provided ---
        if watermark_text and watermark_text.strip():
            clean_input = watermark_text.strip()
            # Standardize prefix: Zalo/Phone: <SĐT>
            if clean_input.lower().startswith("zalo/phone:"):
                full_text = clean_input
            else:
                full_text = f"Zalo/Phone: {clean_input}"

            # Calculate alpha channels
            alpha_val = int(max(0.15, min(0.85, opacity)) * 255)
            shadow_alpha = int(alpha_val * 0.75)
            fill_color = (255, 255, 255, alpha_val)
            outline_color = (15, 15, 15, shadow_alpha)

            overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)

            # Adaptive font sizing
            corner_font_size = max(13, int(min(width, height) * 0.036))
            center_font_size = max(18, int(corner_font_size * 1.35))

            corner_font = get_system_font(corner_font_size)
            center_font = get_system_font(center_font_size)

            # Measure corner text dimensions
            try:
                c_bbox = draw.textbbox((0, 0), full_text, font=corner_font)
                text_w = c_bbox[2] - c_bbox[0]
                text_h = c_bbox[3] - c_bbox[1]
            except Exception:
                text_w = len(full_text) * (corner_font_size * 0.58)
                text_h = corner_font_size * 1.2

            pad = max(12, int(min(width, height) * 0.03))

            # 1. Top-Left
            draw_watermark_text(
                draw=draw,
                position=(pad, pad),
                text=full_text,
                font=corner_font,
                fill_color=fill_color,
                outline_color=outline_color
            )

            # 2. Top-Right
            tr_x = max(pad, width - int(text_w) - pad)
            draw_watermark_text(
                draw=draw,
                position=(tr_x, pad),
                text=full_text,
                font=corner_font,
                fill_color=fill_color,
                outline_color=outline_color
            )

            # 3. Bottom-Left
            bl_y = max(pad, height - int(text_h) - pad)
            draw_watermark_text(
                draw=draw,
                position=(pad, bl_y),
                text=full_text,
                font=corner_font,
                fill_color=fill_color,
                outline_color=outline_color
            )

            # 4. Bottom-Right
            br_x = max(pad, width - int(text_w) - pad)
            br_y = max(pad, height - int(text_h) - pad)
            draw_watermark_text(
                draw=draw,
                position=(br_x, br_y),
                text=full_text,
                font=corner_font,
                fill_color=fill_color,
                outline_color=outline_color
            )

            # 5. Center (Rotated diagonally ~18 degrees)
            try:
                temp_draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
                try:
                    center_bbox = temp_draw.textbbox((0, 0), full_text, font=center_font)
                    cw = int((center_bbox[2] - center_bbox[0]) * 1.3) + 40
                    ch = int((center_bbox[3] - center_bbox[1]) * 1.3) + 40
                except Exception:
                    cw = int(len(full_text) * center_font_size * 0.75) + 40
                    ch = int(center_font_size * 2) + 40

                txt_img = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
                txt_draw = ImageDraw.Draw(txt_img)
                draw_watermark_text(
                    draw=txt_draw,
                    position=(20, 20),
                    text=full_text,
                    font=center_font,
                    fill_color=(255, 255, 255, int(alpha_val * 0.9)),
                    outline_color=(15, 15, 15, int(shadow_alpha * 0.9))
                )
                rotated_txt = txt_img.rotate(18, expand=True, resample=Image.BICUBIC)

                paste_x = (width - rotated_txt.width) // 2
                paste_y = (height - rotated_txt.height) // 2
                if paste_x > 0 and paste_y > 0:
                    overlay.paste(rotated_txt, (paste_x, paste_y), rotated_txt)
                else:
                    # Fallback direct center
                    cx = (width - int(text_w * 1.35)) // 2
                    cy = (height - int(text_h * 1.35)) // 2
                    draw_watermark_text(draw, (cx, cy), full_text, center_font, fill_color, outline_color)
            except Exception:
                pass

            # Alpha composite overlay onto original image
            img_rgba = Image.alpha_composite(img_rgba, overlay)

        # Convert to RGB on white background for JPEG
        if ext in [".jpg", ".jpeg"]:
            background = Image.new("RGB", (width, height), (255, 255, 255))
            background.paste(img_rgba, mask=img_rgba.split()[3])
            processed = background
        else:
            processed = img_rgba

        # --- Anti-detect Modifications ---
        # 1. Random subtle crop (1 to 3 pixels)
        p_width, p_height = processed.size
        if p_width > 12 and p_height > 12:
            crop_left = random.randint(0, min(3, p_width // 10))
            crop_top = random.randint(0, min(3, p_height // 10))
            crop_right = p_width - random.randint(0, min(3, p_width // 10))
            crop_bottom = p_height - random.randint(0, min(3, p_height // 10))
            
            if crop_right > crop_left and crop_bottom > crop_top:
                processed = processed.crop((crop_left, crop_top, crop_right, crop_bottom))

        # 2. Adjust brightness (+- 0.5% - 1.0%)
        brightness_factor = random.uniform(0.992, 1.008)
        processed = ImageEnhance.Brightness(processed).enhance(brightness_factor)

        # 3. Adjust contrast (+- 0.5% - 1.0%)
        contrast_factor = random.uniform(0.992, 1.008)
        processed = ImageEnhance.Contrast(processed).enhance(contrast_factor)

        # 4. Save with no metadata and random quality variation
        save_kwargs = {}
        if ext in [".jpg", ".jpeg"]:
            save_kwargs["quality"] = random.randint(92, 96)
            save_kwargs["optimize"] = True
        elif ext == ".png":
            save_kwargs["optimize"] = True
            save_kwargs["compress_level"] = random.randint(6, 9)
        elif ext == ".webp":
            save_kwargs["quality"] = random.randint(90, 95)

        processed.save(output_path, **save_kwargs)

    return output_path


def process_image_for_antidetect(input_path: str, output_dir: str = "/tmp") -> str:
    """Backwards-compatible wrapper for anti-detect processing."""
    return apply_watermark_and_antidetect(input_path, output_dir=output_dir)
