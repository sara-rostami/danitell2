import asyncio
import os
import logging
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
HF_REPO_ID = os.environ.get("HF_REPO_ID")  # Format: username/repo-name
PORT = int(os.environ.get("PORT", 8000))

# Initialize Hugging Face
login(token=HF_TOKEN)
hf_api = HfApi()

# Global variables
user_files = {}
bot_status = {"running": False, "last_error": None}

# Use session file for persistence
session_path = "/data/bot_session" if os.path.exists("/data") else "bot_session"
client = TelegramClient(session_path, API_ID, API_HASH)

# ==================== HTTP HEALTH CHECK SERVER ====================
async def health_check(request):
    """Health check endpoint for Koyeb"""
    status = "healthy" if bot_status["running"] else "starting"
    return web.Response(text=f"OK - Bot status: {status}", status=200)

async def root_handler(request):
    """Root endpoint"""
    info = {
        "service": "Telegram to Hugging Face Bot",
        "status": "running" if bot_status["running"] else "starting",
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
    
    # Keep the server running
    while True:
        await asyncio.sleep(3600)

# ==================== TELEGRAM BOT ====================
async def start_telegram_bot():
    """Start Telegram bot with error handling"""
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
            # Sleep but don't exit - keep health check alive
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
                "🤖 **Telegram to Hugging Face Bot**\n\n"
                "📤 Send me any file and I will upload it to Hugging Face Space!\n\n"
                "📥 After upload, you can download it from:\n"
                f"`https://huggingface.co/spaces/{HF_REPO_ID}/tree/main`\n\n"
                "Commands:\n"
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
                "2️⃣ I will upload it to Hugging Face Space\n"
                "3️⃣ You'll receive a download link\n\n"
                "**Download your files:**\n"
                f"Visit: https://huggingface.co/spaces/{HF_REPO_ID}/tree/main\n\n"
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
                message = f"**Your uploaded files:**\n\n{files_list}\n\n"
                message += f"Download from: https://huggingface.co/spaces/{HF_REPO_ID}/tree/main"
                await event.reply(message)
            else:
                await event.reply("You haven't uploaded any files yet!")
        except Exception as e:
            logger.error(f"Error in list_handler: {e}")
    
    @client.on(events.NewMessage)
    async def file_handler(event):
        """Handle incoming files"""
        # Skip if it's a command
        if event.message.text and event.message.text.startswith('/'):
            return
        
        # Check if message contains a file
        if not event.file:
            return
        
        user_id = event.sender_id
        status_msg = None
        
        try:
            # Send processing message
            status_msg = await event.reply("📥 Downloading file from Telegram...")
            
            # Download file
            file_path = await event.download_media(file=f"downloads/")
            file_name = os.path.basename(file_path)
            
            logger.info(f"File downloaded: {file_name}")
            
            # Update status
            await status_msg.edit("📤 Uploading to Hugging Face Space...")
            
            # Upload to Hugging Face
            try:
                hf_api.upload_file(
                    path_or_fileobj=file_path,
                    path_in_repo=file_name,
                    repo_id=HF_REPO_ID,
                    repo_type="space",
                    commit_message=f"Upload {file_name} via Telegram bot"
                )
                
                logger.info(f"File uploaded to HF: {file_name}")
                
                # Store file in user's list
                if user_id not in user_files:
                    user_files[user_id] = []
                user_files[user_id].append(file_name)
                
                # Create download URL
                download_url = f"https://huggingface.co/spaces/{HF_REPO_ID}/resolve/main/{file_name}"
                
                # Send success message
                success_message = (
                    "✅ **File uploaded successfully!**\n\n"
                    f"📁 File name: `{file_name}`\n"
                    f"📊 Size: {event.file.size / 1024 / 1024:.2f} MB\n\n"
                    f"**Download link:**\n{download_url}\n\n"
                    f"**Or browse all files:**\nhttps://huggingface.co/spaces/{HF_REPO_ID}/tree/main"
                )
                await status_msg.edit(success_message)
                
            except Exception as e:
                logger.error(f"Error uploading to HF: {e}")
                if status_msg:
                    await status_msg.edit(f"❌ Error uploading to Hugging Face: {str(e)}")
            
            # Clean up downloaded file
            if os.path.exists(file_path):
                os.remove(file_path)
                
        except Exception as e:
            logger.error(f"Error processing file: {e}")
            try:
                if status_msg:
                    await status_msg.edit(f"❌ Error processing file: {str(e)}")
                else:
                    await event.reply(f"❌ Error processing file: {str(e)}")
            except:
                pass

# ==================== MAIN ====================
async def main():
    """Main function - runs both HTTP server and Telegram bot"""
    logger.info("=" * 60)
    logger.info("🚀 Starting Telegram to Hugging Face Bot")
    logger.info(f"📦 HF Repository: {HF_REPO_ID}")
    logger.info(f"🌐 Health check port: {PORT}")
    logger.info("=" * 60)
    
    # Create downloads directory
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
