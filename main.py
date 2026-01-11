import os
import discord
from discord.ext import commands
import json
from collections import defaultdict
import asyncio
from datetime import datetime, timedelta
import hashlib

# --- Setup ---
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# --- State ---
user_message_count = defaultdict(list)
user_mute_level = defaultdict(int)
daily_given = defaultdict(lambda: {"count": 0, "reset": None})
last_daily_reward = {}
pending_verifications = {}  # dm_message_id -> data

# --- File I/O Lock ---
file_lock = asyncio.Lock()

# --- Konfigurasi ---
ENGAGEMENT_PRICES = {
    "like": 0.5,
    "comment": 1.0,
    "retweet": 1.5,
    "follow": 2.0
}

ROLE_TIERS = [
    (100, "Whale"),
    (50, "Sultan"),
    (9, "Ekonomi Menengah"),
    (5, "Butuh Donasi"),
]

PENDING_FILE = 'pending_dm.json'

# --- UTILITIES ---
def make_engagement_key(user_id: int, link: str) -> str:
    return hashlib.sha256(f"{user_id}_{link}".encode()).hexdigest()[:16]

async def has_engaged(user_id: int, link: str, task_type: str) -> bool:
    log = await load_json('engagement_log.json', dict)
    key = make_engagement_key(user_id, link)
    return log.get(key, {}).get(task_type, False)

async def mark_engaged(user_id: int, link: str, task_type: str):
    log = await load_json('engagement_log.json', dict)
    key = make_engagement_key(user_id, link)
    if key not in log:
        log[key] = {}
    log[key][task_type] = True
    await save_json('engagement_log.json', log)

async def load_json(filename, default=None):
    async with file_lock:
        try:
            if os.path.exists(filename):
                with open(filename, 'r') as f:
                    return json.load(f)
            return default() if callable(default) else default
        except Exception:
            return default() if callable(default) else default

async def save_json(filename, data):
    async with file_lock:
        with open(filename, 'w') as f:
            json.dump(data, f, indent=4)

async def load_pending():
    return await load_json(PENDING_FILE, dict)

async def save_pending(data):
    await save_json(PENDING_FILE, data)

async def notify_dm_failure(guild, user: discord.User, message: str):
    log_channel = discord.utils.get(guild.text_channels, name="bukti-transaksi")
    if log_channel:
        await log_channel.send(f"⚠️ Gagal kirim DM ke {user.mention}: {message}")

async def update_user_role(member: discord.Member):
    points_data = await load_json('points.json', dict)
    points = points_data.get(str(member.id), 0)

    # Hapus semua role tier lama
    for _, role_name in ROLE_TIERS:
        role = discord.utils.get(member.guild.roles, name=role_name)
        if role and role in member.roles:
            try:
                await member.remove_roles(role)
            except discord.Forbidden:
                print(f"⚠️ Bot tidak punya izin untuk hapus role {role.name} dari {member}")
            except Exception as e:
                print(f"❌ Error saat hapus role {role.name}: {e}")

    # Berikan role tier tertinggi yang memenuhi syarat
    assigned = False
    for threshold, role_name in ROLE_TIERS:
        if points >= threshold:
            role = discord.utils.get(member.guild.roles, name=role_name)
            if role:
                try:
                    await member.add_roles(role)
                    assigned = True
                except discord.Forbidden:
                    print(f"⚠️ Bot tidak punya izin untuk tambah role {role.name} ke {member}")
                except Exception as e:
                    print(f"❌ Error saat tambah role {role.name}: {e}")
            break  # Hanya berikan satu role (tier tertinggi)

    # --- Role Khusus: Dermawan (tidak termasuk tier) ---
    giver_data = await load_json('giver_count.json', dict)
    give_count = giver_data.get(str(member.id), 0)
    total_given = giver_data.get(f"{member.id}_total", 0)
    dermawan_role = discord.utils.get(member.guild.roles, name="Dermawan")

    qualifies_dermawan = give_count >= 200 and total_given >= 2000
    if qualifies_dermawan and dermawan_role and dermawan_role not in member.roles:
        try:
            await member.add_roles(dermawan_role)
        except discord.Forbidden:
            print(f"⚠️ Bot tidak punya izin untuk tambah role Dermawan ke {member}")
        except Exception as e:
            print(f"❌ Error saat tambah role Dermawan: {e}")
    elif not qualifies_dermawan and dermawan_role and dermawan_role in member.roles:
        try:
            await member.remove_roles(dermawan_role)
        except discord.Forbidden:
            print(f"⚠️ Bot tidak punya izin untuk hapus role Dermawan dari {member}")
        except Exception as e:
            print(f"❌ Error saat hapus role Dermawan: {e}")

