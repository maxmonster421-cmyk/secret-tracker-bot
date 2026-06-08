import os
import json
import secrets
import threading
import logging
from flask import Flask, request, jsonify
import discord
from discord.ext import commands
from discord import app_commands

# ─── Configuration ───────────────────────────────────────────
API_SECRET = os.environ.get("API_SECRET", "Hatches101310")
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
MAX_VERIFIED_PER_PERSON = 5
PORT = int(os.environ.get("PORT", 10000))

# ─── In-memory storage ──────────────────────────────────────
pending_codes = {}       # username -> {code, expires}
verified_accounts = {}   # discord_user_id -> [roblox_usernames]
hatch_log = []           # list of hatch events

# ─── Flask App (MAIN process — this is what Render runs) ────
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("verification_api")

def check_auth(req):
    auth = req.headers.get("Authorization", "")
    return auth == f"Bearer {API_SECRET}"

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

@app.route("/hatch", methods=["POST"])
def hatch():
    if not check_auth(request):
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "invalid json"}), 400
    entry = {
        "username": data.get("username", "Unknown"),
        "pet": data.get("pet", "Unknown"),
        "rarity": data.get("rarity", "Unknown"),
        "egg": data.get("egg", "Unknown"),
    }
    hatch_log.append(entry)
    log.info(f"[HATCH] {entry['username']} hatched {entry['rarity']} {entry['pet']} from {entry['egg']}")
    
    # Try to DM verified users (non-blocking)
    try:
        notify_hatch(entry)
    except Exception as e:
        log.warning(f"[HATCH] DM notification failed: {e}")
    
    return jsonify({"success": True})

@app.route("/getcode", methods=["GET"])
def getcode():
    if not check_auth(request):
        return jsonify({"error": "unauthorized"}), 401
    username = request.args.get("user", "")
    if not username:
        return jsonify({"error": "missing user param"}), 400
    
    if username in pending_codes:
        return jsonify({"code": pending_codes[username]["code"]})
    
    return jsonify({"code": None})

@app.route("/confirmverify", methods=["POST"])
def confirmverify():
    if not check_auth(request):
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "invalid json"}), 400
    username = data.get("username", "")
    code = data.get("code", "")
    log.info(f"[VERIFY] Confirmed: {username} with code {code}")
    # Remove the pending code after confirmation
    pending_codes.pop(username, None)
    return jsonify({"success": True})

@app.route("/generatecode", methods=["POST"])
def generatecode():
    """Used by the Discord bot slash command to generate a code for a Roblox player."""
    if not check_auth(request):
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "invalid json"}), 400
    username = data.get("username", "")
    discord_id = data.get("discord_id", "")
    if not username:
        return jsonify({"error": "missing username"}), 400
    
    # Check max verified accounts
    user_accounts = verified_accounts.get(discord_id, [])
    if len(user_accounts) >= MAX_VERIFIED_PER_PERSON:
        return jsonify({"error": f"Max {MAX_VERIFIED_PER_PERSON} accounts per person"}), 400
    
    code = str(secrets.randbelow(900000) + 100000)  # 6-digit code
    pending_codes[username] = {"code": code, "discord_id": discord_id}
    
    if discord_id and username not in user_accounts:
        user_accounts.append(username)
        verified_accounts[discord_id] = user_accounts
    
    log.info(f"[VERIFY] Generated code {code} for {username} (Discord: {discord_id})")
    return jsonify({"code": code, "username": username})

@app.route("/verified", methods=["GET"])
def get_verified():
    if not check_auth(request):
        return jsonify({"error": "unauthorized"}), 401
    discord_id = request.args.get("discord_id", "")
    accounts = verified_accounts.get(discord_id, [])
    return jsonify({"accounts": accounts})

# ─── Discord Bot (runs in background thread) ─────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

def notify_hatch(entry):
    """Send DM to verified users about a hatch event."""
    # This is called from the /hatch endpoint
    # We'll queue it for the bot to process
    pass

@bot.event
async def on_ready():
    log.info(f"[BOT] Logged in as {bot.user}")
    try:
        synced = await bot.tree.sync()
        log.info(f"[BOT] Synced {len(synced)} commands")
    except Exception as e:
        log.error(f"[BOT] Failed to sync commands: {e}")

@bot.tree.command(name="verify", description="Generate a verification code for your Roblox account")
@app_commands.describe(username="Your Roblox username")
async def verify_cmd(interaction: discord.Interaction, username: str):
    discord_id = str(interaction.user.id)
    user_accounts = verified_accounts.get(discord_id, [])
    
    if len(user_accounts) >= MAX_VERIFIED_PER_PERSON:
        await interaction.response.send_message(
            f"You already have {MAX_VERIFIED_PER_PERSON} verified accounts (the maximum).",
            ephemeral=True
        )
        return
    
    code = str(secrets.randbelow(900000) + 100000)
    pending_codes[username] = {"code": code, "discord_id": discord_id}
    
    if username not in user_accounts:
        user_accounts.append(username)
        verified_accounts[discord_id] = user_accounts
    
    await interaction.response.send_message(
        f"Your verification code is: **{code}**\nJoin the game and enter this code when prompted.",
        ephemeral=True
    )

@bot.tree.command(name="myaccounts", description="View your verified Roblox accounts")
async def myaccounts_cmd(interaction: discord.Interaction):
    discord_id = str(interaction.user.id)
    accounts = verified_accounts.get(discord_id, [])
    if accounts:
        account_list = "\n".join(f"• {a}" for a in accounts)
        await interaction.response.send_message(
            f"Your verified accounts ({len(accounts)}/{MAX_VERIFIED_PER_PERSON}):\n{account_list}",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "You have no verified accounts yet. Use /verify to get started!",
            ephemeral=True
        )

@bot.tree.command(name="unverify", description="Remove a verified Roblox account")
@app_commands.describe(username="The Roblox username to unverify")
async def unverify_cmd(interaction: discord.Interaction, username: str):
    discord_id = str(interaction.user.id)
    accounts = verified_accounts.get(discord_id, [])
    if username in accounts:
        accounts.remove(username)
        pending_codes.pop(username, None)
        await interaction.response.send_message(
            f"Unverified **{username}**.",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"**{username}** is not verified under your account.",
            ephemeral=True
        )

def run_bot():
    """Run the Discord bot in a separate thread. If it fails, the API stays up."""
    if not BOT_TOKEN:
        log.warning("[BOT] No DISCORD_BOT_TOKEN set. Bot will not start. API will still work.")
        return
    try:
        bot.run(BOT_TOKEN)
    except Exception as e:
        log.error(f"[BOT] Bot crashed: {e}. API server will continue running.")

# ─── Startup ─────────────────────────────────────────────────
if __name__ == "__main__":
    # Start the Discord bot in a background thread
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    
    # Start Flask as the MAIN process (this is what Render expects)
    log.info(f"[API] Starting Flask on 0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT)
