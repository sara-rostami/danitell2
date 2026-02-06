import asyncio
import os
import logging
import math
import json
import hashlib
from datetime import datetime
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, ApiIdInvalidError
from huggingface_hub import HfApi, login, HfFileSystem
from aiohttp import web

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Environment variables
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
HF_TOKEN = os.environ.get("HF_TOKEN")
HF_REPO_ID = os.environ.get("HF_REPO_ID")  # Space repo: username/space-name
PORT = int(os.environ.get("PORT", 8000))

# 🔥 SMART CHUNK CONFIG - با قابلیت 500MB
CHUNK_STRATEGIES = {
    "tiny": 10 * 1024 * 1024,      # 10MB  - برای فایل‌های کوچک
    "safe": 90 * 1024 * 1024,      # 90MB  - امن تضمینی
    "balanced": 200 * 1024 * 1024, # 200MB - تعادل خوب
    "aggressive": 500 * 1024 * 1024, # 500MB - ریسکی اما ممکن
}
MAX_FILE_SIZE = 2000 * 1024 * 1024  # 2GB حداکثر
DOWNLOAD_CHUNK = 2 * 1024 * 1024    # 2MB برای دانلود

# Initialize Hugging Face
login(token=HF_TOKEN)
hf_api = HfApi()
fs = HfFileSystem(token=HF_TOKEN)

# Global variables
bot_status = {
    "running": False, 
    "last_error": None, 
    "space_mode": True,
    "chunk_strategy": "aggressive",  # پیش‌فرض: 500MB
    "success_rate": {}  # ثبت موفقیت‌ها برای هر سایز
}
active_uploads = {}
user_stats = {}

# Session
session_path = "/data/bot_session" if os.path.exists("/data") else "bot_session"
client = TelegramClient(session_path, API_ID, API_HASH)

# ==================== UTILITIES ====================
def format_size(size_bytes):
    """Format file size to human readable"""
    if size_bytes == 0:
        return "0B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {size_name[i]}"

def get_chunk_strategy(file_size):
    """تصمیم هوشمند برای انتخاب chunk size"""
    # اگر قبلاً برای این سایز موفق بودیم، همان استراتژی را استفاده کن
    for size_range, strategy in [
        (100 * 1024 * 1024, "tiny"),      # زیر 100MB: 10MB
        (500 * 1024 * 1024, "balanced"),  # زیر 500MB: 200MB
        (1000 * 1024 * 1024, "safe"),     # زیر 1GB: 90MB
        (float('inf'), "safe")            # بالاتر: 90MB
    ]:
        if file_size <= size_range:
            return strategy
    return "safe"

def calculate_md5(file_path):
    """محاسبه MD5 برای بررسی سلامت فایل"""
    hash_md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()

# ==================== HTTP SERVER ====================
async def health_check(request):
    """Health check endpoint"""
    status = "healthy" if bot_status["running"] else "starting"
    strategy = bot_status["chunk_strategy"]
    chunk_mb = CHUNK_STRATEGIES[strategy] // (1024 * 1024)
    return web.Response(
        text=f"OK - Status: {status} | Chunk: {chunk_mb}MB",
        status=200
    )

async def root_handler(request):
    """Root endpoint"""
    info = {
        "service": "Telegram to HF Space Bot",
        "status": "running" if bot_status["running"] else "starting",
        "mode": "SPACE",
        "chunk_strategy": bot_status["chunk_strategy"],
        "chunk_size_mb": CHUNK_STRATEGIES[bot_status["chunk_strategy"]] // (1024 * 1024),
        "max_file_mb": MAX_FILE_SIZE // (1024 * 1024),
        "hf_repo": HF_REPO_ID,
        "success_stats": bot_status["success_rate"]
    }
    if bot_status["last_error"]:
        info["last_error"] = str(bot_status["last_error"])
    
    return web.json_response(info)

async def start_http_server():
    """Start HTTP server"""
    app = web.Application()
    app.router.add_get("/", root_handler)
    app.router.add_get("/health", health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"🌐 HTTP server on port {PORT}")
    
    while True:
        await asyncio.sleep(3600)