async def award_point(user: discord.Member, amount: float, reason: str = "berkontribusi"):
    points_data = await load_json('points.json', dict)
    user_id = str(user.id)
    current = points_data.get(user_id, 0)
    new_balance = round(current + amount, 1)
    points_data[user_id] = new_balance
    await save_json('points.json', points_data)

    log_channel = discord.utils.get(user.guild.text_channels, name="bukti-transaksi")
    if log_channel:
        await log_channel.send(f"✨ {user.mention} mendapatkan **{amount} poin** untuk {reason}! Saldo: **{new_balance}**")

    await update_user_role(user)

def can_give_point(giver_id):
    now = asyncio.get_event_loop().time()
    giver = daily_given[giver_id]
    if giver["reset"] is None or now - giver["reset"] > 86400:
        giver["count"] = 0
        giver["reset"] = now
    return giver["count"] < 3

def use_give_point(giver_id):
    daily_given[giver_id]["count"] += 1

async def apply_mute(message, user):
    muted_role = discord.utils.get(user.guild.roles, name="🔇 Muted")
    if not muted_role:
        muted_role = await user.guild.create_role(name="🔇 Muted", reason="Anti-spam")
        for channel in user.guild.channels:
            await channel.set_permissions(muted_role, send_messages=False, add_reactions=False)

    if muted_role not in user.roles:
        level = user_mute_level[user.id]
        mute_duration = 20 * (level + 1)
        await user.add_roles(muted_role)
        await message.channel.send(f"⚠️ {user.mention} di-mute karena spam! Durasi: {mute_duration} menit.", delete_after=5)
        await asyncio.sleep(mute_duration * 60)
        if muted_role in user.roles:
            await user.remove_roles(muted_role)
            user_mute_level[user.id] += 1
        else:
            user_mute_level[user.id] = 0

def build_embed(request):
    comments = []
    for task in request["tasks"]:
        if task["type"] == "comment":
            safe_text = task["text"].replace("`", "'")
            if task["status"] != "open":
                comments.append(f"[✅] ```{safe_text}```")
            else:
                comments.append(f"[ ] ```{safe_text}```")

    liked_by = request.get("liked_by", [])
    retweeted_by = request.get("retweeted_by", [])
    followed_by = request.get("followed_by", [])

    description = f"**Dari:** <@{request['requester_id']}>\n"
    description += f"**Link Post:** {request['link']}\n"
    description += f"**Berlaku hingga:** <t:{int(request['expiry_timestamp'])}:R>\n\n"

    if comments:
        description += "**Komentar yang Dibutuhkan:**\n" + "\n".join(comments) + "\n\n"

    description += f"[{len(liked_by)}] ❤️ Like (**0.5 poin**)\n"
    description += f"[{len(retweeted_by)}] 🔁 Retweet (**1.5 poin**)\n"
    description += f"[{len(followed_by)}] 👥 Follow (**2.0 poin**)\n\n"
    description += "ℹ️ **Petunjuk:** Reply ke embed ini dengan `!ambil [nomor]` untuk ambil komentar."

    embed = discord.Embed(
        title="📣 Request Engagement",
        description=description,
        color=0x1DA1F2
    )
    embed.set_footer(text=f"Total: {len([t for t in request['tasks'] if t['type'] == 'comment'])} komentar")
    return embed

