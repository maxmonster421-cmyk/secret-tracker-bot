import os
import json
import secrets
import threading
import logging
import asyncio
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.extras import Json
from datetime import datetime
from difflib import get_close_matches

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

pet_catalog = {}
pet_catalog_lock = threading.Lock()


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



def load_pet_catalog():
    global pet_catalog
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT pet_name, pet_data
            FROM pet_catalog
            """)
        rows = cur.fetchall()
        cache = {}
        for row in rows:
            cache[row["pet_name"].lower()] = row["pet_data"]
        with pet_catalog_lock:
            pet_catalog = cache
        conn.close()
        log.info(f"[PETDATA] Loaded {len(cache)} pets from database")
    except Exception:
        log.exception("[PETDATA] Failed loading pet catalog")

load_pet_catalog()

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


@app.route("/petdata", methods=["POST"])
def petdata():
    if not check_auth():
        return jsonify({"error":"unauthorized"}),401
    req=request.get_json(silent=True) or {}
    if req.get("secret") != API_SECRET:
        return jsonify({"error":"invalid secret"}),401
    pets=req.get("pets",[])
    if not isinstance(pets,list):
        return jsonify({"error":"invalid pets"}),400
    try:
        conn=get_db(); cur=conn.cursor(); cache={}
        for pet in pets:
            if not pet.get("name"): continue
            cur.execute("""
                INSERT INTO pet_catalog (pet_name, pet_data, updated_at)
                VALUES (%s,%s,NOW())
                ON CONFLICT (pet_name)
                DO UPDATE SET pet_data=EXCLUDED.pet_data, updated_at=NOW()
            """,(pet["name"], Json(pet)))
            cache[pet["name"].lower()] = pet
        conn.commit(); conn.close()
        with pet_catalog_lock:
            pet_catalog.clear(); pet_catalog.update(cache)
        return jsonify({"success":True,"count":len(cache)})
    except Exception:
        log.exception("[PETDATA] Update failed")
        return jsonify({"success":False}),500

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


    def parse_variant_and_name(search):
        words=search.split(); variant="Normal"
        lower=[w.lower() for w in words]
        shiny="shiny" in lower; mythic="mythic" in lower
        if shiny and mythic: variant="MythicShiny"
        elif shiny: variant="Shiny"
        elif mythic: variant="Mythic"
        pet_name=" ".join([w for w in words if w.lower() not in ("shiny","mythic")])
        return variant, pet_name

    def find_pet(search_name):
        with pet_catalog_lock:
            catalog=dict(pet_catalog)
        s=search_name.lower()
        if s in catalog: return catalog[s]
        for name,pet in catalog.items():
            if s in name: return pet
        search_words=set(s.split()); best=None; best_score=0
        for name,pet in catalog.items():
            score=len(search_words & set(name.split()))
            if score>best_score: best_score=score; best=pet
        if best: return best
        matches=get_close_matches(s,catalog.keys(),n=1,cutoff=0.55)
        return catalog[matches[0]] if matches else None

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

    @bot.tree.command(name="petsearch", description="Search the pet database")
    @app_commands.describe(name="Pet name")
    async def petsearch(interaction: discord.Interaction, name: str):
        await interaction.response.defer()
        variant, pet_name = parse_variant_and_name(name)
        pet = find_pet(pet_name)
        if not pet:
            await interaction.followup.send(embed=discord.Embed(title="❌ Pet Not Found",description=f"No pet found matching **{name}**",color=0xED4245))
            return
        try: embed_color=int((pet.get("color") or "#5865F2").lstrip("#"),16)
        except: embed_color=0x5865F2
        image_data=pet.get("images",{})
        thumb=image_data.get(variant) or image_data.get(variant.lower()) or image_data.get("normal") or image_data.get("Normal")
        embed=discord.Embed(title=f"🐾 {variant} {pet['name']}",color=embed_color)
        if thumb: embed.set_thumbnail(url=thumb)
        embed.add_field(name="Rarity",value=pet.get("rarityDisplay",pet.get("rarity","Unknown")),inline=True)
        embed.add_field(name="Chance",value=str(pet.get("chance","Unknown")),inline=True)
        embed.add_field(name="Rarity Rank",value=str(pet.get("rarityRank","Unknown")),inline=True)
        embed.add_field(name="Power",value=f"{pet.get('power',0):,}",inline=True)
        embed.add_field(name="Egg",value=pet.get("egg","Unknown"),inline=True)
        embed.add_field(name="World",value=pet.get("world","Unknown"),inline=True)
        embed.set_footer(text=f"Pet Database • Last Updated {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
        await interaction.followup.send(embed=embed)


    bot.run(BOT_TOKEN)

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