# ==================== UPLOAD ENGINE ====================
async def upload_to_space(file_path, target_path, original_size, status_msg=None):
    """آپلود به Space با retry و fallback"""
    file_size = os.path.getsize(file_path)
    strategy = get_chunk_strategy(original_size)
    chunk_size = CHUNK_STRATEGIES[strategy]
    
    attempts = [
        {"chunk": chunk_size, "retries": 2, "name": strategy},
        {"chunk": CHUNK_STRATEGIES["safe"], "retries": 3, "name": "safe_fallback"},
        {"chunk": CHUNK_STRATEGIES["tiny"], "retries": 3, "name": "tiny_fallback"}
    ]
    
    for attempt in attempts:
        chunk_size = attempt["chunk"]
        chunk_mb = chunk_size // (1024 * 1024)
        
        if status_msg:
            await status_msg.edit(
                f"🔄 **Trying {chunk_mb}MB chunks**\n"
                f"📊 File: {format_size(file_size)}\n"
                f"🎯 Strategy: {attempt['name']}"
            )
        
        # اگر فایل از chunk کوچک‌تر است، مستقیم آپلود کن
        if file_size <= chunk_size:
            for retry in range(attempt['retries']):
                try:
                    logger.info(f"📤 Direct upload attempt {retry+1} ({chunk_mb}MB strategy)")
                    
                    hf_api.upload_file(
                        path_or_fileobj=file_path,
                        path_in_repo=target_path,
                        repo_id=HF_REPO_ID,
                        repo_type="space",
                        commit_message=f"Telegram upload: {target_path}"
                    )
                    
                    # ثبت موفقیت
                    size_key = f"{original_size // (1024*1024)}MB"
                    bot_status["success_rate"][size_key] = bot_status["success_rate"].get(size_key, 0) + 1
                    
                    logger.info(f"✅ Direct upload successful: {target_path}")
                    return True
                    
                except Exception as e:
                    error_msg = str(e)
                    logger.warning(f"⚠️ Upload failed (attempt {retry+1}): {error_msg[:100]}")
                    
                    # اگر 403 یا LFS خطا داد، استراتژی را عوض کن
                    if "403" in error_msg or "LFS" in error_msg or "quota" in error_msg.lower():
                        logger.error("🚫 Space limitation hit, trying smaller chunks")
                        break  # به استراتژی بعدی برو
                    
                    if retry < attempt['retries'] - 1:
                        await asyncio.sleep(2 * (retry + 1))
                    else:
                        continue  # به استراتژی بعدی برو
        
        # اگر فایل بزرگ است، split کن
        else:
            parts = math.ceil(file_size / chunk_size)
            success = await split_and_upload(
                file_path, target_path, chunk_size, parts, status_msg
            )
            if success:
                return True
    
    return False

async def split_and_upload(file_path, base_name, chunk_size, total_parts, status_msg=None):
    """فایل را split کن و آپلود کن"""
    uploaded_parts = []
    file_size = os.path.getsize(file_path)
    
    with open(file_path, 'rb') as f:
        for part_num in range(1, total_parts + 1):
            # chunk را بخوان
            chunk_data = f.read(chunk_size)
            if not chunk_data:
                break
            
            chunk_path = f"/tmp/{base_name}.part{part_num:03d}"
            with open(chunk_path, 'wb') as chunk_file:
                chunk_file.write(chunk_data)
            
            chunk_name = f"{base_name}.part{part_num:03d}"
            
            if status_msg:
                progress = (part_num / total_parts) * 100
                await status_msg.edit(
                    f"📤 **Uploading part {part_num}/{total_parts}**\n"
                    f"📊 Progress: {progress:.1f}%\n"
                    f"🔢 Chunk: {format_size(len(chunk_data))}"
                )
            
            # آپلود chunk
            for retry in range(3):
                try:
                    hf_api.upload_file(
                        path_or_fileobj=chunk_path,
                        path_in_repo=chunk_name,
                        repo_id=HF_REPO_ID,
                        repo_type="space"
                    )
                    
                    uploaded_parts.append({
                        "name": chunk_name,
                        "size": len(chunk_data),
                        "part": part_num
                    })
                    
                    # پاک‌کردن فایل موقت
                    os.remove(chunk_path)
                    logger.info(f"✅ Part {part_num}/{total_parts} uploaded")
                    break
                    
                except Exception as e:
                    logger.warning(f"⚠️ Part {part_num} upload failed (retry {retry+1}): {e}")
                    if retry == 2:
                        # اگر ۳ بار fail شد، کل عملیات fail
                        return False
                    await asyncio.sleep(2)
    
    # ساخت manifest
    manifest = {
        "original_name": base_name,
        "total_parts": total_parts,
        "total_size": file_size,
        "chunk_size": chunk_size,
        "parts": uploaded_parts,
        "timestamp": datetime.now().isoformat(),
        "md5": calculate_md5(file_path) if os.path.exists(file_path) else None,
        "reassembly": f"cat {base_name}.part* > \"{base_name}\""
    }
    
    manifest_path = f"/tmp/{base_name}.manifest.json"
    with open(manifest_path, 'w') as mf:
        json.dump(manifest, mf, indent=2)
    
    # آپلود manifest
    try:
        hf_api.upload_file(
            path_or_fileobj=manifest_path,
            path_in_repo=f"{base_name}.manifest.json",
            repo_id=HF_REPO_ID,
            repo_type="space"
        )
        os.remove(manifest_path)
    except:
        pass
    
    return True