async def cleanup_expired_requests():
    while True:
        try:
            req_data = await load_json('requests.json', dict)
            now = datetime.utcnow().timestamp()
            to_delete = []
            for msg_id, request in req_data.items():
                if now > request.get("expiry_timestamp", 0):
                    to_delete.append(msg_id)
                    escrow_key = f"escrow_{msg_id}"
                    points_data = await load_json('points.json', dict)
                    escrow = points_data.get(escrow_key, 0)
                    if escrow > 0:
                        requester_id = request["requester_id"]
                        current = points_data.get(requester_id, 0)
                        points_data[requester_id] = round(current + escrow, 1)
                        del points_data[escrow_key]
                        await save_json('points.json', points_data)
                        requester = bot.get_user(int(requester_id))
                        if requester:
                            try:
                                await requester.send(f"⏰ Request engagement-mu telah kadaluarsa. **{escrow} poin** dikembalikan.")
                            except:
                                pass

            for msg_id in to_delete:
                del req_data[msg_id]
            if to_delete:
                await save_json('requests.json', req_data)

        except Exception as e:
            print(f"Error in cleanup: {e}")

        await asyncio.sleep(3600)

async def process_payment(data, approved):
    request_id = data["request_id"]
    task_type = data.get("task_type", "unknown")
    if task_type == "unknown":
        print(f"⚠️ process_payment: data tidak valid → {data}")
        return

    seller_id = data["seller_id"]
    requester_id = data["requester_id"]
    price = data["price"]
    user_pays = data["user_pays"]
    is_comment = data.get("is_comment", False)
    task_idx = data.get("task_idx")

    req_data = await load_json('requests.json', dict)
    if request_id not in req_data:
        return

    request = req_data[request_id]
    points_data = await load_json('points.json', dict)

    if not approved:
        if is_comment and task_idx is not None:
            if task_idx < len(request["tasks"]):
                task = request["tasks"][task_idx]
                if task["assigned_to"] == str(seller_id):
                    task["status"] = "open"
                    task["assigned_to"] = None
        else:
            pass

        req_data[request_id] = request
        await save_json('requests.json', req_data)

        channel = bot.get_channel(int(request["channel_id"]))
        if channel:
            try:
                msg = await channel.fetch_message(int(request["message_id"]))
                await msg.edit(embed=build_embed(request))
            except:
                pass

        seller = bot.get_user(seller_id)
        if seller:
            requester = bot.get_user(int(requester_id))
            requester_name = requester.mention if requester else f"<@{requester_id}>"
            try:
                await seller.send(f"❌ {requester_name} membatalkan verifikasi tugas **{task_type}**. Kamu tidak mendapat poin.")
            except:
                pass
        return

    requester_bal = points_data.get(requester_id, 0)
    if requester_bal < user_pays:
        seller = bot.get_user(seller_id)
        if seller:
            try:
                await seller.send("❌ Gagal menerima pembayaran: pembeli kehabisan saldo.")
            except:
                pass
        return

    points_data[requester_id] = round(requester_bal - user_pays, 1)
    seller_bal = points_data.get(str(seller_id), 0)
    points_data[str(seller_id)] = round(seller_bal + price, 1)
    await save_json('points.json', points_data)

    if is_comment and task_idx is not None:
        if task_idx < len(request["tasks"]):
            task = request["tasks"][task_idx]
            if task["assigned_to"] == str(seller_id):
                task["status"] = "confirmed"

    req_data[request_id] = request
    await save_json('requests.json', req_data)

    channel = bot.get_channel(int(request["channel_id"]))
    if channel:
        try:
            msg = await channel.fetch_message(int(request["message_id"]))
            await msg.edit(embed=build_embed(request))
        except:
            pass

    log_channel = discord.utils.get(bot.guilds[0].text_channels, name="bukti-transaksi")
    if log_channel:
        subsidy = price - user_pays
        subsidy_msg = f" (subsidi bot: {subsidy} poin)" if subsidy > 0 else ""
        await log_channel.send(
            f"✅ **Transaksi Berhasil!**\n"
            f"• Pembeli: <@{requester_id}>\n"
            f"• Penjual: <@{seller_id}>\n"
            f"• Jenis: {task_type}\n"
            f"• Dibayar user: {user_pays} poin{subsidy_msg}\n"
            f"• Total diterima penjual: {price} poin"
        )

    seller_member = bot.guilds[0].get_member(seller_id)
    if seller_member:
        await update_user_role(seller_member)

