import os
import json
from datetime import datetime, timedelta
import discord
from discord.ext import commands, tasks
from google import genai
from google.genai import types

# --- 1. CẤU HÌNH API TRỰC TIẾP ---
DISCORD_TOKEN = "MTU0OTM1MTY1MDE3NzcxNjMwNw.GCz9sO.bEdsVmBKw8MWI-JT_ctuL305v1_4kV-5bVELZ8"
GEMINI_API_KEY = "AIzaSyDuOEYpg2d5bZxxxVDU3FrTZm_Ist1DLbE"
TASK_FILE = "tasks.json"

# Khởi tạo Gemini client
client_ai = genai.Client(api_key=GEMINI_API_KEY)

SYSTEM_INSTRUCTION = (
    "Bạn là một AI Agent quản lý dự án và server Discord cực kỳ nghiêm khắc, "
    "kỷ luật và chuyên nghiệp. Nhiệm vụ của bạn là dí deadline, không chấp nhận lý do trễ hạn "
    "và luôn giữ thái độ đanh thép nhưng lịch sự."
)

# --- 2. QUẢN LÝ DỮ LIỆU JSON (TRÁNH MẤT DATA KHI TẮT BOT) ---
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

# --- 3. CẤU HÌNH DISCORD BOT ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Hàm cắt nhỏ tin nhắn nếu AI trả lời dài quá 2000 ký tự
async def send_long_message(destination, text: str):
    for i in range(0, len(text), 1990):
        await destination.send(text[i:i+1990])

@bot.event
async def on_ready():
    print(f"[SYSTEM] Bot đã đăng nhập thành công: {bot.user} (ID: {bot.user.id})")
    if not check_deadlines.is_running():
        check_deadlines.start()

# Bắt lỗi toàn cục khi người dùng gõ sai cú pháp lệnh
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(
            "⚠️ **Thiếu thông tin!** Vui lòng kiểm tra lại cú pháp.\n"
            "• Thêm task: `!themtask \"Tên công việc\" \"YYYY-MM-DD HH:MM\"`\n"
            "• Xong task: `!xong [Số thứ tự]`\n"
            "• Hỏi AI: `!ai [Câu hỏi]`"
        )
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ **Sai kiểu dữ liệu!** Số thứ tự task phải là một con số (VD: `!xong 1`).")

# --- 4. CÁC LỆNH TƯƠNG TÁC (COMMANDS) ---
@bot.command(name="ai")
async def ask_ai(ctx, *, prompt: str):
    """Chat trực tiếp với Quản lý AI: !ai [câu hỏi]"""
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
    """Thêm task: !themtask "Tên công việc" "YYYY-MM-DD HH:MM" """
    try:
        deadline_dt = datetime.strptime(deadline_str, "%Y-%m-%d %H:%M")
        if deadline_dt <= datetime.now():
            await ctx.send("⚠️ Deadline phải là một thời điểm trong tương lai!")
            return
    except ValueError:
        await ctx.send(
            "❌ **Sai định dạng hoặc quên dấu ngoặc kép!**\n"
            "Cú pháp chuẩn: `!themtask \"Làm Key Visual\" \"2026-10-30 23:59\"`"
        )
        return

    new_task = {
        "task": task_name,
        "deadline": deadline_str,
        "user_id": ctx.author.id,
        "channel_id": ctx.channel.id,
        "notified_soon": False,     # Nhắc trước 60 phút
        "notified_overdue": False   # Nhắc khi trễ hạn
    }
    TASKS.append(new_task)
    save_tasks(TASKS)
    await ctx.send(f"✅ Đã ghi nhận task: **{task_name}** | Hạn chót: `{deadline_str}` cho <@{ctx.author.id}>.")

