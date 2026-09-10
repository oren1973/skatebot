#!/usr/bin/env python3
"""
SkateCoachBot — טלגרם בוט לתחקיר סקייטבורד
"""

import os
import math
import base64
import tempfile
import subprocess
from pathlib import Path

import anthropic
from PIL import Image, ImageDraw, ImageFont
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CLAUDE_API_KEY = os.environ["CLAUDE_API_KEY"]

ANALYSIS_PROMPT = """אתה מאמן סקייטבורד מנוסה. לפניך פריימים ממוספרים מסרטון של גולש.

כללים:
- התבסס רק על מה שנראה בבירור בתמונות
- אל תמציא טריקים — אל תזכיר ollie, kickflip וכו' אלא אם הלוח עזב את הקרקע באופן ברור
- התמקד ביציבה, ברכיים, ידיים, מבט, גוף

החזר תשובה במבנה JSON הבא בלבד, ללא טקסט נוסף:

{
  "tips": [
    {
      "title": "כותרת קצרה בעברית",
      "body": "הסבר קצר מה לתקן ואיך, בעברית",
      "frame": 5
    },
    {
      "title": "כותרת קצרה בעברית",
      "body": "הסבר קצר מה לתקן ואיך, בעברית",
      "frame": 12
    },
    {
      "title": "כותרת קצרה בעברית",
      "body": "הסבר קצר מה לתקן ואיך, בעברית",
      "frame": 8
    }
  ]
}

שדה "frame" הוא מספר הפריים (מתוך הרשימה שקיבלת) שבו ההערה הכי נראית בבירור."""


def extract_frames(video_path: str, fps: float = 2, max_frames: int = 16) -> list[str]:
    tmpdir = tempfile.mkdtemp(prefix="skate_")
    pattern = os.path.join(tmpdir, "frame_%03d.jpg")
    subprocess.run([
        "ffmpeg", "-i", video_path,
        "-vf", f"fps={fps},scale=640:-1",
        "-q:v", "3", "-frames:v", str(max_frames),
        pattern, "-y", "-loglevel", "error"
    ], check=True)
    return sorted(str(p) for p in Path(tmpdir).glob("frame_*.jpg"))


def font(size, bold=False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(f"/usr/share/fonts/truetype/dejavu/{name}", size)
    except:
        return ImageFont.load_default()


def arrow(draw, x1, y1, x2, y2, color, width=5):
    draw.line([(x1, y1), (x2, y2)], fill=color, width=width)
    angle = math.atan2(y2 - y1, x2 - x1)
    L, a = 20, math.pi / 6
    pts = [(x2, y2),
           (x2 - L * math.cos(angle - a), y2 - L * math.sin(angle - a)),
           (x2 - L * math.cos(angle + a), y2 - L * math.sin(angle + a))]
    draw.polygon(pts, fill=color)


def bubble(draw, x, y, lines, bg, W, H, box_w=260):
    pad, lh = 14, 26
    bh = len(lines) * lh + pad * 2
    bx = min(x, W - box_w - 8)
    by = y if y + bh < H else y - bh - 8
    draw.rounded_rectangle([bx, by, bx + box_w, by + bh], radius=12, fill=bg)
    for i, line in enumerate(lines):
        f = font(18, bold=(i == 0))
        draw.text((bx + pad, by + pad + i * lh), line, fill=(255, 255, 255, 255), font=f)
    return bx, by, bx + box_w, by + bh


def annotate_frame(frame_path: str, tip: dict, index: int) -> str:
    img = Image.open(frame_path).convert("RGBA")
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    W, H = img.size

    colors = [(220, 40, 40, 230), (220, 140, 0, 230), (30, 100, 210, 230)]
    color = colors[index % 3]

    # חלק הכיתוב לשורות
    title = f"❶❷❸"[index] + " " + tip["title"]
    body = tip["body"]
    # חלק body לשורות של ~30 תווים
    words = body.split(" ")
    lines = [title]
    current = ""
    for w in words:
        if len(current) + len(w) + 1 > 28:
            lines.append(current.strip())
            current = w
        else:
            current += " " + w
    if current:
        lines.append(current.strip())

    # מיקום bubble — לסירוגין: ימין-למעלה, שמאל-למטה, ימין-למטה
    positions = [(W - 270, 40), (8, H - 180), (W - 270, H - 180)]
    bx, by = positions[index % 3]

    bx1, by1, bx2, by2 = bubble(d, bx, by, lines, color, W, H)

    # מרכז הבועה
    cx = (bx1 + bx2) // 2
    cy = (by1 + by2) // 2
    # מרכז הגולש (בערך אמצע הפריים)
    sx, sy = W // 2, H // 2
    arrow(d, cx, cy, sx, sy, color)

    result = Image.alpha_composite(img, ov).convert("RGB")
    out_path = frame_path.replace(".jpg", f"_tip{index}.jpg")
    result.save(out_path, quality=92)
    return out_path


def analyze_frames(frame_paths: list[str]) -> dict:
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    content = []
    for i, path in enumerate(frame_paths):
        with open(path, "rb") as f:
            data = base64.standard_b64encode(f.read()).decode()
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}})
        content.append({"type": "text", "text": f"[פריים {i + 1}]"})
    content.append({"type": "text", "text": ANALYSIS_PROMPT})

    msg = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1024,
        messages=[{"role": "user", "content": content}]
    )
    import json
    text = msg.content[0].text.strip()
    # נקה אם יש ```json
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    await msg.reply_text("🛹 קיבלתי! מוריד ומנתח את הסרטון — זה לוקח כ-30 שניות...")

    # הורד וידאו
    video = msg.video or msg.document
    file = await context.bot.get_file(video.file_id)
    tmpdir = tempfile.mkdtemp(prefix="skatebot_")
    video_path = os.path.join(tmpdir, "input.mp4")
    await file.download_to_drive(video_path)

    await msg.reply_text("📸 מוציא פריימים...")
    frames = extract_frames(video_path)

    if not frames:
        await msg.reply_text("❌ לא הצלחתי לפרק את הסרטון. נסה שוב.")
        return

    await msg.reply_text("🤖 מנתח עם Claude...")
    result = analyze_frames(frames)
    tips = result.get("tips", [])

    if not tips:
        await msg.reply_text("❌ לא הצלחתי לנתח. נסה עם סרטון אחר.")
        return

    # שלח סיכום טקסט
    summary = "🛹 *תחקיר — 3 דברים לשיפור:*\n\n"
    for i, tip in enumerate(tips[:3]):
        summary += f"{'❶❷❸'[i]} *{tip['title']}*\n{tip['body']}\n\n"
    await msg.reply_text(summary, parse_mode="Markdown")

    # שלח 3 תמונות מוערות
    for i, tip in enumerate(tips[:3]):
        frame_idx = min(tip.get("frame", 1) - 1, len(frames) - 1)
        frame_idx = max(0, frame_idx)
        annotated = annotate_frame(frames[frame_idx], tip, i)
        with open(annotated, "rb") as f:
            await msg.reply_photo(f)


def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    print("🛹 SkateCoachBot מתחיל...")
    app.run_polling()


if __name__ == "__main__":
    main()