# --- EVENTS ---
@bot.event
async def on_ready():
    global pending_verifications
    print(f"✅ Bot aktif sebagai {bot.user}")
    pending_verifications.update(await load_pending())
    bot.loop.create_task(cleanup_expired_requests())

@bot.event
async def on_member_join(member):
    await award_point(member, 10, "selamat datang!")
    await update_user_role(member)

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if message.channel.name == "general":
        user_id = str(message.author.id)
        now = asyncio.get_event_loop().time()
        last_time = last_daily_reward.get(user_id, 0)
        if now - last_time >= 86400:
            points_data = await load_json('points.json', dict)
            current = points_data.get(user_id, 0)
            if current < 5:
                points_data[user_id] = round(current + 2, 1)
                await save_json('points.json', points_data)
                last_daily_reward[user_id] = now
                try:
                    await message.author.send("🎁 Kamu mendapatkan **2 poin** dari aktivitas di #general! (Hanya berlaku jika saldo < 5)")
                except:
                    pass

    allowed_channels = ["jual-beli", "bukti-transaksi"]
    if message.channel.name in allowed_channels:
        is_allowed = False
        if message.channel.name == "bukti-transaksi":
            if message.content.startswith("!"):
                is_allowed = True
            else:
                await message.delete()
                try:
                    await message.author.send(
                        "❌ Di channel #bukti-transaksi, hanya boleh kirim command:\n"
                        "• `!saldo` → cek poinmu\n"
                        "• `!givepoint @user [1-3]` → beri poin ke orang lain"
                    )
                except:
                    pass
                return
        elif message.channel.name == "jual-beli":
            if message.content.startswith("!beli") or message.content.startswith("!ambil"):
                is_allowed = True
            else:
                await message.delete()
                try:
                    await message.author.send("❌ Di channel #jual-beli, hanya boleh kirim `!beli` atau reply ke embed dengan `!ambil ...`. Pesanmu dihapus.")
                except:
                    pass
                return

        if not is_allowed:
            await message.delete()
            return

    if message.channel.name in ["jual-beli", "bukti-transaksi", "general"]:
        user_id = message.author.id
        now = message.created_at.timestamp()
        user_message_count[user_id] = [t for t in user_message_count[user_id] if now - t < 60]
        user_message_count[user_id].append(now)
        if len(user_message_count[user_id]) > 7:
            await apply_mute(message, message.author)
            return

    await bot.process_commands(message)