# ==================== TELEGRAM BOT ====================
async def start_telegram_bot():
    """Start Telegram bot"""
    while True:
        try:
            logger.info("🚀 Starting bot (500MB chunk mode)...")
            await client.start(bot_token=BOT_TOKEN)
            bot_status["running"] = True
            bot_status["last_error"] = None
            
            strategy = bot_status["chunk_strategy"]
            chunk_mb = CHUNK_STRATEGIES[strategy] // (1024 * 1024)
            logger.info(f"✅ Bot started | Chunk strategy: {strategy} ({chunk_mb}MB)")
            
            register_handlers()
            await client.run_until_disconnected()
            
        except FloodWaitError as e:
            wait_time = e.seconds
            logger.warning(f"⏳ FloodWait: {wait_time}s")
            await asyncio.sleep(wait_time + 5)
            
        except Exception as e:
            bot_status["last_error"] = str(e)
            logger.exception("❌ Bot crashed, restarting...")
            await asyncio.sleep(10)

def register_handlers():
    """Register event handlers"""
    
    @client.on(events.NewMessage(pattern='/start'))
    async def start_handler(event):
        try:
            strategy = bot_status["chunk_strategy"]
            chunk_mb = CHUNK_STRATEGIES[strategy] // (1024 * 1024)
            
            welcome = (
                "🤖 **Telegram to HF Space Bot**\n\n"
                f"⚡ **Chunk Strategy:** {strategy.upper()} ({chunk_mb}MB)\n"
                f"📦 **Max File:** {MAX_FILE_SIZE//(1024*1024)}MB\n"
                f"🔗 **Space:** `{HF_REPO_ID}`\n\n"
                "**How it works:**\n"
                "• Sends file to your HuggingFace Space\n"
                "• Auto-splits if > chunk size\n"
                "• Creates manifest for reassembly\n\n"
                "**Commands:**\n"
                "/start - This message\n"
                "/chunk - Change chunk size\n"
                "/stats - Upload statistics\n"
                "/help - Detailed help"
            )
            await event.reply(welcome)
        except Exception as e:
            logger.error(f"Start error: {e}")
    
    @client.on(events.NewMessage(pattern='/chunk'))
    async def chunk_handler(event):
        try:
            buttons = [
                ["10MB (tiny)", "90MB (safe)", "200MB (balanced)"],
                ["500MB (aggressive)", "Auto Detect"]
            ]
            
            current = bot_status["chunk_strategy"]
            current_mb = CHUNK_STRATEGIES[current] // (1024 * 1024)
            
            message = (
                f"🔧 **Current chunk size:** {current_mb}MB ({current})\n\n"
                "**Select new strategy:**\n"
                "• **tiny:** 10MB - Maximum reliability\n"
                "• **safe:** 90MB - Guaranteed to work\n"
                "• **balanced:** 200MB - Good balance\n"
                "• **aggressive:** 500MB - May fail\n"
                "• **auto:** Let bot decide\n"
            )
            
            # در اینجا می‌توانی inline keyboard اضافه کنی
            await event.reply(message)
            
            # برای سادگی، الان فقط پیام می‌دهیم
            # برای تغییر واقعی باید دستی در env تغییر دهی یا دیتابیس اضافه کنی
        except Exception as e:
            logger.error(f"Chunk error: {e}")
    
    @client.on(events.NewMessage(pattern='/stats'))
    async def stats_handler(event):
        try:
            total_uploads = sum(bot_status["success_rate"].values())
            
            if total_uploads == 0:
                await event.reply("📊 No uploads yet!")
                return
            
            stats_text = "📈 **Upload Statistics**\n\n"
            for size, count in sorted(bot_status["success_rate"].items()):
                percentage = (count / total_uploads) * 100
                stats_text += f"• {size}: {count} ({percentage:.1f}%)\n"
            
            current = bot_status["chunk_strategy"]
            chunk_mb = CHUNK_STRATEGIES[current] // (1024 * 1024)
            
            stats_text += f"\n🔧 **Current strategy:** {current} ({chunk_mb}MB)"
            stats_text += f"\n📦 **Total uploads:** {total_uploads}"
            
            await event.reply(stats_text)
        except Exception as e:
            logger.error(f"Stats error: {e}")
    
    @client.on(events.NewMessage(pattern='/help'))
    async def help_handler(event):
        try:
            help_text = (
                "📚 **Help Guide**\n\n"
                "**For files < 100MB:**\n"
                "• Uploaded directly\n\n"
                "**For larger files:**\n"
                "• Split into chunks\n"
                "• Each chunk uploaded separately\n"
                "• Manifest file created\n\n"
                "**To reassemble on Linux/Mac:**\n"
                "```bash\n"
                "cat filename.part* > original_file\n"
                "```\n\n"
                "**On Windows PowerShell:**\n"
                "```powershell\n"
                "Get-Content file.part* -AsByteStream | Set-Content output -AsByteStream\n"
                "```\n\n"
                f"**Browse files:**\nhttps://huggingface.co/spaces/{HF_REPO_ID}/tree/main\n\n"
                "**Note:** Space has LFS limits. If upload fails, bot will retry with smaller chunks."
            )
            await event.reply(help_text)
        except Exception as e:
            logger.error(f"Help error: {e}")
    
    @client.on(events.NewMessage)
    async def file_handler(event):
        """Main file handler"""
        if event.message.text and event.message.text.startswith('/'):
            return
        
        if not event.file:
            return
        
        user_id = event.sender_id
        original_filename = event.file.name or f"file_{int(datetime.now().timestamp())}"
        file_size = event.file.size
        
        # بررسی سایز
        if file_size > MAX_FILE_SIZE:
            await event.reply(
                f"❌ **File too large!**\n\n"
                f"File: {original_filename}\n"
                f"Size: {format_size(file_size)}\n"
                f"Limit: {format_size(MAX_FILE_SIZE)}\n\n"
                "Please split file manually or use Dataset mode."
            )
            return
        
        upload_id = f"{user_id}_{int(datetime.now().timestamp())}"
        status_msg = None
        
        try:
            active_uploads[user_id] = upload_id
            
            # ابتدا فایل را دانلود کن
            status_msg = await event.reply(
                f"📥 **Downloading:** `{original_filename}`\n"
                f"📊 **Size:** {format_size(file_size)}\n"
                f"⚡ **Strategy:** {bot_status['chunk_strategy']}\n"
                f"⏳ **Status:** Starting..."
            )
            
            # دانلود فایل
            temp_path = f"/tmp/{upload_id}_{original_filename}"
            await event.download_media(file=temp_path)
            
            # بررسی سلامت دانلود
            if not os.path.exists(temp_path) or os.path.getsize(temp_path) != file_size:
                raise Exception("Download incomplete or corrupted")
            
            # تصمیم‌گیری برای chunk strategy
            strategy = get_chunk_strategy(file_size)
            chunk_size = CHUNK_STRATEGIES[strategy]
            needs_split = file_size > chunk_size
            
            await status_msg.edit(
                f"📤 **Preparing upload...**\n"
                f"📊 File: {format_size(file_size)}\n"
                f"🔧 Strategy: {strategy} ({chunk_size//(1024*1024)}MB)\n"
                f"🔢 Parts: {math.ceil(file_size / chunk_size) if needs_split else 1}"
            )
            
            # ساخت نام منحصربفرد برای فایل
            timestamp = int(datetime.now().timestamp())
            safe_name = ''.join(c for c in original_filename if c.isalnum() or c in '._- ')[:80]
            base_name = f"{timestamp}_{safe_name}"
            
            # آپلود
            if needs_split:
                total_parts = math.ceil(file_size / chunk_size)
                success = await split_and_upload(
                    temp_path, base_name, chunk_size, total_parts, status_msg
                )
            else:
                target_name = f"{base_name}_{original_filename}"
                success = await upload_to_space(
                    temp_path, target_name, file_size, status_msg
                )
            
            if success:
                base_url = f"https://huggingface.co/spaces/{HF_REPO_ID}/resolve/main"
                
                if needs_split:
                    success_msg = (
                        f"✅ **Upload complete!**\n\n"
                        f"📁 File: `{original_filename}`\n"
                        f"📊 Size: {format_size(file_size)}\n"
                        f"🔢 Parts: {total_parts} × {chunk_size//(1024*1024)}MB\n\n"
                        f"**Download parts:**\n"
                        f"`{base_url}/{base_name}.part001`\n"
                        f"(+ {total_parts-1} more parts)\n\n"
                        f"**Manifest:**\n"
                        f"`{base_url}/{base_name}.manifest.json`\n\n"
                        f"**Reassemble:**\n"
                        f"```bash\ncat {base_name}.part* > \"{original_filename}\"\n```"
                    )
                else:
                    success_msg = (
                        f"✅ **Upload complete!**\n\n"
                        f"📁 File: `{original_filename}`\n"
                        f"📊 Size: {format_size(file_size)}\n"
                        f"🔗 Direct: `{base_url}/{base_name}_{original_filename}`\n\n"
                        f"📂 Browse: https://huggingface.co/spaces/{HF_REPO_ID}/tree/main"
                    )
                
                await status_msg.edit(success_msg)
                
                # آمار کاربر
                if user_id not in user_stats:
                    user_stats[user_id] = {"uploads": 0, "total_size": 0}
                user_stats[user_id]["uploads"] += 1
                user_stats[user_id]["total_size"] += file_size
                
            else:
                await status_msg.edit(
                    f"❌ **Upload failed!**\n\n"
                    f"File: `{original_filename}`\n"
                    f"Size: {format_size(file_size)}\n\n"
                    "**Possible reasons:**\n"
                    "• Space LFS quota exceeded\n"
                    "• Network issue\n"
                    "• Try smaller file or different time\n"
                )
            
        except Exception as e:
            logger.exception(f"Upload error for {original_filename}:")
            error_msg = f"❌ **Error:** `{str(e)[:150]}`"
            try:
                if status_msg:
                    await status_msg.edit(error_msg)
                else:
                    await event.reply(error_msg)
            except:
                pass
                
        finally:
            # پاک‌سازی
            if user_id in active_uploads and active_uploads[user_id] == upload_id:
                del active_uploads[user_id]
            
            try:
                if 'temp_path' in locals() and os.path.exists(temp_path):
                    os.remove(temp_path)
            except:
                pass

