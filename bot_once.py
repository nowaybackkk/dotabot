"""
Dota 2 -> Telegram бот для запуска по расписанию (GitHub Actions).
Каждый запуск: читает команды и нажатия кнопок, проверяет новые матчи,
шлёт отчёты, сохраняет state.json. Только стандартная библиотека Python.
"""
import html
import json
import os
import re
import time
import urllib.request

TOKEN = os.environ["BOT_TOKEN"]
OPENDOTA = "https://api.opendota.com/api"
STATE_FILE = "state.json"
BEHAVIOR_EVERY = 15      # раз в сколько игр просить порядочность
MMR_STEP = 25            # оценка MMR за игру, пока вы не нажали кнопку
RANKED_LOBBY = 7
GAIN_MIN, GAIN_MAX = 20, 30  # диапазон возможного изменения MMR за игру (кнопки)
STATS_DAYS = 5

DEFAULT = {
    "owner": None, "account_id": None, "mmr": None, "behavior": None,
    "last_match_id": 0, "games_since_check": 0, "total_games": 0,
    "wins": 0, "offset": 0, "history": [],
    "since": 0,    # игры, закончившиеся до этого времени, игнорируются
    "mmr_at": 0,   # MMR меняется только по играм, закончившимся после этого времени
    "goal": None,     # желаемый рейтинг (показывается как 5655/6000)
    "friends": [],    # ID друзей для вкладки «С друзьями» (необязательно)
    "pending": None,  # бот ждёт число после нажатия кнопки меню (mmr / behavior / link)
}

