import os
import gc
import time
import warnings
import re
import asyncio
import io
import json
import string
import base64
import urllib.request
from datetime import datetime, timezone, timedelta
import aiohttp
from aiohttp import web
from dotenv import load_dotenv
import discord
from discord.ext import commands, tasks
from google import genai
from google.genai import types
from PIL import Image

warnings.filterwarnings('ignore')
load_dotenv()

# ==============================================================================
# 1. CẤU HÌNH MÚI GIỜ VIỆT NAM (UTC+7 CHO RENDER)
# ==============================================================================
VN_TZ = timezone(timedelta(hours=7))

def now_vn():
    return datetime.now(VN_TZ).replace(tzinfo=None)

# ==============================================================================
# 2. CẤU HÌNH TOKEN & API KEYS (TỰ ĐỘNG LẤY TỪ RENDER ENVIRONMENT)
# ==============================================================================
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
APIFY_API_TOKEN = os.environ.get("APIFY_API_TOKEN", "").strip()
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://nad-bot.onrender.com").strip()
DATA_FILE = "production_data.json"

MAX_RAW_DOWNLOAD_MB = 25     # Ngưỡng an toàn bảo vệ 512MB RAM của Render Free
MAX_RAW_PIXELS = 45_000_000  # Chặn bung pixel >45MP với PNG/TIFF tránh sập RAM

FALLBACK_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.8-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
]

FAST_WIP_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.8-flash",
    "gemini-2.5-flash",
]

# Danh sách các Role cho bảng nút bấm !setuproles
STUDIO_ROLES = [
    {"id": "role_art_director", "name": "Art Director", "emoji": "🎨"},
    {"id": "role_visual_artist", "name": "Visual Artist", "emoji": "🎬"},
    {"id": "role_3d_artist", "name": "3D Artist", "emoji": "🧊"},
    {"id": "role_di_retouch", "name": "DI / Retouch", "emoji": "✨"},
    {"id": "role_producer", "name": "Producer", "emoji": "📋"},
]

MEETING_PREFIX = "🎙️ Họp - "

client_ai = genai.Client(api_key=GEMINI_API_KEY)
HTTP_SESSION = None
CACHED_FAST_MODEL = ""  # Tự ghim model chạy nhanh & ổn định nhất để bỏ qua độ trễ fallback

async def get_http_session() -> aiohttp.ClientSession:
    global HTTP_SESSION
    if HTTP_SESSION is None or HTTP_SESSION.closed:
        HTTP_SESSION = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=30, ttl_dns_cache=300)
        )
    return HTTP_SESSION

# ==============================================================================
# 3. LƯU TRỮ DỮ LIỆU
# ==============================================================================
def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return (
                    data.get("tasks", []),
                    data.get("meetings", []),
                    data.get("submissions", []),
                    data.get("active_model", "")
                )
        except Exception:
            pass
    return [], [], [], ""

def save_data():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "tasks": TASKS,
                "meetings": meetings,
                "submissions": SUBMISSIONS,
                "active_model": ACTIVE_MODEL
            }, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Lỗi lưu dữ liệu: {e}")

TASKS, meetings, SUBMISSIONS, ACTIVE_MODEL = load_data()
USER_CHATS = {}
TEMP_VOICE_CHANNELS = set()
VIEWS_REGISTERED = False

# ==============================================================================
# 4. CẤU HÌNH BOT DISCORD & PHÂN QUYỀN
# ==============================================================================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

ALLOWED_USER_IDS = {
    572791257558941736,
    792068366335475723,
    649222792071348236,
}

@bot.check
async def globally_block_unauthorized(ctx):
    if ctx.command and ctx.command.name == "tomtat":
        return True
    if ALLOWED_USER_IDS and ctx.author.id not in ALLOWED_USER_IDS:
        await ctx.send("⛔ Bạn không có quyền sử dụng bot này.")
        return False
    return True

SYSTEM_PROMPT = (
    "Bạn là chuyên gia Production House (Art Director, Creative Director, Producer, DI Artist). "
    "Trả lời cực kỳ nhanh gọn, thẳng vào vấn đề, súc tích, không rào đón. "
    "Khi duyệt WIP: chỉ thẳng lỗi bố cục, ánh sáng, màu (DI) và cách fix ngắn gọn nhất."
)

# ==============================================================================
# 5. TIỆN ÍCH, XỬ LÝ ẢNH SIÊU TỐC CHỐNG TRÀN RAM & HỖ TRỢ LINK GOOGLE DRIVE
# ==============================================================================
async def send_long_message(destination, text: str):
    if not text:
        await destination.send("⚠️ Không có nội dung phản hồi.")
        return
    for i in range(0, len(text), 1990):
        await destination.send(text[i:i+1990])

def extract_gdrive_id(text: str):
    if not text:
        return None
    m = re.search(r'(?:drive\.google\.com/file/d/|drive\.google\.com/open\?id=|id=)([a-zA-Z0-9_-]{20,})', text)
    return m.group(1) if m else None

def strip_gdrive_links(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r'https?://drive\.google\.com/[^\s]+', '', text)
    return ' '.join(cleaned.split()).strip()

def compress_to_jpeg_bytes(image_bytes: bytes, max_size: int = 768, quality: int = 75) -> bytes:
    """Nén ảnh sang JPEG nhẹ an toàn, chống tràn RAM khi gặp file độ phân giải cực lớn"""
    with Image.open(io.BytesIO(image_bytes)) as img:
        if img.format == "JPEG":
            img.draft("RGB", (max_size, max_size))
        elif (img.width * img.height) > MAX_RAW_PIXELS:
            raise Exception(
                f"Ảnh có độ phân giải quá lớn ({img.width}x{img.height}px) dễ tràn RAM server. "
                "Hãy gửi bản preview nhỏ hơn hoặc dùng link Google Drive (công khai)."
            )

        if img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((max_size, max_size), Image.Resampling.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=False)
        return buf.getvalue()

