import os
import json
import secrets
import threading
import logging
import time

from flask import Flask, request, jsonify

# ─── Configuration ───────────────────────────────────────────
API_SECRET = os.environ.get("API_SECRET", "Hatches101310")
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", 10000))
DATA_FILE = "data.json"
MAX_ACCOUNTS = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("hatch_tracker")

# ─── Thread-safe Data Storage ────────────────────────────────
data_lock = threading.Lock()

def load_data():
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        log.error(f"Load failed: {e}")
    return {
        "pending_codes": {},
        "verified": {},
        "roblox_to_discord": {},
        "hatch_queue": []
    }

def save_data():
    try:
        with data_lock:
            with open(DATA_FILE, "w") as f:
                json.dump(data, f, indent=2)
    except Exception as e:
        log.error(f"Save failed: {e}")

data = load_data()

# ─── Flask API ───────────────────────────────────────────────
app = Flask(__name__)

def check_auth():
    auth = request.headers.get("Authorization", "")
    return auth == f"Bearer {API_SECRET}"

@app.route("/", methods=["GET"])
def root():
    return jsonify({"status": "ok", "service": "hatch-tracker"})

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

@app.route("/hatch", methods=["POST"])
def hatch():
    if not check_auth():
        return jsonify({"error": "unauthorized"}), 401
    req = request.get_json(silent=True)
    if not req:
        return jsonify({"error": "invalid json"}), 400

    username = req.get("username", "")
    pet = req.get("pet", "")
    rarity = req.get("rarity", "")
    egg = req.get("egg", "")

    log.info(f"[HATCH] {username} hatched {rarity} {pet} from {egg}")

    with data_lock:
        discord_id = data["roblox_to_discord"].get(username.lower())
        if discord_id:
            data["hatch_queue"].append({
                "discord_id": discord_id,
                "username": username,
                "pet": pet,
                "rarity": rarity,
                "egg": egg
            })
    save_data()

    return jsonify({"success": True})

@app.route("/getcode", methods=["GET"])
def getcode():
    if not check_auth():
        return jsonify({"error": "unauthorized"}), 401
    username = request.args.get("user", "")
    if not username:
        return jsonify({"error": "missing user param"}), 400

    with data_lock:
        code_entry = data["pending_codes"].get(username)

    if code_entry:
        return jsonify({"code": code_entry["code"]})
    return jsonify({"code": None})

@app.route("/confirmverify", methods=["POST"])
def confirmverify():
    if not check_auth():
        return jsonify({"error": "unauthorized"}), 401
    req = request.get_json(silent=True)
    if not req:
        return jsonify({"error": "invalid json"}), 400

    username = req.get("username", "")
    code = req.get("code", "")

    with data_lock:
        pending = data["pending_codes"].get(username)
        if pending and pending["code"] == code:
            discord_id = pending.get("discord_id", "")
            if discord_id:
                accounts = data["verified"].get(discord_id, [])
                if username not in [a.lower() for a in accounts]:
                    accounts.append(username)
                    data["verified"][discord_id] = accounts
                data["roblox_to_discord"][username.lower()] = discord_id
            data["pending_codes"].pop(username, None)
    save_data()

    log.info(f"[VERIFY] Confirmed: {username}")
    return jsonify({"success": True, "verified": True})

@app.route("/generatecode", methods=["POST"])
def generatecode():
    if not check_auth():
        return jsonify({"error": "unauthorized"}), 401
    req = request.get_json(silent=True)
    if not req:
        return jsonify({"error": "invalid json"}), 400

    username = req.get("username", "")
    discord_id = req.get("discord_id", "")

    if not username:
        return jsonify({"error": "missing username"}), 400

    with data_lock:
        accounts = data["verified"].get(discord_id, [])
        if len(accounts) >= MAX_ACCOUNTS:
            return jsonify({"error": f"Max {MAX_ACCOUNTS} accounts"}), 400

        code = str(secrets.randbelow(900000) + 100000)
        data["pending_codes"][username] = {
            "code": code,
            "discord_id": discord_id
        }
    save_data()

    log.info(f"[VERIFY] Generated code {code} for {username}")
    return jsonify({"code": code, "username": username})

@app.route("/verified", methods=["GET"])
def get_verified():
    if not check_auth():
        return jsonify({"error": "unauthorized"}), 401
    discord_id = request.args.get("discord_id", "")
    with data_lock:
        accounts = data["verified"].get(discord_id, [])
    return jsonify({"accounts": accounts})

