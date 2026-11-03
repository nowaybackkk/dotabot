"""
Dota 2 -> Telegram бот для запуска по расписанию (GitHub Actions).
Каждый запуск: читает команды и нажатия кнопок, проверяет новые матчи,
шлёт отчёты, сохраняет state.json. Только стандартная библиотека Python.
"""
import json
import os
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
    "pending": None,  # бот ждёт число после нажатия кнопки меню (mmr / behavior / link)
}

# Постоянная панель кнопок под полем ввода
MENU = {
    "keyboard": [
        ["🎮 Последняя игра", "📊 За 5 дней"],
        ["📈 Вся статистика", "🔄 Сверить MMR"],
        ["🧠 Порядочность", "🎯 Цель", "❓ Помощь"],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}
LABELS = {
    "🎮 Последняя игра": "/last",
    "📊 За 5 дней": "/5days",
    "📈 Вся статистика": "/stats",
    "🔄 Сверить MMR": "/mmr",
    "🧠 Порядочность": "/behavior",
    "🎯 Цель": "/goal",
    "❓ Помощь": "/start",
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


def send(chat, text, markup=None):
    p = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
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
    return st


def sg(n):
    return f"+{n}" if n > 0 else str(n)


def goal_txt(st):
    return f"/{st['goal']}" if st.get("goal") else ""


def is_win(m):
    return (m["player_slot"] < 128) == m["radiant_win"]


def beh_text(st):
    return f"Порядочность: {st['behavior']}" if st["behavior"] is not None else "Порядочность: не задана (/behavior)"


def report(m, mmr_line, beh_line):
    hero = heroes().get(m["hero_id"], f"hero {m['hero_id']}")
    mins, secs = divmod(m["duration"], 60)
    return (
        f"{'🏆 ПОБЕДА' if is_win(m) else '💀 ПОРАЖЕНИЕ'}\n"
        f"Герой: {hero}\n"
        f"KDA: {m['kills']}/{m['deaths']}/{m['assists']}\n"
        f"GPM: {m.get('gold_per_min', '—')}\n"
        f"Длительность: {mins}:{secs:02d}\n"
        f"{mmr_line}\n{beh_line}\n"
        f"https://www.opendota.com/matches/{m['match_id']}"
    )


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
        f"MMR: {sg(delta)}"
    )


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
                   "/reset — обнулить статистику.")
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
            send(chat, f"MMR установлен: {new}")
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
            send(chat, f"Порядочность сохранена: {new}")
        else:
            send(chat, f"Порядочность: {old} → {new} ({sg(new - old)})")
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
        send(chat, f"Цель установлена: {st['goal']}. {now}")
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
        send(chat, period_stats(st))
    elif cmd == "/stats":
        if not st["total_games"]:
            return send(chat, "Пока нет учтённых игр.")
        wr = st["wins"] / st["total_games"] * 100
        goal_line = ""
        if st["goal"] and st["mmr"] is not None:
            left = st["goal"] - st["mmr"]
            goal_line = (f"\nДо цели {st['goal']}: {left} MMR" if left > 0
                         else f"\n🎉 Цель {st['goal']} достигнута!")
        send(chat, f"Всего игр: {st['total_games']}\nПобед: {st['wins']} ({wr:.1f}%)\n"
                   f"MMR: {st['mmr'] if st['mmr'] is not None else '—'}{goal_txt(st)}{goal_line}\n"
                   f"{beh_text(st)}\n\n" + period_stats(st))
    elif cmd == "/last":
        if not st["account_id"]:
            return send(chat, "Сначала привяжи аккаунт: /link <ID>")
        ms = od(f"/players/{st['account_id']}/recentMatches")
        if not ms:
            return send(chat, "Матчей не найдено.")
        mmr_line = f"MMR: {st['mmr']}{goal_txt(st)}" if st["mmr"] is not None else "MMR: не задан (/mmr)"
        send(chat, report(ms[0], mmr_line, beh_text(st)))
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
    new_line = (f"MMR: {g['b']} → {g['m']}{goal_txt(st)} ({sg(exact)} ✅)" if "b" in g
                else f"MMR: {g['m']}{goal_txt(st)} ({sg(exact)} ✅)")
    lines = [new_line if ln.startswith("MMR:") else ln for ln in lines]
    try:
        tg("editMessageText", chat_id=chat, message_id=msg["message_id"],
           text="\n".join(lines), disable_web_page_preview=True)
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
            mmr_line = f"MMR: {st['mmr']}{goal_txt(st)} (нерейтинговая игра)"

        st["total_games"] += 1
        st["wins"] += int(win)
        st["games_since_check"] += 1
        entry = {"id": m["match_id"], "t": m["start_time"], "win": win, "d": delta, "r": delta != 0}
        if delta != 0:
            entry["m"] = st["mmr"]
            entry["b"] = st["mmr"] - delta
        st["history"].append(entry)
        cutoff = time.time() - 30 * 86400
        st["history"] = [g for g in st["history"] if g["t"] >= cutoff]

        send(st["owner"], report(m, mmr_line, beh_text(st)), markup)

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
