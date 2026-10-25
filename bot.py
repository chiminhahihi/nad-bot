import warnings
import re
import asyncio
import io
import string
import base64
from datetime import datetime
import discord
from discord.ext import commands, tasks
from google import genai
from openai import OpenAI
from PIL import Image

warnings.filterwarnings('ignore')

# --- CẤU HÌNH TOKEN & API KEYS ---
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
GEMINI_API_KEY = "AQ.Ab8RN6IatI2yRei_xMf9u6OGlmVDkRQRYPuvhflZr-aIcESy0w"
OPENAI_API_KEY = "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"

client_ai = genai.Client(api_key=GEMINI_API_KEY)
openai_client = OpenAI(api_key=OPENAI_API_KEY)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

TASKS = []
meetings = []  
USER_CHATS = {} 

# ==========================================
# --- 🔒 CẤU HÌNH DANH SÁCH ĐƯỢC PHÉP DÙNG BOT ---
# ==========================================
ALLOWED_USER_IDS = {
    572791257558941736,
    792068366335475723,
    649222792071348236,
}

@bot.check
async def globally_block_unauthorized(ctx):
    if ALLOWED_USER_IDS and ctx.author.id not in ALLOWED_USER_IDS:
        await ctx.send("Bạn không có quyền sử dụng bot này.")
        return False
    return True
# ==========================================

# --- HỆ THỐNG ĐA MÔ HÌNH (GEMINI, GEMMA & GPT) ---
AVAILABLE_MODELS = {
    "gemini": "models/gemini-3.8-flash",
    "3.8-flash": "models/gemini-3.8-flash",
    "3.7-flash": "models/gemini-3.7-flash",
    "3.6-flash": "models/gemini-3.6-flash",
    "3.5-flash": "models/gemini-3.5-flash",
    "3.5-lite": "models/gemini-3.5-flash-lite",
    "3-flash": "models/gemini-3-flash",
    "2.5-flash": "models/gemini-2.5-flash",
    "2.5-lite": "models/gemini-2.5-flash-lite",
    "gemma": "models/gemma-4-31b-it",
    "gemma-26b": "models/gemma-4-26b",
    "gpt-4o": "gpt-4o",
    "gpt-4o-mini": "gpt-4o-mini",
    "gpt-4-turbo": "gpt-4-turbo",
}

DEFAULT_MODEL = "gemini"

# --- HỆ THỐNG TƯ DUY CHUYÊN SÂU ---
SYSTEM_PROMPT = (
    "Bạn là chuyên gia Production House, hội tụ tư duy của Art Director, Creative Director, Producer, DI Artist và Visual Artist. "
    "Giao tiếp: bình thường, thẳng thắn, rút gọn, clean, không rườm rà. "
    "Khi duyệt file/WIP: dùng mắt chuyên môn khắt khe. Phân tích layout, lighting, color grading (DI), visual flow. "
    "Khi xử lý tiến độ: tư duy Producer, tối ưu quy trình. Đưa ra giải pháp kỹ thuật chính xác, thực chiến."
)

# --- THUẬT TOÁN BÓC TÁCH NGÀY GIỜ ---
def parse_smart_datetime(text):
    text = text.strip().lower()
    now = datetime.now()
    
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

# --- HÀM GỌI API ---
async def async_safe_generate_content(model_name, contents_list):
    def _call_gemini(m):
        return client_ai.models.generate_content(model=m, contents=contents_list)
    
    def _call_openai(m):
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
            
        response = openai_client.chat.completions.create(
            model=m,
            messages=[{"role": "user", "content": openai_content}]
        )
        class ResultWrapper:
            text = response.choices[0].message.content
        return ResultWrapper()
    
    fallback_sequence = [
        model_name,
        "gpt-4o",
        "gpt-4o-mini",
        "models/gemini-3.8-flash",
        "models/gemini-3.5-flash",
        "models/gemma-4-31b-it"
    ]
    models_to_try = list(dict.fromkeys(fallback_sequence))
    
    last_error = None
    for m in models_to_try:
        try:
            if m.startswith("gpt-"):
                return await asyncio.to_thread(_call_openai, m)
            else:
                return await asyncio.to_thread(_call_gemini, m)
        except Exception as e:
            last_error = e
            continue
                
    raise last_error

@bot.event
async def on_ready():
    print(f"Bot đã sẵn sàng: {bot.user}")
    if not check_deadlines.is_running():
        check_deadlines.start()
    if not check_meetings.is_running():
        check_meetings.start()

