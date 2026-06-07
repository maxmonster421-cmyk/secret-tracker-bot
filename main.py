"""
Secret Pet Tracker Bot
Discord bot + Flask API for Roblox pet hatch tracking and account verification.
Deploy on Render. The Flask API handles requests from the Roblox game,
and the Discord bot handles slash commands and DM notifications.
"""

import os
import sqlite3
import random
import string
import threading
import asyncio
import logging
from datetime import datetime, timedelta

import discord
from discord import app_commands
from flask import Flask, request, jsonify

# ─── Configuration ───────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
API_SECRET = os.environ.get("API_SECRET", "change_me_to_a_real_secret")
DATABASE_PATH = os.environ.get("DATABASE_PATH", "tracker.db")
MAX_ACCOUNTS = 5
CODE_EXPIRY_MINUTES = 10
PORT = int(os.environ.get("PORT", 10000))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("tracker")


# ─── Database ─────────────────────────────────────────────────────────────────
db_lock = threading.Lock()


def get_db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS pending_verifications (
                roblox_username TEXT PRIMARY KEY,
                code TEXT NOT NULL,
                discord_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS verified_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id INTEGER NOT NULL,
                roblox_username TEXT NOT NULL,
                verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(discord_id, roblox_username)
            )
        """)
        conn.commit()
        conn.close()
    log.info("Database initialized")


def generate_code(length=6):
    chars = string.ascii_uppercase + string.digits
    return "".join(random.choices(chars, k=length))


def cleanup_expired_codes():
    with db_lock:
        conn = get_db()
        conn.execute(
            "DELETE FROM pending_verifications WHERE created_at < ?",
            (datetime.utcnow() - timedelta(minutes=CODE_EXPIRY_MINUTES).isoformat(),),
        )
        conn.commit()
        conn.close()


# ─── Discord Bot ──────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


@client.event
async def on_ready():
    try:
        synced = await tree.sync()
        log.info(f"Synced {len(synced)} slash commands")
    except Exception as e:
        log.error(f"Failed to sync commands: {e}")
    log.info(f"Bot online as {client.user}")


# ── /verify ──────────────────────────────────────────────────────────────────
@tree.command(name="verify", description="Start verifying your Roblox account")
@app_commands.describe(username="Your exact Roblox username")
async def verify_cmd(interaction: discord.Interaction, username: str):
    discord_id = interaction.user.id
    username = username.strip()

    if not username:
        await interaction.response.send_message(
            "❌ Please provide a valid username.", ephemeral=True
        )
        return

    with db_lock:
        conn = get_db()
        c = conn.cursor()

        # Already verified?
        c.execute(
            "SELECT 1 FROM verified_accounts WHERE discord_id = ? AND roblox_username = ?",
            (discord_id, username),
        )
        if c.fetchone():
            conn.close()
            await interaction.response.send_message(
                f"✅ **{username}** is already verified on your account!",
                ephemeral=True,
            )
            return

        # At max accounts?
        c.execute(
            "SELECT COUNT(*) as cnt FROM verified_accounts WHERE discord_id = ?",
            (discord_id,),
        )
        count = c.fetchone()["cnt"]
        if count >= MAX_ACCOUNTS:
            conn.close()
            await interaction.response.send_message(
                f"❌ You already have **{MAX_ACCOUNTS}** verified accounts (the maximum). "
                f"Use `/unverify` to remove one first.",
                ephemeral=True,
            )
            return

        # Generate code and store
        code = generate_code()
        c.execute(
            "INSERT OR REPLACE INTO pending_verifications (roblox_username, code, discord_id) VALUES (?, ?, ?)",
            (username, code, discord_id),
        )
        conn.commit()
        conn.close()

    embed = discord.Embed(title="🔐 Verify Your Roblox Account", color=0x2ECC71)
    embed.add_field(
        name="Step 1",
        value=f"Join the Roblox game on account **{username}**",
        inline=False,
    )
    embed.add_field(
        name="Step 2",
        value="A verification code will appear as a notification in-game",
        inline=False,
    )
    embed.add_field(
        name="Step 3",
        value="Verification is **automatic** — just join and you're done!",
        inline=False,
    )
    embed.set_footer(text=f"Code expires in {CODE_EXPIRY_MINUTES} minutes")

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── /confirm (manual backup) ─────────────────────────────────────────────────
@tree.command(name="confirm", description="Manually confirm a verification code (backup)")
@app_commands.describe(code="The 6-character code shown in-game")
async def confirm_cmd(interaction: discord.Interaction, code: str):
    discord_id = interaction.user.id
    code = code.strip().upper()

    with db_lock:
        conn = get_db()
        c = conn.cursor()

        c.execute(
            "SELECT * FROM pending_verifications WHERE discord_id = ? AND code = ?",
            (discord_id, code),
        )
        row = c.fetchone()

        if not row:
            conn.close()
            await interaction.response.send_message(
                "❌ Invalid code! Make sure you entered it correctly, or use `/verify` to start over.",
                ephemeral=True,
            )
            return

        roblox_username = row["roblox_username"]
        created_at = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")

        # Check expiry
        if datetime.utcnow() - created_at > timedelta(minutes=CODE_EXPIRY_MINUTES):
            c.execute(
                "DELETE FROM pending_verifications WHERE roblox_username = ?",
                (roblox_username,),
            )
            conn.commit()
            conn.close()
            await interaction.response.send_message(
                "❌ That code has expired! Use `/verify` to generate a new one.",
                ephemeral=True,
            )
            return

        # Check account limit
        c.execute(
            "SELECT COUNT(*) as cnt FROM verified_accounts WHERE discord_id = ?",
            (discord_id,),
        )
        count = c.fetchone()["cnt"]
        if count >= MAX_ACCOUNTS:
            c.execute(
                "DELETE FROM pending_verifications WHERE roblox_username = ?",
                (roblox_username,),
            )
            conn.commit()
            conn.close()
            await interaction.response.send_message(
                f"❌ You've reached the maximum of **{MAX_ACCOUNTS}** verified accounts!",
                ephemeral=True,
            )
            return

        # Verify!
        c.execute(
            "INSERT INTO verified_accounts (discord_id, roblox_username) VALUES (?, ?)",
            (discord_id, roblox_username),
        )
        c.execute(
            "DELETE FROM pending_verifications WHERE roblox_username = ?",
            (roblox_username,),
        )
        conn.commit()
        conn.close()

    embed = discord.Embed(
        title="✅ Verified!",
        description=f"**{roblox_username}** is now linked to your Discord!",
        color=0x2ECC71,
    )
    embed.add_field(
        name="What's Next",
        value="You'll receive DM notifications when you hatch Secret or Nova pets!",
        inline=False,
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── /accounts ────────────────────────────────────────────────────────────────
@tree.command(name="accounts", description="View your verified Roblox accounts")
async def accounts_cmd(interaction: discord.Interaction):
    discord_id = interaction.user.id

    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "SELECT roblox_username, verified_at FROM verified_accounts WHERE discord_id = ? ORDER BY verified_at",
            (discord_id,),
        )
        rows = c.fetchall()
        conn.close()

    if not rows:
        await interaction.response.send_message(
            "You don't have any verified accounts yet. Use `/verify` to get started!",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title="📋 Your Verified Accounts",
        description=f"**{len(rows)}/{MAX_ACCOUNTS}** slots used",
        color=0x5865F2,
    )

    for i, row in enumerate(rows, 1):
        embed.add_field(
            name=f"#{i} — {row['roblox_username']}",
            value=f"Verified: {row['verified_at']}",
            inline=False,
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── /unverify ─────────────────────────────────────────────────────────────────
@tree.command(name="unverify", description="Remove a verified Roblox account")
@app_commands.describe(username="The Roblox username to unlink")
async def unverify_cmd(interaction: discord.Interaction, username: str):
    discord_id = interaction.user.id
    username = username.strip()

    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "DELETE FROM verified_accounts WHERE discord_id = ? AND roblox_username = ?",
            (discord_id, username),
        )
        deleted = c.rowcount
        conn.commit()
        conn.close()

    if deleted:
        await interaction.response.send_message(
            f"✅ Removed **{username}** from your verified accounts.", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"❌ **{username}** isn't linked to your Discord.", ephemeral=True
        )


# ─── DM Helper ───────────────────────────────────────────────────────────────
def send_dm(discord_id, content):
    """Thread-safe: schedule a DM from the Discord bot's event loop."""

    async def _send():
        try:
            user = client.get_user(discord_id)
            if user is None:
                user = await client.fetch_user(discord_id)
            if user:
                await user.send(content)
                log.info(f"DM sent to user {discord_id}")
            else:
                log.warning(f"User {discord_id} not found for DM")
        except discord.Forbidden:
            log.warning(f"Cannot DM user {discord_id} (DMs disabled or blocked)")
        except Exception as e:
            log.error(f"DM error for {discord_id}: {e}")

    if client.loop and client.is_ready():
        asyncio.run_coroutine_threadsafe(_send(), client.loop)
    else:
        log.warning(f"Bot not ready, skipping DM to {discord_id}")


