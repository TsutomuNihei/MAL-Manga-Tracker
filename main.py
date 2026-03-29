from fpdf import FPDF
from discord.ext import tasks
import discord
from discord import app_commands
import os
import requests
import sqlite3
from dotenv import load_dotenv

# --- DATABASE SETUP ---
def init_db():
    conn = sqlite3.connect('manga_bot.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS tracking
                 (user_id INTEGER, manga_id INTEGER, manga_title TEXT, last_chapter INTEGER)''')
    # NEW: Table to link Discord users to their MAL usernames
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (discord_id INTEGER PRIMARY KEY, mal_username TEXT)''')
    conn.commit()
    conn.close()

init_db()

# --- TOKEN LOADING ---
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
MAL_ID = os.getenv('MAL_CLIENT_ID')

# Check if tokens are missing to prevent crash
if not TOKEN or not MAL_ID:
    print("ERROR: DISCORD_TOKEN or MAL_CLIENT_ID missing in .env file!")
    exit()

class MyBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True 
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
    
    async def setup_hook(self):
        # Starts the background loop when the bot starts
        self.check_for_updates.start()

    @tasks.loop(hours=6) # Checks every 6 hours to be safe with API limits
    async def check_for_updates(self):
        print("Checking for manga updates...")
        conn = sqlite3.connect('manga_bot.db')
        c = conn.cursor()
        c.execute("SELECT user_id, manga_id, manga_title, last_chapter FROM tracking")
        all_tracked = c.fetchall()

        headers = {'X-MAL-CLIENT-ID': MAL_ID}

        for user_id, m_id, m_title, last_ch in all_tracked:
            url = f'https://api.myanimelist.net/v2/manga/{m_id}?fields=num_chapters'
            response = requests.get(url, headers=headers)
            
            if response.status_code == 200:
                data = response.json()
                new_ch = data.get('num_chapters', 0)

                # If a new chapter is out (and it's not 0)
                if new_ch > last_ch and new_ch != 0:
                    try:
                        user = await self.fetch_user(user_id)
                        await user.send(f"📚 **Update Alert!** A new chapter of **{m_title}** is out! (Chapter {new_ch})")
                        
                        # Update the database so we don't notify for the same chapter again
                        c.execute("UPDATE tracking SET last_chapter = ? WHERE user_id = ? AND manga_id = ?", 
                                  (new_ch, user_id, m_id))
                        conn.commit()
                    except Exception as e:
                        print(f"Could not message user {user_id}: {e}")
            
        conn.close()
        print("Update check complete.")

    async def on_ready(self):
        await self.tree.sync()
        print(f'Logged in as {self.user} (ID: {self.user.id})')
        print("Slash commands synced!")

bot = MyBot()

@bot.tree.command(name="user", description="Link your MyAnimeList username to the bot")
async def set_user(interaction: discord.Interaction, username: str):
    conn = sqlite3.connect('manga_bot.db')
    c = conn.cursor()
    
    # Use 'INSERT OR REPLACE' so users can update their username later if they want
    c.execute("INSERT OR REPLACE INTO users (discord_id, mal_username) VALUES (?, ?)", 
              (interaction.user.id, username))
    
    conn.commit()
    conn.close()
    
    await interaction.response.send_message(f"✅ Linked! I've set your MyAnimeList username to **{username}**.")

@bot.tree.command(name="export", description="Export your personal MAL list as a PDF")
async def export_pdf(interaction: discord.Interaction):
    # 1. Fetch the saved MAL username
    conn = sqlite3.connect('manga_bot.db')
    c = conn.cursor()
    c.execute("SELECT mal_username FROM users WHERE discord_id = ?", (interaction.user.id,))
    result = c.fetchone()
    
    if not result:
        await interaction.response.send_message("Please set your MAL username first using `/set_user`!")
        conn.close()
        return
        
    mal_user = result[0]
    
    # 2. Get the tracking data
    c.execute("SELECT manga_title, last_chapter FROM tracking WHERE user_id = ?", (interaction.user.id,))
    rows = c.fetchall()
    conn.close()

    # ... [Rest of the PDF generation code from before] ...
    # Just update the title line in the PDF:
    # pdf.cell(40, 10, f"MAL List for {mal_user}")

    # 2. Create PDF
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", "B", 16)
    pdf.cell(40, 10, f"MAL list for {mal_user}")
    pdf.ln(10)
    
    pdf.set_font("Arial", "", 12)
    pdf.cell(100, 10, "Manga Title", border=1)
    pdf.cell(40, 10, "Last Chapter", border=1)
    pdf.ln()

    for title, chapter in rows:
        pdf.cell(100, 10, str(title), border=1)
        pdf.cell(40, 10, str(chapter), border=1)
        pdf.ln()

    # 3. Save and Send
    file_name = f"{interaction.user.id}_list.pdf"
    pdf.output(file_name)
    
    file = discord.File(file_name)
    await interaction.response.send_message("Here is your exported list!", file=file)
    
    # 4. Cleanup (Delete the file from your computer after sending)
    os.remove(file_name)
    