# --- XỬ LÝ MESSAGE ---
@bot.event
async def on_message(message):
    if message.author.bot:
        return
    
    clean_content = message.content.lower().translate(str.maketrans('', '', string.punctuation)).strip()
    is_done_keyword = clean_content in ["done", "xong", "hoàn thành", "da xong", "hoan thanh"]
    
    if is_done_keyword:
        user_mention = f"<@{message.author.id}>"
        for t in TASKS:
            if t['mention_str'] == user_mention and t['status'] == "Chưa hoàn thành":
                t['status'] = "Đã hoàn thành"
                await message.channel.send(f"Đã cập nhật task **{t['task']}** thành hoàn thành.")
                break

    await bot.process_commands(message)

# --- VÒNG LẶP DEADLINE ---
@tasks.loop(seconds=10)
async def check_deadlines():
    now = datetime.now()
    for t in TASKS:
        if t['status'] == "Chưa hoàn thành":
            try:
                deadline_time = datetime.strptime(t['deadline'], "%d/%m/%Y %H:%M")
                if now >= deadline_time:
                    channel = bot.get_channel(t['channel_id'])
                    if channel:
                        await channel.send(f"**Quá hạn**: {t['mention_str']} Task **{t['task']}** đã đến hạn.")
                    t['status'] = "Quá hạn"
                else:
                    last_rem = t.get('last_reminder')
                    if not last_rem or (now - last_rem).total_seconds() >= 900:
                        channel = bot.get_channel(t['channel_id'])
                        if channel:
                            await channel.send(f"**Nhắc nhở**: {t['mention_str']} Task **{t['task']}** đến hạn lúc `{t['deadline']}`.")
                        t['last_reminder'] = now
            except Exception:
                pass

# --- VÒNG LẶP HỌP ---
@tasks.loop(seconds=5)
async def check_meetings():
    now = datetime.now()
    meetings_to_remove = []
    for i, m in enumerate(meetings):
        try:
            meeting_time = datetime.strptime(m['time'], "%d/%m/%Y %H:%M")
            if now >= meeting_time:
                channel = bot.get_channel(m['channel_id'])
                if channel:
                    target_ping = "@everyone" if m['ping_everyone'] else m['mention_str']
                    await channel.send(f"{target_ping} Đến giờ họp: **{m['topic']}**.")
                meetings_to_remove.append(i)
        except Exception:
            pass
    for i in reversed(meetings_to_remove):
        meetings.pop(i)

# --- LỆNH HƯỚNG DẪN ---
@bot.command(name="huongdan")
async def show_help(ctx):
    embed = discord.Embed(
        title="Cẩm nang Bot Production",
        description="Hỗ trợ GPT & Gemini phân tích chuyên sâu.",
        color=discord.Color.blue()
    )
    embed.add_field(
        name="Task",
        value=(
            "• Tạo task: `!task [Tên task] [Giờ] [Ngày]`\n"
            "• Nộp task: Nhắn `done` hoặc `xong`\n"
            "• Danh sách: `!danhsachtask` | Hoàn thành: `!hoanthanh [số]` | Xóa: `!xoatask [số]`"
        ),
        inline=False
    )
    embed.add_field(
        name="Lịch họp & Tiện ích",
        value=(
            "• Họp chung: `!hop [Giờ] [Ngày] [Nội dung]`\n"
            "• Họp riêng: `!hoprieng [@User] [Giờ] [Ngày] [Nội dung]`\n"
            "• Chat AI: `!ai [model] [câu hỏi]`\n"
            "• Soi ảnh: Kèm ảnh + `!checkwip [model]`\n"
            "• Link/File: `!nopfile [Dự án]` | `!tonghoplink`"
        ),
        inline=False
    )
    await ctx.send(embed=embed)

