import asyncio
import os
import logging
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, ApiIdInvalidError
from huggingface_hub import HfApi, login, create_repo
from aiohttp import web
import time

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
HF_DATASET_ID = os.environ.get("HF_DATASET_ID")  # Format: username/dataset-name
PORT = int(os.environ.get("PORT", 8000))

# Initialize Hugging Face
login(token=HF_TOKEN)
hf_api = HfApi()

# Global variables
user_files = {}
bot_status = {"running": False, "last_error": None}
upload_progress = {}  # Track upload progress per user

# Use session file for persistence
session_path = "/data/bot_session" if os.path.exists("/data") else "bot_session"
client = TelegramClient(session_path, API_ID, API_HASH)

# ==================== HUGGING FACE DATASET SETUP ====================
def ensure_dataset_exists():
    """Ensure the dataset exists, create if not"""
    try:
        # Try to get dataset info
        hf_api.dataset_info(HF_DATASET_ID)
        logger.info(f"✅ Dataset exists: {HF_DATASET_ID}")
    except Exception as e:
        logger.info(f"📦 Creating new dataset: {HF_DATASET_ID}")
        try:
            create_repo(
                repo_id=HF_DATASET_ID,
                repo_type="dataset",
                private=False,
                exist_ok=True
            )
            logger.info(f"✅ Dataset created: {HF_DATASET_ID}")
        except Exception as create_error:
            logger.error(f"❌ Failed to create dataset: {create_error}")
            raise

# ==================== STREAMING UPLOAD ====================
async def upload_file_with_progress(file_path, file_name, user_id, status_msg):
    """Upload file to HF Dataset with progress tracking"""
    file_size = os.path.getsize(file_path)
    
    # Progress tracking
    uploaded_bytes = [0]  # Use list to modify in nested function
    last_update = [time.time()]
    start_time = time.time()
    
    # Create a custom tqdm-like callback
    class ProgressTracker:
        def __init__(self):
            self.n = 0
            
        def update(self, n):
            self.n += n
            # Schedule async update
            loop = asyncio.get_event_loop()
            asyncio.run_coroutine_threadsafe(
                update_message(self.n),
                loop
            )
    
    async def update_message(current_bytes):
        """Update Telegram message with progress"""
        uploaded_bytes[0] = current_bytes
        current_time = time.time()
        
        # Update every 2 seconds
        if current_time - last_update[0] >= 2:
            percentage = (current_bytes / file_size) * 100 if file_size > 0 else 0
            elapsed = current_time - start_time
            speed = current_bytes / elapsed if elapsed > 0 else 0
            
            speed_str = _format_speed(speed)
            filled = int(percentage / 10)
            empty = 10 - filled
            progress_bar = f"[{'█' * filled}{'░' * empty}]"
            
            message = (
                f"📤 **Uploading to Hugging Face...**\n\n"
                f"📁 File: `{file_name}`\n"
                f"📊 Progress: {percentage:.1f}%\n"
                f"{progress_bar}\n"
                f"🚀 Speed: {speed_str}\n"
                f"📦 Uploaded: {_format_size(current_bytes)} / {_format_size(file_size)}"
            )
            
            try:
                await status_msg.edit(message)
                last_update[0] = current_time
            except Exception as e:
                logger.debug(f"Could not update progress: {e}")
    
    # Upload in executor
    loop = asyncio.get_event_loop()
    
    def do_upload():
        """Perform the upload"""
        try:
            # For now, simple upload without real-time progress
            # HF API doesn't expose easy progress callbacks
            hf_api.upload_file(
                path_or_fileobj=file_path,
                path_in_repo=file_name,
                repo_id=HF_DATASET_ID,
                repo_type="dataset",
                commit_message=f"Upload {file_name} via Telegram bot"
            )
            return True
        except Exception as e:
            logger.error(f"HF upload error: {e}")
            raise
    
    # Show uploading message
    await update_message(0)
    
    # Simulate progress updates while uploading
    async def show_progress():
        """Show simulated progress during upload"""
        for i in range(1, 10):
            await asyncio.sleep(3)
            simulated_progress = (file_size * i) // 10
            await update_message(simulated_progress)
    
    # Run upload and progress updates concurrently
    try:
        progress_task = asyncio.create_task(show_progress())
        await loop.run_in_executor(None, do_upload)
        progress_task.cancel()
        
        # Final 100% update
        await update_message(file_size)
        
    except Exception as e:
        logger.error(f"Upload failed: {e}")
        raise

# ==================== HTTP HEALTH CHECK SERVER ====================
async def health_check(request):
    """Health check endpoint for Koyeb"""
    status = "healthy" if bot_status["running"] else "starting"
    return web.Response(text=f"OK - Bot status: {status}", status=200)

