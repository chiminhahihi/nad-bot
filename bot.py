import os
import warnings
import re
import asyncio
import io
import json
import string
import base64
from datetime import datetime, timezone, timedelta
from aiohttp import web
from dotenv import load_dotenv
import discord
from discord.ext import commands, tasks
from google import genai
from google.genai import types
from openai import AsyncOpenAI
from PIL import Image

warnings.filterwarnings('ignore')
load_dotenv()

# --- 1. CẤU HÌNH MÚI GIỜ VIỆT NAM (UTC+7 CHO SERVER RENDER) ---
VN_TZ = timezone(timedelta(hours=7))

def now_vn():
    """Lấy thời gian hiện tại chuẩn giờ Việt Nam bất kể server đặt ở đâu"""
    return datetime.now(VN_TZ).replace(tzinfo=None)

# --- 2. CẤU HÌNH TOKEN & API KEYS (ĐỌC TỪ BIẾN MÔI TRƯỜNG RENDER) ---
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("AIzaSyDuOEYpg2d5bZxxxVDU3FrTZm_Ist1DLbE")
DATA_FILE = "production_data.json"

client_ai = genai.Client(api_key=GEMINI_API_KEY)
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# --- 3. LƯU TRỮ DỮ LIỆU BỀN VỮNG ---
def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("tasks", []), data.get("meetings", []), data.get("submissions", [])
        except Exception:
            pass
    return [], [], []

def save_data():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "tasks": TASKS,
                "meetings": meetings,
                "submissions": SUBMISSIONS
            }, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Lỗi lưu dữ liệu: {e}")

TASKS, meetings, SUBMISSIONS = load_data()
USER_CHATS = {}

# --- 4. CẤU HÌNH BOT & PHÂN QUYỀN ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

# Tắt help_command mặc định để tránh xung đột và dùng hệ thống hướng dẫn riêng
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

ALLOWED_USER_IDS = {
    572791257558941736,
    792068366335475723,
    649222792071348236,
}

@bot.check
async def globally_block_unauthorized(ctx):
    if ALLOWED_USER_IDS and ctx.author.id not in ALLOWED_USER_IDS:
        await ctx.send("⛔ Bạn không có quyền sử dụng bot này.")
        return False
    return True

# --- 5. HỆ THỐNG ĐA MÔ HÌNH CHUẨN ---
AVAILABLE_MODELS = {
    "gemini": "gemini-2.5-flash",
    "2.5-flash": "gemini-2.5-flash",
    "2.5-pro": "gemini-2.5-pro",
    "2.5-lite": "gemini-2.5-flash-lite",
    "2.0-flash": "gemini-2.0-flash",
    "gemma": "gemma-3-27b-it",
    "gpt-4o": "gpt-4o",
    "gpt-4o-mini": "gpt-4o-mini",
    "gpt-4-turbo": "gpt-4-turbo",
}

DEFAULT_MODEL = "gemini"

SYSTEM_PROMPT = (
    "Bạn là chuyên gia Production House, hội tụ tư duy của Art Director, Creative Director, Producer, DI Artist và Visual Artist. "
    "Giao tiếp: bình thường, thẳng thắn, rút gọn, clean, không rườm rà. "
    "Khi duyệt file/WIP: dùng mắt chuyên môn khắt khe. Phân tích layout, lighting, color grading (DI), visual flow. "
    "Khi xử lý tiến độ: tư duy Producer, tối ưu quy trình. Đưa ra giải pháp kỹ thuật chính xác, thực chiến."
)

# --- 6. HÀM TIỆN ÍCH & BÓC TÁCH NGÀY GIỜ ---
async def send_long_message(destination, text: str):
    """Tự động chia nhỏ tin nhắn nếu vượt quá giới hạn 2000 ký tự của Discord"""
    if not text:
        await destination.send("⚠️ Không có nội dung phản hồi.")
        return
    for i in range(0, len(text), 1990):
        await destination.send(text[i:i+1990])

def parse_smart_datetime(text):
    text = text.strip().lower()
    now = now_vn()
    
    time_match = re.search(r'\b(\d{1,2})[:h](\d{2})\b', text)
    if not time_match:
        return None
    hour, minute = int(time_match.group(1)), int(time_match.group(2))

    date_full = re.search(r'\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b', text)
    date_rev = re.search(r'\b(\d{4})[/-](\d{1,2})[/-](\d{1,2})\b', text)
    date_short = re.search(r'\b(\d{1,2})[/-](\d{1,2})\b', text)

    if date_full:
        day, month, year = int(date_full.group(1)), int(date_full.group(2)), int(date_full.group(3))
    elif date_rev:
        year, month, day = int(date_rev.group(1)), int(date_rev.group(2)), int(date_rev.group(3))
    elif date_short:
        day, month = int(date_short.group(1)), int(date_short.group(2))
        year = now.year
    else:
        day, month, year = now.day, now.month, now.year

    try:
        return datetime(year, month, day, hour, minute)
    except ValueError:
        return None

def extract_topic(text):
    text = re.sub(r'\b\d{1,2}[/-]\d{1,2}[/-]\d{4}\b', '', text)
    text = re.sub(r'\b\d{4}[/-]\d{1,2}[/-]\d{1,2}\b', '', text)
    text = re.sub(r'\b\d{1,2}[/-]\d{1,2}\b', '', text)
    text = re.sub(r'\b\d{1,2}[:hH]\d{2}\b', '', text)
    text = re.sub(r'<@!?\d+>', '', text)
    text = re.sub(r'\b(ngày|lúc|vào)\b', '', text, flags=re.IGNORECASE)
    return ' '.join(text.split()).strip()

# --- 7. HÀM GỌI API ĐA MÔ HÌNH BẤT ĐỒNG BỘ (ASYNC) ---
async def async_safe_generate_content(model_name, contents_list):
    async def _call_gemini(m):
        return await client_ai.aio.models.generate_content(
            model=m,
            contents=contents_list,
            config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT)
        )
    
    async def _call_openai(m):
        openai_content = []
        if isinstance(contents_list, list):
            for item in contents_list:
                if isinstance(item, str):
                    openai_content.append({"type": "text", "text": item})
                elif isinstance(item, Image.Image):
                    buffered = io.BytesIO()
                    fmt = item.format if item.format else "PNG"
                    item.save(buffered, format=fmt)
                    img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
                    mime_type = f"image/{fmt.lower()}"
                    openai_content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{img_str}"}
                    })
        else:
            openai_content = str(contents_list)
            
        response = await openai_client.chat.completions.create(
            model=m,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": openai_content}
            ]
        )
        class ResultWrapper:
            text = response.choices[0].message.content
        return ResultWrapper()
    
    fallback_sequence = [
        model_name,
        "gemini-2.5-flash",
        "gpt-4o-mini",
        "gpt-4o",
        "gemma-3-27b-it"
    ]
    models_to_try = list(dict.fromkeys(fallback_sequence))
    
    last_error = None
    for m in models_to_try:
        try:
            if m.startswith("gpt-"):
                return await _call_openai(m)
            else:
                return await _call_gemini(m)
        except Exception as e:
            last_error = e
            continue
                
    raise last_error

# --- 8. SỰ KIỆN BOT & VÒNG LẶP CHẠY NGẦM ---
@bot.event
async def on_ready():
    print(f"[SYSTEM] Bot đã sẵn sàng: {bot.user}")
