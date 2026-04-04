import discord
from discord import app_commands
from discord.ext import tasks
import os
import requests
import psycopg2
from fpdf import FPDF
from dotenv import load_dotenv
import urllib.parse

# --- DATABASE & TOKEN SETUP ---
load_dotenv()
DATABASE_URL = os.getenv('DATABASE_URL')
TOKEN = os.getenv('DISCORD_TOKEN')
MAL_ID = os.getenv('MAL_CLIENT_ID')

# Connection helper for Supabase
def get_db_connection():
    return psycopg2.connect(
        dbname=os.getenv('DB_NAME'),
        user=os.getenv('DB_USER'),
        password=os.getenv('DB_PASS'),
        host=os.getenv('DB_HOST'),
        port=os.getenv('DB_PORT')
    )

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    # Note: Use BIGINT for Discord IDs as they are very long
    c.execute('''CREATE TABLE IF NOT EXISTS tracking
                 (user_id BIGINT, manga_id INTEGER, manga_title TEXT, last_chapter INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (discord_id BIGINT PRIMARY KEY, mal_username TEXT)''')
    conn.commit()
    c.close()
    conn.close()

init_db()

class MyBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True 
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
    
    async def setup_hook(self):
        self.check_for_updates.start()

    @tasks.loop(hours=6)
    async def check_for_updates(self):
        print("Checking for manga updates in Supabase...")
        conn = get_db_connection()
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

                if new_ch > last_ch and new_ch != 0:
                    try:
                        user = await self.fetch_user(user_id)
                        await user.send(f"📚 **Update Alert!** A new chapter of **{m_title}** is out! (Chapter {new_ch})")
                        
                        # Postgres uses %s instead of ?
                        c.execute("UPDATE tracking SET last_chapter = %s WHERE user_id = %s AND manga_id = %s", 
                                  (new_ch, user_id, m_id))
                        conn.commit()
                    except Exception as e:
                        print(f"Could not message user {user_id}: {e}")
            
        c.close()
        conn.close()
        print("Update check complete.")

    async def on_ready(self):
        await self.tree.sync()
        print(f'Logged in as {self.user} (ID: {self.user.id})')

bot = MyBot()

@bot.tree.command(name="user", description="Link your discordid for notifications!")
async def set_user(interaction: discord.Interaction, username: str):
    conn = get_db_connection()
    c = conn.cursor()
    # Postgres specific "Upsert" syntax
    c.execute("""INSERT INTO users (discord_id, mal_username) VALUES (%s, %s) 
                 ON CONFLICT (discord_id) DO UPDATE SET mal_username = EXCLUDED.mal_username""", 
              (interaction.user.id, username))
    conn.commit()
    c.close()
    conn.close()
    await interaction.response.send_message(f"✅ Linked! MAL username set to **{username}**.")

@bot.tree.command(name="help", description="list of commands")
async def help_command(interaction: discord.Interaction):
    helpT = """
    **Available Commands:**
    - `/user <userid>`: Link your discord username.
    - `/manga <title>`: Search for a manga.
    - `/track <title>`: Track a manga for updates (MALBOT will dm you when a new chapter drops!).
    - `/list`: Show your tracked manga.
    - `/export`: Export your tracked list as a PDF.
    """
    await interaction.response.send_message(helpT)

@bot.tree.command(name="export", description="Export your list as a PDF")
async def export_pdf(interaction: discord.Interaction):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT mal_username FROM users WHERE discord_id = %s", (interaction.user.id,))
    result = c.fetchone()
    
    if not result:
        await interaction.response.send_message("Please set your username first using `/user`!")
        c.close()
        conn.close()
        return
        
    mal_user = result[0]
    c.execute("SELECT manga_title, last_chapter FROM tracking WHERE user_id = %s", (interaction.user.id,))
    rows = c.fetchall()
    c.close()
    conn.close()

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

    file_name = f"{interaction.user.id}_list.pdf"
    pdf.output(file_name)
    await interaction.response.send_message("Here is your exported list!", file=discord.File(file_name))
    os.remove(file_name)

@bot.tree.command(name="manga", description="Search for a manga")
async def manga(interaction: discord.Interaction, title: str):
    url = f'https://api.myanimelist.net/v2/manga?q={title}&limit=1&fields=id,title,main_picture,synopsis'
    headers = {'X-MAL-CLIENT-ID': MAL_ID}
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        data = response.json()
        if data.get('data'):
            node = data['data'][0]['node']
            embed = discord.Embed(title=node['title'], url=f"https://myanimelist.net/manga/{node['id']}", color=discord.Color.blue())
            if 'main_picture' in node:
                embed.set_thumbnail(url=node['main_picture']['large'])
            embed.add_field(name="Synopsis", value=node.get('synopsis', 'N/A')[:300] + "...", inline=False)
            await interaction.response.send_message(embed=embed)

@bot.tree.command(name="track", description="Track manga for updates")
async def track(interaction: discord.Interaction, title: str):
    url = f'https://api.myanimelist.net/v2/manga?q={title}&limit=1&fields=num_chapters'
    headers = {'X-MAL-CLIENT-ID': MAL_ID}
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200 and response.json().get('data'):
        node = response.json()['data'][0]['node']
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("INSERT INTO tracking (user_id, manga_id, manga_title, last_chapter) VALUES (%s, %s, %s, %s)",
                  (interaction.user.id, node['id'], node['title'], node.get('num_chapters', 0)))
        conn.commit()
        c.close()
        conn.close()
        await interaction.response.send_message(f"✅ Now tracking **{node['title']}** in the cloud!")
    else:
        await interaction.response.send_message("Manga not found or API error.")

@bot.tree.command(name="list", description="Show tracked manga")
async def list_tracked(interaction: discord.Interaction):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT manga_title FROM tracking WHERE user_id = %s", (interaction.user.id,))
    rows = c.fetchall()
    c.close()
    conn.close()

    if not rows:
        await interaction.response.send_message("Tracking list is empty.")
        return

    manga_list = "\n".join([f"• {r[0]}" for r in rows])
    await interaction.response.send_message(f"**Tracking List:**\n{manga_list}")

bot.run(TOKEN)