@bot.command(name="xemtask")
async def list_tasks(ctx):
    """Xem danh sách toàn bộ task hiện tại: !xemtask"""
    if not TASKS:
        await ctx.send("🎉 Hiện tại không có task nào tồn đọng!")
        return

    now = datetime.now()
    msg = "📋 **DANH SÁCH CÔNG VIỆC ĐANG THEO DÕI:**\n"
    for idx, t in enumerate(TASKS, 1):
        deadline_dt = datetime.strptime(t["deadline"], "%Y-%m-%d %H:%M")
        diff = deadline_dt - now
        
        if t.get("notified_overdue") or diff.total_seconds() <= 0:
            status = "🔴 TRỄ HẠN"
        elif diff <= timedelta(hours=1):
            mins = int(diff.total_seconds() // 60)
            status = f"🟡 Gấp (còn {mins} phút)"
        else:
            hours = int(diff.total_seconds() // 3600)
            status = f"🟢 Đang chạy (còn ~{hours}h)"

        msg += f"`{idx}.` **{t['task']}** - Hạn: `{t['deadline']}` - Phụ trách: <@{t['user_id']}> [{status}]\n"
    await send_long_message(ctx, msg)

@bot.command(name="xong")
async def complete_task(ctx, index: int):
    """Hoàn thành và xóa task theo số thứ tự: !xong [STT]"""
    if index < 1 or index > len(TASKS):
        await ctx.send("❌ Số thứ tự task không hợp lệ! Gõ `!xemtask` để kiểm tra lại.")
        return

    target_task = TASKS[index - 1]
    is_admin = ctx.author.guild_permissions.administrator if ctx.guild else False
    if ctx.author.id != target_task["user_id"] and not is_admin:
        await ctx.send(f"⛔ Bạn không phải người phụ trách task này (<@{target_task['user_id']}>) nên không được quyền đóng!")
        return

    removed = TASKS.pop(index - 1)
    save_tasks(TASKS)
    await ctx.send(f"🎯 Đã nghiệm thu và đóng task: **{removed['task']}** của <@{removed['user_id']}>!")

# --- 5. VÒNG LẶP TỰ ĐỘNG DÍ DEADLINE ---
@tasks.loop(minutes=1)
async def check_deadlines():
    now = datetime.now()
    updated = False

    for t in TASKS:
        if t.get("notified_overdue"):
            continue

        deadline_dt = datetime.strptime(t["deadline"], "%Y-%m-%d %H:%M")
        time_left = deadline_dt - now

        channel = bot.get_channel(t["channel_id"])
        if not channel:
            try:
                channel = await bot.fetch_channel(t["channel_id"])
            except Exception:
                continue

        # CẤP ĐỘ 1: ĐÃ TRỄ HẠN
        if time_left.total_seconds() <= 0:
            try:
                prompt = (
                    f"Nhân sự <@{t['user_id']}> vừa bị TRỄ HẠN công việc '{t['task']}' "
                    f"(Deadline: {t['deadline']}). Hãy viết 1 tin nhắn ngắn gọn (dưới 3 câu), "
                    f"cực kỳ nghiêm khắc yêu cầu nộp bài hoặc giải trình ngay lập tức."
                )
                res = await client_ai.aio.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                    config=types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION)
                )
                await channel.send(f"🚨 **BÁO ĐỘNG ĐỎ: TRỄ DEADLINE!** <@{t['user_id']}>\n{res.text}")
            except Exception:
                await channel.send(f"🚨 <@{t['user_id']}>! Task **{t['task']}** đã quá hạn từ `{t['deadline']}`! Báo cáo ngay!")
            
            t["notified_overdue"] = True
            updated = True

        # CẤP ĐỘ 2: CÒN DƯỚI 60 PHÚT (NHẮC SỚM CHẠY NƯỚC RÚT)
        elif timedelta(minutes=0) < time_left <= timedelta(minutes=60) and not t.get("notified_soon"):
            mins_left = int(time_left.total_seconds() // 60)
            await channel.send(
                f"⏳ **NHẮC NHỞ NƯỚC RÚT:** <@{t['user_id']}> chú ý! "
                f"Task **{t['task']}** chỉ còn **{mins_left} phút** nữa là tới hạn (`{t['deadline']}`). Tập trung hoàn thiện!"
            )
            t["notified_soon"] = True
            updated = True

    if updated:
        save_tasks(TASKS)

@check_deadlines.before_loop
async def before_check_deadlines():
    await bot.wait_until_ready()

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