# ─── Flask API (called by the Roblox game) ───────────────────────────────────
flask_app = Flask(__name__)


def check_auth(req):
    auth = req.headers.get("Authorization", "")
    return auth == f"Bearer {API_SECRET}"


@flask_app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "bot": client.is_ready()})


@flask_app.route("/hatch", methods=["POST"])
def hatch():
    """Called by Roblox when a Secret/Nova pet is hatched."""
    if not check_auth(request):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "invalid json"}), 400

    username = data.get("username", "")
    pet = data.get("pet", "")
    rarity = data.get("rarity", "")
    egg = data.get("egg", "")

    if not all([username, pet, rarity, egg]):
        return jsonify({"error": "missing fields"}), 400

    # Find verified Discord users for this Roblox account
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "SELECT discord_id FROM verified_accounts WHERE roblox_username = ?",
            (username,),
        )
        rows = c.fetchall()
        conn.close()

    notified = 0
    for row in rows:
        dm_msg = (
            f"🎉 **Secret Pet Hatched!**\n\n"
            f"**Player:** {username}\n"
            f"**Pet:** {pet}\n"
            f"**Rarity:** {rarity}\n"
            f"**Egg:** {egg}\n\n"
            f"Congratulations on the incredible hatch!"
        )
        send_dm(row["discord_id"], dm_msg)
        notified += 1

    log.info(
        f"Hatch: {username} hatched {rarity} {pet} from {egg} | DM'd {notified} user(s)"
    )
    return jsonify({"success": True, "notified": notified})