@bot.event
async def on_reaction_add(reaction, user):
    if user == bot.user:
        return

    if isinstance(reaction.message.channel, discord.DMChannel):
        msg_id = str(reaction.message.id)
        pending_data = await load_pending()
        if msg_id in pending_data:
            data = pending_data.pop(msg_id)
            await save_pending(pending_data)
            emoji = str(reaction.emoji)
            approved = (emoji == "✅")
            await process_payment(data, approved=approved)

            try:
                if reaction.message.author == bot.user:
                    await reaction.message.delete()
            except discord.NotFound:
                pass
            except Exception as e:
                print(f"⚠️ Gagal hapus DM: {e}")
        return

    if reaction.message.author != bot.user or reaction.message.channel.name != "jual-beli":
        return

    allowed_emojis = {"❤️", "🔁", "👥"}
    emoji_str = str(reaction.emoji)
    if emoji_str not in allowed_emojis:
        await reaction.message.remove_reaction(emoji_str, user)
        try:
            await user.send("❌ Hanya reaksi ❤️, 🔁, dan 👥 yang diizinkan.")
        except:
            pass
        return

    req_data = await load_json('requests.json', dict)
    msg_id = str(reaction.message.id)
    if msg_id not in req_data:
        return

    request = req_data[msg_id]
    requester_id = request["requester_id"]
    if str(user.id) == requester_id:
        await reaction.message.remove_reaction(emoji_str, user)
        try:
            await user.send("❌ Kamu tidak bisa mereact postinganmu sendiri.")
        except:
            pass
        return

    # 🔒 CEK MUTUAL FOLLOW WAJIB
    global_follows = await load_json('global_follows.json', dict)
    follow_key = f"{user.id}_{requester_id}"
    if follow_key not in global_follows:
        await reaction.message.remove_reaction(emoji_str, user)
        try:
            await user.send(f"🔒 Kamu harus follow <@{requester_id}> dan selesaikan verifikasi terlebih dahulu sebelum membantu engagement-nya.")
        except:
            pass
        return

    emoji_to_type = {"❤️": "like", "🔁": "retweet", "👥": "follow"}
    task_type = emoji_to_type[emoji_str]

    # 🔒 CEK ANTI-SPAM PERMANEN (SEKALI SEUMUR HIDUP)
    if task_type in ("like", "retweet"):
        if await has_engaged(user.id, request['link'], task_type):
            await reaction.message.remove_reaction(emoji_str, user)
            try:
                await user.send(f"❌ Kamu sudah pernah {task_type} postingan ini sebelumnya.")
            except:
                pass
            return
    elif task_type == "follow":
        if follow_key in global_follows:
            await reaction.message.remove_reaction(emoji_str, user)
            try:
                await user.send("❌ Kamu sudah pernah follow akun ini sebelumnya (sekali seumur hidup).")
            except:
                pass
            return
        global_follows[follow_key] = True
        await save_json('global_follows.json', global_follows)

    if task_type in ("like", "retweet"):
        await mark_engaged(user.id, request['link'], task_type)

    user_id_str = str(user.id)
    if task_type == "like":
        if user_id_str not in request.get("liked_by", []):
            request.setdefault("liked_by", []).append(user_id_str)
    elif task_type == "retweet":
        if user_id_str not in request.get("retweeted_by", []):
            request.setdefault("retweeted_by", []).append(user_id_str)
    elif task_type == "follow":
        if user_id_str not in request.get("followed_by", []):
            request.setdefault("followed_by", []).append(user_id_str)

    req_data[msg_id] = request
    await save_json('requests.json', req_data)
    await reaction.message.edit(embed=build_embed(request))

    requester = bot.get_user(int(requester_id))
    if not requester:
        return

    price = ENGAGEMENT_PRICES[task_type]
    requester_member = bot.guilds[0].get_member(int(requester_id))
    dermawan_role = discord.utils.get(bot.guilds[0].roles, name="Dermawan")
    is_dermawan = requester_member and dermawan_role and dermawan_role in requester_member.roles
    user_pays = round(price * 0.5, 1) if is_dermawan else price

    try:
        confirm_msg = await requester.send(
            f"💬 <@{user.id}> mengklaim sudah menyelesaikan: **{task_type.capitalize()}**\n"
            f"Link: {request['link']}\n"
            f"Harga: **{price} poin**\n"
            f"{'(subsidi sistem: ' + str(price - user_pays) + ' poin)' if (price - user_pays) > 0 else ''}\n\n"
            f"✅ **React ini jika TUGAS BENAR**\n"
            f"❌ **React ini jika TUGAS SALAH/TIDAK DILAKUKAN**\n"
            f"⏳ Jika tidak ada reaksi dalam **15 menit**, transaksi **dianggap sah**.\n\n"
            f"(request_id={msg_id},task_type={task_type},seller_id={user.id})"
        )
        await confirm_msg.add_reaction("✅")
        await confirm_msg.add_reaction("❌")
        pending_data = await load_pending()
        pending_data[str(confirm_msg.id)] = {
            "request_id": msg_id,
            "task_type": task_type,
            "seller_id": user.id,
            "requester_id": requester_id,
            "price": price,
            "user_pays": user_pays,
            "is_comment": False
        }
        await save_pending(pending_data)

        async def timeout_handler():
            await asyncio.sleep(900)
            pending_data = await load_pending()
            key = str(confirm_msg.id)
            if key in pending_data:
                data = pending_data.pop(key)
                await save_pending(pending_data)
                await process_payment(data, approved=True)
                try:
                    if confirm_msg.author == bot.user:
                        await confirm_msg.delete()
                except discord.NotFound:
                    pass
                except Exception as e:
                    print(f"⚠️ Gagal hapus DM setelah timeout: {e}")

        bot.loop.create_task(timeout_handler())

    except discord.Forbidden:
        await notify_dm_failure(bot.guilds[0], requester, f"Gagal kirim konfirmasi {task_type} oleh <@{user.id}>.")