# --- LỆNH TASK ---
@bot.command(name="task")
async def add_task(ctx, *, content: str):
    parsed_dt = parse_smart_datetime(content)
    if not parsed_dt:
        await ctx.send("Thiếu thời gian. Nhập giờ (VD: `18:00`) và ngày (VD: `20/10`).")
        return
    
    task_name = extract_topic(content)
    if not task_name:
        task_name = "Task mới"

    deadline_str = parsed_dt.strftime("%d/%m/%Y %H:%M")
    assignee = f"<@{ctx.author.id}>"

    forum_name = "bảng-công-việc"
    forum = discord.utils.get(ctx.guild.forums, name=forum_name)
    if not forum:
        try:
            forum = await ctx.guild.create_forum(name=forum_name)
        except Exception as e:
            await ctx.send(f"Lỗi tạo forum: {e}")
            return
    try:
        thread_msg = await forum.create_thread(
            name=task_name,
            content=f"🚀 **Deadline**\nNgười chạy job: {assignee}\nNội dung: **{task_name}**\nNộp trước hạn: `{deadline_str}`"
        )
        new_thread = thread_msg.thread
    except Exception as e:
        await ctx.send(f"Lỗi tạo thread: {e}")
        return

    TASKS.append({
        "task": task_name, 
        "deadline": deadline_str, 
        "mention_str": assignee, 
        "channel_id": new_thread.id,
        "status": "Chưa hoàn thành",
        "last_reminder": datetime.now()
    })
    await ctx.send(f"Đã tạo task **'{task_name}'** (Hạn: `{deadline_str}`). Nhắc nhở mỗi 15 phút.")

@bot.command(name="hoanthanh")
async def complete_task(ctx, index_str: str):
    try:
        index = int(index_str)
        if 0 <= index < len(TASKS):
            TASKS[index]["status"] = "Đã hoàn thành"
            await ctx.send(f"Cập nhật task `{index}`: **{TASKS[index]['task']}** thành hoàn thành.")
        else:
            await ctx.send("Số thứ tự không tồn tại.")
    except ValueError:
        await ctx.send("Số thứ tự phải là số nguyên.")

@bot.command(name="danhsachtask")
async def list_tasks(ctx):
    if not TASKS:
        await ctx.send("Không có task.")
        return
    msg = "**Danh sách Task:**\n"
    for i, t in enumerate(TASKS):
        icon = "[Xong]" if t['status'] == "Đã hoàn thành" else ("[Quá hạn]" if t['status'] == "Quá hạn" else "[Đang làm]")
        msg += f"`{i}`. {icon} **{t['task']}** — Hạn: `{t['deadline']}` — {t['mention_str']}\n"
    await ctx.send(msg)

@bot.command(name="xoatask")
async def remove_task(ctx, index_str: str):
    try:
        index = int(index_str)
        if 0 <= index < len(TASKS):
            removed = TASKS.pop(index)
            ch = bot.get_channel(removed['channel_id'])
            if ch:
                try: await ch.delete()
                except: pass
            await ctx.send(f"Đã xóa task: **{removed['task']}**.")
        else:
            await ctx.send("Số thứ tự không tồn tại.")
    except ValueError:
        await ctx.send("Nhập số nguyên.")

# --- LỆNH HỌP ---
@bot.command(name="hop")
async def schedule_meeting(ctx, *, content: str):
    parsed_dt = parse_smart_datetime(content)
    if not parsed_dt:
        await ctx.send("Thiếu thời gian.")
        return
    
    topic = extract_topic(content) or "Họp chung"
    full_time = parsed_dt.strftime("%d/%m/%Y %H:%M")
    meetings.append({"time": full_time, "topic": topic, "channel_id": ctx.channel.id, "ping_everyone": True, "mention_str": ""})
    await ctx.send(f"Lịch họp **'{topic}'** lúc `{full_time}` (@everyone).")

@bot.command(name="hoprieng")
async def schedule_meeting_user(ctx, *, content: str):
    user_id_match = re.search(r'<@!?(\d+)>', content)
    if not user_id_match:
        await ctx.send("Chưa tag người.")
        return
    
    member_mention = f"<@{user_id_match.group(1)}>"
    parsed_dt = parse_smart_datetime(content)
    if not parsed_dt:
        await ctx.send("Thiếu thời gian.")
        return

    topic = extract_topic(content) or "Họp riêng"
    full_time = parsed_dt.strftime("%d/%m/%Y %H:%M")
    meetings.append({"time": full_time, "topic": topic, "channel_id": ctx.channel.id, "ping_everyone": False, "mention_str": member_mention})
    await ctx.send(f"Lịch họp với {member_mention}: **'{topic}'** lúc `{full_time}`.")

@bot.command(name="danhsachhop")
async def list_meetings(ctx):
    if not meetings:
        await ctx.send("Không có lịch họp.")
        return
    msg = "**Lịch họp:**\n"
    for i, m in enumerate(meetings):
        target = "Everyone" if m['ping_everyone'] else m['mention_str']
        msg += f"`{i}`. **{m['topic']}** — `{m['time']}` — {target}\n"
    await ctx.send(msg)

