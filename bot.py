import os
import warnings
import re
import asyncio
import io
import json
import string
from datetime import datetime, timezone, timedelta
from aiohttp import web
from dotenv import load_dotenv
import discord
from discord.ext import commands, tasks
from google import genai
from google.genai import types
from PIL import Image

warnings.filterwarnings('ignore')
load_dotenv()

# --- 1. CẤU HÌNH MÚI GIỜ VIỆT NAM (UTC+7 CHO RENDER) ---
VN_TZ = timezone(timedelta(hours=7))

def now_vn():
    return datetime.now(VN_TZ).replace(tzinfo=None)

# --- 2. CẤU HÌNH TOKEN & GEMINI API KEY (LẤY TỪ ENVIRONMENT CỦA RENDER) ---
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
DATA_FILE = "production_data.json"

client_ai = genai.Client(api_key=GEMINI_API_KEY)

# --- 3. LƯU TRỮ DỮ LIỆU ---
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

# --- 5. HỆ THỐNG MÔ HÌNH GEMINI (ĐÃ LOẠI BỎ 2.0-FLASH CŨ) ---
AVAILABLE_MODELS = {
    "gemini": "models/gemini-3.8-flash",
    "3.8-flash": "models/gemini-3.8-flash",
    "flash": "models/gemini-3.8-flash",
    "2.5-flash": "models/gemini-2.5-flash",
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

# --- 7. HÀM GỌI GEMINI API BẤT ĐỒNG BỘ (CHỈ DÙNG MODEL MỚI & HIỆN ĐÚNG LỖI GỐC) ---
async def async_safe_generate_content(model_name, contents_list):
    fallback_sequence = [
        model_name,
        "models/gemini-3.8-flash",
        "gemini-3.8-flash",
        "models/gemini-2.5-flash",
    ]
    models_to_try = list(dict.fromkeys(fallback_sequence))
    
    first_error = None
    for m in models_to_try:
        try:
            return await client_ai.aio.models.generate_content(
                model=m,
                contents=contents_list,
                config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT)
            )
        except Exception as e:
            if first_error is None:
                first_error = e  # Lưu lại lỗi của model đầu tiên thay vì model cuối
            continue
                
    raise first_error

# --- 8. SỰ KIỆN BOT & VÒNG LẶP CHẠY NGẦM ---
@bot.event
async def on_ready():
    print(f"[SYSTEM] Bot đã sẵn sàng: {bot.user}")
    if not check_deadlines.is_running():
        check_deadlines.start()
    if not check_meetings.is_running():
        check_meetings.start()

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
async def before_loops():
    await bot.wait_until_ready()