@bot.event
async def on_reaction_remove(reaction, user):
    if user == bot.user or not isinstance(reaction.message.channel, discord.TextChannel):
        return
    if reaction.message.author == bot.user and reaction.message.channel.name == "jual-beli":
        emoji_str = str(reaction.emoji)
        if emoji_str in {"❤️", "🔁", "👥"}:
            return

# --- COMMANDS ---
@bot.command(name="beli")
async def buy_engagement(ctx, days: int, link: str, *, comments_raw: str = ""):
    if ctx.channel.name != "jual-beli":
        await ctx.send("❌ Gunakan command ini hanya di `#jual-beli`.", delete_after=5)
        await ctx.message.delete()
        return
    if "x.com/" not in link and "twitter.com/" not in link:
        await ctx.send("❌ Link harus dari X (Twitter).", delete_after=5)
        await ctx.message.delete()
        return
    if days < 1 or days > 7:
        await ctx.send("❌ Durasi harus antara **1–7 hari**.", delete_after=5)
        await ctx.message.delete()
        return
    comment_lines = [line.strip() for line in comments_raw.split('\n') if line.strip()]
    if not comment_lines:
        await ctx.send("❌ Minimal 1 komentar diperlukan.", delete_after=5)
        await ctx.message.delete()
        return

    tasks = [{"type": "comment", "text": text, "price": ENGAGEMENT_PRICES["comment"], "assigned_to": None, "status": "open"} for text in comment_lines]
    total_price = len(tasks)
    points_data = await load_json('points.json', dict)
    user_id_str = str(ctx.author.id)
    current_points = points_data.get(user_id_str, 0)
    if current_points < total_price:
        await ctx.send(f"❌ Kamu butuh **{total_price} poin**. Saldo: **{current_points}**.", delete_after=5)
        await ctx.message.delete()
        return

    points_data[user_id_str] = round(current_points - total_price, 1)
    escrow_key = f"escrow_{ctx.message.id}"
    points_data[escrow_key] = total_price
    await save_json('points.json', points_data)

    expiry = datetime.utcnow() + timedelta(days=days)
    expiry_ts = int(expiry.timestamp())
    new_request = {
        "requester_id": user_id_str,
        "link": link,
        "tasks": tasks,
        "channel_id": str(ctx.channel.id),
        "liked_by": [],
        "retweeted_by": [],
        "followed_by": [],
        "expiry_timestamp": expiry_ts
    }

    embed = build_embed(new_request)
    msg = await ctx.send(embed=embed)
    new_request["message_id"] = str(msg.id)

    req_data = await load_json('requests.json', dict)
    req_data[str(msg.id)] = new_request
    await save_json('requests.json', req_data)

    for emoji in ["❤️", "🔁", "👥"]:
        await msg.add_reaction(emoji)

    await ctx.message.delete()