# --- COMMAND: SEARCH MANGA ---
@bot.tree.command(name="manga", description="Search for a manga and see its details")
async def manga(interaction: discord.Interaction, title: str):
    # Requesting specific fields: title, main_picture, and synopsis
    url = f'https://api.myanimelist.net/v2/manga?q={title}&limit=1&fields=id,title,main_picture,synopsis'
    headers = {'X-MAL-CLIENT-ID': MAL_ID}
    
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        data = response.json()
        if data.get('data'):
            node = data['data'][0]['node']
            m_title = node['title']
            m_id = node['id']
            m_synopsis = node.get('synopsis', 'No description available.')[:300] + "..."
            m_img = node['main_picture']['large'] if 'main_picture' in node else None

            # Create a professional Discord Embed
            embed = discord.Embed(title=m_title, url=f"https://myanimelist.net/manga/{m_id}", color=discord.Color.blue())
            if m_img:
                embed.set_thumbnail(url=m_img)
            embed.add_field(name="MAL ID", value=m_id, inline=True)
            embed.add_field(name="Synopsis", value=m_synopsis, inline=False)
            embed.set_footer(text="Use /track to get update notifications!")

            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message("No manga found with that name.")
    else:
        await interaction.response.send_message("Failed to connect to MAL API. Check your Client ID.")

# --- COMMAND: TRACK MANGA ---
@bot.tree.command(name="track", description="Save a manga to your watch list for chapter updates")
async def track(interaction: discord.Interaction, title: str):
    url = f'https://api.myanimelist.net/v2/manga?q={title}&limit=1&fields=num_chapters'
    headers = {'X-MAL-CLIENT-ID': MAL_ID}
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        data = response.json()
        if data.get('data'):
            node = data['data'][0]['node']
            m_id = node['id']
            m_title = node['title']
            m_chapters = node.get('num_chapters', 0)

            # Database Interaction
            conn = sqlite3.connect('manga_bot.db')
            c = conn.cursor()
            # Check if already tracking to avoid duplicates
            c.execute("SELECT * FROM tracking WHERE user_id = ? AND manga_id = ?", (interaction.user.id, m_id))
            if c.fetchone():
                await interaction.response.send_message(f"You are already tracking **{m_title}**!")
            else:
                c.execute("INSERT INTO tracking (user_id, manga_id, manga_title, last_chapter) VALUES (?, ?, ?, ?)",
                          (interaction.user.id, m_id, m_title, m_chapters))
                conn.commit()
                await interaction.response.send_message(f"✅ Successfully tracking **{m_title}**! I'll ping you here when new chapters arrive.")
            conn.close()
        else:
            await interaction.response.send_message("Manga not found.")
    else:
        await interaction.response.send_message("MAL API Error.")

# --- COMMAND: LIST TRACKED ---
@bot.tree.command(name="list", description="Show all manga you are currently tracking")
async def list_tracked(interaction: discord.Interaction):
    conn = sqlite3.connect('manga_bot.db')
    c = conn.cursor()
    c.execute("SELECT manga_title FROM tracking WHERE user_id = ?", (interaction.user.id,))
    rows = c.fetchall()
    conn.close()

    if not rows:
        await interaction.response.send_message("Your tracking list is empty! Use `/track` to add some.")
        return

    manga_list = "\n".join([f"• {r[0]}" for r in rows])
    embed = discord.Embed(title=f"{interaction.user.name}'s Tracking List", description=manga_list, color=discord.Color.green())
    await interaction.response.send_message(embed=embed)

bot.run(TOKEN)