async def fetch_safe_image_bytes(
    attachment: discord.Attachment = None,
    text: str = "",
    max_size: int = 512,
    quality: int = 72
) -> bytes:
    """Kéo ảnh JPEG đã nén sẵn từ CDN Discord/Google Drive — bỏ qua Pillow nếu đã đủ nhẹ để tiết kiệm 1s"""
    session = await get_http_session()

    drive_id = extract_gdrive_id(text)
    if drive_id and not attachment:
        thumb_url = f"https://drive.google.com/thumbnail?id={drive_id}&sz=w{max_size}"
        async with session.get(thumb_url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            ctype = resp.headers.get("Content-Type", "").lower()
            if resp.status == 200 and "image" in ctype:
                raw = await resp.read()
                if ("jpeg" in ctype or "jpg" in ctype) and len(raw) < 400_000:
                    return raw
                res = await asyncio.to_thread(compress_to_jpeg_bytes, raw, max_size, quality)
                del raw
                return res
        raise Exception(
            "Không lấy được ảnh từ Google Drive. Hãy bật quyền 'Bất kỳ ai có đường liên kết' "
            "và đảm bảo đó là link file ảnh/PSD."
        )

    if attachment:
        sep = "&" if "?" in attachment.proxy_url else "?"
        fast_url = f"{attachment.proxy_url}{sep}format=jpeg&width={max_size}&height={max_size}"
        try:
            async with session.get(fast_url, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                clen = int(resp.headers.get("Content-Length") or 0)
                ctype = resp.headers.get("Content-Type", "").lower()
                if resp.status == 200 and "image" in ctype and clen < 5 * 1024 * 1024:
                    raw = await resp.read()
                    # Nếu Discord CDN đã trả về JPEG nhẹ (<400KB), dùng thẳng không cần qua Pillow
                    if ("jpeg" in ctype or "jpg" in ctype) and len(raw) < 400_000:
                        return raw
                    if len(raw) < 5 * 1024 * 1024:
                        res = await asyncio.to_thread(compress_to_jpeg_bytes, raw, max_size, quality)
                        del raw
                        return res
                    del raw
        except Exception:
            pass

        size_mb = attachment.size / (1024 * 1024)
        if size_mb > MAX_RAW_DOWNLOAD_MB:
            raise Exception(
                f"File nặng {size_mb:.1f}MB vượt ngưỡng an toàn RAM ({MAX_RAW_DOWNLOAD_MB}MB). "
                "Hãy gửi bản JPEG preview nhẹ hoặc dán link Google Drive (công khai)."
            )

        raw = await attachment.read()
        try:
            return await asyncio.to_thread(compress_to_jpeg_bytes, raw, max_size, quality)
        finally:
            del raw
            gc.collect()

    raise Exception("Không tìm thấy ảnh đính kèm hoặc link Google Drive hợp lệ.")

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

# ==============================================================================
# 6. HÀM GỌI AI SIÊU TỐC (TẮT THINKING, CACHE MODEL NHANH, STREAMING)
# ==============================================================================
class AIResponseWrapper:
    def __init__(self, text: str, model_used: str):
        self.text = text
        self.model_used = model_used

def is_openrouter_model(model_name: str) -> bool:
    return "/" in model_name and not model_name.startswith("models/")

def build_gemini_config(max_tokens: int = 600, temperature: float = 0.6, disable_thinking: bool = True):
    cfg_kwargs = {
        "system_instruction": SYSTEM_PROMPT,
        "temperature": temperature,
        "max_output_tokens": max_tokens,
    }
    if disable_thinking and hasattr(types, "ThinkingConfig"):
        return types.GenerateContentConfig(
            **cfg_kwargs,
            thinking_config=types.ThinkingConfig(thinking_budget=0)
        )
    return types.GenerateContentConfig(**cfg_kwargs)

async def call_single_model(model_name: str, contents, max_tokens: int = 800, temperature: float = 0.6):
    if is_openrouter_model(model_name):
        if not OPENROUTER_API_KEY:
            raise Exception("Model dạng 'hãng/tên-model' cần gắn OPENROUTER_API_KEY trên Render!")

        user_parts = []
        items = contents if isinstance(contents, list) else [contents]
        for item in items:
            if isinstance(item, str):
                user_parts.append({"type": "text", "text": item})
            elif isinstance(item, Image.Image):
                buf = io.BytesIO()
                item.save(buf, format="JPEG", quality=80)
                b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
                user_parts.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
            elif hasattr(item, "inline_data") and getattr(item.inline_data, "data", None):
                mime = item.inline_data.mime_type or "image/jpeg"
                b64 = base64.b64encode(item.inline_data.data).decode("utf-8")
                user_parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})

        session = await get_http_session()
        headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
        payload = {
            "model": model_name,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_parts}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        async with session.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=20)
        ) as resp:
            data = await resp.json()
            if resp.status != 200:
                err_msg = data.get("error", {}).get("message", str(data))
                raise Exception(f"OpenRouter {resp.status}: {err_msg}")
            return AIResponseWrapper(text=data["choices"][0]["message"]["content"], model_used=model_name)
    else:
        if not GEMINI_API_KEY:
            raise Exception("Chưa cấu hình GEMINI_API_KEY trong mục Environment của Render!")
        try:
            res = await asyncio.wait_for(
                client_ai.aio.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=build_gemini_config(max_tokens=max_tokens, temperature=temperature, disable_thinking=True)
                ),
                timeout=15.0
            )
        except Exception as te:
            if any(k in str(te).lower() for k in ("thinking", "400", "invalid", "unsupported")):
                res = await asyncio.wait_for(
                    client_ai.aio.models.generate_content(
                        model=model_name,
                        contents=contents,
                        config=build_gemini_config(max_tokens=max_tokens, temperature=temperature, disable_thinking=False)
                    ),
                    timeout=15.0
                )
            else:
                raise te
        return AIResponseWrapper(text=res.text, model_used=model_name)

async def async_safe_generate_content(arg1, arg2=None, max_tokens: int = 800):
    global CACHED_FAST_MODEL
    contents = arg2 if arg2 is not None else arg1
    sequence = (
        ([ACTIVE_MODEL] if ACTIVE_MODEL else [])
        + ([CACHED_FAST_MODEL] if CACHED_FAST_MODEL else [])
        + FALLBACK_MODELS
    )
    models_to_try = list(dict.fromkeys(sequence))

    errors = []
    for model_name in models_to_try:
        try:
            res = await call_single_model(model_name, contents, max_tokens=max_tokens)
            if not ACTIVE_MODEL and not is_openrouter_model(model_name):
                CACHED_FAST_MODEL = model_name
            return res
        except asyncio.TimeoutError:
            if model_name == CACHED_FAST_MODEL:
                CACHED_FAST_MODEL = ""
            errors.append(f"{model_name}: Timeout")
            continue
        except Exception as e:
            if model_name == CACHED_FAST_MODEL:
                CACHED_FAST_MODEL = ""
            err_str = str(e).lower()
            errors.append(f"{model_name}: {str(e)[:70]}")
            if any(k in err_str for k in ("503", "unavailable", "demand", "404", "overloaded", "timeout", "429", "quota", "not found")):
                continue
            raise e

    raise Exception(" | ".join(errors))

