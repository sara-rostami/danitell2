import asyncio
import os
import logging
import math
import json
from datetime import datetime
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, ApiIdInvalidError
from huggingface_hub import HfApi, login
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
HF_REPO_ID = os.environ.get("HF_REPO_ID")  # باید Space باشد: username/space-name
PORT = int(os.environ.get("PORT", 8000))

# 🔴 محدودیت‌های CRITICAL برای Space
MAX_FILE_SIZE = 950 * 1024 * 1024  # 950MB - حاشیه امن از 1GB
SPACE_CHUNK_SIZE = 90 * 1024 * 1024  # 90MB - زیر حد LFS Space
DOWNLOAD_CHUNK = 1 * 1024 * 1024    # 1MB - برای RAM کم

# Initialize Hugging Face
login(token=HF_TOKEN)
hf_api = HfApi()

# Global variables
bot_status = {"running": False, "last_error": None, "space_mode": True}
active_uploads = {}  # برای track کردن آپلودهای جاری

# Use session file for persistence
session_path = "/data/bot_session" if os.path.exists("/data") else "bot_session"
client = TelegramClient(session_path, API_ID, API_HASH)

# ==================== HTTP HEALTH CHECK SERVER ====================
async def health_check(request):
    """Health check endpoint for Koyeb"""
    status = "healthy" if bot_status["running"] else "starting"
    mode = "SPACE (90MB chunks)" if bot_status["space_mode"] else "DATASET"
    return web.Response(text=f"OK - Bot: {status} | Mode: {mode}", status=200)

async def root_handler(request):
    """Root endpoint"""
    info = {
        "service": "Telegram to Hugging Face Bot",
        "status": "running" if bot_status["running"] else "starting",
        "mode": "SPACE",
        "warning": "⚠️ Space mode - Max 950MB per file, split into 90MB chunks",
        "limits": {
            "max_file_mb": MAX_FILE_SIZE // (1024*1024),
            "chunk_size_mb": SPACE_CHUNK_SIZE // (1024*1024),
            "safe_for_koyeb": True
        },
        "hf_repo": HF_REPO_ID,
    }
    if bot_status["last_error"]:
        info["last_error"] = str(bot_status["last_error"])
    
    return web.json_response(info)

async def start_http_server():
    """Start HTTP server for Koyeb health checks"""
    app = web.Application()
    app.router.add_get("/", root_handler)
    app.router.add_get("/health", health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"🌐 Health check server running on port {PORT}")
    
    while True:
        await asyncio.sleep(3600)

# ==================== SPACE UPLOAD STRATEGY ====================
async def upload_to_space_with_retry(file_path, target_path, retries=3):
    """آپلود به Space با retry و error handling مخصوص Space"""
    for attempt in range(retries):
        try:
            logger.info(f"🔄 Attempt {attempt+1}/{retries} uploading {target_path}")
            
            hf_api.upload_file(
                path_or_fileobj=file_path,
                path_in_repo=target_path,
                repo_id=HF_REPO_ID,
                repo_type="space",  # 🔴 اینجا Space می‌ماند
                commit_message=f"Upload via Telegram Bot - {target_path}"
            )
            
            logger.info(f"✅ Successfully uploaded {target_path}")
            return True
            
        except Exception as e:
            error_str = str(e)
            logger.warning(f"⚠️ Upload attempt {attempt+1} failed: {error_str[:100]}")
            
            # تشخیص خطای LFS/403
            if "403" in error_str or "LFS" in error_str.upper():
                logger.error("🚫 LFS/403 Error - Space limitation hit")
                # اگر خطای LFS باشد، سایز chunk را کاهش می‌دهیم
                global SPACE_CHUNK_SIZE
                SPACE_CHUNK_SIZE = max(50 * 1024 * 1024, SPACE_CHUNK_SIZE * 0.8)  # 20% کاهش
                logger.info(f"🔽 Reduced chunk size to {SPACE_CHUNK_SIZE//(1024*1024)}MB")
            
            if attempt < retries - 1:
                wait_time = 5 * (attempt + 1)
                logger.info(f"⏳ Waiting {wait_time}s before retry...")
                await asyncio.sleep(wait_time)
    
    return False