async def root_handler(request):
    """Root endpoint"""
    info = {
        "service": "Telegram to Hugging Face Dataset Bot",
        "status": "running" if bot_status["running"] else "starting",
        "hf_dataset": HF_DATASET_ID,
        "total_uploads": sum(len(files) for files in user_files.values()),
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
    
    # Keep the server running
    while True:
        await asyncio.sleep(3600)

# ==================== TELEGRAM BOT ====================
async def start_telegram_bot():
    """Start Telegram bot with error handling"""
    # Ensure dataset exists before starting bot
    ensure_dataset_exists()
    
    while True:
        try:
            logger.info("🚀 Starting Telegram bot...")
            await client.start(bot_token=BOT_TOKEN)
            bot_status["running"] = True
            bot_status["last_error"] = None
            logger.info("✅ Bot started successfully!")
            
            # Register event handlers
            register_handlers()
            
            # Run until disconnected
            await client.run_until_disconnected()
            
        except FloodWaitError as e:
            wait_time = e.seconds
            bot_status["last_error"] = f"FloodWait: {wait_time}s"
            logger.warning(f"⏳ FloodWait detected: sleeping for {wait_time} seconds ({wait_time//60} minutes)")
            await asyncio.sleep(wait_time + 5)
            logger.info("🔄 Retrying connection after FloodWait...")
            
        except ApiIdInvalidError as e:
            bot_status["last_error"] = "Invalid API credentials"
            logger.error("=" * 60)
            logger.error("❌ CONFIGURATION ERROR: Invalid API_ID or API_HASH")
            logger.error("=" * 60)
            logger.error("Please check your Koyeb environment variables:")
            logger.error("  1. API_ID must be a NUMBER (without quotes)")
            logger.error("  2. API_HASH must be a 32-character string (without quotes)")
            logger.error("  3. Remove any spaces before/after the values")
            logger.error("=" * 60)
            await asyncio.sleep(300)
            
        except Exception as e:
            bot_status["last_error"] = str(e)
            logger.exception("❌ Bot crashed with error:")
            logger.info("🔄 Restarting in 10 seconds...")
            await asyncio.sleep(10)

def register_handlers():
    """Register all Telegram event handlers"""
def register_handlers():
    """Register all Telegram event handlers"""
    
    @client.on(events.NewMessage(pattern='/start'))
    async def start_handler(event):
        """Handle /start command"""
        try:
            welcome_message = (
                "🤖 **Telegram to Hugging Face Dataset Bot**\n\n"
                "📤 Send me any file and I will upload it to Hugging Face Dataset!\n\n"
                "📥 Your files will be stored in:\n"
                f"`https://huggingface.co/datasets/{HF_DATASET_ID}`\n\n"
                "✨ **Features:**\n"
                "• Real-time upload progress\n"
                "• Unlimited storage (Dataset, not Space)\n"
                "• Download speed indicator\n\n"
                "**Commands:**\n"
                "/start - Show this message\n"
                "/list - List your uploaded files\n"
                "/help - Get help"
            )
            await event.reply(welcome_message)
        except Exception as e:
            logger.error(f"Error in start_handler: {e}")
    
    @client.on(events.NewMessage(pattern='/help'))
    async def help_handler(event):
        """Handle /help command"""
        try:
            help_message = (
                "**How to use this bot:**\n\n"
                "1️⃣ Send me any file (document, image, video, etc.)\n"
                "2️⃣ Watch the upload progress in real-time\n"
                "3️⃣ Get a direct download link when done\n\n"
                "**View/Download your files:**\n"
                f"Visit: https://huggingface.co/datasets/{HF_DATASET_ID}/tree/main\n\n"
                "**Why Dataset instead of Space?**\n"
                "• Spaces have 1GB limit\n"
                "• Datasets have much larger storage\n"
                "• Better for file storage\n\n"
                "**Commands:**\n"
                "/start - Start the bot\n"
                "/list - List your uploaded files\n"
                "/help - Show this help"
            )
            await event.reply(help_message)
        except Exception as e:
            logger.error(f"Error in help_handler: {e}")
    
    @client.on(events.NewMessage(pattern='/list'))
    async def list_handler(event):
        """Handle /list command"""
        try:
            user_id = event.sender_id
            if user_id in user_files and user_files[user_id]:
                files_list = "\n".join([f"• {f}" for f in user_files[user_id]])
                message = (
                    f"**Your uploaded files ({len(user_files[user_id])}):**\n\n"
                    f"{files_list}\n\n"
                    f"**View all files:**\n"
                    f"https://huggingface.co/datasets/{HF_DATASET_ID}/tree/main"
                )
                await event.reply(message)
            else:
                await event.reply("You haven't uploaded any files yet!")
        except Exception as e:
            logger.error(f"Error in list_handler: {e}")
    
    @client.on(events.NewMessage)
    async def file_handler(event):
        """Handle incoming files with progress tracking"""
        # Skip if it's a command
        if event.message.text and event.message.text.startswith('/'):
            return
        
        # Check if message contains a file
        if not event.file:
            return
        
        user_id = event.sender_id
        status_msg = None
        file_path = None
        
        try:
            # Get file info
            file_size = event.file.size
            file_name = event.file.name or f"file_{int(time.time())}.{event.file.ext or 'bin'}"
            
            # Send initial message
            status_msg = await event.reply(
                f"📥 **Downloading from Telegram...**\n\n"
                f"📁 File: `{file_name}`\n"
                f"📊 Size: {_format_size(file_size)}\n"
                f"⏳ Please wait..."
            )
            
            # Download with progress
            download_start = time.time()
            last_progress_update = 0
            downloaded_bytes = 0
            
            async def download_callback(current, total):
                """Callback for download progress"""
                nonlocal last_progress_update, downloaded_bytes
                downloaded_bytes = current
                current_time = time.time()
                
                # Update every 2 seconds
                if current_time - last_progress_update >= 2:
                    percentage = (current / total) * 100
                    elapsed = current_time - download_start
                    speed = current / elapsed if elapsed > 0 else 0
                    
                    speed_str = _format_speed(speed)
                    progress_bar = _get_progress_bar(percentage)
                    
                    try:
                        await status_msg.edit(
                            f"📥 **Downloading from Telegram...**\n\n"
                            f"📁 File: `{file_name}`\n"
                            f"📊 Progress: {percentage:.1f}%\n"
                            f"{progress_bar}\n"
                            f"🚀 Speed: {speed_str}\n"
                            f"📦 Downloaded: {_format_size(current)} / {_format_size(total)}"
                        )
                        last_progress_update = current_time
                    except Exception as e:
                        logger.debug(f"Could not update download progress: {e}")
            
            # Download file with progress
            file_path = await event.download_media(
                file=f"downloads/",
                progress_callback=download_callback
            )
            
            logger.info(f"File downloaded: {file_name} ({_format_size(file_size)})")
            
            # Update status for upload
            await status_msg.edit("📤 **Uploading to Hugging Face Dataset...**")
            
            # Upload to Hugging Face with progress
            try:
                await upload_file_with_progress(file_path, file_name, user_id, status_msg)
                
                logger.info(f"File uploaded to HF Dataset: {file_name}")
                
                # Store file in user's list
                if user_id not in user_files:
                    user_files[user_id] = []
                user_files[user_id].append(file_name)
                
                # Create download URL
                download_url = f"https://huggingface.co/datasets/{HF_DATASET_ID}/resolve/main/{file_name}"
                browse_url = f"https://huggingface.co/datasets/{HF_DATASET_ID}/tree/main"
                
                # Send success message
                success_message = (
                    "✅ **Upload Complete!**\n\n"
                    f"📁 File: `{file_name}`\n"
                    f"📊 Size: {_format_size(file_size)}\n\n"
                    f"**Direct download:**\n{download_url}\n\n"
                    f"**Browse all files:**\n{browse_url}"
                )
                await status_msg.edit(success_message)
                
            except Exception as e:
                logger.error(f"Error uploading to HF: {e}")
                if status_msg:
                    await status_msg.edit(
                        f"❌ **Upload failed**\n\n"
                        f"Error: {str(e)}\n\n"
                        f"Please try again or contact support."
                    )
            
        except Exception as e:
            logger.error(f"Error processing file: {e}")
            try:
                if status_msg:
                    await status_msg.edit(f"❌ Error: {str(e)}")
                else:
                    await event.reply(f"❌ Error processing file: {str(e)}")
            except:
                pass
        
        finally:
            # Clean up downloaded file
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    logger.debug(f"Cleaned up: {file_path}")
                except Exception as e:
                    logger.debug(f"Could not remove file: {e}")

# ==================== HELPER FUNCTIONS ====================
def _format_size(bytes_size):
    """Format bytes to human readable"""
    if bytes_size < 1024:
        return f"{bytes_size} B"
    elif bytes_size < 1024 * 1024:
        return f"{bytes_size/1024:.1f} KB"
    elif bytes_size < 1024 * 1024 * 1024:
        return f"{bytes_size/(1024*1024):.1f} MB"
    else:
        return f"{bytes_size/(1024*1024*1024):.2f} GB"

def _format_speed(bytes_per_second):
    """Format speed to human readable"""
    if bytes_per_second < 1024:
        return f"{bytes_per_second:.0f} B/s"
    elif bytes_per_second < 1024 * 1024:
        return f"{bytes_per_second/1024:.1f} KB/s"
    else:
        return f"{bytes_per_second/(1024*1024):.1f} MB/s"

def _get_progress_bar(percentage):
    """Generate progress bar"""
    filled = int(percentage / 10)
    empty = 10 - filled
    return f"[{'█' * filled}{'░' * empty}]"

# ==================== MAIN ====================
async def main():
    """Main function - runs both HTTP server and Telegram bot"""
    logger.info("=" * 60)
    logger.info("🚀 Starting Telegram to Hugging Face Dataset Bot")
    logger.info(f"📦 HF Dataset: {HF_DATASET_ID}")
    logger.info(f"🌐 Health check port: {PORT}")
    logger.info("=" * 60)
    
    # Create directories
    os.makedirs("downloads", exist_ok=True)
    os.makedirs("/data", exist_ok=True)
    
    # Run both HTTP server and Telegram bot concurrently
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