async def stream_fast_reply(
    ctx,
    status_msg,
    contents,
    start_t: float,
    max_tokens: int = 350,
    temperature: float = 0.5,
    prefix: str = "",
    show_timer: bool = False,
    view=None,
) -> str:
    """Trả lời siêu tốc bằng Streaming (chữ chạy sau ~1s, xong trong 2-4s)"""
    global CACHED_FAST_MODEL

    # Nếu admin chọn đích danh OpenRouter thì gọi qua nhánh thường
    if ACTIVE_MODEL and is_openrouter_model(ACTIVE_MODEL):
        res = await call_single_model(ACTIVE_MODEL, contents, max_tokens=max_tokens, temperature=temperature)
        elapsed = round(time.monotonic() - start_t, 2)
        footer = f"\n\n*⚡ `{elapsed}s`*" if show_timer else ""
        final_text = f"{prefix}{res.text}{footer}"
        if len(final_text) <= 2000:
            await status_msg.edit(content=final_text, view=view)
        else:
            await status_msg.edit(content=final_text[:1990])
            await send_long_message(ctx, final_text[1990:])
            if view:
                await ctx.send("Tùy chọn thêm:", view=view)
        return res.text

    sequence = (
        ([ACTIVE_MODEL] if ACTIVE_MODEL else [])
        + ([CACHED_FAST_MODEL] if CACHED_FAST_MODEL else [])
        + FAST_WIP_MODELS
    )
    models_to_try = list(dict.fromkeys(sequence))

    full_text = ""
    errors = []

    for model_name in models_to_try:
        try:
            try:
                stream = await asyncio.wait_for(
                    client_ai.aio.models.generate_content_stream(
                        model=model_name,
                        contents=contents,
                        config=build_gemini_config(max_tokens=max_tokens, temperature=temperature, disable_thinking=True)
                    ),
                    timeout=8.0
                )
            except Exception as te:
                if any(k in str(te).lower() for k in ("thinking", "400", "invalid", "unsupported")):
                    stream = await asyncio.wait_for(
                        client_ai.aio.models.generate_content_stream(
                            model=model_name,
                            contents=contents,
                            config=build_gemini_config(max_tokens=max_tokens, temperature=temperature, disable_thinking=False)
                        ),
                        timeout=8.0
                    )
                else:
                    raise te

            last_edit = time.monotonic()
            async for chunk in stream:
                if chunk.text:
                    full_text += chunk.text
                    now_t = time.monotonic()
                    if now_t - last_edit >= 0.45 and full_text.strip():
                        try:
                            preview = f"{prefix}{full_text}"
                            await status_msg.edit(content=f"{preview[:1950]} ▌")
                        except Exception:
                            pass
                        last_edit = now_t

            if full_text.strip():
                if not ACTIVE_MODEL:
                    CACHED_FAST_MODEL = model_name
                break
        except asyncio.TimeoutError:
            if model_name == CACHED_FAST_MODEL:
                CACHED_FAST_MODEL = ""
            errors.append(f"{model_name}: Timeout 8s")
            full_text = ""
            continue
        except Exception as e:
            if model_name == CACHED_FAST_MODEL:
                CACHED_FAST_MODEL = ""
            errors.append(f"{model_name}: {str(e)[:70]}")
            full_text = ""
            continue

    if not full_text.strip():
        raise Exception(" | ".join(errors) or "Không nhận được phản hồi từ AI.")

    elapsed = round(time.monotonic() - start_t, 2)
    footer = f"\n\n*⚡ `{elapsed}s`*" if show_timer else ""
    final_text = f"{prefix}{full_text}{footer}"

    if len(final_text) <= 2000:
        await status_msg.edit(content=final_text, view=view)
    else:
        await status_msg.edit(content=final_text[:1990])
        await send_long_message(ctx, final_text[1990:])
        if view:
            await ctx.send("Bấm bên dưới nếu cần phân tích chi tiết hơn:", view=view)

    return full_text

async def run_apify_actor(actor_id: str, payload: dict):
    if not APIFY_API_TOKEN:
        raise Exception("Chưa cấu hình APIFY_API_TOKEN trong mục Environment của Render!")
    session = await get_http_session()
    endpoint = f"https://api.apify.com/v2/acts/{actor_id}/run-sync-get-dataset-items?token={APIFY_API_TOKEN}"
    async with session.post(endpoint, json=payload, timeout=aiohttp.ClientTimeout(total=60)) as resp:
        if resp.status not in (200, 201):
            raise Exception(f"Lỗi Apify (HTTP {resp.status}). Kiểm tra lại link hoặc Token.")
        return await resp.json()

# ==============================================================================
# 6.5. CÁC BẢNG NÚT BẤM TƯƠNG TÁC (WIP, NHẬN ROLE, TẠO PHÒNG HỌP)
# ==============================================================================
class DeepWipView(discord.ui.View):
    def __init__(self, attachment, text: str):
        super().__init__(timeout=300)
        self.attachment = attachment
        self.text = text

    @discord.ui.button(label="🔍 Soi sâu hơn", style=discord.ButtonStyle.secondary)
    async def deep_check(self, interaction: discord.Interaction, button: discord.ui.Button):
        if ALLOWED_USER_IDS and interaction.user.id not in ALLOWED_USER_IDS:
            await interaction.response.send_message("⛔ Bạn không có quyền dùng nút này.", ephemeral=True)
            return

        button.disabled = True
        button.label = "⏳ Đang soi sâu..."
        await interaction.response.edit_message(view=self)

        try:
            jpeg_bytes = await fetch_safe_image_bytes(self.attachment, self.text, max_size=1024, quality=82)
            image_part = types.Part.from_bytes(data=jpeg_bytes, mime_type="image/jpeg")

            note = strip_gdrive_links(self.text)
            extra_note = f"\nYêu cầu/Ghi chú từ Artist: {note}" if note else ""

            prompt_wip = (
                f"Phân tích WIP này chuyên sâu theo các hạng mục:{extra_note}\n"
                "1. Layout & Composition (Bố cục & Điểm nhìn)\n"
                "2. Lighting & Contrast (Ánh sáng & Tương phản)\n"
                "3. Color Grading / Blending (Màu sắc & Hòa trộn)\n"
                "4. Các điểm lỗi cần fix ngay và hướng dẫn xử lý kỹ thuật cụ thể."
            )
            response = await async_safe_generate_content([prompt_wip, image_part], max_tokens=900)
            reply_text = response.text if response and response.text else "Lỗi phản hồi."

            button.label = "✅ Đã soi sâu"
            await interaction.message.edit(view=self)
            await send_long_message(interaction.channel, f"🔎 **PHÂN TÍCH CHUYÊN SÂU:**\n{reply_text}")
        except Exception as e:
            button.disabled = False
            button.label = "🔍 Soi sâu hơn"
            try:
                await interaction.message.edit(view=self)
            except Exception:
                pass
            await interaction.channel.send(f"❌ Lỗi soi sâu: `{str(e)[:300]}`")