def format_size(size_bytes):
    """Format file size to human readable"""
    if size_bytes == 0:
        return "0B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {size_name[i]}"

def create_manifest_file(parts_info, original_filename, user_id):
    """فایل manifest می‌سازد برای ترکیب مجدد تکه‌ها"""
    manifest = {
        "original_filename": original_filename,
        "total_parts": len(parts_info),
        "parts": parts_info,
        "timestamp": datetime.now().isoformat(),
        "user_id": user_id,
        "instructions": "Combine with: cat *.part* > original_filename"
    }
    
    manifest_path = f"/tmp/{original_filename}.manifest.json"
    with open(manifest_path, 'w') as f:
        json.dump(manifest, f, indent=2)
    
    return manifest_path

# ==================== TELEGRAM BOT ====================
async def start_telegram_bot():
    """Start Telegram bot with error handling"""
    while True:
        try:
            logger.info("🚀 Starting Telegram bot (SPACE MODE)...")
            await client.start(bot_token=BOT_TOKEN)
            bot_status["running"] = True
            bot_status["last_error"] = None
            logger.info("✅ Bot started successfully in SPACE mode!")
            logger.warning("⚠️  SPACE MODE: Files split into 90MB chunks due to LFS limits")
            
            register_handlers()
            await client.run_until_disconnected()
            
        except FloodWaitError as e:
            wait_time = e.seconds
            bot_status["last_error"] = f"FloodWait: {wait_time}s"
            logger.warning(f"⏳ FloodWait: sleeping for {wait_time}s")
            await asyncio.sleep(wait_time + 5)
            
        except ApiIdInvalidError as e:
            bot_status["last_error"] = "Invalid API credentials"
            logger.error("❌ CONFIGURATION ERROR: Invalid API_ID or API_HASH")
            await asyncio.sleep(300)
            
        except Exception as e:
            bot_status["last_error"] = str(e)
            logger.exception("❌ Bot crashed:")
            await asyncio.sleep(10)

