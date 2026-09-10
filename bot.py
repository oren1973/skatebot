#!/usr/bin/env python3
"""
SkateCoachBot — webhook mode for Render free tier
"""

import os
import math
import base64
import tempfile
import subprocess
import json
import logging
from pathlib import Path

import anthropic
from PIL import Image, ImageDraw, ImageFont
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from aiohttp import web

logging.basicConfig(level=logging.INFO)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CLAUDE_API_KEY = os.environ["CLAUDE_API_KEY"]
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "")

# ─── Pass 1: סיווג פריימים ───────────────────────────────────────────────────

CLASSIFY_PROMPT = """אתה מערכת סיווג. לפניך פריימים ממוספרים מסרטון.

עבור כל פריים, סווג לאחת מהקטגוריות:
- skating: נראה גלישה פעילה — לוח, תנועה, תרגיל, נסיעה
- static: אדם עומד ללא תנועה (עם לוח או בלי)
- talking: אדם מדבר למצלמה, מסביר, מצביע
- other: לא רלוונטי — קרקע, שמיים, ידיים בלי גוף, טשטוש

החזר JSON בלבד, ללא טקסט נוסף:
{"frames": [{"n": 1, "type": "skating"}, {"n": 2, "type": "talking"}, ...]}"""

# ─── Pass 2: ניתוח ─────────────────────────────────────────────────────────

ANALYSIS_PROMPT = """אתה מאמן סקייטבורד מנוסה. לפניך פריימים של גולש מתחיל — קטעי גלישה בלבד.

ראשית, תאר לעצמך מה אתה רואה בכל פריים. אחר כך החלט מה חשוב.

כללי ברזל:
1. כל הערה — חיובית או שלילית — חייבת להתחיל ב"בפריים X רואים ש..."
   אם אי אפשר לעגן אותה בפריים ספציפי שקיבלת — אל תכתוב אותה.
2. אל תמציא שמות טריקים (ollie, kickflip וכו') אלא אם הלוח עזב את הקרקע באופן ברור ומוכח.
3. 3 דברים לשיפור — לפי סדר עדיפות: מה שאם יתוקן ישפיר הכי הרבה את הגלישה.
4. דבר חיובי אחד — לא "הגולש עמד על הלוח". משהו ספציפי שהגולש הזה עושה טוב יותר מהממוצע.
   אם אין כזה דבר אמיתי — החזר null בשדה positive.

החזר JSON בלבד, ללא טקסט נוסף:
{
  "positive": {
    "title": "כותרת קצרה",
    "body": "בפריים X רואים ש...",
    "frame": 3
  },
  "tips": [
    {"title": "כותרת קצרה", "body": "בפריים X רואים ש...", "frame": 5},
    {"title": "כותרת קצרה", "body": "בפריים X רואים ש...", "frame": 9},
    {"title": "כותרת קצרה", "body": "בפריים X רואים ש...", "frame": 12}
  ]
}"""


def extract_frames(video_path: str, fps: float = 2, max_frames: int = 20) -> list[str]:
    tmpdir = tempfile.mkdtemp(prefix="skate_")
    pattern = os.path.join(tmpdir, "frame_%03d.jpg")
    subprocess.run([
        "ffmpeg", "-i", video_path,
        "-vf", f"fps={fps},scale=640:-1",
        "-q:v", "3", "-frames:v", str(max_frames),
        pattern, "-y", "-loglevel", "error"
    ], check=True)
    return sorted(str(p) for p in Path(tmpdir).glob("frame_*.jpg"))


def classify_frames(frame_paths: list[str]) -> list[int]:
    """
    Pass 1: שלח כל הפריימים ל-Claude, קבל חזרה אילו מהם הם 'skating'.
    מחזיר רשימת אינדקסים (0-based) של פריימי גלישה.
    """
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    content = []
    for i, path in enumerate(frame_paths):
        with open(path, "rb") as f:
            data = base64.standard_b64encode(f.read()).decode()
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}})
        content.append({"type": "text", "text": f"[פריים {i + 1}]"})
    content.append({"type": "text", "text": CLASSIFY_PROMPT})

    msg = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=512,
        messages=[{"role": "user", "content": content}]
    )
    text = msg.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]

    result = json.loads(text.strip())
    skating_indices = [
        item["n"] - 1  # המרה ל-0-based
        for item in result.get("frames", [])
        if item.get("type") == "skating"
    ]
    logging.info(f"Pass 1: {len(skating_indices)}/{len(frame_paths)} פריימי גלישה זוהו")
    return skating_indices


def analyze_frames(frame_paths: list[str]) -> dict:
    """
    Pass 2: ניתוח עמוק של פריימי גלישה בלבד.
    """
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
    text = msg.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


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


def bubble(draw, x, y, lines, bg, W, H, box_w=280):
    pad, lh = 14, 26
    bh = len(lines) * lh + pad * 2
    bx = min(x, W - box_w - 8)
    by = y if y + bh < H else y - bh - 8
    draw.rounded_rectangle([bx, by, bx + box_w, by + bh], radius=12, fill=bg)
    for i, line in enumerate(lines):
        f = font(18, bold=(i == 0))
        draw.text((bx + pad, by + pad + i * lh), line, fill=(255, 255, 255, 255), font=f)
    return bx, by, bx + box_w, by + bh


