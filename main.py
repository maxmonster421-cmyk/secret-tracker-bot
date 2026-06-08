import os
import json
import secrets
import threading
import logging
import asyncio

from flask import Flask, request, jsonify

API_SECRET = os.environ.get("API_SECRET", "CHANGE_ME")
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", 10000))
DATA_FILE = "data.json"
MAX_ACCOUNTS = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("hatch_tracker")

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

data = load_data()

def save_data():
    try:
        with data_lock:
            with open(DATA_FILE, "w") as f:
                json.dump(data, f, indent=2)
    except Exception as e:
        log.error(f"Save failed: {e}")

app = Flask(__name__)

def check_auth():
    return request.headers.get("Authorization", "") == f"Bearer {API_SECRET}"

@app.route("/")
def root():
    return jsonify({"status": "ok"})

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

@app.route("/hatch", methods=["POST"])
def hatch():
    if not check_auth():
        return jsonify({"error": "unauthorized"}), 401

    req = request.get_json(silent=True)
    if not req:
        return jsonify({"error": "invalid json"}), 400

    username = req.get("username", "").strip()
    pet = req.get("pet", "")
    rarity = req.get("rarity", "")
    egg = req.get("egg", "")

    discord_id = data["roblox_to_discord"].get(username.lower())

    log.info(f"[HATCH] {username} hatched {rarity} {pet} from {egg}")
    log.info(f"[LOOKUP] {username} -> {discord_id}")

    if discord_id:
        with data_lock:
            data["hatch_queue"].append({
                "discord_id": discord_id,
                "username": username,
                "pet": pet,
                "rarity": rarity,
                "egg": egg
            })
        save_data()
        log.info(f"[QUEUE] Added hatch for {discord_id}")
    else:
        log.warning(f"[QUEUE] No Discord account linked for {username}")

    return jsonify({"success": True})

@app.route("/getcode")
def getcode():
    if not check_auth():
        return jsonify({"error": "unauthorized"}), 401

    username = request.args.get("user", "")
    with data_lock:
        entry = data["pending_codes"].get(username)

    return jsonify({"code": entry["code"] if entry else None})

@app.route("/confirmverify", methods=["POST"])
def confirmverify():
    if not check_auth():
        return jsonify({"error": "unauthorized"}), 401

    req = request.get_json(silent=True) or {}
    username = req.get("username", "")
    code = req.get("code", "")

    with data_lock:
        pending = data["pending_codes"].get(username)

        if pending and pending["code"] == code:
            discord_id = pending["discord_id"]

            accounts = data["verified"].get(discord_id, [])
            if username not in accounts:
                accounts.append(username)

            data["verified"][discord_id] = accounts
            data["roblox_to_discord"][username.lower()] = discord_id
            data["pending_codes"].pop(username, None)

    save_data()
    log.info(f"[VERIFY] Confirmed: {username}")
    return jsonify({"success": True})

@app.route("/generatecode", methods=["POST"])
def generatecode():
    if not check_auth():
        return jsonify({"error": "unauthorized"}), 401

    req = request.get_json(silent=True) or {}

    username = req.get("username", "")
    discord_id = req.get("discord_id", "")

    with data_lock:
        accounts = data["verified"].get(discord_id, [])

        if len(accounts) >= MAX_ACCOUNTS:
            return jsonify({"error": "max accounts"}), 400

        code = str(secrets.randbelow(900000) + 100000)

        data["pending_codes"][username] = {
            "code": code,
            "discord_id": discord_id
        }

    save_data()
    return jsonify({"code": code})