class RoleButton(discord.ui.Button):
    def __init__(self, role_cfg: dict):
        super().__init__(
            label=role_cfg["name"],
            emoji=role_cfg["emoji"],
            style=discord.ButtonStyle.secondary,
            custom_id=f"nad_{role_cfg['id']}"
        )
        self.role_name = role_cfg["name"]

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        member = interaction.user
        role = discord.utils.get(guild.roles, name=self.role_name)

        try:
            if not role:
                role = await guild.create_role(name=self.role_name, mentionable=True)

            if role in member.roles:
                await member.remove_roles(role)
                await interaction.response.send_message(f"➖ Đã gỡ role **{role.name}**.", ephemeral=True)
            else:
                await member.add_roles(role)
                await interaction.response.send_message(f"✅ Đã nhận role **{role.name}**!", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                "⛔ Bot chưa đủ quyền gán role này. Hãy bật **Manage Roles** và kéo role của bot lên trên cùng trong Server Settings.",
                ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(f"❌ Lỗi cập nhật role: `{str(e)[:200]}`", ephemeral=True)

class RoleSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for cfg in STUDIO_ROLES:
            self.add_item(RoleButton(cfg))

async def auto_delete_empty_meeting(channel: discord.VoiceChannel, delay: int = 120):
    await asyncio.sleep(delay)
    ch = bot.get_channel(channel.id)
    if ch and isinstance(ch, discord.VoiceChannel) and len(ch.members) == 0:
        try:
            await ch.delete(reason="Tự động xóa phòng họp trống")
        except Exception:
            pass
        TEMP_VOICE_CHANNELS.discard(channel.id)

class MeetingRoomView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Tạo phòng họp",
        emoji="🎙️",
        style=discord.ButtonStyle.primary,
        custom_id="nad_create_meeting_room"
    )
    async def create_meeting_room(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        member = interaction.user
        room_name = f"{MEETING_PREFIX}{member.display_name}"[:95]

        existing = discord.utils.get(guild.voice_channels, name=room_name)
        if existing:
            await interaction.response.send_message(
                f"🎙️ Bạn đang có sẵn phòng họp tại {existing.mention}. Bấm vào để tham gia!",
                ephemeral=True
            )
            return

        try:
            category = interaction.channel.category
            voice_ch = await guild.create_voice_channel(
                name=room_name,
                category=category,
                reason=f"Tạo phòng họp nhanh cho {member.display_name}"
            )
            TEMP_VOICE_CHANNELS.add(voice_ch.id)
            asyncio.create_task(auto_delete_empty_meeting(voice_ch, delay=120))

            await interaction.response.send_message(
                f"✅ Đã tạo phòng họp: {voice_ch.mention}\n"
                f"*(Phòng sẽ tự động xóa khi mọi người rời đi, hoặc sau 2 phút nếu không ai vào)*",
                ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "⛔ Bot chưa có quyền tạo kênh thoại. Hãy bật quyền **Manage Channels** cho bot.",
                ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(f"❌ Lỗi tạo phòng họp: `{str(e)[:200]}`", ephemeral=True)

# ==============================================================================
# 7. SỰ KIỆN BOT & CÁC VÒNG LẶP CHẠY NGẦM
# ==============================================================================
@bot.event
async def on_ready():
    global VIEWS_REGISTERED
    if not VIEWS_REGISTERED:
        bot.add_view(RoleSelectView())
        bot.add_view(MeetingRoomView())
        VIEWS_REGISTERED = True
        try:
            await bot.tree.sync()
            print("[SYSTEM] Đã đồng bộ Slash Command lên Discord.")
        except Exception as e:
            print(f"[SYSTEM] Lỗi đồng bộ Slash Command: {e}")

    print(f"[SYSTEM] Bot đã sẵn sàng: {bot.user}")
    print(f"[SYSTEM] Model ưu tiên: {ACTIVE_MODEL or 'Auto'} | Dự phòng: {', '.join(FALLBACK_MODELS)}")
    if not check_deadlines.is_running():
        check_deadlines.start()
    if not check_meetings.is_running():
        check_meetings.start()
    if not keep_alive_ping.is_running():
        keep_alive_ping.start()

@bot.event
async def on_voice_state_update(member, before, after):
    left_ch = before.channel
    if left_ch and left_ch != after.channel:
        is_temp = (left_ch.id in TEMP_VOICE_CHANNELS) or left_ch.name.startswith(MEETING_PREFIX)
        if is_temp and len(left_ch.members) == 0:
            try:
                await left_ch.delete(reason="Phòng họp đã kết thúc (không còn thành viên)")
            except Exception:
                pass
            TEMP_VOICE_CHANNELS.discard(left_ch.id)

@tasks.loop(minutes=5)
async def keep_alive_ping():
    def _ping():
        try:
            req = urllib.request.Request(f"{RENDER_EXTERNAL_URL}/health", headers={'User-Agent': 'BotKeeper'})
            with urllib.request.urlopen(req, timeout=10):
                pass
        except Exception:
            pass
    await asyncio.to_thread(_ping)

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    
    if not ALLOWED_USER_IDS or message.author.id in ALLOWED_USER_IDS:
        clean_content = message.content.lower().translate(str.maketrans('', '', string.punctuation)).strip()
        is_done_keyword = clean_content in ["done", "xong", "hoàn thành", "da xong", "hoan thanh"]
        
        if is_done_keyword:
            user_mention = f"<@{message.author.id}>"
            for t in TASKS:
                if t['mention_str'] == user_mention and t['status'] in ["Chưa hoàn thành", "Quá hạn"]:
                    t['status'] = "Đã hoàn thành"
                    save_data()
                    await message.channel.send(f"✅ Đã cập nhật task **{t['task']}** thành hoàn thành.")
                    break

    await bot.process_commands(message)

async def get_channel_safe(channel_id):
    ch = bot.get_channel(channel_id)
    if not ch:
        try:
            ch = await bot.fetch_channel(channel_id)
        except Exception:
            return None
    return ch

@tasks.loop(seconds=30)
async def check_deadlines():
    now = now_vn()
    updated = False
    for t in TASKS:
        if t['status'] == "Chưa hoàn thành":
            try:
                deadline_time = datetime.strptime(t['deadline'], "%d/%m/%Y %H:%M")
                if now >= deadline_time:
                    channel = await get_channel_safe(t['channel_id'])
                    if channel:
                        await channel.send(f"🚨 **Quá hạn**: {t['mention_str']} Task **{t['task']}** đã đến hạn (`{t['deadline']}`). Báo cáo ngay!")
                    t['status'] = "Quá hạn"
                    updated = True
                else:
                    last_rem_str = t.get('last_reminder')
                    last_rem = datetime.fromisoformat(last_rem_str) if last_rem_str else None
                    if not last_rem or (now - last_rem).total_seconds() >= 900:
                        channel = await get_channel_safe(t['channel_id'])
                        if channel:
                            await channel.send(f"⏳ **Nhắc nhở**: {t['mention_str']} Task **{t['task']}** đến hạn lúc `{t['deadline']}`.")
                        t['last_reminder'] = now.isoformat()
                        updated = True
            except Exception:
                pass
    if updated:
        save_data()

@tasks.loop(seconds=15)
async def check_meetings():
    now = now_vn()
    meetings_to_remove = []
    for i, m in enumerate(meetings):
        try:
            meeting_time = datetime.strptime(m['time'], "%d/%m/%Y %H:%M")
            if now >= meeting_time:
                channel = await get_channel_safe(m['channel_id'])
                if channel:
                    target_ping = "@everyone" if m['ping_everyone'] else m['mention_str']
                    await channel.send(f"📢 {target_ping} Đến giờ họp: **{m['topic']}** (`{m['time']}`).")
                meetings_to_remove.append(i)
        except Exception:
            pass
    if meetings_to_remove:
        for i in reversed(meetings_to_remove):
            meetings.pop(i)
        save_data()

@check_deadlines.before_loop
@check_meetings.before_loop
@keep_alive_ping.before_loop
async def before_loops():
    await bot.wait_until_ready()

# ==============================================================================
# 8. LỆNH HƯỚNG DẪN (!huongdan / !help & Slash /huongdan)
# ==============================================================================
@bot.command(name="huongdan", aliases=["help", "lenh"])
async def show_help(ctx):
    current_m = ACTIVE_MODEL if ACTIVE_MODEL else f"Auto ({CACHED_FAST_MODEL or FALLBACK_MODELS[0]})"
    embed = discord.Embed(
        title="🎬 CẨM NANG BOT PRODUCTION HOUSE",
        description=f"Model đang chạy: **`{current_m}`**",
        color=discord.Color.gold()
    )
    embed.add_field(
        name="📋 1. Task & Deadline",
        value=(
            "• `!task Key Visual 18:00 28/10` — Tạo task & mở Thread\n"
            "• Nhắn `done` hoặc `xong` — Tự động báo hoàn thành\n"
            "• `!danhsachtask` | `!hoanthanh 0` | `!xoatask 0`"
        ),
        inline=False
    )
    embed.add_field(
        name="📅 2. Lịch họp",
        value=(
            "• `!hop 20:30 26/10 Duyệt concept` — Họp chung (@everyone)\n"
            "• `!hoprieng @User 20:30 26/10 Duyệt bài` — Họp riêng\n"
            "• `!danhsachhop` — Xem lịch họp sắp tới"
        ),
        inline=False
    )
    embed.add_field(
        name="🧠 3. AI, Duyệt WIP & Tóm tắt kênh",
        value=(
            "• `!wip` (hoặc `!checkwip`) — Thả ảnh/link Drive soi siêu tốc, cần soi kỹ bấm nút ngay dưới bài\n"
            "• `!tomtat` (hoặc `!tt`, `!dangnoigi`) — Tóm tắt chat kênh hiện tại, tag `#kênh` hoặc đặt câu hỏi\n"
            "• `!ai câu hỏi` — Hỏi đáp AI (kèm ảnh hoặc link Drive được)\n"
            "• `!taoprompt ý tưởng` | `!datten tên file` | `!resetai`\n"
            "• `!listmodel` | `!setmodel tên_model` | `!checkapi`"
        ),
        inline=False
    )
    embed.add_field(
        name="🌐 4. Soi Social Media",
        value=(
            "• `!soitiktok link` | `!soifb link` | `!soiig link` | `!soix link`"
        ),
        inline=False
    )
    embed.add_field(
        name="⚙️ 5. Tự động hóa Server & File",
        value=(
            "• `!setuproles` — Gửi bảng nút bấm tự nhận/gỡ Role bộ phận\n"
            "• `!setuphop` — Gửi bảng nút bấm tự tạo phòng họp thoại (tự xóa khi trống)\n"
            "• `!nopfile Tên dự án` (kèm file/link) | `!tonghoplink`"
        ),
        inline=False
    )
    embed.set_footer(text=f"Múi giờ: UTC+7 ({now_vn().strftime('%H:%M - %d/%m/%Y')})")
    await ctx.send(embed=embed)

@bot.tree.command(name="huongdan", description="Xem danh sách lệnh của NAD Prism")
async def slash_help(interaction: discord.Interaction):
    await interaction.response.send_message(
        "🎬 **NAD Prism** đã sẵn sàng! Gõ `!huongdan` trong kênh chat để mở đầy đủ bảng lệnh (`!wip`, `!tomtat`, `!task`, `!hop`, `!setuproles`, `!setuphop`).",
        ephemeral=True
    )

# ==============================================================================
# 8.5. CÁC LỆNH QUẢN LÝ SERVER (BẢNG ROLE & NÚT TẠO PHÒNG HỌP)
# ==============================================================================
@bot.command(name="setuproles")
async def setup_roles(ctx):
    embed = discord.Embed(
        title="🎭 CHỌN BỘ PHẬN / ROLE CỦA BẠN",
        description=(
            "Bấm vào các nút bên dưới để **nhận Role** (hoặc bấm lại lần nữa để **gỡ Role**).\n"
            "Nhận đúng Role để mở khóa các kênh làm việc tương ứng trong Studio."
        ),
        color=discord.Color.blurple()
    )
    roles_list = "\n".join(f"{r['emoji']} **{r['name']}**" for r in STUDIO_ROLES)
    embed.add_field(name="Danh sách vị trí", value=roles_list, inline=False)
    await ctx.send(embed=embed, view=RoleSelectView())

@bot.command(name="setuphop")
async def setup_meeting_panel(ctx):
    embed = discord.Embed(
        title="🎙️ TẠO PHÒNG HỌP NHANH",
        description=(
            "Bấm nút **Tạo phòng họp** bên dưới để mở ngay một phòng thoại riêng.\n"
            "• Phòng sẽ **tự động xóa** ngay khi người cuối cùng thoát ra.\n"
            "• Nếu không có ai vào sau 2 phút, phòng cũng tự dọn dẹp."
        ),
        color=discord.Color.green()
    )
    await ctx.send(embed=embed, view=MeetingRoomView())

# ==============================================================================
# 9. CÁC LỆNH QUẢN LÝ TASK
# ==============================================================================
@bot.command(name="task")
async def add_task(ctx, *, content: str):
    parsed_dt = parse_smart_datetime(content)
    if not parsed_dt:
        await ctx.send("⚠️ Thiếu thời gian hợp lệ. Hãy nhập giờ (VD: `18:00`) và ngày (VD: `28/10`).")
        return
    
    task_name = extract_topic(content) or "Task mới"
    deadline_str = parsed_dt.strftime("%d/%m/%Y %H:%M")
    assignee = f"<@{ctx.author.id}>"

    forum_name = "bảng-công-việc"
    forum = discord.utils.get(ctx.guild.forums, name=forum_name)
    if not forum:
        try:
            forum = await ctx.guild.create_forum(name=forum_name)
        except Exception as e:
            await ctx.send(f"❌ Lỗi tạo kênh Forum: {e}")
            return
    try:
        thread_msg = await forum.create_thread(
            name=task_name[:95],
            content=f"🚀 **Deadline Mới**\n• Người phụ trách: {assignee}\n• Nội dung: **{task_name}**\n• Hạn nộp: `{deadline_str}`"
        )
        new_thread = thread_msg.thread
    except Exception as e:
        await ctx.send(f"❌ Lỗi tạo Thread: {e}")
        return

    TASKS.append({
        "task": task_name, 
        "deadline": deadline_str, 
        "mention_str": assignee, 
        "channel_id": new_thread.id,
        "status": "Chưa hoàn thành",
        "last_reminder": now_vn().isoformat()
    })
    save_data()
    await ctx.send(f"✅ Đã tạo task **'{task_name}'** tại {new_thread.mention} (Hạn: `{deadline_str}`). Nhắc nhở mỗi 15 phút.")

@bot.command(name="hoanthanh")
async def complete_task(ctx, index_str: str):
    try:
        index = int(index_str)
        if 0 <= index < len(TASKS):
            TASKS[index]["status"] = "Đã hoàn thành"
            save_data()
            await ctx.send(f"✅ Cập nhật task `{index}`: **{TASKS[index]['task']}** thành hoàn thành.")
        else:
            await ctx.send("❌ Số thứ tự không tồn tại. Gõ `!danhsachtask` để kiểm tra.")
    except ValueError:
        await ctx.send("❌ Số thứ tự phải là số nguyên.")

@bot.command(name="danhsachtask")
async def list_tasks(ctx):
    if not TASKS:
        await ctx.send("🎉 Không có task nào tồn đọng.")
        return
    msg = "📋 **Danh sách Task:**\n"
    for i, t in enumerate(TASKS):
        icon = "🟢 [Xong]" if t['status'] == "Đã hoàn thành" else ("🔴 [Quá hạn]" if t['status'] == "Quá hạn" else "🟡 [Đang làm]")
        msg += f"`{i}`. {icon} **{t['task']}** — Hạn: `{t['deadline']}` — {t['mention_str']}\n"
    await send_long_message(ctx, msg)

@bot.command(name="xoatask")
async def remove_task(ctx, index_str: str):
    try:
        index = int(index_str)
        if 0 <= index < len(TASKS):
            removed = TASKS.pop(index)
            save_data()
            ch = await get_channel_safe(removed['channel_id'])
            if ch:
                try:
                    await ch.delete()
                except Exception:
                    pass
            await ctx.send(f"🗑️ Đã xóa task: **{removed['task']}**.")
        else:
            await ctx.send("❌ Số thứ tự không tồn tại.")
    except ValueError:
        await ctx.send("❌ Vui lòng nhập số nguyên.")

# ==============================================================================
# 10. CÁC LỆNH LỊCH HỌP
# ==============================================================================
@bot.command(name="hop")
async def schedule_meeting(ctx, *, content: str):
    parsed_dt = parse_smart_datetime(content)
    if not parsed_dt:
        await ctx.send("⚠️ Thiếu thời gian. Nhập theo cú pháp: `!hop 20:00 28/10 Nội dung họp`")
        return
    
    topic = extract_topic(content) or "Họp chung"
    full_time = parsed_dt.strftime("%d/%m/%Y %H:%M")
    meetings.append({"time": full_time, "topic": topic, "channel_id": ctx.channel.id, "ping_everyone": True, "mention_str": ""})
    save_data()
    await ctx.send(f"📅 Đã lên lịch họp **'{topic}'** lúc `{full_time}` (@everyone).")

@bot.command(name="hoprieng")
async def schedule_meeting_user(ctx, *, content: str):
    user_id_match = re.search(r'<@!?(\d+)>', content)
    if not user_id_match:
        await ctx.send("⚠️ Bạn chưa tag người cần họp (VD: `!hoprieng @User 20:00 28/10 Duyệt bài`).")
        return
    
    member_mention = f"<@{user_id_match.group(1)}>"
    parsed_dt = parse_smart_datetime(content)
    if not parsed_dt:
        await ctx.send("⚠️ Thiếu thời gian hợp lệ.")
        return

    topic = extract_topic(content) or "Họp riêng"
    full_time = parsed_dt.strftime("%d/%m/%Y %H:%M")
    meetings.append({"time": full_time, "topic": topic, "channel_id": ctx.channel.id, "ping_everyone": False, "mention_str": member_mention})
    save_data()
    await ctx.send(f"📅 Đã lên lịch họp với {member_mention}: **'{topic}'** lúc `{full_time}`.")

@bot.command(name="danhsachhop")
async def list_meetings(ctx):
    if not meetings:
        await ctx.send("📭 Hiện không có lịch họp nào.")
        return
    msg = "📅 **Lịch họp sắp tới:**\n"
    for i, m in enumerate(meetings):
        target = "@everyone" if m['ping_everyone'] else m['mention_str']
        msg += f"`{i}`. **{m['topic']}** — `{m['time']}` — {target}\n"
    await send_long_message(ctx, msg)

# ==============================================================================
# 11. CÁC LỆNH AI, QUẢN LÝ MODEL & TÓM TẮT KÊNH (TỐI ƯU < 3-4S)
# ==============================================================================
@bot.command(name="listmodel")
async def list_models(ctx):
    async with ctx.typing():
        try:
            def _fetch():
                return [m.name.replace("models/", "") for m in client_ai.models.list()]
            models = await asyncio.wait_for(asyncio.to_thread(_fetch), timeout=15.0)
            await send_long_message(
                ctx,
                "📋 **Các Model khả dụng trên Gemini Key của bạn:**\n`" + "`\n`".join(models) + "`\n\n"
                "👉 Dùng `!setmodel tên_model` để chuyển ngay (hoặc nhập mã OpenRouter dạng `hãng/tên-model`)."
            )
        except Exception as e:
            await ctx.send(f"❌ Lỗi lấy danh sách model: `{str(e)[:250]}`")

@bot.command(name="setmodel")
async def set_model(ctx, model_id: str = None):
    global ACTIVE_MODEL, CACHED_FAST_MODEL
    if not model_id:
        current = ACTIVE_MODEL if ACTIVE_MODEL else f"Auto ({CACHED_FAST_MODEL or FALLBACK_MODELS[0]})"
        await ctx.send(f"ℹ️ Model hiện tại: **`{current}`**\nDùng `!setmodel tên_model` để đổi hoặc `!setmodel auto` để tự động.")
        return

    if model_id.lower() == "auto":
        ACTIVE_MODEL = ""
        CACHED_FAST_MODEL = ""
        save_data()
        await ctx.send(f"✅ Đã chuyển về chế độ **Auto Fallback**.")
        return

    await ctx.send(f"🔄 Đang kiểm tra kết nối tới model **`{model_id}`**...")
    async with ctx.typing():
        try:
            await call_single_model(model_id, ["Trả lời 1 chữ: OK"], max_tokens=10)
            ACTIVE_MODEL = model_id
            CACHED_FAST_MODEL = ""
            save_data()
            source = "OpenRouter" if is_openrouter_model(model_id) else "Google Gemini"
            await ctx.send(f"✅ Đã kích hoạt model **`{model_id}`** ({source}) thành công!")
        except Exception as e:
            await ctx.send(
                f"❌ Không thể dùng model **`{model_id}`** (đã giữ nguyên model cũ).\n"
                f"Lý do: `{str(e)[:250]}`"
            )

@bot.command(name="checkapi")
async def check_api(ctx):
    start_t = time.monotonic()
    try:
        res = await async_safe_generate_content(["Ping test. Trả lời đúng 1 chữ: OK"], max_tokens=10)
        elapsed = round(time.monotonic() - start_t, 2)
        apify_status = "✅ Đã gắn" if APIFY_API_TOKEN else "⚪ Chưa gắn"
        or_status = "✅ Đã gắn" if OPENROUTER_API_KEY else "⚪ Chưa gắn (tùy chọn)"
        await ctx.send(
            f"⚡ **Phản hồi AI:** `{elapsed}s` | **Ping Discord:** `{round(bot.latency * 1000)}ms`\n"
            f"🔑 **Apify:** {apify_status} | **OpenRouter:** {or_status}"
        )
    except Exception as e:
        await ctx.send(f"❌ **Lỗi kiểm tra AI:** `{str(e)[:250]}`")

@bot.command(name="ai")
async def ask_ai(ctx, *, prompt: str = None):
    has_attachment = bool(ctx.message.attachments)
    has_drive = bool(extract_gdrive_id(prompt or ""))
    if not prompt and not has_attachment:
        await ctx.send("⚠️ Vui lòng nhập câu hỏi, đính kèm ảnh hoặc dán link Google Drive.")
        return

    start_t = time.monotonic()
    send_task = asyncio.create_task(ctx.send("⚡ ..."))
    img_task = None
    if has_attachment or has_drive:
        att = ctx.message.attachments[0] if has_attachment else None
        img_task = asyncio.create_task(fetch_safe_image_bytes(att, prompt or "", max_size=640, quality=75))

    status_msg = await send_task
    try:
        if ctx.author.id not in USER_CHATS:
            USER_CHATS[ctx.author.id] = []
        history = USER_CHATS[ctx.author.id]
        full_context = "".join(f"User: {h['user']}\nAssistant: {h['assistant']}\n" for h in history[-2:])

        clean_prompt = strip_gdrive_links(prompt) if has_drive else (prompt or "")
        user_text = clean_prompt if clean_prompt else "Phân tích hình ảnh này ngắn gọn dưới góc độ chuyên môn."
        full_context += f"User: {user_text}\nAssistant (trả lời súc tích, đúng trọng tâm):"

        contents = [full_context]
        if img_task:
            jpeg_bytes = await img_task
            contents.append(types.Part.from_bytes(data=jpeg_bytes, mime_type="image/jpeg"))

        reply_text = await stream_fast_reply(
            ctx, status_msg, contents, start_t,
            max_tokens=550, temperature=0.6, show_timer=True
        )
        history.append({"user": user_text, "assistant": reply_text[:500]})
        if len(history) > 3:
            history.pop(0)
    except Exception as e:
        await status_msg.edit(content=f"❌ Lỗi AI: `{str(e)[:300]}`")

@bot.command(name="wip", aliases=["checkwip"])
async def check_wip(ctx, *, text: str = ""):
    """Soi WIP siêu tốc dưới 3-4s (chạy chữ live + nút Soi sâu hơn)"""
    has_attachment = bool(ctx.message.attachments)
    has_drive = bool(extract_gdrive_id(text))
    if not has_attachment and not has_drive:
        await ctx.send("⚠️ Thả ảnh WIP hoặc dán link Google Drive công khai kèm lệnh `!wip`.")
        return
    if not GEMINI_API_KEY:
        await ctx.send("❌ Chưa cấu hình `GEMINI_API_KEY` trong mục Environment của Render!")
        return

    start_t = time.monotonic()
    attachment = ctx.message.attachments[0] if has_attachment else None

    # Chạy song song gửi tin nhắn chờ & tải ảnh để tiết kiệm ~0.4s
    send_task = asyncio.create_task(ctx.send("⚡ Đang soi WIP..."))
    img_task = asyncio.create_task(fetch_safe_image_bytes(attachment, text, max_size=512, quality=72))
    status_msg = await send_task

    try:
        jpeg_bytes = await img_task
        image_part = types.Part.from_bytes(data=jpeg_bytes, mime_type="image/jpeg")

        note = strip_gdrive_links(text)
        extra_note = f"\nGhi chú: {note}" if note else ""

        prompt_wip = (
            f"Soi WIP cực ngắn gọn (tối đa 120 chữ), gạch đầu dòng thẳng lỗi cần sửa:{extra_note}\n"
            "• Bố cục/Layout:\n"
            "• Ánh sáng/Contrast:\n"
            "• Màu/DI/Blending:\n"
            "• Fix gấp:"
        )

        view = DeepWipView(attachment, text)
        await stream_fast_reply(
            ctx, status_msg, [prompt_wip, image_part], start_t,
            max_tokens=280, temperature=0.4, show_timer=True, view=view
        )
    except Exception as e:
        await status_msg.edit(content=f"❌ Lỗi soi WIP: `{str(e)[:300]}`")

@bot.command(name="tomtat", aliases=["tt", "dangnoigi"])
async def summarize_channel(ctx, *, query: str = ""):
    """Tóm tắt kênh siêu tốc bằng Streaming"""
    start_t = time.monotonic()
    target_channel = ctx.channel
    limit = 50

    if ctx.message.channel_mentions:
        target_channel = ctx.message.channel_mentions[0]
        query = re.sub(r'<#\d+>', '', query).strip()

    num_match = re.search(r'\b(\d{1,3})\b', query)
    if num_match:
        val = int(num_match.group(1))
        if 5 <= val <= 200:
            limit = val
            query = re.sub(r'\b' + num_match.group(1) + r'\b', '', query, count=1).strip()

    status_msg = await ctx.send(f"📖 Đang đọc {limit} tin nhắn ở {target_channel.mention}...")
    try:
        messages = []
        async for msg in target_channel.history(limit=limit + 1):
            if msg.id == ctx.message.id:
                continue
            content = msg.content.strip()
            if not content and msg.attachments:
                content = f"[{len(msg.attachments)} file]"
            if not content or content.startswith(("!tomtat", "!tt", "!dangnoigi")):
                continue
            t_str = msg.created_at.astimezone(VN_TZ).strftime("%H:%M")
            messages.append(f"[{t_str}] {msg.author.display_name}: {content[:200]}")

        if not messages:
            await status_msg.edit(content="📭 Không có tin nhắn nào để đọc trong kênh này.")
            return

        messages.reverse()
        chat_log = "\n".join(messages)

        if query:
            prompt = f"Chat #{target_channel.name}:\n{chat_log}\n---\nTrả lời ngắn gọn câu hỏi: {query}"
        else:
            prompt = (
                f"Chat #{target_channel.name}:\n{chat_log}\n---\n"
                "Tóm tắt siêu ngắn gọn (tối đa 150 chữ):\n"
                "1. Đang bàn chuyện gì?\n2. Ý đã chốt.\n3. Đầu việc & người phụ trách."
            )

        prefix = f"📝 **TÓM TẮT #{target_channel.name}** ({len(messages)} tin nhắn):\n\n"
        await stream_fast_reply(
            ctx, status_msg, [prompt], start_t,
            max_tokens=350, temperature=0.4, prefix=prefix, show_timer=True
        )
    except discord.Forbidden:
        await status_msg.edit(content=f"⛔ Bot chưa có quyền đọc {target_channel.mention}. Hãy bật **View Channel** và **Read Message History**.")
    except Exception as e:
        await status_msg.edit(content=f"❌ Lỗi tóm tắt: `{str(e)[:250]}`")

@bot.command(name="resetai")
async def reset_ai(ctx):
    if ctx.author.id in USER_CHATS:
        del USER_CHATS[ctx.author.id]
        await ctx.send("🧹 Đã xóa lịch sử trò chuyện AI của bạn.")
    else:
        await ctx.send("📭 Lịch sử trò chuyện đang trống.")

@bot.command(name="taoprompt")
async def tao_prompt(ctx, *, idea: str):
    start_t = time.monotonic()
    status_msg = await ctx.send("⚡ Đang tạo prompt...")
    try:
        prompt_req = f"Tạo prompt AI tiếng Anh chi tiết nhưng súc tích (Subject, Environment, Lighting, Camera, Render Style) từ ý tưởng: '{idea}'."
        await stream_fast_reply(ctx, status_msg, [prompt_req], start_t, max_tokens=350, temperature=0.6, show_timer=True)
    except Exception as e:
        await status_msg.edit(content=f"❌ Lỗi: `{str(e)[:300]}`")

@bot.command(name="datten")
async def dat_ten(ctx, *, filename: str):
    start_t = time.monotonic()
    status_msg = await ctx.send("⚡ ...")
    try:
        prompt_req = f"Chuyển '{filename}' sang chuẩn đặt tên Production House (YYYYMMDD_Project_Asset_Version, không dấu, underscore). Chỉ trả về đúng tên file."
        await stream_fast_reply(ctx, status_msg, [prompt_req], start_t, max_tokens=100, temperature=0.2, show_timer=True)
    except Exception as e:
        await status_msg.edit(content=f"❌ Lỗi: `{str(e)[:300]}`")

@bot.command(name="nopfile")
async def nop_file(ctx, *, project_name: str = "Dự án chung"):
    urls = re.findall(r'(https?://[^\s]+)', ctx.message.content)
    files = [att.url for att in ctx.message.attachments]
    all_links = urls + files
    if not all_links:
        await ctx.send("⚠️ Vui lòng gửi kèm file đính kèm hoặc gắn link trong tin nhắn `!nopfile`.")
        return
    
    clean_project = re.sub(r'https?://[^\s]+', '', project_name).strip() or "Dự án chung"
    SUBMISSIONS.append({
        "project": clean_project,
        "user": f"<@{ctx.author.id}>",
        "links": all_links,
        "time": now_vn().strftime("%d/%m/%Y %H:%M")
    })
    save_data()
    await ctx.send(f"📁 Đã ghi nhận {len(all_links)} file/link cho dự án **{clean_project}**.")

@bot.command(name="tonghoplink")
async def aggregate_links(ctx, limit: int = 100):
    drive_links = []
    async for message in ctx.channel.history(limit=limit):
        for url in re.findall(r'(https?://(?:drive|docs)\.google\.com/[^\s]+)', message.content):
            if url not in drive_links:
                drive_links.append(url)
    if not drive_links:
        await ctx.send("📭 Không tìm thấy link Google Drive/Docs nào trong lịch sử gần đây.")
        return
    msg = "🔗 **Tổng hợp Link Google Drive/Docs:**\n" + "\n".join(f"`{i+1}`. <{link}>" for i, link in enumerate(drive_links))
    await send_long_message(ctx, msg)

# ==============================================================================
# 12. CÁC LỆNH SOI SOCIAL MEDIA QUA APIFY (TIKTOK / FB / IG / X)
# ==============================================================================
@bot.command(name="soitiktok")
async def soi_tiktok(ctx, url: str):
    await ctx.send("🔍 Đang kéo dữ liệu TikTok và phân tích...")
    async with ctx.typing():
        try:
            data = await run_apify_actor("clockworks~tiktok-scraper", {"postURLs": [url], "resultsPerPage": 1})
            if not data:
                await ctx.send("⚠️ Không lấy được dữ liệu (video có thể đang để riêng tư).")
                return

            v = data[0]
            stats = (
                f"• **Kênh:** {v.get('authorMeta', {}).get('name', 'N/A')}\n"
                f"• **Caption:** {v.get('text', 'N/A')}\n"
                f"• **Chỉ số:** {v.get('playCount', 0):,} views | {v.get('diggCount', 0):,} tim | "
                f"{v.get('commentCount', 0):,} cmt | {v.get('shareCount', 0):,} share\n"
                f"• **Thời lượng:** {v.get('videoMeta', {}).get('duration', 0)} giây"
            )
            prompt = (
                f"Thông số video TikTok tham khảo cho Invisible Space Studio:\n{stats}\n\n"
                "Phân tích ngắn gọn theo góc nhìn Creative/Art Director:\n"
                "1. Đánh giá hiệu quả tương tác.\n"
                "2. Dự đoán Hook & Mood chính.\n"
                "3. Gợi ý 1 hướng chuyển hóa sang Visual/TVC cho Studio."
            )
            ai_res = await async_safe_generate_content([prompt], max_tokens=500)
            await send_long_message(ctx, f"🎵 **THÔNG SỐ TIKTOK:**\n{stats}\n\n🎬 **PHÂN TÍCH STUDIO:**\n{ai_res.text}")
        except Exception as e:
            await ctx.send(f"❌ Lỗi soi TikTok: `{str(e)[:300]}`")

@bot.command(name="soifb")
async def soi_fb(ctx, url: str):
    await ctx.send("🔍 Đang kéo dữ liệu Facebook và phân tích...")
    async with ctx.typing():
        try:
            data = await run_apify_actor("apify~facebook-posts-scraper", {"startUrls": [{"url": url}], "resultsLimit": 1})
            if not data:
                await ctx.send("⚠️ Không lấy được dữ liệu (bài viết cần để chế độ Công khai).")
                return

            p = data[0]
            text_content = (p.get("text") or "N/A")[:400]
            stats = (
                f"• **Page/Tác giả:** {p.get('pageName') or p.get('user', {}).get('name', 'N/A')}\n"
                f"• **Nội dung:** {text_content}\n"
                f"• **Chỉ số:** {p.get('likes', 0):,} likes | {p.get('comments', 0):,} cmt | {p.get('shares', 0):,} share"
            )
            prompt = (
                f"Thông số bài viết Facebook tham khảo cho Invisible Space Studio:\n{stats}\n\n"
                "Phân tích ngắn gọn: Điểm mạnh của góc tiếp cận/copywriting, đánh giá tương tác và gợi ý cách áp dụng cho bài đăng của Studio."
            )
            ai_res = await async_safe_generate_content([prompt], max_tokens=500)
            await send_long_message(ctx, f"📘 **THÔNG SỐ FACEBOOK:**\n{stats}\n\n🎬 **PHÂN TÍCH STUDIO:**\n{ai_res.text}")
        except Exception as e:
            await ctx.send(f"❌ Lỗi soi Facebook: `{str(e)[:300]}`")

@bot.command(name="soiig")
async def soi_ig(ctx, url: str):
    await ctx.send("🔍 Đang kéo dữ liệu Instagram và phân tích...")
    async with ctx.typing():
        try:
            data = await run_apify_actor("apify~instagram-scraper", {"directUrls": [url], "resultsLimit": 1})
            if not data:
                await ctx.send("⚠️ Không lấy được dữ liệu (tài khoản cần ở chế độ Công khai).")
                return

            p = data[0]
            caption = (p.get("caption") or "N/A")[:400]
            stats = (
                f"• **Tài khoản:** @{p.get('ownerUsername', 'N/A')}\n"
                f"• **Loại:** {p.get('type', 'Post')}\n"
                f"• **Caption:** {caption}\n"
                f"• **Chỉ số:** {p.get('likesCount', 0):,} likes | {p.get('commentsCount', 0):,} cmt | {p.get('videoViewCount', 0):,} views"
            )
            prompt = (
                f"Thông số bài Instagram tham khảo cho Invisible Space Studio:\n{stats}\n\n"
                "Phân tích ngắn gọn dưới góc nhìn Art Director: Cách dẫn dắt nội dung, mức độ hiệu quả và 1 gợi ý làm Visual/Reels tương tự."
            )
            ai_res = await async_safe_generate_content([prompt], max_tokens=500)
            await send_long_message(ctx, f"📸 **THÔNG SỐ INSTAGRAM:**\n{stats}\n\n🎬 **PHÂN TÍCH STUDIO:**\n{ai_res.text}")
        except Exception as e:
            await ctx.send(f"❌ Lỗi soi Instagram: `{str(e)[:300]}`")

@bot.command(name="soix")
async def soi_x(ctx, url: str):
    await ctx.send("🔍 Đang kéo dữ liệu X (Twitter) và phân tích...")
    async with ctx.typing():
        try:
            data = await run_apify_actor("apidojo~tweet-scraper", {"startUrls": [url], "maxItems": 1})
            if not data:
                await ctx.send("⚠️ Không lấy được dữ liệu bài đăng X này.")
                return

            t = data[0]
            author = t.get("author", {}).get("userName") or t.get("user", {}).get("screen_name", "N/A")
            stats = (
                f"• **Tác giả:** @{author}\n"
                f"• **Nội dung:** {(t.get('text') or t.get('fullText') or 'N/A')[:400]}\n"
                f"• **Chỉ số:** {t.get('likeCount', 0):,} likes | {t.get('retweetCount', 0):,} RT | "
                f"{t.get('replyCount', 0):,} reply | {t.get('viewCount', 0):,} views"
            )
            prompt = (
                f"Thông số bài đăng X (Twitter) tham khảo cho Invisible Space Studio:\n{stats}\n\n"
                "Phân tích ngắn gọn: Tại sao bài này thu hút (hoặc chưa thu hút), insight chính và gợi ý ứng dụng cho kênh truyền thông/portfolio quốc tế của Studio."
            )
            ai_res = await async_safe_generate_content([prompt], max_tokens=500)
            await send_long_message(ctx, f"🐦 **THÔNG SỐ X (TWITTER):**\n{stats}\n\n🎬 **PHÂN TÍCH STUDIO:**\n{ai_res.text}")
        except Exception as e:
            await ctx.send(f"❌ Lỗi soi X: `{str(e)[:300]}`")

# ==============================================================================
# 13. CỔNG MẠNG VÀ CHỐNG NGỦ ĐÔNG CHO RENDER
# ==============================================================================
async def handle_ping(request):
    return web.Response(text="Bot is running fast 24/7!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[WEB SERVER] Cổng mạng Render {port} đã mở thành công.")

async def main():
    await start_web_server()
    try:
        async with bot:
            await bot.start(DISCORD_TOKEN)
    finally:
        if HTTP_SESSION and not HTTP_SESSION.closed:
            await HTTP_SESSION.close()

if __name__ == "__main__":
    asyncio.run(main())