def annotate_frame(frame_path: str, tip: dict, index: int, is_positive: bool = False) -> str:
    img = Image.open(frame_path).convert("RGBA")
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    W, H = img.size

    if is_positive:
        color = (30, 160, 80, 230)  # ירוק
        label = "✓ " + tip["title"]
    else:
        colors = [(220, 40, 40, 230), (220, 140, 0, 230), (30, 100, 210, 230)]
        color = colors[index % 3]
        label = "❶❷❸"[index] + " " + tip["title"]

    body = tip["body"]
    words = body.split(" ")
    lines = [label]
    current = ""
    for w in words:
        if len(current) + len(w) + 1 > 30:
            lines.append(current.strip())
            current = w
        else:
            current += " " + w
    if current:
        lines.append(current.strip())

    positions = [(W - 290, 40), (8, H - 200), (W - 290, H - 200)]
    bx, by = positions[index % 3] if not is_positive else (8, 40)

    bx1, by1, bx2, by2 = bubble(d, bx, by, lines, color, W, H)
    cx = (bx1 + bx2) // 2
    cy = (by1 + by2) // 2
    arrow(d, cx, cy, W // 2, H // 2, color)

    result = Image.alpha_composite(img, ov).convert("RGB")
    suffix = "_positive" if is_positive else f"_tip{index}"
    out_path = frame_path.replace(".jpg", f"{suffix}.jpg")
    result.save(out_path, quality=92)
    return out_path


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    await msg.reply_text("🛹 קיבלתי! מנתח את הסרטון — כ-40 שניות...")

    video = msg.video or msg.document
    file = await context.bot.get_file(video.file_id)
    tmpdir = tempfile.mkdtemp(prefix="skatebot_")
    video_path = os.path.join(tmpdir, "input.mp4")
    await file.download_to_drive(video_path)

    # חילוץ פריימים
    all_frames = extract_frames(video_path)
    if not all_frames:
        await msg.reply_text("❌ לא הצלחתי לפרק את הסרטון.")
        return

    if len(all_frames) < 3:
        await msg.reply_text("❌ הסרטון קצר מדי — שלח סרטון של לפחות 2 שניות.")
        return

    # Pass 1: סינון פריימים
    skating_indices = classify_frames(all_frames)
    skating_frames = [all_frames[i] for i in skating_indices if i < len(all_frames)]

    if len(skating_frames) < 3:
        await msg.reply_text("🛹 לא זיהיתי מספיק קטעי גלישה בסרטון. וודא שהסרטון מציג גלישה פעילה.")
        return

    # Pass 2: ניתוח
    result = analyze_frames(skating_frames)
    tips = result.get("tips", [])
    positive = result.get("positive")

    if not tips:
        await msg.reply_text("❌ לא הצלחתי לנתח.")
        return

    # בניית סיכום טקסט
    summary = "🛹 *תחקיר:*\n\n"

    if positive:
        summary += f"✅ *{positive['title']}*\n{positive['body']}\n\n"

    summary += "*3 דברים לשיפור (לפי סדר חשיבות):*\n\n"
    for i, tip in enumerate(tips[:3]):
        summary += f"{'❶❷❸'[i]} *{tip['title']}*\n{tip['body']}\n\n"

    await msg.reply_text(summary, parse_mode="Markdown")

    # שליחת תמונות מסומנות
    # תמונה חיובית
    if positive:
        frame_idx = max(0, min(positive.get("frame", 1) - 1, len(skating_frames) - 1))
        annotated = annotate_frame(skating_frames[frame_idx], positive, 0, is_positive=True)
        with open(annotated, "rb") as f:
            await msg.reply_photo(f)

    # 3 תמונות שיפור
    for i, tip in enumerate(tips[:3]):
        frame_idx = max(0, min(tip.get("frame", 1) - 1, len(skating_frames) - 1))
        annotated = annotate_frame(skating_frames[frame_idx], tip, i)
        with open(annotated, "rb") as f:
            await msg.reply_photo(f)


async def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))

    webhook_url = f"{RENDER_URL}/{TELEGRAM_TOKEN}"
    logging.info(f"Setting webhook: {webhook_url}")
    await app.bot.set_webhook(webhook_url)

    async def handle_webhook(request):
        data = await request.json()
        update = Update.de_json(data, app.bot)
        await app.process_update(update)
        return web.Response(text="OK")

    async def handle_health(request):
        return web.Response(text="OK")

    await app.initialize()
    server = web.Application()
    server.router.add_post(f"/{TELEGRAM_TOKEN}", handle_webhook)
    server.router.add_get("/", handle_health)

    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 10000)))
    await site.start()
    logging.info("🛹 SkateCoachBot רץ בmodus webhook")

    import asyncio
    await asyncio.Event().wait()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