@flask_app.route("/getcode", methods=["GET"])
def getcode():
    """Called by Roblox when a player joins to get their verification code."""
    if not check_auth(request):
        return jsonify({"error": "unauthorized"}), 401

    username = request.args.get("user", "").strip()
    if not username:
        return jsonify({"error": "missing user param"}), 400

    cleanup_expired_codes()

    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "SELECT code FROM pending_verifications WHERE roblox_username = ?",
            (username,),
        )
        row = c.fetchone()
        conn.close()

    if row:
        return jsonify({"code": row["code"]})
    return jsonify({"code": None})


@flask_app.route("/confirmverify", methods=["POST"])
def confirmverify():
    """Called by Roblox to auto-confirm verification when a player joins."""
    if not check_auth(request):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "invalid json"}), 400

    username = data.get("username", "").strip()
    code = data.get("code", "").strip().upper()

    if not all([username, code]):
        return jsonify({"error": "missing fields"}), 400

    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "SELECT * FROM pending_verifications WHERE roblox_username = ? AND code = ?",
            (username, code),
        )
        row = c.fetchone()

        if not row:
            conn.close()
            return jsonify({"success": False, "error": "invalid code or username"})

        discord_id = row["discord_id"]
        created_at = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")

        # Check expiry
        if datetime.utcnow() - created_at > timedelta(minutes=CODE_EXPIRY_MINUTES):
            c.execute(
                "DELETE FROM pending_verifications WHERE roblox_username = ?",
                (username,),
            )
            conn.commit()
            conn.close()
            return jsonify({"success": False, "error": "code expired"})

        # Check account limit
        c.execute(
            "SELECT COUNT(*) as cnt FROM verified_accounts WHERE discord_id = ?",
            (discord_id,),
        )
        count = c.fetchone()["cnt"]
        if count >= MAX_ACCOUNTS:
            c.execute(
                "DELETE FROM pending_verifications WHERE roblox_username = ?",
                (username,),
            )
            conn.commit()
            conn.close()
            return jsonify({"success": False, "error": "max accounts reached"})

        # Already verified?
        c.execute(
            "SELECT 1 FROM verified_accounts WHERE discord_id = ? AND roblox_username = ?",
            (discord_id, username),
        )
        if c.fetchone():
            c.execute(
                "DELETE FROM pending_verifications WHERE roblox_username = ?",
                (username,),
            )
            conn.commit()
            conn.close()
            return jsonify({"success": False, "error": "already verified"})

        # Verify!
        c.execute(
            "INSERT INTO verified_accounts (discord_id, roblox_username) VALUES (?, ?)",
            (discord_id, username),
        )
        c.execute(
            "DELETE FROM pending_verifications WHERE roblox_username = ?",
            (username,),
        )
        conn.commit()
        conn.close()

    # DM the user about successful verification
    send_dm(
        discord_id,
        f"✅ Your Roblox account **{username}** has been verified! "
        f"You'll now receive hatch notifications here.",
    )

    log.info(f"Verified: {username} → Discord {discord_id}")
    return jsonify({"success": True})


# ─── Main ─────────────────────────────────────────────────────────────────────
def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    if not BOT_TOKEN:
        print("ERROR: DISCORD_BOT_TOKEN environment variable is required!")
        exit(1)

    init_db()

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    log.info(f"API server on port {PORT}")

    client.run(BOT_TOKEN)