def register_handlers():
    """Register all Telegram event handlers"""
    
    @client.on(events.NewMessage(pattern='/start'))
    async def start_handler(event):
        """Handle /start command"""
        try:
            welcome_message = (
                "🤖 **Telegram to Hugging Face Bot (SPACE MODE)**\n\n"
                f"📤 **Max file size:** {MAX_FILE_SIZE//(1024*1024)}MB\n"
                "⚠️ **Note:** Large files split into 90MB chunks automatically\n\n"
                f"📦 **Your Space:**\n`{HF_REPO_ID}`\n\n"
                "**How it works:**\n"
                "1. Send any file (up to 950MB)\n"
                "2. Bot splits into 90MB chunks if needed\n"
                "3. Uploads to your Space\n"
                "4. You get a manifest file for reassembly\n\n"
                "**Commands:**\n"
                "/start - Show this\n"
                "/help - Detailed help\n"
                "/status - Bot status\n"
                "/mode - Current upload mode"
            )
            await event.reply(welcome_message)
        except Exception as e:
            logger.error(f"Error in start_handler: {e}")
    
    @client.on(events.NewMessage(pattern='/help'))
    async def help_handler(event):
        """Handle /help command"""
        try:
            help_message = (
                "**📚 SPACE MODE HELP**\n\n"
                "**Why 90MB chunks?**\n"
                "HuggingFace Spaces limit Git LFS for large files.\n"
                "90MB chunks avoid 403 errors.\n\n"
                "**For files > 90MB:**\n"
                "1. File split into 90MB chunks\n"
                "2. Each uploaded separately\n"
                "3. You get filename.part001, .part002, etc.\n"
                "4. Also get a .manifest.json file\n\n"
                "**To reassemble on Linux/Mac:**\n"
                "```bash\n"
                "cat filename.part* > original_filename\n"
                "```\n\n"
                "**On Windows (PowerShell):**\n"
                "```powershell\n"
                "Get-Content filename.part* -AsByteStream | Set-Content original_filename -AsByteStream\n"
                "```\n\n"
                f"**Browse files:**\nhttps://huggingface.co/spaces/{HF_REPO_ID}/tree/main"
            )
            await event.reply(help_message)
        except Exception as e:
            logger.error(f"Error in help_handler: {e}")
    
    @client.on(events.NewMessage(pattern='/mode'))
    async def mode_handler(event):
        """Show current upload mode"""
        try:
            mode_info = (
                "**🔧 Current Mode: SPACE**\n\n"
                f"• **Max file:** {MAX_FILE_SIZE//(1024*1024)}MB\n"
                f"• **Chunk size:** {SPACE_CHUNK_SIZE//(1024*1024)}MB\n"
                f"• **Space:** `{HF_REPO_ID}`\n\n"
                "**⚠️ Limitations:**\n"
                "• Files >90MB split automatically\n"
                "• Need to reassemble manually\n"
                "• 950MB hard limit\n\n"
                "**Switch to Dataset mode?**\n"
                "Create a Dataset repo and change HF_REPO_ID"
            )
            await event.reply(mode_info)
        except Exception as e:
            logger.error(f"Error in mode_handler: {e}")
    
    @client.on(events.NewMessage(pattern='/status'))
    async def status_handler(event):
        """Handle /status command"""
        try:
            # Check disk space
            disk_info = ""
            try:
                stat = os.statvfs('/')
                free_gb = (stat.f_bavail * stat.f_frsize) / (1024**3)
                disk_info = f"• 💾 Disk free: {free_gb:.1f}GB\n"
            except:
                pass
            
            status_msg = (
                "**🤖 Bot Status (SPACE MODE)**\n\n"
                f"• ✅ Status: {'Running' if bot_status['running'] else 'Starting'}\n"
                f"{disk_info}"
                f"• 📦 Space: `{HF_REPO_ID}`\n"
                f"• 🔧 Chunk size: {SPACE_CHUNK_SIZE//(1024*1024)}MB\n"
                f"• 🚫 Max file: {MAX_FILE_SIZE//(1024*1024)}MB\n"
            )
            
            if bot_status["last_error"]:
                status_msg += f"\n**⚠️ Last error:**\n`{bot_status['last_error'][:100]}`"
            
            await event.reply(status_msg)
        except Exception as e:
            logger.error(f"Error in status_handler: {e}")
    
    @client.on(events.NewMessage)
    async def file_handler(event):
        """Handle incoming files - SPACE OPTIMIZED VERSION"""
        # Skip commands
        if event.message.text and event.message.text.startswith('/'):
            return
        
        # Check if message contains a file
        if not event.file:
            return
        
        user_id = event.sender_id
        original_filename = event.file.name or f"file_{int(datetime.now().timestamp())}"
        file_size = event.file.size
        upload_id = f"{user_id}_{int(datetime.now().timestamp())}"
        
        # 🔴 بررسی سایز فایل
        if file_size > MAX_FILE_SIZE:
            await event.reply(
                f"❌ **File too large!**\n\n"
                f"File: {original_filename}\n"
                f"Size: {format_size(file_size)}\n"
                f"Limit: {format_size(MAX_FILE_SIZE)}\n\n"
                f"**Solutions:**\n"
                f"1. Split file manually before sending\n"
                f"2. Or switch to Dataset mode (no limit)"
            )
            return
        
        status_msg = None
        
        try:
            # Prevent multiple uploads from same user
            if user_id in active_uploads:
                await event.reply("⏳ You have an active upload. Please wait...")
                return
            
            active_uploads[user_id] = upload_id
            
            # Send initial status
            status_msg = await event.reply(
                f"📥 **Processing:** `{original_filename}`\n"
                f"📊 **Size:** {format_size(file_size)}\n"
                f"🔧 **Mode:** Space (90MB chunks)\n"
                f"⏳ **Status:** Starting..."
            )
            
            # Calculate chunks needed
            total_chunks = math.ceil(file_size / SPACE_CHUNK_SIZE)
            timestamp = int(datetime.now().timestamp())
            
            # Clean filename for parts
            safe_name = ''.join(c for c in original_filename if c.isalnum() or c in '._- ')[:50]
            base_name = f"{timestamp}_{safe_name}"
            
            # 🔴 STREAMING DOWNLOAD + REAL-TIME UPLOAD
            uploaded_parts = []
            current_part = 1
            current_chunk_data = b""
            downloaded_bytes = 0
            
            # Create temp directory
            temp_dir = f"/tmp/{upload_id}"
            os.makedirs(temp_dir, exist_ok=True)
            
            await status_msg.edit(
                f"📥 **Downloading:** `{original_filename}`\n"
                f"📊 **Progress:** 0% (0/{format_size(file_size)})\n"
                f"🔢 **Chunks:** {total_chunks} needed\n"
                f"⏳ **Status:** Streaming..."
            )
            
            # Stream from Telegram
            async for chunk in client.iter_download(event.media, chunk_size=DOWNLOAD_CHUNK):
                current_chunk_data += chunk
                downloaded_bytes += len(chunk)
                
                # Upload chunk when it reaches SPACE_CHUNK_SIZE
                if len(current_chunk_data) >= SPACE_CHUNK_SIZE:
                    chunk_filename = f"{base_name}.part{current_part:03d}"
                    chunk_path = f"{temp_dir}/{chunk_filename}"
                    
                    # Save chunk to temp file
                    with open(chunk_path, 'wb') as f:
                        f.write(current_chunk_data)
                    
                    # Update status
                    progress = (downloaded_bytes / file_size) * 100
                    await status_msg.edit(
                        f"📤 **Uploading chunk {current_part}/{total_chunks}**\n"
                        f"📊 **Progress:** {progress:.1f}% ({format_size(downloaded_bytes)}/{format_size(file_size)})\n"
                        f"📦 **Chunk size:** {format_size(len(current_chunk_data))}\n"
                        f"⏳ **Status:** Uploading to Space..."
                    )
                    
                    # Upload to Space
                    success = await upload_to_space_with_retry(
                        chunk_path, 
                        chunk_filename,
                        retries=2
                    )
                    
                    if success:
                        uploaded_parts.append({
                            "filename": chunk_filename,
                            "size": len(current_chunk_data),
                            "part": current_part
                        })
                        # Delete temp file immediately
                        os.remove(chunk_path)
                        current_part += 1
                    else:
                        raise Exception(f"Failed to upload chunk {current_part}")
                    
                    # Reset for next chunk
                    current_chunk_data = b""
                
                # Update progress every 5%
                if downloaded_bytes % (file_size // 20) < DOWNLOAD_CHUNK:
                    progress = (downloaded_bytes / file_size) * 100
                    await status_msg.edit(
                        f"📥 **Downloading:** `{original_filename}`\n"
                        f"📊 **Progress:** {progress:.1f}% ({format_size(downloaded_bytes)}/{format_size(file_size)})\n"
                        f"🔢 **Chunks uploaded:** {current_part-1}/{total_chunks}\n"
                        f"⏳ **Status:** Streaming..."
                    )
            
            # Upload final chunk (if any remaining data)
            if current_chunk_data:
                chunk_filename = f"{base_name}.part{current_part:03d}"
                chunk_path = f"{temp_dir}/{chunk_filename}"
                
                with open(chunk_path, 'wb') as f:
                    f.write(current_chunk_data)
                
                await status_msg.edit(
                    f"📤 **Uploading final chunk {current_part}/{total_chunks}**\n"
                    f"📊 **Size:** {format_size(len(current_chunk_data))}\n"
                    f"⏳ **Status:** Finalizing..."
                )
                
                success = await upload_to_space_with_retry(chunk_path, chunk_filename)
                if success:
                    uploaded_parts.append({
                        "filename": chunk_filename,
                        "size": len(current_chunk_data),
                        "part": current_part
                    })
                    os.remove(chunk_path)
                else:
                    raise Exception("Failed to upload final chunk")
            
            # Create and upload manifest file
            manifest_path = create_manifest_file(uploaded_parts, original_filename, user_id)
            manifest_filename = f"{base_name}.manifest.json"
            
            await status_msg.edit(
                f"📝 **Creating manifest file...**\n"
                f"⏳ **Status:** Finalizing upload..."
            )
            
            # Upload manifest
            await upload_to_space_with_retry(manifest_path, manifest_filename)
            os.remove(manifest_path)
            
            # Clean up temp directory
            os.rmdir(temp_dir)
            
            # Create success message
            base_url = f"https://huggingface.co/spaces/{HF_REPO_ID}/resolve/main"
            
            if len(uploaded_parts) == 1:
                success_message = (
                    f"✅ **Upload complete!**\n\n"
                    f"📁 **File:** `{original_filename}`\n"
                    f"📊 **Size:** {format_size(file_size)}\n"
                    f"🔗 **Direct download:**\n`{base_url}/{uploaded_parts[0]['filename']}`\n\n"
                    f"📂 **Browse:** https://huggingface.co/spaces/{HF_REPO_ID}/tree/main"
                )
            else:
                # Show first 3 parts
                parts_list = "\n".join(
                    [f"• Part {p['part']:03d}: `{base_url}/{p['filename']}`" 
                     for p in uploaded_parts[:3]]
                )
                if len(uploaded_parts) > 3:
                    parts_list += f"\n• ... and {len(uploaded_parts)-3} more parts"
                
                success_message = (
                    f"✅ **Upload complete!**\n\n"
                    f"📁 **File:** `{original_filename}`\n"
                    f"📊 **Size:** {format_size(file_size)}\n"
                    f"🔢 **Split into:** {len(uploaded_parts)} chunks (90MB each)\n\n"
                    f"**🔗 Download links:**\n{parts_list}\n\n"
                    f"**📋 Manifest file:**\n`{base_url}/{manifest_filename}`\n\n"
                    f"**🔧 Reassemble command:**\n"
                    f"```bash\n"
                    f"cat {base_name}.part* > \"{original_filename}\"\n"
                    f"```\n\n"
                    f"📂 **Browse all:** https://huggingface.co/spaces/{HF_REPO_ID}/tree/main"
                )
            
            await status_msg.edit(success_message)
            
        except Exception as e:
            logger.exception(f"Error processing {original_filename}:")
            try:
                error_msg = (
                    f"❌ **Upload failed!**\n\n"
                    f"File: `{original_filename}`\n"
                    f"Error: `{str(e)[:150]}`\n\n"
                    f"**Possible reasons:**\n"
                    f"• Space storage limit reached\n"
                    f"• LFS limitation\n"
                    f"• Network issue\n\n"
                    f"Try smaller file or switch to Dataset mode."
                )
                if status_msg:
                    await status_msg.edit(error_msg)
                else:
                    await event.reply(error_msg)
            except:
                pass
        finally:
            # Cleanup
            if user_id in active_uploads and active_uploads[user_id] == upload_id:
                del active_uploads[user_id]
            
            # Clean any leftover temp files
            try:
                temp_dir = f"/tmp/{upload_id}"
                if os.path.exists(temp_dir):
                    for f in os.listdir(temp_dir):
                        os.remove(f"{temp_dir}/{f}")
                    os.rmdir(temp_dir)
            except:
                pass

# ==================== MAIN ====================
async def main():
    """Main function"""
    logger.info("=" * 60)
    logger.info("🚀 Starting Telegram to Hugging Face Bot")
    logger.info("🔧 MODE: SPACE (with 90MB chunking)")
    logger.info(f"📦 HF Space: {HF_REPO_ID}")
    logger.info(f"⚠️  Max file size: {MAX_FILE_SIZE//(1024*1024)}MB")
    logger.info(f"🔽 Chunk size: {SPACE_CHUNK_SIZE//(1024*1024)}MB")
    logger.info("=" * 60)
    logger.warning("⚠️  Space mode has limitations. Consider switching to Dataset for large files.")
    
    # Create necessary directories
    os.makedirs("/data", exist_ok=True)
    
    # Clean old temp files
    try:
        for f in os.listdir("/tmp"):
            if f.endswith(".part") or f.endswith(".manifest.json"):
                try:
                    os.remove(f"/tmp/{f}")
                except:
                    pass
    except:
        pass
    
    # Run both services
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