# --- 9. LỆNH HƯỚNG DẪN (!huongdan / !help) ---
@bot.command(name="huongdan", aliases=["help", "lenh"])
async def show_help(ctx):
    models_str = ", ".join(f"`{k}`" for k in AVAILABLE_MODELS.keys())
    embed = discord.Embed(
        title="🎬 CẨM NANG BOT PRODUCTION HOUSE",
        description="Hệ thống quản lý tiến độ, lịch họp và trợ lý Gemini 3.8 Flash chuyên sâu.",
        color=discord.Color.gold()
    )
    embed.add_field(
        name="📋 1. Quản lý Task & Deadline",
        value=(
            "• `!task [Tên task] [Giờ] [Ngày]` — Tạo task & mở Thread riêng (VD: `!task Key Visual 18:00 28/10`)\n"
            "• Nhắn `done` hoặc `xong` — Tự động báo hoàn thành task của bạn\n"
            "• `!danhsachtask` — Xem toàn bộ danh sách task và số thứ tự\n"
            "• `!hoanthanh [số]` — Đánh dấu xong task theo số thứ tự\n"
            "• `!xoatask [số]` — Xóa task và đóng Thread tương ứng"
        ),
        inline=False
    )
    embed.add_field(
        name="📅 2. Quản lý Lịch họp",
        value=(
            "• `!hop [Giờ] [Ngày] [Nội dung]` — Đặt lịch họp chung ping @everyone (VD: `!hop 20:30 26/10 Duyệt concept`)\n"
            "• `!hoprieng [@User] [Giờ] [Ngày] [Nội dung]` — Đặt lịch họp riêng với thành viên\n"
            "• `!danhsachhop` — Xem các lịch họp sắp tới"
        ),
        inline=False
    )
    embed.add_field(
        name="🧠 3. Trợ lý AI & Duyệt WIP (Gemini 3.8 Flash)",
        value=(
            "• `!ai [câu hỏi]` — Hỏi đáp AI (có nhớ ngữ cảnh)\n"
            "• `!checkwip` (Đính kèm ảnh) — AI đóng vai Art Director/DI soi bố cục, ánh sáng, màu sắc\n"
            "• `!taoprompt [ý tưởng]` — Chuyển ý tưởng tiếng Việt thành Prompt tiếng Anh chuẩn\n"
            "• `!datten [tên file]` — Chuẩn hóa tên file theo quy chuẩn Production\n"
            "• `!resetai` — Xóa lịch sử nhớ ngữ cảnh trò chuyện của bạn\n"
            "• `!checkapi` — Kiểm tra tình trạng API Key và danh sách Model khả dụng\n"
            f"👉 **Các model hỗ trợ:** {models_str}"
        ),
        inline=False
    )
    embed.add_field(
        name="📁 4. Quản lý File & Link Dự án",
        value=(
            "• `!nopfile [Tên dự án]` (Kèm file hoặc link) — Lưu trữ bản nộp vào hệ thống\n"
            "• `!tonghoplink [số tin nhắn]` — Quét và gom toàn bộ link Google Drive/Docs trong kênh"
        ),
        inline=False
    )
    embed.set_footer(text=f"Múi giờ hệ thống: UTC+7 (Hiện tại: {now_vn().strftime('%H:%M - %d/%m/%Y')})")
    await ctx.send(embed=embed)

# --- 10. CÁC LỆNH QUẢN LÝ TASK ---
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

# --- 11. CÁC LỆNH LỊCH HỌP ---
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

# --- 12. CÁC LỆNH AI & PRODUCTION ---
@bot.command(name="checkapi")
async def check_api(ctx):
    """Lệnh kiểm tra xem API Key có hoạt động không và liệt kê các model khả dụng"""
    try:
        models = [m.name for m in client_ai.models.list() if "gemini" in m.name]
        await ctx.send(f"✅ **API Key hoạt động tốt!**\nCác model khả dụng:\n`" + "`, `".join(models[:15]) + "`")
    except Exception as e:
        await ctx.send(f"❌ **API Key đang bị lỗi hoặc bị khóa:** `{str(e)[:300]}`")

@bot.command(name="ai")
async def ask_ai(ctx, model_key: str = None, *, prompt: str = None):
    if model_key and model_key.lower() not in AVAILABLE_MODELS:
        prompt = f"{model_key} {prompt}" if prompt else model_key
        model_key = DEFAULT_MODEL
    elif not model_key:
        model_key = DEFAULT_MODEL

    if not prompt and not ctx.message.attachments:
        await ctx.send("⚠️ Vui lòng nhập câu hỏi hoặc đính kèm ảnh.")
        return
    
    selected_model = AVAILABLE_MODELS.get(model_key.lower(), AVAILABLE_MODELS[DEFAULT_MODEL])

    async with ctx.typing():
        try:
            if ctx.author.id not in USER_CHATS:
                USER_CHATS[ctx.author.id] = []
            
            history = USER_CHATS[ctx.author.id]
            full_context = ""
            for h in history:
                full_context += f"User: {h['user']}\nAssistant: {h['assistant']}\n"
            
            user_text = prompt if prompt else "Phân tích hình ảnh này dưới góc độ chuyên môn."
            full_context += f"User: {user_text}\nAssistant:"

            contents = [full_context]
            if ctx.message.attachments:
                attachment = ctx.message.attachments[0]
                image_bytes = await attachment.read()
                contents.append(Image.open(io.BytesIO(image_bytes)))

            response = await async_safe_generate_content(selected_model, contents)
            reply_text = response.text if response and response.text else "Lỗi phản hồi."
            
            history.append({"user": user_text, "assistant": reply_text})
            if len(history) > 10: 
                history.pop(0)

            await send_long_message(ctx, reply_text)
        except Exception as e:
            await ctx.send(f"❌ Lỗi AI: `{str(e)[:300]}`")

