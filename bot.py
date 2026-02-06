import asyncio
import os
import logging
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from huggingface_hub import HfApi, login

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

# Initialize Hugging Face
login(token=HF_TOKEN)
hf_api = HfApi()

# Global variable to store user file mappings
user_files = {}

async def main():
    """Main bot function"""
    try:
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.start(bot_token=BOT_TOKEN)
        
        logger.info("Bot started successfully!")
        
        @client.on(events.NewMessage(pattern='/start'))
        async def start_handler(event):
            """Handle /start command"""
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
        
        @client.on(events.NewMessage(pattern='/help'))
        async def help_handler(event):
            """Handle /help command"""
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
        
        @client.on(events.NewMessage(pattern='/list'))
        async def list_handler(event):
            """Handle /list command"""
            user_id = event.sender_id
            if user_id in user_files and user_files[user_id]:
                files_list = "\n".join([f"• {f}" for f in user_files[user_id]])
                message = f"**Your uploaded files:**\n\n{files_list}\n\n"
                message += f"Download from: https://huggingface.co/spaces/{HF_REPO_ID}/tree/main"
                await event.reply(message)
            else:
                await event.reply("You haven't uploaded any files yet!")
        
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
                    await status_msg.edit(f"❌ Error uploading to Hugging Face: {str(e)}")
                
                # Clean up downloaded file
                if os.path.exists(file_path):
                    os.remove(file_path)
                    
            except Exception as e:
                logger.error(f"Error processing file: {e}")
                await event.reply(f"❌ Error processing file: {str(e)}")
        
        logger.info("Bot is running and listening for messages...")
        await client.run_until_disconnected()
        
    except Exception as e:
        logger.error(f"Bot error: {e}")
        raise

if __name__ == "__main__":
    # Create downloads directory
    os.makedirs("downloads", exist_ok=True)
    
    # Run the bot
    asyncio.run(main())
