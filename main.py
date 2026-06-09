import os
import json
import secrets
import threading
import logging
import asyncio
from datetime import datetime, timedelta
import psycopg2
from psycopg2.extras import RealDictCursor

from flask import Flask, request, jsonify

API_SECRET = os.environ.get("API_SECRET", "CHANGE_ME")
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DATABASE_URL = os.environ.get("DATABASE_URL")
PORT = int(os.environ.get("PORT", 10000))
DATA_FILE = "data.json"
MAX_ACCOUNTS = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("hatch_tracker")

data_lock = threading.Lock()

def get_db():
    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=RealDictCursor
    )

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

try:
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT discord_id, roblox_username
        FROM linked_accounts
        """
    )

    rows = cur.fetchall()

    for row in rows:
        discord_id = row["discord_id"]
        username = row["roblox_username"]

        accounts = data["verified"].get(
            discord_id,
            []
        )

        accounts.append(username)

        data["verified"][discord_id] = accounts
        data["roblox_to_discord"][
            username.lower()
        ] = discord_id

    conn.close()

except Exception:
    log.exception(
        "Failed loading linked accounts"
    )


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

    pet_rarity = req.get("petRarity", "")
    color = req.get("color", "")
    thumbnail = req.get("thumbnail")
    world = req.get("world", "Unknown")
    serial = req.get("serial")
    total_count = req.get("totalCount")

    discord_id = data["roblox_to_discord"].get(username.lower())

    log.info(f"[HATCH] {username} hatched {rarity} {pet} from {egg}")
    log.info(f"[LOOKUP] {username} -> {discord_id}")

    if discord_id:

        try:
            conn = get_db()
            cur = conn.cursor()

            cur.execute(
                """
                INSERT INTO hatch_history
                (
                    discord_id,
                    roblox_username,
                    pet,
                    rarity,
                    pet_rarity,
                    egg,
                    world,
                    serial,
                    total_count
                )
                VALUES
                (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s
                )
                """,
                (
                    discord_id,
                    username,
                    pet,
                    rarity,
                    pet_rarity,
                    egg,
                    world,
                    serial,
                    total_count
                )
            )

            conn.commit()
            conn.close()

        except Exception:
            log.exception(
                "Failed saving hatch history"
            )

        with data_lock:
            data["hatch_queue"].append({
                "discord_id": discord_id,
                "username": username,
                "pet": pet,
                "rarity": rarity,
                "pet_rarity": pet_rarity,
                "color": color,
                "thumbnail": thumbnail,
                "egg": egg,
                "world": world,
                "serial": serial,
                "total_count": total_count
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

            try:
                conn = get_db()
                cur = conn.cursor()

                cur.execute(
                    """
                    INSERT INTO linked_accounts
                    (
                        discord_id,
                        roblox_username
                    )
                    VALUES (%s, %s)
                    ON CONFLICT
                    (
                        roblox_username
                    )
                    DO NOTHING
                    """,
                    (
                        discord_id,
                        username
                    )
                )

                conn.commit()
                conn.close()

            except Exception:
                log.exception(
                    "Failed saving linked account"
                )

            data["pending_codes"].pop(username, None)

            try:
                conn = get_db()
                cur = conn.cursor()

                cur.execute(
                    """
                    DELETE FROM pending_codes
                    WHERE roblox_username = %s
                    """,
                    (username,)
                )

                conn.commit()
                conn.close()

            except Exception:
                log.exception(
                    "Failed deleting pending code"
                )

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

        try:
            conn = get_db()
            cur = conn.cursor()

            cur.execute(
                """
                INSERT INTO pending_codes
                (
                    roblox_username,
                    code,
                    discord_id
                )
                VALUES (%s, %s, %s)
                ON CONFLICT
                (
                    roblox_username
                )
                DO UPDATE SET
                    code = EXCLUDED.code,
                    discord_id = EXCLUDED.discord_id
                """,
                (
                    username,
                    code,
                    discord_id
                )
            )

            conn.commit()
            conn.close()

        except Exception:
            log.exception(
                "Failed saving pending code"
            )

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


                        # Parse color from hex string like "#ff0000", fallback to gold/cyan
                        embed_color = 0xFFD700
                        if hatch.get("color"):
                            try:
                                embed_color = int(hatch["color"].lstrip("#"), 16)
                            except ValueError:
                                if hatch.get("pet_rarity") == "Nova":
                                    embed_color = 0x00FFFF

                        embed = discord.Embed(
                            title=f"{hatch['username']} has hatched a {hatch.get('pet_rarity', 'rare')} pet!",
                            color=embed_color
                        )

                        embed.add_field(name="Pet", value=hatch["pet"], inline=True)
                        embed.add_field(name="Rarity", value=f"1 in {hatch['rarity']}", inline=True)
                        embed.add_field(name="Egg", value=hatch["egg"] or "Unknown", inline=True)
                        embed.add_field(name="World", value=hatch.get("world", "Unknown"), inline=True)

                        if hatch.get("serial"):
                            embed.add_field(name="Serial #", value=f"{hatch['serial']:,}", inline=True)

                        if hatch.get("total_count"):
                            embed.add_field(name="Total in Existence", value=f"{hatch['total_count']:,}", inline=True)

                        if hatch.get("thumbnail"):
                            embed.set_thumbnail(url=hatch["thumbnail"])

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
        name="hatchstats",
        description="View your hatch statistics"
    )
    async def hatchstats(
        interaction: discord.Interaction
    ):

        discord_id = str(interaction.user.id)

        try:
            conn = get_db()
            cur = conn.cursor()

            cur.execute(
                """
                SELECT *
                FROM hatch_history
                WHERE discord_id = %s
                """,
                (discord_id,)
            )

            rows = cur.fetchall()
            conn.close()

        except Exception:
            await interaction.response.send_message(
                "Database error.",
                ephemeral=True
            )
            return

        if not rows:
            await interaction.response.send_message(
                "No hatch history found.",
                ephemeral=True
            )
            return

        total = len(rows)

        secrets = sum(
            1 for r in rows
            if r["pet_rarity"] == "Secret"
        )

        novas = sum(
            1 for r in rows
            if r["pet_rarity"] == "Nova"
        )

        rarest = max(
            rows,
            key=lambda r: float(
            str(r["rarity"]).replace(",", "")
        )
        )

        embed = discord.Embed(
            title="📊 Hatch Statistics",
            color=0x5865F2
        )

        embed.add_field(
            name="Total Rare Hatches",
            value=f"{total:,}"
        )

        embed.add_field(
            name="Secrets",
            value=f"{secrets:,}"
        )

        embed.add_field(
            name="Novas",
            value=f"{novas:,}"
        )

        embed.add_field(
            name="Rarest Hatch",
            value=rarest["pet"],
            inline=False
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )

    @bot.tree.command(
        name="rarest",
        description="View your rarest hatches"
    )
    async def rarest(
        interaction: discord.Interaction
    ):

        discord_id = str(interaction.user.id)

        try:
            conn = get_db()
            cur = conn.cursor()

            cur.execute(
                """
                SELECT *
                FROM hatch_history
                WHERE discord_id = %s
                ORDER BY
                CAST(
                    REPLACE(rarity, ',', '')
                    AS DOUBLE PRECISION
                ) DESC
                LIMIT 10
                """,
                (discord_id,)
            )

            rows = cur.fetchall()
            conn.close()

        except Exception:
            await interaction.response.send_message(
                "Database error.",
                ephemeral=True
            )
            return

        if not rows:
            await interaction.response.send_message(
                "No hatch history found.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title="🏆 Rarest Hatches",
            color=0xF1C40F
        )

        for i, row in enumerate(rows, start=1):
            embed.add_field(
                name=f"#{i} {row['pet']}",
                value=f"1 in {row['rarity']}",
                inline=False
            )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )


    @bot.tree.command(
        name="hatches",
        description="View your recent hatches"
    )
    async def hatches(
        interaction: discord.Interaction
    ):

        discord_id = str(interaction.user.id)

        try:
            conn = get_db()
            cur = conn.cursor()

            cur.execute(
                """
                SELECT *
                FROM hatch_history
                WHERE discord_id = %s
                ORDER BY hatched_at DESC
                LIMIT 10
                """,
                (discord_id,)
            )

            rows = cur.fetchall()
            conn.close()

        except Exception:
            await interaction.response.send_message(
                "Database error.",
                ephemeral=True
            )
            return

        if not rows:
            await interaction.response.send_message(
                "No hatch history found.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title="📜 Recent Hatches",
            color=0x5865F2
        )

        for row in rows:

            unix_time = int(
                row["hatched_at"].timestamp()
            )

            embed.add_field(
                name=row["pet"],
                value=(
                    f"{row['pet_rarity']} • "
                    f"1 in {row['rarity']}\n"
                    f"<t:{unix_time}:R>"
                ),
                inline=False
            )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )

    @bot.tree.command(
        name="flex",
        description="Show off your best hatch"
    )
    async def flex(
        interaction: discord.Interaction
    ):

        discord_id = str(interaction.user.id)

        try:
            conn = get_db()
            cur = conn.cursor()

            cur.execute(
                """
                SELECT *
                FROM hatch_history
                WHERE discord_id = %s
                """,
                (discord_id,)
            )

            rows = cur.fetchall()
            conn.close()

        except Exception:
            await interaction.response.send_message(
                "Database error.",
                ephemeral=True
            )
            return

        if not rows:
            await interaction.response.send_message(
                "No hatch history found.",
                ephemeral=True
            )
            return

        best = max(
            rows,
            key=lambda r: float(
                str(r["rarity"]).replace(",", "")
            )
        )

        embed = discord.Embed(
            title="💎 FLEX CARD",
            color=0xFFD700
        )

        embed.add_field(
            name="Best Hatch",
            value=best["pet"],
            inline=False
        )

        embed.add_field(
            name="Rarity",
            value=f"1 in {best['rarity']}",
            inline=True
        )

        embed.add_field(
            name="Type",
            value=best["pet_rarity"],
            inline=True
        )

        if best["serial"]:
            embed.add_field(
                name="Serial",
                value=f"#{best['serial']:,}",
                inline=True
            )

        embed.add_field(
            name="Total Rare Hatches",
            value=f"{len(rows):,}",
            inline=False
        )

        await interaction.response.send_message(
            embed=embed
        )

    @bot.tree.command(
        name="compare",
        description="Compare hatch stats"
    )
    @app_commands.describe(
        user="User to compare against"
    )
    async def compare(
        interaction: discord.Interaction,
        user: discord.Member
    ):

        try:

            conn = get_db()
            cur = conn.cursor()

            cur.execute(
                """
                SELECT *
                FROM hatch_history
                WHERE discord_id = %s
                """,
                (str(interaction.user.id),)
            )

            me = cur.fetchall()

            cur.execute(
                """
                SELECT *
                FROM hatch_history
                WHERE discord_id = %s
                """,
                (str(user.id),)
            )

            them = cur.fetchall()

            conn.close()

        except Exception:
            await interaction.response.send_message(
                "Database error.",
                ephemeral=True
            )
            return

        if not me or not them:

            await interaction.response.send_message(
                "One of the users has no hatch history.",
                ephemeral=True
            )

            return

        my_secrets = sum(
            1 for r in me
            if r["pet_rarity"] == "Secret"
        )

        their_secrets = sum(
            1 for r in them
            if r["pet_rarity"] == "Secret"
        )

        my_novas = sum(
            1 for r in me
            if r["pet_rarity"] == "Nova"
        )

        their_novas = sum(
            1 for r in them
            if r["pet_rarity"] == "Nova"
        )

        embed = discord.Embed(
            title="⚔️ Hatch Comparison",
            color=0x5865F2
        )

        embed.add_field(
            name="Total Rare Hatches",
            value=f"{len(me)} vs {len(them)}",
            inline=False
        )

        embed.add_field(
            name="Secrets",
            value=f"{my_secrets} vs {their_secrets}",
            inline=False
        )

        embed.add_field(
            name="Novas",
            value=f"{my_novas} vs {their_novas}",
            inline=False
        )

        embed.set_footer(
            text=f"{interaction.user.display_name} vs {user.display_name}"
        )

        await interaction.response.send_message(
            embed=embed
        )




    @bot.tree.command(
        name="profile",
        description="View your Bubble Gum profile"
    )
    async def profile(
        interaction: discord.Interaction
    ):

        discord_id = str(interaction.user.id)

        with data_lock:
            linked = data["verified"].get(
                discord_id,
                []
            )

        try:
            conn = get_db()
            cur = conn.cursor()

            cur.execute(
                """
                SELECT *
                FROM hatch_history
                WHERE discord_id = %s
                """,
                (discord_id,)
            )

            rows = cur.fetchall()

            conn.close()

        except Exception:
            await interaction.response.send_message(
                "Database error.",
                ephemeral=True
            )
            return

        total = len(rows)

        secrets = sum(
            1 for r in rows
            if r["pet_rarity"] == "Secret"
        )

        novas = sum(
            1 for r in rows
            if r["pet_rarity"] == "Nova"
        )

        rarest = None

        if rows:
            rarest = max(
                rows,
                key=lambda r: float(
                    str(r["rarity"]).replace(",", "")
                )
            )

        embed = discord.Embed(
            title=f"👤 {interaction.user.display_name}",
            color=0x5865F2
        )

        embed.add_field(
            name="Linked Accounts",
            value=str(len(linked)),
            inline=True
        )

        embed.add_field(
            name="Rare Hatches",
            value=f"{total:,}",
            inline=True
        )

        embed.add_field(
            name="Secrets",
            value=f"{secrets:,}",
            inline=True
        )

        embed.add_field(
            name="Novas",
            value=f"{novas:,}",
            inline=True
        )

        if rarest:
            embed.add_field(
                name="Best Hatch",
                value=rarest["pet"],
                inline=False
            )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )

    @bot.tree.command(
        name="leaderboard",
        description="Top hatchers"
    )
    async def leaderboard(
        interaction: discord.Interaction
    ):

        try:

            conn = get_db()
            cur = conn.cursor()

            cur.execute(
                """
                SELECT
                    discord_id,
                    COUNT(*) AS total
                FROM hatch_history
                GROUP BY discord_id
                ORDER BY total DESC
                LIMIT 10
                """
            )

            rows = cur.fetchall()

            conn.close()

        except Exception:

            await interaction.response.send_message(
                "Database error.",
                ephemeral=True
            )

            return

        embed = discord.Embed(
            title="🏆 Hatch Leaderboard",
            color=0xF1C40F
        )

        for i, row in enumerate(rows, start=1):

            try:
                user = await bot.fetch_user(
                    int(row["discord_id"])
                )

                name = user.name

            except Exception:
                name = row["discord_id"]

            embed.add_field(
                name=f"#{i} {name}",
                value=f"{row['total']:,} hatches",
                inline=False
            )

        await interaction.response.send_message(
            embed=embed
        )

    @bot.tree.command(
        name="serial",
        description="Search a serial number"
    )
    @app_commands.describe(
        number="Serial number"
    )
    async def serial(
        interaction: discord.Interaction,
        number: int
    ):

        try:

            conn = get_db()
            cur = conn.cursor()

            cur.execute(
                """
                SELECT *
                FROM hatch_history
                WHERE serial = %s
                LIMIT 1
                """,
                (number,)
            )

            row = cur.fetchone()

            conn.close()

        except Exception:

            await interaction.response.send_message(
                "Database error.",
                ephemeral=True
            )

            return

        if not row:

            await interaction.response.send_message(
                "Serial not found.",
                ephemeral=True
            )

            return

        embed = discord.Embed(
            title=f"🔎 Serial #{number}",
            color=0x5865F2
        )

        embed.add_field(
            name="Pet",
            value=row["pet"],
            inline=True
        )

        embed.add_field(
            name="Type",
            value=row["pet_rarity"],
            inline=True
        )

        embed.add_field(
            name="Rarity",
            value=f"1 in {row['rarity']}",
            inline=True
        )

        embed.add_field(
            name="Owner",
            value=row["roblox_username"],
            inline=False
        )

        await interaction.response.send_message(
            embed=embed
        )



    @bot.tree.command(
        name="firsthatch",
        description="View your first recorded hatch"
    )
    async def firsthatch(
        interaction: discord.Interaction
    ):

        try:

            conn = get_db()
            cur = conn.cursor()

            cur.execute(
                """
                SELECT *
                FROM hatch_history
                WHERE discord_id = %s
                ORDER BY hatched_at ASC
                LIMIT 1
                """,
                (str(interaction.user.id),)
            )

            row = cur.fetchone()
            conn.close()

        except Exception:

            await interaction.response.send_message(
                "Database error.",
                ephemeral=True
            )
            return

        if not row:
            await interaction.response.send_message(
                "No hatch history found.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title="🥚 First Hatch",
            color=0x57F287
        )

        embed.add_field(
            name="Pet",
            value=row["pet"],
            inline=False
        )

        embed.add_field(
            name="Rarity",
            value=f"1 in {row['rarity']}",
            inline=True
        )

        embed.add_field(
            name="Type",
            value=row["pet_rarity"],
            inline=True
        )

        await interaction.response.send_message(
            embed=embed
        )

    @bot.tree.command(
        name="whohatched",
        description="Find who hatched a pet"
    )
    @app_commands.describe(
        pet="Pet name"
    )
    async def whohatched(
        interaction: discord.Interaction,
        pet: str
    ):

        try:

            conn = get_db()
            cur = conn.cursor()

            cur.execute(
                """
                SELECT *
                FROM hatch_history
                WHERE LOWER(pet) = LOWER(%s)
                ORDER BY hatched_at ASC
                LIMIT 10
                """,
                (pet,)
            )

            rows = cur.fetchall()
            conn.close()

        except Exception:

            await interaction.response.send_message(
                "Database error.",
                ephemeral=True
            )
            return

        if not rows:
            await interaction.response.send_message(
                "No hatches found.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"🔎 {pet}",
            color=0x5865F2
        )

        for row in rows[:10]:

            embed.add_field(
                name=row["roblox_username"],
                value=f"1 in {row['rarity']}",
                inline=False
            )

        await interaction.response.send_message(
            embed=embed
        )

    @bot.tree.command(
        name="streak",
        description="View your hatch streak"
    )
    async def streak(
        interaction: discord.Interaction
    ):

        try:

            conn = get_db()
            cur = conn.cursor()

            cur.execute(
                """
                SELECT hatched_at
                FROM hatch_history
                WHERE discord_id = %s
                ORDER BY hatched_at DESC
                """,
                (str(interaction.user.id),)
            )

            rows = cur.fetchall()
            conn.close()

        except Exception:

            await interaction.response.send_message(
                "Database error.",
                ephemeral=True
            )
            return

        if not rows:
            await interaction.response.send_message(
                "No hatch history found.",
                ephemeral=True
            )
            return

        days = sorted(
            {
                r["hatched_at"].date()
                for r in rows
            },
            reverse=True
        )

        streak_count = 0
        current = days[0]

        for day in days:

            if day == current:
                streak_count += 1
                current -= timedelta(days=1)
            else:
                break

        embed = discord.Embed(
            title="🔥 Hatch Streak",
            color=0xE67E22
        )

        embed.add_field(
            name="Current Streak",
            value=f"{streak_count} day(s)",
            inline=False
        )

        await interaction.response.send_message(
            embed=embed
        )


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

                try:
                    conn = get_db()
                    cur = conn.cursor()

                    cur.execute(
                        """
                        DELETE FROM linked_accounts
                        WHERE roblox_username = %s
                        """,
                        (username,)
                    )

                    conn.commit()
                    conn.close()

                except Exception:
                    log.exception(
                        "Failed unlinking account"
                    )

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