# ─── Discord Bot (background thread — cannot kill Flask) ─────
def run_bot():
    if not BOT_TOKEN:
        log.warning("[BOT] No DISCORD_BOT_TOKEN set. Bot not starting. API still works.")
        return

    try:
        import discord
        from discord.ext import commands
        from discord import app_commands
    except ImportError:
        log.error("[BOT] discord.py not installed. Bot not starting.")
        return

    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents)

    @bot.event
    async def on_ready():
        log.info(f"[BOT] Online as {bot.user}")
        try:
            synced = await bot.tree.sync()
            log.info(f"[BOT] Synced {len(synced)} slash commands")
        except Exception as e:
            log.error(f"[BOT] Sync failed: {e}")
        bot.loop.create_task(_process_hatch_queue())

    async def _process_hatch_queue():
        while True:
            try:
                with data_lock:
                    queue = list(data.get("hatch_queue", []))
                    if queue:
                        data["hatch_queue"] = []
                        save_data()

                for hatch in queue:
                    discord_id = hatch.get("discord_id", "")
                    if not discord_id:
                        continue
                    try:
                        user = await bot.fetch_user(int(discord_id))
                        if user:
                            embed = discord.Embed(
                                title=f"🎉 Your {hatch['rarity']} Hatch!",
                                color=0xFFD700 if hatch["rarity"] == "Secret" else 0x00FFFF,
                            )
                            embed.add_field(name="Pet", value=hatch["pet"], inline=True)
                            embed.add_field(name="Rarity", value=hatch["rarity"], inline=True)
                            embed.add_field(name="Egg", value=hatch["egg"], inline=True)
                            embed.set_footer(text="Hatch Tracker")
                            await user.send(embed=embed)
                            log.info(f"[DM] Sent hatch DM to {discord_id}")
                    except discord.Forbidden:
                        log.warning(f"[DM] Cannot DM user {discord_id} — DMs disabled")
                    except Exception as e:
                        log.error(f"[DM] Failed: {e}")
            except Exception as e:
                log.error(f"[HATCH QUEUE] Error: {e}")

            await discord.utils.sleep(5)

    @bot.tree.command(name="verify", description="Verify your Roblox account for hatch DMs")
    @app_commands.describe(username="Your exact Roblox username")
    async def verify_cmd(interaction: discord.Interaction, username: str):
        discord_id = str(interaction.user.id)
        username = username.strip()

        with data_lock:
            accounts = data["verified"].get(discord_id, [])

            if username.lower() in [a.lower() for a in accounts]:
                await interaction.response.send_message(
                    f"**{username}** is already verified to your account!",
                    ephemeral=True
                )
                return

            if len(accounts) >= MAX_ACCOUNTS:
                await interaction.response.send_message(
                    f"You have {MAX_ACCOUNTS} accounts (max). Use /unverify first.",
                    ephemeral=True
                )
                return

            code = str(secrets.randbelow(900000) + 100000)
            data["pending_codes"][username] = {
                "code": code,
                "discord_id": discord_id
            }
        save_data()

        await interaction.response.send_message(
            f"🔑 Your verification code is: **{code}**\n"
            f"Join the game — this code will appear automatically.\n"
            f"Once confirmed, you'll get DMs for Secret/Nova hatches on **{username}**.",
            ephemeral=True
        )
        log.info(f"[BOT] /verify: {interaction.user.name} -> {username}, code {code}")

    @bot.tree.command(name="unverify", description="Remove a verified Roblox account")
    @app_commands.describe(username="The Roblox username to unlink")
    async def unverify_cmd(interaction: discord.Interaction, username: str):
        discord_id = str(interaction.user.id)
        username = username.strip()

        with data_lock:
            accounts = data["verified"].get(discord_id, [])
            matched = None
            for a in accounts:
                if a.lower() == username.lower():
                    matched = a
                    break

            if matched:
                accounts.remove(matched)
                data["verified"][discord_id] = accounts
                data["roblox_to_discord"].pop(matched.lower(), None)
        save_data()

        if matched:
            await interaction.response.send_message(
                f"✅ Unverified **{matched}**. No more DMs for that account.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"**{username}** is not linked to your account.",
                ephemeral=True
            )

    @bot.tree.command(name="myaccounts", description="View your verified Roblox accounts")
    async def myaccounts_cmd(interaction: discord.Interaction):
        discord_id = str(interaction.user.id)
        with data_lock:
            accounts = data["verified"].get(discord_id, [])

        if accounts:
            account_list = "\n".join(f"• {a}" for a in accounts)
            await interaction.response.send_message(
                f"Your accounts ({len(accounts)}/{MAX_ACCOUNTS}):\n{account_list}",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "No verified accounts. Use `/verify <username>` to get started!",
                ephemeral=True
            )

    try:
        log.info("[BOT] Starting Discord bot...")
        bot.run(BOT_TOKEN)
    except Exception as e:
        log.error(f"[BOT] Bot crashed: {e}. API server continues running.")


# ─── Startup ─────────────────────────────────────────────────
if __name__ == "__main__":
    # Start Discord bot in a daemon thread (if it dies, Flask stays alive)
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    # Flask is the MAIN process — this is what Render expects
    log.info(f"[API] Starting Flask on 0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
