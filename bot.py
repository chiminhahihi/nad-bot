import os
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from dotenv import load_dotenv
import discord
from discord.ext import commands, tasks
from google import genai
from google.genai import types

# --- 1. MỞ CỔNG WEB NGẦM ĐỂ RENDER NHẬN DIỆN BOT ĐANG SỐNG ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Bot is alive and running 24/7 on Render!")

    def log_message(self, format, *args):
        pass # Tắt log web cho đỡ rối

def start_web_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=start_web_server, daemon=True).start()

# --- 2. NẠP CẤU HÌNH BẢO MẬT ---
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AIzaSyDuOEYpg2d5bZxxxVDU3FrTZm_Ist1DLbE")
TASK_FILE = "tasks.json"

client_ai = genai.Client(api_key=GEMINI_API_KEY)

SYSTEM_INSTRUCTION = (
    "Bạn là một AI Agent quản lý dự án và server Discord cực kỳ nghiêm khắc, "
    "kỷ luật và chuyên nghiệp. Nhiệm vụ của bạn là dí deadline, không chấp nhận lý do trễ hạn "
    "và luôn giữ thái độ đanh thép nhưng lịch sự."
)

def load_tasks():
    if not os.path.exists(TASK_FILE):
        return []
    try:
        with open(TASK_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def save_tasks(tasks_data):
    with open(TASK_FILE, "w", encoding="utf-8") as f:
        json.dump(tasks_data, f, ensure_ascii=False, indent=4)

TASKS = load_tasks()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

async def send_long_message(destination, text: str):
    for i in range(0, len(text), 1990):
        await destination.send(text[i:i+1990])

@bot.event
async def on_ready():
    print(f"Bot đã sẵn sàng và online: {bot.user}")
    if not check_deadlines.is_running():
        check_deadlines.start()

@bot.command(name="ai")
async def ask_ai(ctx, *, prompt: str):
    async with ctx.typing():
        try:
            response = await client_ai.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    temperature=0.7,
                )
            )
            await send_long_message(ctx, response.text)
        except Exception as e:
            await ctx.send(f"❌ Lỗi kết nối AI: `{e}`")

@bot.command(name="themtask")
async def add_task(ctx, task_name: str, deadline_str: str):
    try:
        deadline_dt = datetime.strptime(deadline_str, "%Y-%m-%d %H:%M")
        if deadline_dt <= datetime.now():
            await ctx.send("⚠️ Deadline phải là một thời điểm trong tương lai!")
            return
    except ValueError:
        await ctx.send("❌ Sai định dạng thời gian! Vui lòng nhập đúng chuẩn: `\"YYYY-MM-DD HH:MM\"` (VD: `!themtask \"Làm Key Visual\" \"2026-10-30 23:59\"`)")
        return

    new_task = {
        "task": task_name,
        "deadline": deadline_str,
        "user_id": ctx.author.id,
        "channel_id": ctx.channel.id,
        "notified_overdue": False
    }
    TASKS.append(new_task)
    save_tasks(TASKS)
    await ctx.send(f"✅ Đã ghi nhận task: **{task_name}** | Hạn chót: `{deadline_str}` cho <@{ctx.author.id}>.")

@bot.command(name="xemtask")
async def list_tasks(ctx):
    if not TASKS:
        await ctx.send("🎉 Hiện tại không có task nào tồn đọng!")
        return

    msg = "📋 **DANH SÁCH CÔNG VIỆC ĐANG THEO DÕI:**\n"
    for idx, t in enumerate(TASKS, 1):
        status = "🔴 TRỄ HẠN" if t.get("notified_overdue") else "🟢 Đang chạy"
        msg += f"`{idx}.` **{t['task']}** - Hạn: `{t['deadline']}` - Phụ trách: <@{t['user_id']}> [{status}]\n"
    await send_long_message(ctx, msg)

@bot.command(name="xong")
async def complete_task(ctx, index: int):
    if index < 1 or index > len(TASKS):
        await ctx.send("❌ Số thứ tự task không hợp lệ! Gõ `!xemtask` để kiểm tra lại.")
        return

    removed = TASKS.pop(index - 1)
    save_tasks(TASKS)
    await ctx.send(f"🎯 Đã nghiệm thu và đóng task: **{removed['task']}** của <@{removed['user_id']}>!")

@tasks.loop(minutes=1)
async def check_deadlines():
    now = datetime.now()
    updated = False

    for t in TASKS:
        if t.get("notified_overdue"):
            continue

        deadline_dt = datetime.strptime(t["deadline"], "%Y-%m-%d %H:%M")
        if now >= deadline_dt:
            channel = bot.get_channel(t["channel_id"])
            if channel:
                try:
                    prompt = (
                        f"Nhân sự có ID <@{t['user_id']}> vừa bị trễ hạn công việc '{t['task']}' "
                        f"(Deadline là {t['deadline']}). Hãy viết 1 tin nhắn ngắn gọn (dưới 3 câu), "
                        f"nghiêm khắc yêu cầu báo cáo tiến độ ngay lập tức."
                    )
                    res = await client_ai.aio.models.generate_content(
                        model="gemini-2.5-flash",
                        contents=prompt,
                        config=types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION)
                    )
                    await channel.send(f"🚨 **CẢNH BÁO TRỄ DEADLINE** <@{t['user_id']}>\n{res.text}")
                except Exception as e:
                    await channel.send(f"🚨 <@{t['user_id']}>! Task **{t['task']}** đã quá hạn từ `{t['deadline']}`! Báo cáo tiến độ ngay!")
            
            t["notified_overdue"] = True
            updated = True

    if updated:
        save_tasks(TASKS)

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