@bot.command(name="ambil")
async def take_task(ctx, task_number: int):
    if ctx.channel.name != "jual-beli":
        await ctx.send("❌ Gunakan hanya di `#jual-beli`.", delete_after=5)
        await ctx.message.delete()
        return

    if not ctx.message.reference or not ctx.message.reference.message_id:
        await ctx.send("❌ Kamu harus reply ke embed request!", delete_after=5)
        await ctx.message.delete()
        return

    try:
        referenced_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
    except discord.NotFound:
        await ctx.send("❌ Pesan yang direply tidak ditemukan.", delete_after=5)
        await ctx.message.delete()
        return

    if referenced_msg.author != bot.user or not referenced_msg.embeds:
        await ctx.send("❌ Reply harus ke embed request dari bot.", delete_after=5)
        await ctx.message.delete()
        return

    req_data = await load_json('requests.json', dict)
    msg_id = str(referenced_msg.id)
    if msg_id not in req_data:
        await ctx.send("❌ Request tidak ditemukan atau sudah kadaluarsa.", delete_after=5)
        await ctx.message.delete()
        return

    request = req_data[msg_id]
    requester_id = request["requester_id"]
    if str(ctx.author.id) == requester_id:
        await ctx.send("❌ Kamu tidak bisa mengambil request milikmu sendiri.", delete_after=5)
        await ctx.message.delete()
        return

    global_follows = await load_json('global_follows.json', dict)
    follow_key = f"{ctx.author.id}_{requester_id}"
    if follow_key not in global_follows:
        await ctx.send(
            f"🔒 Kamu harus follow <@{requester_id}> dan selesaikan verifikasi terlebih dahulu sebelum mengambil komentar.",
            delete_after=10
        )
        await ctx.message.delete()
        return

    if await has_engaged(ctx.author.id, request['link'], "comment"):
        await ctx.send("❌ Kamu sudah pernah ambil komentar untuk postingan ini.", delete_after=5)
        await ctx.message.delete()
        return

    open_comments = [(idx, task) for idx, task in enumerate(request["tasks"]) if task["type"] == "comment" and task["status"] == "open"]
    if not open_comments:
        await ctx.send("❌ Tidak ada komentar tersedia.", delete_after=5)
        await ctx.message.delete()
        return

    if not (1 <= task_number <= len(open_comments)):
        await ctx.send(f"❌ Nomor tugas harus 1–{len(open_comments)}.", delete_after=5)
        await ctx.message.delete()
        return

    task_idx, task = open_comments[task_number - 1]
    task["assigned_to"] = str(ctx.author.id)
    task["status"] = "claimed"
    req_data[msg_id] = request
    await save_json('requests.json', req_data)
    await referenced_msg.edit(embed=build_embed(request))
    await ctx.message.delete()

    await mark_engaged(ctx.author.id, request['link'], "comment")

    requester = bot.get_user(int(requester_id))
    if not requester:
        return

    price = task["price"]
    requester_member = bot.guilds[0].get_member(int(requester_id))
    dermawan_role = discord.utils.get(bot.guilds[0].roles, name="Dermawan")
    is_dermawan = requester_member and dermawan_role and dermawan_role in requester_member.roles
    user_pays = round(price * 0.5, 1) if is_dermawan else price

    try:
        confirm_msg = await requester.send(
            f"💬 <@{ctx.author.id}> telah mengambil dan mengklaim menyelesaikan komentar: _‘{task['text']}’_\n"
            f"Link: {request['link']}\n"
            f"Harga: **{price} poin**\n"
            f"{'(subsidi sistem: ' + str(price - user_pays) + ' poin)' if (price - user_pays) > 0 else ''}\n\n"
            f"✅ **React ini jika TUGAS BENAR**\n"
            f"❌ **React ini jika TUGAS SALAH/TIDAK DILAKUKAN**\n"
            f"⏳ Jika tidak ada reaksi dalam **15 menit**, transaksi **dianggap sah**."
        )
        await confirm_msg.add_reaction("✅")
        await confirm_msg.add_reaction("❌")
        pending_data = await load_pending()
        pending_data[str(confirm_msg.id)] = {
            "request_id": msg_id,
            "task_idx": task_idx,
            "seller_id": ctx.author.id,
            "requester_id": requester_id,
            "price": price,
            "user_pays": user_pays,
            "is_comment": True,
            "task_type": "comment"
        }
        await save_pending(pending_data)

        async def timeout_handler():
            await asyncio.sleep(900)
            pending_data = await load_pending()
            key = str(confirm_msg.id)
            if key in pending_data:
                data = pending_data.pop(key)
                await save_pending(pending_data)
                await process_payment(data, approved=True)
                try:
                    if confirm_msg.author == bot.user:
                        await confirm_msg.delete()
                except discord.NotFound:
                    pass
                except Exception as e:
                    print(f"⚠️ Gagal hapus DM setelah timeout: {e}")

        bot.loop.create_task(timeout_handler())

    except discord.Forbidden:
        await notify_dm_failure(ctx.guild, requester, f"Gagal kirim konfirmasi komentar oleh <@{ctx.author.id}>.")

