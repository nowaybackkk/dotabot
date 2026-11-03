"""
Dota 2 -> Telegram бот для запуска по расписанию (GitHub Actions).
Каждый запуск: читает команды, проверяет новые матчи, шлёт отчёты, сохраняет state.json.
Только стандартная библиотека Python, ничего устанавливать не нужно.
"""
import json
import os
import time
import urllib.request

TOKEN = os.environ["BOT_TOKEN"]
OPENDOTA = "https://api.opendota.com/api"
STATE_FILE = "state.json"
BEHAVIOR_EVERY = 15
MMR_STEP = 25
RANKED_LOBBY = 7

DEFAULT = {
    "owner": None, "account_id": None, "mmr": None, "behavior": None,
    "last_match_id": 0, "games_since_check": 0, "total_games": 0,
    "wins": 0, "offset": 0, "history": [],
}
STATS_DAYS = 5


def http(url, data=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json", "User-Agent": "dota-bot"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def tg(method, **params):
    return http(f"https://api.telegram.org/bot{TOKEN}/{method}", params)


def send(chat, text):
    tg("sendMessage", chat_id=chat, text=text, disable_web_page_preview=True)


def od(path):
    return http(f"{OPENDOTA}{path}")


def load():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = {**DEFAULT, **json.load(f)}
    except FileNotFoundError:
        st = dict(DEFAULT)
    st["history"] = list(st["history"])
    return st


def period_stats(st, days=STATS_DAYS):
    since = time.time() - days * 86400
    window = [g for g in st["history"] if g["t"] >= since]
    games = [g for g in window if g["win"] is not None]  # поправки MMR — не игры
    if not games:
        return f"За последние {days} дней игр нет."
    wins = sum(1 for g in games if g["win"])
    losses = len(games) - wins
    delta = sum(g["d"] for g in window)
    wr = wins / len(games) * 100
    return (
        f"📊 Статистика за {days} дней\n"
        f"Игр: {len(games)}\n"
        f"Победы: {wins} | Поражения: {losses} ({wr:.0f}% побед)\n"
        f"MMR: {'+' if delta >= 0 else ''}{delta} (приблизительно)"
    )


def is_win(m):
    return (m["player_slot"] < 128) == m["radiant_win"]


def report(m, heroes, mmr_line, beh_line):
    hero = heroes.get(m["hero_id"], f"hero {m['hero_id']}")
    mins, secs = divmod(m["duration"], 60)
    return (
        f"{'🏆 ПОБЕДА' if is_win(m) else '💀 ПОРАЖЕНИЕ'}\n"
        f"Герой: {hero}\n"
        f"KDA: {m['kills']}/{m['deaths']}/{m['assists']}\n"
        f"Длительность: {mins}:{secs:02d}\n"
        f"{mmr_line}\n{beh_line}\n"
        f"https://www.opendota.com/matches/{m['match_id']}"
    )


def beh_text(st):
    return f"Порядочность: {st['behavior']}" if st["behavior"] is not None else "Порядочность: не задана (/behavior)"


def handle(st, msg, heroes):
    chat = msg["chat"]["id"]
    text = msg["text"].strip()

    # Владелец — тот, кто первым написал /start. Остальных бот игнорирует.
    if st["owner"] is None and text.startswith("/start"):
        st["owner"] = chat
    if chat != st["owner"]:
        return

    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    arg = arg.strip()

    if cmd == "/start":
        send(chat, "Привет! Настрой меня:\n"
                   "/link <ID> — твой ID с opendota.com\n"
                   "/mmr <число> — сверить рейтинг с реальным\n"
                   "/d <число> — точное изменение MMR за последнюю игру\n"
                   "/behavior <число> — текущая порядочность\n"
                   "/last — последняя игра\n/5days — статистика за 5 дней\n"
                   "/stats — общая статистика\n\n"
                   "Я проверяю матчи примерно раз в 10 минут.")
    elif cmd == "/link":
        if not arg.isdigit():
            return send(chat, "Использование: /link 123456789")
        try:
            ms = od(f"/players/{arg}/recentMatches")
        except Exception:
            return send(chat, "Не удалось получить данные. Проверь ID и открытость данных матчей в Dota 2.")
        st["account_id"] = int(arg)
        st["last_match_id"] = ms[0]["match_id"] if ms else 0
        send(chat, "Аккаунт привязан ✅ Отчёты придут после следующей игры.")
    elif cmd == "/mmr":
        if not arg.isdigit():
            return send(chat, "Использование: /mmr 3500")
        old, new = st["mmr"], int(arg)
        st["mmr"] = new
        if old is None or old == new:
            send(chat, f"MMR установлен: {new}")
        else:
            diff = new - old
            # поправка попадает в статистику, чтобы сумма за 5 дней совпала с реальностью
            st["history"].append({"t": int(time.time()), "win": None, "d": diff, "adj": True})
            send(chat, f"MMR сверен: бот считал {old}, по факту {new} "
                       f"(поправка {'+' if diff > 0 else ''}{diff}). Статистика обновлена.")
    elif cmd == "/d":
        digits = arg.lstrip("+-")
        if not digits.isdigit():
            return send(chat, "Использование: /d 27 — сколько MMR дали или забрали за последнюю игру")
        last = next((g for g in reversed(st["history"]) if g.get("r")), None)
        if last is None:
            return send(chat, "Нет рейтинговой игры, которую можно исправить.")
        exact = int(digits) if last["win"] else -int(digits)
        diff = exact - last["d"]
        last["d"] = exact
        if st["mmr"] is not None:
            st["mmr"] += diff
        send(chat, f"Последняя игра исправлена: {'+' if exact > 0 else ''}{exact} MMR. "
                   f"Текущий MMR: {st['mmr'] if st['mmr'] is not None else '—'}")
    elif cmd == "/behavior":
        if not arg.isdigit():
            return send(chat, "Использование: /behavior 9800")
        old, new = st["behavior"], int(arg)
        st["behavior"] = new
        st["games_since_check"] = 0
        if old is None:
            send(chat, f"Порядочность сохранена: {new}")
        else:
            d = new - old
            send(chat, f"Порядочность: {old} → {new} ({'+' if d >= 0 else ''}{d})")
    elif cmd in ("/5days", "/week"):
        send(chat, period_stats(st))
    elif cmd == "/stats":
        if not st["total_games"]:
            return send(chat, "Пока нет учтённых игр.")
        wr = st["wins"] / st["total_games"] * 100
        send(chat, f"Всего игр: {st['total_games']}\nПобед: {st['wins']} ({wr:.1f}%)\n"
                   f"MMR: {st['mmr'] if st['mmr'] is not None else '—'}\n{beh_text(st)}\n\n"
                   + period_stats(st))
    elif cmd == "/last":
        if not st["account_id"]:
            return send(chat, "Сначала привяжи аккаунт: /link <ID>")
        ms = od(f"/players/{st['account_id']}/recentMatches")
        if not ms:
            return send(chat, "Матчей не найдено.")
        mmr_line = f"MMR: {st['mmr']}" if st["mmr"] is not None else "MMR: не задан (/mmr)"
        send(chat, report(ms[0], heroes, mmr_line, beh_text(st)))


def check_matches(st, heroes):
    ms = od(f"/players/{st['account_id']}/recentMatches")
    new = sorted((m for m in ms if m["match_id"] > st["last_match_id"]), key=lambda m: m["match_id"])
    for m in new:
        win = is_win(m)
        delta = 0
        if m["lobby_type"] == RANKED_LOBBY and st["mmr"] is not None:
            delta = MMR_STEP if win else -MMR_STEP
            st["mmr"] += delta
            mmr_line = f"MMR: {st['mmr']} ({'+' if delta > 0 else ''}{delta}, примерно; точнее: /d число)"
        elif st["mmr"] is not None:
            mmr_line = f"MMR: {st['mmr']} (нерейтинговая игра)"
        else:
            mmr_line = "MMR: не задан (/mmr)"

        st["last_match_id"] = m["match_id"]
        st["total_games"] += 1
        st["wins"] += int(win)
        st["games_since_check"] += 1
        st["history"].append({"t": m.get("start_time", int(time.time())), "win": win, "d": delta, "r": delta != 0})
        cutoff = time.time() - 30 * 86400
        st["history"] = [g for g in st["history"] if g["t"] >= cutoff]
        send(st["owner"], report(m, heroes, mmr_line, beh_text(st)))

        if st["games_since_check"] >= BEHAVIOR_EVERY:
            send(st["owner"], f"🔔 Сыграно {BEHAVIOR_EVERY} игр. Зайди в Dota 2, посмотри "
                              "порядочность и отправь: /behavior <число>")
            st["games_since_check"] = 0


def main():
    st = load()
    before = json.dumps(st, sort_keys=True)
    try:
        try:
            heroes = {h["id"]: h["localized_name"] for h in od("/heroes")}
        except Exception:
            heroes = {}
        for u in tg("getUpdates", offset=st["offset"], timeout=0)["result"]:
            st["offset"] = u["update_id"] + 1
            msg = u.get("message")
            if msg and "text" in msg:
                try:
                    handle(st, msg, heroes)
                except Exception as e:
                    print("command error:", e)
        if st["owner"] and st["account_id"]:
            check_matches(st, heroes)
    finally:
        if json.dumps(st, sort_keys=True) != before or not os.path.exists(STATE_FILE):
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
