MAL-bot is a discord bot that uses MAL API to run commands for general MAL functionality and Manga tracking and exporting

Features:
Automated notification from the bot for manga that have been tracked using "/track" command
Secure Storage using SQLite as the DB to make discord IDs to their list trackings using "/list" command
Export manga in a pdf format using "/export"

Tech Stack:
Uses Python, SQLite, MyAnimeList v2

Setup:
Clone the repo via `git clone https://github.com/YourUsername/repo.git`
Install dependencies: `pip install -r requirements.txt`
Create a `.env` file with your `DISCORD_TOKEN` and `MAL_CLIENT_ID`
Run: `python main.py`   