def run_bot():
    if not BOT_TOKEN:
        log.warning("No token configured")
        return

    import discord
    from discord.ext import commands
    from discord import app_commands

    intents = discord.Intents.default()

    bot = commands.Bot(command_prefix="!", intents=intents)

    async def process_hatch_queue():
        await bot.wait_until_ready()

        while not bot.is_closed():

            try:
                hatch = None

                with data_lock:
                    if data["hatch_queue"]:
                        hatch = data["hatch_queue"].pop(0)

                if hatch:
                    save_data()


                    discord_id = hatch["discord_id"]

                    try:
                        user = await bot.fetch_user(int(discord_id))


                        embed = discord.Embed(
                            title=f"{hatch['username']} has hatched a {hatch['rarity']} pet!",
                            color=0xFFD700 if hatch["rarity"] == "Secret" else 0x00FFFF
                        )

                        embed.add_field(name="Pet", value=hatch["pet"], inline=True)
                        embed.add_field(name="Rarity", value=hatch["rarity"], inline=True)
                        embed.add_field(name="Egg", value=hatch["egg"] or "Unknown", inline=True)
                        embed.add_field(name="Account", value=hatch["username"], inline=True)
                        embed.add_field(name="World", value="Unknown", inline=True)
                        embed.timestamp = discord.utils.utcnow()


                        await user.send(
                            content=(
                                f"🎉 Congratulations <@{discord_id}>!\n"
                                f"A rare hatch has been detected on **{hatch['username']}**."
                            ),
                            embed=embed
                        )


                        log.info(f"[DM] Sent hatch DM to {discord_id}")

                    except discord.Forbidden:
                        log.warning(f"[DM] DMs disabled for {discord_id}")
                    except Exception:
                        log.exception(f"[DM] Failed for {discord_id}")

            except Exception:
                log.exception("[QUEUE] Processing failed")

            await asyncio.sleep(5)

    @bot.event
    async def on_disconnect():
        log.warning("[BOT] DISCONNECTED")

    @bot.event
    async def on_resumed():
        log.info("[BOT] RESUMED")

    @bot.event
    async def on_ready():
        log.info(f"Logged in as {bot.user}")

        try:
            await bot.tree.sync()
        except Exception:
            log.exception("Slash command sync failed")

        if not hasattr(bot, "queue_task"):
            bot.queue_task = asyncio.create_task(process_hatch_queue())

    @bot.tree.command(name="verify")
    @app_commands.describe(username="Your Roblox username")
    async def verify(interaction: discord.Interaction, username: str):
        discord_id = str(interaction.user.id)

        with data_lock:
            accounts = data["verified"].get(discord_id, [])

            if len(accounts) >= MAX_ACCOUNTS:
                await interaction.response.send_message(
                    "Max linked accounts reached.",
                    ephemeral=True
                )
                return

            code = str(secrets.randbelow(900000) + 100000)

            data["pending_codes"][username] = {
                "code": code,
                "discord_id": discord_id
            }

        save_data()

        embed = discord.Embed(
            title="🔐 Roblox Account Verification",
            description=f"Use the code below in-game to link your Roblox account.",
            color=0x5865F2
        )
        embed.add_field(name="Verification Code", value=f"**{code}**", inline=False)
        embed.add_field(name="Linked Accounts", value=f"{len(accounts)}/{MAX_ACCOUNTS}", inline=True)
        embed.set_footer(text="Your account will be linked automatically after verification.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


    @bot.tree.command(
        name="accounts",
        description="View your linked Roblox accounts"
    )
    async def accounts(interaction: discord.Interaction):

        discord_id = str(interaction.user.id)

        with data_lock:
            linked = data["verified"].get(discord_id, [])

        if not linked:
            embed = discord.Embed(
                title="📋 Linked Roblox Accounts",
                description="No Roblox accounts are currently linked to your Discord account.",
                color=0xED4245
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(
            title="📋 Linked Roblox Accounts",
            description="\n".join(f"• {a}" for a in linked),
            color=0x57F287
        )
        embed.add_field(
            name="Account Usage",
            value=f"{len(linked)}/{MAX_ACCOUNTS} linked accounts",
            inline=False
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(
        name="unlink",
        description="Unlink a Roblox account"
    )
    @app_commands.describe(
        username="Roblox username to unlink"
    )
    async def unlink(
        interaction: discord.Interaction,
        username: str
    ):

        discord_id = str(interaction.user.id)

        removed = False

        with data_lock:

            accounts = data["verified"].get(
                discord_id,
                []
            )

            if username in accounts:

                accounts.remove(username)

                data["verified"][discord_id] = accounts

                data["roblox_to_discord"].pop(
                    username.lower(),
                    None
                )

                removed = True

        save_data()

        if removed:
            embed = discord.Embed(
                title="✅ Account Unlinked",
                description=f"**{username}** has been successfully removed from your linked accounts.",
                color=0x57F287
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            embed = discord.Embed(
                title="⚠️ Account Not Found",
                description="That Roblox account is not linked to your Discord account.",
                color=0xFAA61A
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)


    bot.run(BOT_TOKEN)

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