# ==================== MAIN ====================
async def main():
    """Main function"""
    logger.info("=" * 60)
    logger.info("🚀 Telegram to HF Space Bot")
    logger.info(f"📦 Space: {HF_REPO_ID}")
    logger.info(f"🔧 Strategy: {bot_status['chunk_strategy']}")
    logger.info(f"⚡ Chunk: {CHUNK_STRATEGIES[bot_status['chunk_strategy']]//(1024*1024)}MB")
    logger.info(f"📊 Max file: {MAX_FILE_SIZE//(1024*1024)}MB")
    logger.info("=" * 60)
    
    # ایجاد دایرکتوری‌ها
    os.makedirs("/data", exist_ok=True)
    os.makedirs("/tmp/telegram_uploads", exist_ok=True)
    
    # پاک‌سازی فایل‌های موقت قدیمی
    try:
        import time
        now = time.time()
        for f in os.listdir("/tmp"):
            if f.startswith("telegram_") or f.endswith(".part"):
                filepath = f"/tmp/{f}"
                try:
                    if os.path.getmtime(filepath) < now - 3600:  # قدیمی‌تر از 1 ساعت
                        os.remove(filepath)
                except:
                    pass
    except Exception as e:
        logger.warning(f"Cleanup warning: {e}")
    
    # اجرای سرویس‌ها
    await asyncio.gather(
        start_http_server(),
        start_telegram_bot(),
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\n👋 Bot stopped by user")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}")
        raise