@bot.command(name="saldo")
async def check_balance(ctx):
    pts = (await load_json('points.json', dict)).get(str(ctx.author.id), 0)
    await ctx.send(f"💰 **{ctx.author.display_name}** memiliki **{pts} poin**.")
    await ctx.message.delete()

@bot.command(name="givepoint")
async def give_point(ctx, member: discord.Member, amount: int = 1):
    if ctx.channel.name != "bukti-transaksi":
        await ctx.send("❌ Gunakan command ini hanya di `#bukti-transaksi`.", delete_after=5)
        await ctx.message.delete()
        return
    if member == ctx.author:
        await ctx.send("❌ Tidak bisa transfer ke diri sendiri.", delete_after=5)
        await ctx.message.delete()
        return
    if amount < 1:
        await ctx.send("❌ Jumlah minimal 1 poin.", delete_after=5)
        await ctx.message.delete()
        return

    giver_id = str(ctx.author.id)
    if not can_give_point(giver_id):
        await ctx.send("❌ Maksimal 3 poin/hari.", delete_after=5)
        await ctx.message.delete()
        return

    tax = 1 if amount < 10 else max(1, round(amount * 0.2, 1))
    total_cost = amount + tax
    points_data = await load_json('points.json', dict)
    giver_bal = points_data.get(giver_id, 0)
    if giver_bal < total_cost:
        await ctx.send(f"❌ Saldo tidak cukup. Butuh **{total_cost} poin** (termasuk pajak {tax} poin).", delete_after=5)
        await ctx.message.delete()
        return

    points_data[giver_id] = round(giver_bal - total_cost, 1)
    receiver_id = str(member.id)
    points_data[receiver_id] = points_data.get(receiver_id, 0) + amount
    await save_json('points.json', points_data)

    giver_count = await load_json('giver_count.json', dict)
    giver_count[giver_id] = giver_count.get(giver_id, 0) + 1
    giver_count[f"{giver_id}_total"] = giver_count.get(f"{giver_id}_total", 0) + amount
    await save_json('giver_count.json', giver_count)

    use_give_point(giver_id)
    await update_user_role(member)
    await update_user_role(ctx.author)
    await ctx.send(f"✨ {ctx.author.mention} memberi **{amount} poin** ke {member.mention}! (Pajak: {tax} poin)")
    await ctx.message.delete()

@bot.command()
@commands.has_role("🛡️ Peacekeeper")
async def addpoint(ctx, member: discord.Member, amount: float):
    if not (-20 <= amount <= 20):
        await ctx.send("❌ Jumlah harus antara -20 hingga 20.")
        return
    data = await load_json('points.json', dict)
    user_id = str(member.id)
    old_balance = data.get(user_id, 0)
    new_balance = round(old_balance + amount, 1)
    data[user_id] = new_balance
    await save_json('points.json', data)
    action = "ditambahkan" if amount > 0 else "dikurangi"
    await ctx.send(f"✅ Poin {member.mention} {action} sebesar {abs(amount)}. Saldo baru: **{new_balance}**")

# --- Run ---
if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if token:
        bot.run(token)
    else:
        print("❌ ERROR: DISCORD_TOKEN tidak ditemukan!")