# Постоянная панель кнопок под полем ввода
MENU = {
    "keyboard": [
        ["🎮 Последняя игра", "📊 За 5 дней"],
        ["📈 Вся статистика", "👥 С друзьями"],
        ["🔄 Сверить MMR", "🧠 Порядочность"],
        ["🎯 Цель", "❓ Помощь"],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}
LABELS = {
    "🎮 Последняя игра": "/last",
    "📊 За 5 дней": "/5days",
    "📈 Вся статистика": "/stats",
    "👥 С друзьями": "/friends",
    "🔄 Сверить MMR": "/mmr",
    "🧠 Порядочность": "/behavior",
    "🎯 Цель": "/goal",
    "❓ Помощь": "/start",
}

DIV = "━━━━━━━━━━━━━━"
FRIEND_DAYS = 90          # за сколько дней считать винрейт с друзьями
MIN_FRIEND_GAMES = 5      # без списка друзей показываем тех, с кем сыграно не меньше стольких игр
MODES = {
    1: "All Pick", 2: "Captains Mode", 3: "Random Draft", 4: "Single Draft",
    5: "All Random", 8: "Reverse CM", 12: "Least Played", 16: "Captains Draft",
    17: "Balanced Draft", 18: "Ability Draft", 20: "ARDM", 21: "1v1 Mid",
    22: "All Draft", 23: "Turbo", 24: "Mutation",
}


# ---------- сеть ----------
def http(url, data=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json", "User-Agent": "dota-bot"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def tg(method, **params):
    return http(f"https://api.telegram.org/bot{TOKEN}/{method}", params)


def to_html(text, match_id=None):
    """Простой текст -> HTML: жирные заголовки и подписи, ссылка на матч."""
    out = []
    for i, ln in enumerate(text.split("\n")):
        e = html.escape(ln, quote=False)
        if i == 0 or ln.startswith("📊"):
            e = f"<b>{e}</b>"
        elif ln.startswith("🔗") and match_id:
            e = f'🔗 <a href="https://www.opendota.com/matches/{match_id}">Матч на OpenDota</a>'
        else:
            m = re.match(r"^(\S+) ([^:]{1,25}:)(.*)$", e)
            if m:
                e = f"{m.group(1)} <b>{m.group(2)}</b>{m.group(3)}"
        out.append(e)
    return "\n".join(out)


def send(chat, text, markup=None, fancy=False, match_id=None):
    p = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
    if fancy:
        p["text"] = to_html(text, match_id)
        p["parse_mode"] = "HTML"
    p["reply_markup"] = markup or MENU   # у отчётов с рейтингом — кнопки рейтинга, у остальных — меню
    tg("sendMessage", **p)


def od(path):
    return http(f"{OPENDOTA}{path}")


_heroes = None


def heroes():
    """Список героев грузим только когда он нужен (экономим лимит OpenDota)."""
    global _heroes
    if _heroes is None:
        try:
            _heroes = {h["id"]: h["localized_name"] for h in od("/heroes")}
        except Exception:
            _heroes = {}
    return _heroes


# ---------- состояние ----------
def load():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = {**DEFAULT, **json.load(f)}
    except FileNotFoundError:
        st = dict(DEFAULT)
    st["history"] = list(st["history"])
    st["friends"] = list(st["friends"])
    return st


def sg(n):
    return f"+{n}" if n > 0 else str(n)


def goal_txt(st):
    return f"/{st['goal']}" if st.get("goal") else ""


def is_win(m):
    return (m["player_slot"] < 128) == m["radiant_win"]


def beh_text(st):
    return f"Порядочность: {st['behavior']}" if st["behavior"] is not None else "Порядочность: не задана (/behavior)"


def analyze(m):
    """Оценка вашего вклада по цифрам матча. Возвращает (вердикт, причины) или None."""
    full = od(f"/matches/{m['match_id']}")
    players = full.get("players") or []
    me = next((p for p in players if p.get("player_slot") == m["player_slot"]), None)
    if not me or len(players) < 10:
        return None
    if m["duration"] < 15 * 60:
        return ("⚪ Игра закончилась слишком рано для оценки", None)

    def g(p, key):
        return p.get(key) or 0

    mine_side = me["player_slot"] < 128
    team = [p for p in players if (p["player_slot"] < 128) == mine_side]
    foes = [p for p in players if (p["player_slot"] < 128) != mine_side]
    team_sorted = sorted(team, key=lambda p: -g(p, "gold_per_min"))
    foes_sorted = sorted(foes, key=lambda p: -g(p, "gold_per_min"))
    pos = team_sorted.index(me)          # 0 = лучший фарм в команде ... 4 = худший

    t_deaths = sum(g(p, "deaths") for p in team) or 1
    t_kills = sum(g(p, "kills") for p in team) or 1
    t_dmg = sum(g(p, "hero_damage") for p in team) or 1
    deaths = g(me, "deaths")
    death_share = deaths / t_deaths
    dmg_share = g(me, "hero_damage") / t_dmg
    exp_dmg = [0.28, 0.25, 0.20, 0.15, 0.12][pos]
    my_gpm = g(me, "gold_per_min")
    foe_gpm = g(foes_sorted[pos], "gold_per_min") if len(foes_sorted) > pos else 0
    kp = (g(me, "kills") + g(me, "assists")) / t_kills

    parts = []   # (баллы, текст)
    pts = -1 if (deaths >= 5 and death_share >= 0.27) else (1 if death_share <= 0.12 else 0)
    parts.append((pts, f"Смерти {deaths} из {t_deaths} ({death_share:.0%})"))
    ratio = dmg_share / exp_dmg
    pts = 1 if ratio >= 1.2 else (-1 if ratio <= 0.7 else 0)
    parts.append((pts, f"Урон {dmg_share:.0%} команды (ожид. ~{exp_dmg:.0%})"))
    if foe_gpm:
        r = my_gpm / foe_gpm
        pts = 1 if r >= 1.10 else (-1 if r <= 0.90 else 0)
        parts.append((pts, f"Фарм {my_gpm} против {foe_gpm} у соперника"))
    low = 0.30 if pos <= 1 else 0.40
    pts = 1 if kp >= 0.65 else (-1 if kp <= low else 0)
    parts.append((pts, f"Участие в убийствах {kp:.0%}"))

    score = sum(p for p, _ in parts)
    win = is_win(m)
    if win:
        verdict = ("🌟 Сильная игра — ты тащил" if score >= 2
                   else "🟡 Нормальная игра" if score >= 0
                   else "🟠 Победа в основном благодаря команде")
    else:
        verdict = ("🟢 Не твоя вина — ты играл выше команды" if score >= 2
                   else "🟡 Нейтрально — на уровне команды" if score >= 0
                   else "🟠 Вклад чуть ниже ожидаемого" if score == -1
                   else "🔴 Твой вклад заметно ниже ожидаемого")
    verdict += f" (поз. {pos + 1} по фарму)"

    key = [x for x in parts if x[0] != 0]
    key.sort(key=lambda x: -abs(x[0]))
    reasons = " · ".join(f"{'👍' if pt > 0 else '👎'} {tx}" for pt, tx in key[:2]) or "все показатели в норме"
    return (verdict, reasons)


def report(m, mmr_line, beh_line, analysis=None):
    win = is_win(m)
    hero = heroes().get(m["hero_id"], f"hero {m['hero_id']}")
    mins, secs = divmod(m["duration"], 60)
    k, d, a = m["kills"], m["deaths"], m["assists"]
    kda = (k + a) / max(d, 1)
    mode = MODES.get(m.get("game_mode"), f"режим {m.get('game_mode', '?')}")
    kind = "Рейтинг" if m.get("lobby_type") == RANKED_LOBBY else "Обычная игра"
    extra = ""
    if analysis:
        extra = f"⚖️ Вердикт по цифрам: {analysis[0]}\n"
        if analysis[1]:
            extra += f"📝 Почему: {analysis[1]}\n"
    return (
        f"{'🏆 ПОБЕДА' if win else '💀 ПОРАЖЕНИЕ'}\n{DIV}\n"
        f"🦸 Герой: {hero}\n"
        f"⚔️ KDA: {k}/{d}/{a} ({kda:.1f})\n"
        f"💰 GPM: {m.get('gold_per_min', '—')}\n"
        f"⏱ Длительность: {mins}:{secs:02d}\n"
        f"🎲 Режим: {mode} · {kind}\n{DIV}\n"
        f"💎 {mmr_line}\n"
        f"🧠 {beh_line}\n"
        f"{extra}"
        f"🔗 Матч на OpenDota"
    )


def safe_analyze(m):
    try:
        return analyze(m)
    except Exception as e:
        print("analysis error:", e)
        return None


def period_stats(st, days=STATS_DAYS):
    since = time.time() - days * 86400
    window = [g for g in st["history"] if g["t"] >= since]
    games = [g for g in window if g["win"] is not None]  # поправки MMR — не игры
    if not games:
        return f"😴 За последние {days} дней рейтинговых игр нет."
    wins = sum(1 for g in games if g["win"])
    losses = len(games) - wins
    delta = sum(g["d"] for g in window)
    wr = wins / len(games) * 100
    return (
        f"📊 Рейтинг за {days} дней\n{DIV}\n"
        f"🎮 Игр: {len(games)}\n"
        f"✅ Победы: {wins}\n"
        f"❌ Поражения: {losses}\n"
        f"🔥 Винрейт: {wr:.0f}%\n"
        f"{'🟢' if delta >= 0 else '🔴'} MMR за период: {sg(delta)}"
    )


def games_word(n):
    if n % 10 == 1 and n % 100 != 11:
        return "игра"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "игры"
    return "игр"


def friends_report(st):
    """Винрейт в рейтинге вместе с напарниками (данные OpenDota, только рейтинговые игры)."""
    q = f"lobby_type={RANKED_LOBBY}&date={FRIEND_DAYS}"
    peers = od(f"/players/{st['account_id']}/peers?{q}")
    wl = od(f"/players/{st['account_id']}/wl?{q}")

    if st["friends"]:
        by_id = {p["account_id"]: p for p in peers}
        rows = [by_id.get(fid) or {"account_id": fid, "with_games": 0, "with_win": 0} for fid in st["friends"]]
    else:
        rows = [p for p in peers if p.get("with_games", 0) >= MIN_FRIEND_GAMES]
    rows.sort(key=lambda p: -p.get("with_games", 0))

    total = wl.get("win", 0) + wl.get("lose", 0)
    lines = [f"👥 Винрейт с друзьями", DIV, f"📅 Рейтинг, последние {FRIEND_DAYS} дней"]
    if total:
        lines.append(f"🎮 Всего в рейтинге: {total} {games_word(total)} · {wl['win'] / total:.0%}")
    lines.append(DIV)
    if not rows:
        lines.append(f"Не нашёл напарников с {MIN_FRIEND_GAMES}+ играми в рейтинге.")
        lines.append("Добавь друга вручную: /friend <ID> (ID с opendota.com)")
        return "\n".join(lines)
    for p in rows[:10]:
        n, w = p.get("with_games", 0), p.get("with_win", 0)
        name = p.get("personaname") or f"ID {p['account_id']}"
        if n == 0:
            lines.append(f"⚪ {name}: игр вместе нет")
            continue
        rate = w / n
        mark = "🟢" if rate >= 0.55 else ("🔴" if rate < 0.45 else "🟡")
        lines.append(f"{mark} {name}: {n} {games_word(n)} · {rate:.0%} ({w}–{n - w})")
    lines.append(DIV)
    lines.append("🟢 ≥55%  🟡 45–55%  🔴 <45%  ·  в скобках: победы–поражения")
    return "\n".join(lines)


def keyboard(match_id, win, before):
    """Кнопки с итоговым рейтингом; нажатие выставляет именно это число."""
    sign = 1 if win else -1
    buttons = [
        {"text": str(before + sign * v), "callback_data": f"a:{match_id}:{before + sign * v}"}
        for v in range(GAIN_MIN, GAIN_MAX + 1)
    ]
    rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    return {"inline_keyboard": rows}


def apply_exact(st, idx, mag):
    """Ставит точное изменение MMR для игры history[idx]. Возвращает (игра, значение)."""
    g = st["history"][idx]
    exact = mag if g["win"] else -mag
    diff = exact - g["d"]
    g["d"] = exact
    if "m" in g:
        g["m"] += diff
    synced = False
    for e in st["history"][idx + 1:]:
        if e.get("adj"):           # дальше рейтинг уже сверен с реальным
            e["d"] -= diff
            synced = True
            break
        if "m" in e:
            e["m"] += diff
        if "b" in e:
            e["b"] += diff
    if not synced and st["mmr"] is not None:
        st["mmr"] += diff
    return g, exact


# ---------- команды ----------
def handle_message(st, msg):
    chat = msg["chat"]["id"]
    text = msg["text"].strip()
    date = msg.get("date", int(time.time()))

    # Владелец — тот, кто первым написал /start. Остальных бот игнорирует.
    if st["owner"] is None and text.startswith("/start"):
        st["owner"] = chat
    if chat != st["owner"]:
        return

    text = LABELS.get(text, text)
    if st["pending"] and text.isdigit():
        text = f"/{st['pending']} {text}"   # число после кнопки «Сверить MMR» и т. п.
    st["pending"] = None                    # любое другое сообщение отменяет ожидание

    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    arg = arg.strip()

    if cmd == "/start":
        send(chat, "Привет! Кнопки меню — внизу, под полем ввода.\n\n"
                   "Первая настройка:\n"
                   "1) /link — привяжи аккаунт (ID с opendota.com)\n"
                   "2) «🔄 Сверить MMR» — введи текущий рейтинг\n"
                   "3) «🧠 Порядочность» — введи текущую порядочность\n"
                   "4) «🎯 Цель» — желаемый рейтинг (по желанию)\n\n"
                   "Под каждым рейтинговым отчётом будут кнопки с итоговым рейтингом — "
                   "нажми свой, и я исправлю сообщение.\n\n"
                   "Ещё: /d <число> — точное изменение MMR последней игры, "
                   "/reset — обнулить статистику, /friend <ID> — добавить или убрать друга.")
    elif cmd == "/link":
        if not arg:
            st["pending"] = "link"
            return send(chat, "Отправь свой ID — число из адреса профиля на opendota.com/players/…")
        if not arg.isdigit():
            return send(chat, "Нужно число, например: /link 123456789")
        try:
            ms = od(f"/players/{arg}/recentMatches")
        except Exception:
            return send(chat, "Не удалось получить данные. Проверь ID и открытость данных матчей в Dota 2.")
        st["account_id"] = int(arg)
        st["last_match_id"] = max((m["match_id"] for m in ms), default=0)
        st["since"] = date
        send(chat, "Аккаунт привязан ✅ Отчёты придут по играм, которые закончатся с этого момента.")
    elif cmd == "/mmr":
        if not arg:
            st["pending"] = "mmr"
            return send(chat, "Отправь число — твой текущий рейтинг, например 5655.")
        if not arg.isdigit():
            return send(chat, "Нужно число, например: /mmr 5655")
        old, new = st["mmr"], int(arg)
        st["mmr"] = new
        st["mmr_at"] = date
        if old is None or old == new:
            send(chat, f"💎 MMR установлен: {new}")
        else:
            diff = new - old
            st["history"].append({"t": date, "win": None, "d": diff, "adj": True})
            send(chat, f"MMR сверен: бот считал {old}, по факту {new} (поправка {sg(diff)}).")
    elif cmd == "/behavior":
        if not arg:
            st["pending"] = "behavior"
            return send(chat, "Отправь число — твою текущую порядочность, например 9800.")
        if not arg.isdigit():
            return send(chat, "Нужно число, например: /behavior 9800")
        old, new = st["behavior"], int(arg)
        st["behavior"] = new
        st["games_since_check"] = 0
        if old is None:
            send(chat, f"🧠 Порядочность сохранена: {new}")
        else:
            send(chat, f"Порядочность: {old} → {new} ({sg(new - old)})")
    elif cmd == "/friends":
        if not st["account_id"]:
            return send(chat, "Сначала привяжи аккаунт: /link <ID>")
        try:
            text_f = friends_report(st)
        except Exception as e:
            print("friends error:", e)
            return send(chat, "😕 OpenDota сейчас не отвечает, попробуй чуть позже.")
        send(chat, text_f, fancy=True)
    elif cmd == "/friend":
        if not arg.isdigit():
            return send(chat, "Отправь ID друга: /friend 123456789 — добавить или убрать из списка.\n"
                              "Если список пуст, показываю всех, с кем сыграно 5+ рейтинговых игр.")
        fid = int(arg)
        if fid in st["friends"]:
            st["friends"].remove(fid)
            send(chat, f"🗑 ID {fid} убран из друзей (в списке: {len(st['friends'])}).")
        else:
            st["friends"].append(fid)
            send(chat, f"✅ ID {fid} добавлен в друзья (в списке: {len(st['friends'])}).")
    elif cmd == "/goal":
        if not arg:
            st["pending"] = "goal"
            return send(chat, "Отправь число — желаемый рейтинг, например 6000. "
                              "Чтобы убрать цель, отправь 0.")
        if arg.lower() in ("0", "off", "выкл"):
            st["goal"] = None
            return send(chat, "Цель убрана.")
        if not arg.isdigit():
            return send(chat, "Нужно число, например: /goal 6000")
        st["goal"] = int(arg)
        now = f"Сейчас {st['mmr']}/{st['goal']}." if st["mmr"] is not None else "Задай рейтинг: /mmr"
        send(chat, f"🎯 Цель установлена: {st['goal']}. {now}")
    elif cmd == "/d":
        digits = arg.lstrip("+-")
        if not digits.isdigit():
            return send(chat, "Использование: /d 27")
        idx = next((i for i in range(len(st["history"]) - 1, -1, -1) if st["history"][i].get("r")), None)
        if idx is None:
            return send(chat, "Нет рейтинговой игры, которую можно исправить.")
        g, exact = apply_exact(st, idx, int(digits))
        send(chat, f"Последняя игра исправлена: {sg(exact)} MMR. "
                   f"Текущий MMR: {st['mmr'] if st['mmr'] is not None else '—'}")
    elif cmd in ("/5days", "/week"):
        send(chat, period_stats(st), fancy=True)
    elif cmd == "/stats":
        if not st["total_games"]:
            return send(chat, "Пока нет учтённых игр.")
        wr = st["wins"] / st["total_games"] * 100
        goal_line = ""
        if st["goal"] and st["mmr"] is not None:
            left = st["goal"] - st["mmr"]
            goal_line = (f"\n🎯 До цели {st['goal']}: {left} MMR" if left > 0
                         else f"\n🎉 Цель {st['goal']} достигнута!")
        send(chat, f"📈 Статистика рейтинга\n{DIV}\n"
                   f"🎮 Всего игр: {st['total_games']}\n"
                   f"🏆 Побед: {st['wins']} ({wr:.1f}%)\n"
                   f"💎 MMR: {st['mmr'] if st['mmr'] is not None else '—'}{goal_txt(st)}{goal_line}\n"
                   f"🧠 {beh_text(st)}\n\n" + period_stats(st), fancy=True)
    elif cmd == "/last":
        if not st["account_id"]:
            return send(chat, "Сначала привяжи аккаунт: /link <ID>")
        ms = od(f"/players/{st['account_id']}/recentMatches")
        if not ms:
            return send(chat, "Матчей не найдено.")
        mmr_line = f"MMR: {st['mmr']}{goal_txt(st)}" if st["mmr"] is not None else "MMR: не задан (/mmr)"
        send(chat, report(ms[0], mmr_line, beh_text(st), safe_analyze(ms[0])),
             fancy=True, match_id=ms[0]["match_id"])
    elif cmd == "/reset":
        st["history"] = []
        st["total_games"] = st["wins"] = st["games_since_check"] = 0
        st["mmr"] = st["behavior"] = None
        st["since"] = st["mmr_at"] = date
        if st["account_id"]:
            try:
                ms = od(f"/players/{st['account_id']}/recentMatches")
                st["last_match_id"] = max((m["match_id"] for m in ms), default=0)
            except Exception:
                pass
        send(chat, "Всё обнулено ✅ Теперь отправь актуальные /mmr и /behavior.")


# ---------- нажатия кнопок ----------
def handle_callback(st, cq):
    msg = cq.get("message") or {}
    chat = msg.get("chat", {}).get("id")

    def answer(text):
        try:
            tg("answerCallbackQuery", callback_query_id=cq["id"], text=text)
        except Exception:
            pass  # слишком старый запрос — не страшно

    if chat is None or chat != st["owner"]:
        return answer("Нет доступа")
    parts = (cq.get("data") or "").split(":")
    if len(parts) != 3 or parts[0] not in ("a", "d"):
        return answer("")
    match_id, value = int(parts[1]), int(parts[2])
    idx = next((i for i, g in enumerate(st["history"]) if g.get("id") == match_id), None)
    if idx is None:
        return answer("Игра уже не найдена в истории")

    g = st["history"][idx]
    if parts[0] == "a":   # нажат итоговый рейтинг
        if "b" not in g:
            return answer("Для этой игры используй /d")
        change = value - g["b"]
        if (g["win"] and change <= 0) or (not g["win"] and change >= 0):
            return answer("Не сходится с рейтингом до игры. Сверь рейтинг: /mmr")
        mag = abs(change)
    else:                 # старые кнопки с изменением
        mag = value
    g, exact = apply_exact(st, idx, mag)
    answer(f"Записано: {sg(exact)} MMR")

    lines = msg.get("text", "").split("\n")
    mark = "🟢" if exact > 0 else "🔴"
    new_line = (f"💎 MMR: {g['b']} → {g['m']}{goal_txt(st)} ({mark} {sg(exact)})" if "b" in g
                else f"💎 MMR: {g['m']}{goal_txt(st)} ({mark} {sg(exact)})")
    lines = [new_line if ln.startswith("💎 MMR:") else ln for ln in lines]
    try:
        tg("editMessageText", chat_id=chat, message_id=msg["message_id"],
           text=to_html("\n".join(lines), match_id), parse_mode="HTML",
           disable_web_page_preview=True)
    except Exception as e:
        print("edit error:", e)


# ---------- новые матчи ----------
def check_matches(st):
    ms = od(f"/players/{st['account_id']}/recentMatches")
    new = sorted((m for m in ms if m["match_id"] > st["last_match_id"]), key=lambda m: m["match_id"])
    for m in new:
        st["last_match_id"] = m["match_id"]
        end = m["start_time"] + m["duration"]
        if end <= st["since"]:
            continue  # игра закончилась до привязки — не учитываем

        win = is_win(m)
        ranked = m["lobby_type"] == RANKED_LOBBY
        delta = 0
        markup = None
        if st["mmr"] is None:
            mmr_line = "MMR: не задан (/mmr)"
        elif ranked and end > st["mmr_at"]:
            before = st["mmr"]
            delta = MMR_STEP if win else -MMR_STEP
            st["mmr"] += delta
            mmr_line = f"MMR: {before} → ≈{st['mmr']}{goal_txt(st)} (выбери точный итог ⬇️)"
            markup = keyboard(m["match_id"], win, before)
        elif ranked:
            mmr_line = f"MMR: {st['mmr']}{goal_txt(st)} (игра закончилась до ввода рейтинга)"
        else:
            mmr_line = f"MMR: {st['mmr']}{goal_txt(st)} (не рейтинг — в статистику не входит)"

        st["games_since_check"] += 1   # порядочность считается по всем играм
        if ranked:                     # в статистику и винрейт идут только рейтинговые
            st["total_games"] += 1
            st["wins"] += int(win)
            entry = {"id": m["match_id"], "t": m["start_time"], "win": win, "d": delta, "r": delta != 0}
            if delta != 0:
                entry["m"] = st["mmr"]
                entry["b"] = st["mmr"] - delta
            st["history"].append(entry)
            cutoff = time.time() - 30 * 86400
            st["history"] = [g for g in st["history"] if g["t"] >= cutoff]

        send(st["owner"], report(m, mmr_line, beh_text(st), safe_analyze(m)), markup,
             fancy=True, match_id=m["match_id"])

        if st["games_since_check"] >= BEHAVIOR_EVERY:
            send(st["owner"], f"🔔 Сыграно {BEHAVIOR_EVERY} игр. Зайди в Dota 2, посмотри "
                              "порядочность и отправь: /behavior <число>")
            st["games_since_check"] = 0


def main():
    st = load()
    before = json.dumps(st, sort_keys=True)
    try:
        for u in tg("getUpdates", offset=st["offset"], timeout=0)["result"]:
            st["offset"] = u["update_id"] + 1
            try:
                if u.get("message") and "text" in u["message"]:
                    handle_message(st, u["message"])
                elif u.get("callback_query"):
                    handle_callback(st, u["callback_query"])
            except Exception as e:
                print("update error:", e)
        if st["owner"] and st["account_id"]:
            try:
                check_matches(st)
            except Exception as e:
                print("matches error:", e)
    finally:
        if json.dumps(st, sort_keys=True) != before or not os.path.exists(STATE_FILE):
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