@bot.command(name="checkwip")
async def check_wip(ctx, model_key: str = "gemini"):
    if not ctx.message.attachments:
        await ctx.send("⚠️ Vui lòng đính kèm ảnh WIP cùng với lệnh `!checkwip`.")
        return
    
    selected_model = AVAILABLE_MODELS.get(model_key.lower(), AVAILABLE_MODELS[DEFAULT_MODEL])
    attachment = ctx.message.attachments[0]
    
    await ctx.send(f"🔍 Đang soi WIP bằng AI chuyên môn...")
    async with ctx.typing():
        try:
            image_bytes = await attachment.read()
            image = Image.open(io.BytesIO(image_bytes))
            
            prompt_wip = (
                "Phân tích WIP này chi tiết theo các hạng mục: "
                "1. Layout & Composition (Bố cục & Điểm nhìn) "
                "2. Lighting & Contrast (Ánh sáng & Tương phản) "
                "3. Color Grading / Blending (Màu sắc & Hòa trộn) "
                "4. Các điểm lỗi cần fix ngay."
            )
            response = await async_safe_generate_content(selected_model, [prompt_wip, image])
            reply_text = response.text if response and response.text else "Lỗi phản hồi."
            await send_long_message(ctx, reply_text)
        except Exception as e:
            await ctx.send(f"❌ Lỗi: `{str(e)[:300]}`")

@bot.command(name="resetai")
async def reset_ai(ctx):
    if ctx.author.id in USER_CHATS:
        del USER_CHATS[ctx.author.id]
        await ctx.send("🧹 Đã xóa lịch sử trò chuyện AI của bạn.")
    else:
        await ctx.send("📭 Lịch sử trò chuyện đang trống.")

@bot.command(name="taoprompt")
async def tao_prompt(ctx, *, idea: str):
    async with ctx.typing():
        try:
            prompt_req = f"Tạo prompt AI tiếng Anh chi tiết (gồm Subject, Environment, Lighting, Camera Angle, Render Style) từ ý tưởng: '{idea}'."
            response = await async_safe_generate_content(AVAILABLE_MODELS[DEFAULT_MODEL], [prompt_req])
            await send_long_message(ctx, response.text)
        except Exception as e:
            await ctx.send(f"❌ Lỗi: `{str(e)[:300]}`")

@bot.command(name="datten")
async def dat_ten(ctx, *, filename: str):
    async with ctx.typing():
        try:
            prompt_req = f"Chuyển đổi tên file '{filename}' sang chuẩn đặt tên Production House (YYYYMMDD_Project_Asset_Version, không dấu, dùng underscore)."
            response = await async_safe_generate_content(AVAILABLE_MODELS[DEFAULT_MODEL], [prompt_req])
            await send_long_message(ctx, response.text)
        except Exception as e:
            await ctx.send(f"❌ Lỗi: `{str(e)[:300]}`")

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

# --- 13. WEB SERVER GIỮ BOT SỐNG 24/7 TRÊN RENDER ---
async def handle_ping(request):
    return web.Response(text=f"Production Bot is running! Current VN Time: {now_vn().strftime('%d/%m/%Y %H:%M:%S')}")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[WEB SERVER] Đang lắng nghe cổng {port} cho Render...")

async def main():
    await start_web_server()
    async with bot:
        await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