# --- LỆNH AI ---
@bot.command(name="ai")
async def ask_ai(ctx, model_key: str = None, *, prompt: str = None):
    if model_key and model_key.lower() not in AVAILABLE_MODELS:
        prompt = f"{model_key} {prompt}" if prompt else model_key
        model_key = DEFAULT_MODEL
    elif not prompt and not ctx.message.attachments:
        await ctx.send("Nhập câu hỏi hoặc gửi ảnh.")
        return
    
    selected_model = AVAILABLE_MODELS.get(model_key.lower(), AVAILABLE_MODELS[DEFAULT_MODEL])

    async with ctx.typing():
        try:
            if ctx.author.id not in USER_CHATS:
                USER_CHATS[ctx.author.id] = []
            
            history = USER_CHATS[ctx.author.id]
            full_context = f"System Instruction: {SYSTEM_PROMPT}\n\n"
            for h in history:
                full_context += f"User: {h['user']}\nAssistant: {h['assistant']}\n"
            
            user_text = prompt if prompt else "Phân tích ảnh này."
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

            if len(reply_text) > 2000:
                for i in range(0, len(reply_text), 2000):
                    await ctx.send(reply_text[i:i+2000])
            else:
                await ctx.send(reply_text)
        except Exception as e:
            await ctx.send(f"Lỗi AI: {str(e)[:150]}")

@bot.command(name="checkwip")
async def check_wip(ctx, model_key: str = "gpt-4o"):
    if not ctx.message.attachments:
        await ctx.send("Đính kèm ảnh kèm lệnh `!checkwip`.")
        return
    
    selected_model = AVAILABLE_MODELS.get(model_key.lower(), AVAILABLE_MODELS["gpt-4o"])
    attachment = ctx.message.attachments[0]
    
    await ctx.send(f"Phân tích ảnh ({model_key})...")
    async with ctx.typing():
        try:
            image_bytes = await attachment.read()
            image = Image.open(io.BytesIO(image_bytes))
            
            response = await async_safe_generate_content(
                selected_model,
                [f"System Instruction: {SYSTEM_PROMPT}\nPhân tích WIP này.", image]
            )
            reply_text = response.text if response and response.text else "Lỗi phản hồi."
            await ctx.send(reply_text[:2000])
        except Exception as e:
            await ctx.send(f"Lỗi: {str(e)[:150]}")

@bot.command(name="resetai")
async def reset_ai(ctx):
    if ctx.author.id in USER_CHATS:
        del USER_CHATS[ctx.author.id]
        await ctx.send("Đã xóa lịch sử.")
    else:
        await ctx.send("Lịch sử trống.")

@bot.command(name="taoprompt")
async def tao_prompt(ctx, *, idea: str):
    async with ctx.typing():
        try:
            response = await async_safe_generate_content(DEFAULT_MODEL, [f"{SYSTEM_PROMPT}\nTạo prompt AI tiếng Anh từ: '{idea}'."])
            await ctx.send(response.text[:2000])
        except Exception as e:
            await ctx.send(f"Lỗi: {str(e)[:150]}")

@bot.command(name="datten")
async def dat_ten(ctx, *, filename: str):
    async with ctx.typing():
        try:
            response = await async_safe_generate_content(DEFAULT_MODEL, [f"{SYSTEM_PROMPT}\nSửa tên file: '{filename}' theo chuẩn."])
            await ctx.send(response.text[:2000])
        except Exception as e:
            await ctx.send(f"Lỗi: {str(e)[:150]}")

@bot.command(name="nopfile")
async def nop_file(ctx, *, project_name: str = "Dự án chung"):
    if not ctx.message.attachments and not re.findall(r'(https?://[^\s]+)', ctx.message.content):
        await ctx.send("Gửi kèm file/link.")
        return
    await ctx.send(f"Đã nhận file/link cho: {project_name}")

@bot.command(name="tonghoplink")
async def aggregate_links(ctx, limit: int = 100):
    drive_links = []
    async for message in ctx.channel.history(limit=limit):
        for url in re.findall(r'(https?://(?:drive|docs)\.google\.com/[^\s]+)', message.content):
            if url not in drive_links:
                drive_links.append(url)
    if not drive_links:
        await ctx.send("Không tìm thấy link.")
        return
    await ctx.send("**Link Google Drive:**\n" + "\n".join(f"`{i+1}`. <{link}>" for i, link in enumerate(drive_links))[:2000])

bot.run(DISCORD_TOKEN)
