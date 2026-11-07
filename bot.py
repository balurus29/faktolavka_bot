#!/usr/bin/env python3
"""
Автопостинг для Telegram-канала с интересными фактами.

Один запуск = один пост. Расписание задаётся в .github/workflows/autopost.yml.

Цепочка: Википедия (факт) -> бесплатная нейросеть (GigaChat, Gemini или Groq) пишет пост
-> картинки и видео из нескольких бесплатных источников (см. media.py) -> публикация в канал
-> иногда викторина по теме поста.

Запуск:
    python bot.py                 # обычный пост, формат выбирается случайно
    python bot.py --type video    # photo | album | video | mixed | text
    python bot.py --dry-run       # ничего не публикует, только печатает пост
"""
import argparse
import html
import json
import os
import pathlib
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

import media

# ---------------------------------------------------------------- настройки
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHANNEL = os.getenv("TELEGRAM_CHANNEL", "")            # @имя_канала или -100...
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "") or "gemini-flash-latest"
GROQ_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "") or "openai/gpt-oss-120b"
GIGACHAT_KEY = os.getenv("GIGACHAT_AUTH_KEY", "")        # Authorization key из кабинета Сбера
GIGACHAT_MODEL = os.getenv("GIGACHAT_MODEL", "") or "GigaChat-2-Max"
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "") or "GIGACHAT_API_PERS"
# Серверы Сбера подписаны российским сертификатом (НУЦ Минцифры), его добавляет workflow.
GIGACHAT_CA = os.getenv("GIGACHAT_CA_BUNDLE", "")
SIGNATURE = os.getenv("CHANNEL_SIGNATURE", "")         # например «👉 @my_facts»
WIKI_LANG = os.getenv("WIKI_LANG", "") or "ru"

# Доли форматов постов (в процентах). Меняйте как хотите.
POST_TYPE_WEIGHTS = {"photo": 45, "album": 20, "video": 20, "mixed": 15}
QUIZ_CHANCE = 0.3          # вероятность добавить викторину после поста
ONTHISDAY_CHANCE = 0.4     # доля постов «В этот день», остальное — хорошие статьи Википедии

# Темы, которые не подходят развлекательному каналу.
STOP_WORDS = ("теракт", "убит", "расстрел", "погибл", "катастроф", "массов", "казн",
              "геноцид", "самоубий", "войн", "бомбард", "резн")

STATE_FILE = pathlib.Path(__file__).with_name("state.json")
UA = "FactsChannelAutopost/1.0 (Telegram channel bot; python-requests)"
MSK = timezone(timedelta(hours=3))
CAPTION_LIMIT = 1024       # лимит подписи к медиа в Telegram

session = requests.Session()
session.headers["User-Agent"] = UA


def log(*args):
    print(datetime.now(MSK).strftime("%H:%M:%S"), *args, flush=True)


media.setup(session, log)


# ---------------------------------------------------------------- состояние
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text("utf-8"))
        except json.JSONDecodeError:
            pass
    return {"used_sources": [], "used_media": [], "posts": 0}


def save_state(state):
    state["used_sources"] = state["used_sources"][-3000:]
    state["used_media"] = state["used_media"][-3000:]
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")


# ---------------------------------------------------------------- Википедия
def wiki_api(params):
    params = {**params, "format": "json", "formatversion": 2}
    r = session.get(f"https://{WIKI_LANG}.wikipedia.org/w/api.php", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def wiki_article(title):
    """Текст статьи, главная картинка и ссылка."""
    data = wiki_api({
        "action": "query", "titles": title, "redirects": 1,
        "prop": "extracts|pageimages|info", "explaintext": 1, "exchars": 5000,
        "piprop": "thumbnail", "pithumbsize": 1280, "inprop": "url",
    })
    pages = data.get("query", {}).get("pages", [])
    if not pages or pages[0].get("missing"):
        return None
    p = pages[0]
    text = re.sub(r"\n{2,}", "\n", p.get("extract", "")).strip()
    return {
        "title": p["title"],
        "text": text,
        "url": p.get("fullurl", f"https://{WIKI_LANG}.wikipedia.org/wiki/{p['title']}"),
        "image": (p.get("thumbnail") or {}).get("source"),
    }


def is_light(text):
    low = text.lower()
    return not any(w in low for w in STOP_WORDS)


def source_good_article(state):
    """Случайная избранная или хорошая статья — в них проверенные интересные факты."""
    category = random.choice([
        "Категория:Википедия:Избранные статьи по алфавиту",
        "Категория:Википедия:Хорошие статьи по алфавиту",
    ])
    prefix = random.choice("АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШЭЮЯ")
    data = wiki_api({
        "action": "query", "list": "categorymembers", "cmtitle": category,
        "cmnamespace": 0, "cmlimit": 50, "cmstartsortkeyprefix": prefix,
    })
    titles = [m["title"] for m in data.get("query", {}).get("categorymembers", [])]
    random.shuffle(titles)
    for title in titles[:10]:
        sid = f"wiki:{title}"
        if sid in state["used_sources"]:
            continue
        art = wiki_article(title)
        if art and len(art["text"]) > 800 and is_light(art["text"][:1500]):
            return {"id": sid, "kind": "интересная статья", "material": art["text"], **art}
    return None


def source_on_this_day(state):
    """Событие, которое произошло в этот день в истории."""
    now = datetime.now(MSK)
    url = (f"https://{WIKI_LANG}.wikipedia.org/api/rest_v1/feed/onthisday/"
           f"selected/{now:%m}/{now:%d}")
    r = session.get(url, timeout=30)
    if r.status_code != 200:
        return None
    events = r.json().get("selected", [])
    random.shuffle(events)
    for ev in events:
        text, year = ev.get("text", ""), ev.get("year")
        pages = ev.get("pages") or []
        if not pages or not is_light(text):
            continue
        sid = f"otd:{year}:{text[:60]}"
        if sid in state["used_sources"]:
            continue
        art = wiki_article(pages[0].get("title", ""))
        if not art or len(art["text"]) < 400:
            continue
        material = (f"В этот день, {now.day} {month_ru(now.month)} {year} года: {text}\n\n"
                    f"Подробнее о теме:\n{art['text']}")
        return {"id": sid, "kind": f"событие этого дня в истории ({year} год)",
                "material": material, **art}
    return None


def month_ru(m):
    return ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
            "сентября", "октября", "ноября", "декабря"][m - 1]


def pick_source(state):
    order = [source_on_this_day, source_good_article]
    if random.random() > ONTHISDAY_CHANCE:
        order.reverse()
    for attempt in range(4):
        for fn in order:
            try:
                src = fn(state)
                if src:
                    return src
            except requests.RequestException as e:
                log(f"Источник {fn.__name__} недоступен: {e}")
        time.sleep(2)
    raise RuntimeError("Не удалось найти материал для поста")


# ---------------------------------------------------------------- нейросеть
PROMPT = """Ты автор популярного Telegram-канала с интересными фактами. Тон живой и лёгкий, с интригой, без канцелярита и без лживого кликбейта.

Напиши пост по материалу ниже.
Правила:
- Используй ТОЛЬКО факты из материала. Не выдумывай цифры, даты, имена и подробности.
- Найди в материале самый неожиданный или удивительный факт и построй пост вокруг него.
- title: цепляющий заголовок до 70 символов, можно с одним эмодзи в начале.
- text: 2-4 коротких абзаца, всего 350-650 символов, абзацы разделяй пустой строкой. 1-3 эмодзи на весь текст. Закончи коротким вопросом к читателям или неожиданным выводом.
- hashtags: 2-3 тега на русском, одним словом каждый.
- media_queries: 3 коротких запроса НА АНГЛИЙСКОМ для фотостока: что конкретно показать (предметы, места, природа, животные), без имён людей.
- video_query: 1 короткий запрос на английском для атмосферного видео по теме.
- category: тема поста, одно слово из списка: space, history, art, nature, science, other.
- image_prompt: описание иллюстрации к посту на английском, 1 предложение, без текста на картинке.
- quiz: викторина по фактам из поста. question до 200 символов, options ровно 4 варианта до 90 символов каждый, correct — индекс правильного (0-3), explanation до 180 символов.

Ответь строго одним JSON-объектом:
{{"title": "...", "text": "...", "hashtags": ["..."], "media_queries": ["..."], "video_query": "...", "category": "...", "image_prompt": "...", "quiz": {{"question": "...", "options": ["...", "...", "...", "..."], "correct": 0, "explanation": "..."}}}}

Материал ({kind}, статья «{title}»):
{material}
"""


def parse_json(raw):
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("в ответе нет JSON")
    return json.loads(raw[start:end + 1])


def ask_gemini(prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.9, "responseMimeType": "application/json"},
    }
    r = session.post(url, json=body, headers={"x-goog-api-key": GEMINI_KEY}, timeout=90)
    r.raise_for_status()
    parts = r.json()["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts)


def ask_gigachat(prompt):
    import uuid
    verify = GIGACHAT_CA if GIGACHAT_CA and os.path.exists(GIGACHAT_CA) else True
    tok = session.post(
        "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
        headers={"Authorization": f"Basic {GIGACHAT_KEY}", "RqUID": str(uuid.uuid4()),
                 "Accept": "application/json"},
        data={"scope": GIGACHAT_SCOPE}, timeout=30, verify=verify)
    tok.raise_for_status()
    token = tok.json()["access_token"]
    for model in dict.fromkeys([GIGACHAT_MODEL, "GigaChat-2"]):   # кончились токены Max — берём Lite
        r = session.post(
            "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {token}"},
            json={"model": model, "temperature": 0.9,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=120, verify=verify)
        if r.status_code == 402 and model != "GigaChat-2":
            log(f"GigaChat: закончились токены {model}, переключаюсь на GigaChat-2")
            continue
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    raise ValueError("GigaChat не ответил")


def ask_groq(prompt):
    r = session.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_KEY}"},
        json={"model": GROQ_MODEL, "temperature": 0.9,
              "response_format": {"type": "json_object"},
              "messages": [{"role": "user", "content": prompt}]},
        timeout=90,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def validate_post(p):
    if not isinstance(p.get("title"), str) or not isinstance(p.get("text"), str):
        raise ValueError("нет заголовка или текста")
    if len(p["text"]) < 150:
        raise ValueError("слишком короткий текст")
    p["hashtags"] = [t for t in p.get("hashtags", []) if isinstance(t, str)][:3]
    p["media_queries"] = [q for q in p.get("media_queries", []) if isinstance(q, str)][:4]
    p["video_query"] = p.get("video_query") if isinstance(p.get("video_query"), str) else ""
    p["category"] = p.get("category") if p.get("category") in media.CATEGORIES else "other"
    p["image_prompt"] = p.get("image_prompt") if isinstance(p.get("image_prompt"), str) else ""
    q = p.get("quiz")
    ok_quiz = (isinstance(q, dict) and isinstance(q.get("question"), str)
               and isinstance(q.get("options"), list) and len(q["options"]) == 4
               and all(isinstance(o, str) and o.strip() for o in q["options"])
               and isinstance(q.get("correct"), int) and 0 <= q["correct"] <= 3)
    p["quiz"] = q if ok_quiz else None
    return p


def write_post(src):
    prompt = PROMPT.format(kind=src["kind"], title=src["title"], material=src["material"][:6000])
    providers = []
    if GIGACHAT_KEY:
        providers.append(("GigaChat", ask_gigachat))
    if GEMINI_KEY:
        providers.append(("Gemini", ask_gemini))
    if GROQ_KEY:
        providers.append(("Groq", ask_groq))
    for name, fn in providers:
        for attempt in range(3):
            try:
                post = validate_post(parse_json(fn(prompt)))
                log(f"Текст написан: {name}")
                return post
            except requests.HTTPError as e:
                code = e.response.status_code if e.response is not None else "?"
                log(f"{name}: ошибка HTTP {code}")
                if code == 429:          # лимит — подождать и повторить
                    time.sleep(20 * (attempt + 1))
                    continue
                break
            except (ValueError, KeyError, IndexError, requests.RequestException) as e:
                log(f"{name}: {e}")
                time.sleep(3)
    log("Нейросети недоступны — публикую выдержку из статьи как есть")
    return fallback_post(src)


def fallback_post(src):
    paras = [s for s in src["text"].split("\n") if len(s) > 80][:2]
    return {"title": f"📚 {src['title']}", "text": "\n\n".join(paras),
            "hashtags": ["интересное", "факты"], "media_queries": [], "video_query": "", "category": "other", "image_prompt": "",
            "quiz": None}


# ---------------------------------------------------------------- подпись
def trim(text, limit):
    if len(text) <= limit:
        return text
    cut = text[:limit]
    idx = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind(".\n"))
    if idx > limit * 0.5:
        return cut[:idx + 1]
    return cut.rsplit(" ", 1)[0].rstrip(",;:—-") + "…"


def media_credits(photos, video):
    """Подпись авторов медиа без повторов, «Википедия» уже указана как источник."""
    out = []
    for m in photos + ([video] if video else []):
        c = m.get("credit")
        if c and c != "Википедия" and c not in out:
            out.append(c)
    return out


def build_caption(post, src, credits, limit=CAPTION_LIMIT):
    tags = " ".join("#" + re.sub(r"[^\w]", "", t.lstrip("#")) for t in post["hashtags"])
    tags = " ".join(t for t in tags.split() if len(t) > 1)
    credit = "Источник: Википедия" + (f" · Медиа: {', '.join(credits)}" if credits else "")
    if len(credit) > 160:                        # слишком длинный список авторов
        credits = credits[:2]
        credit = "Источник: Википедия · Медиа: " + ", ".join(credits)
    footer_plain = "\n\n".join(x for x in [tags, credit, SIGNATURE] if x)
    title = trim(post["title"].strip(), 100)
    budget = limit - len(title) - len(footer_plain) - 6
    body = trim(post["text"].strip(), max(budget, 100))

    footer_html = []
    if tags:
        footer_html.append(html.escape(tags))
    credit_html = f'Источник: <a href="{html.escape(src["url"], quote=True)}">Википедия</a>'
    if credits:
        credit_html += " · Медиа: " + html.escape(", ".join(credits))
    footer_html.append(credit_html)
    if SIGNATURE:
        footer_html.append(html.escape(SIGNATURE))
    return f"<b>{html.escape(title)}</b>\n\n{html.escape(body)}\n\n" + "\n\n".join(footer_html)


# ---------------------------------------------------------------- Telegram
def tg(method, data=None, files=None):
    r = session.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}",
                     data=data, files=files, timeout=180)
    res = r.json()
    if not res.get("ok"):
        raise RuntimeError(f"Telegram {method}: {res.get('description')}")
    return res["result"]


def send_post(kind, caption, photos, video):
    base = {"chat_id": CHANNEL, "parse_mode": "HTML"}
    if kind == "text" or (not photos and not video):
        tg("sendMessage", {**base, "text": caption,
                           "link_preview_options": json.dumps({"is_disabled": True})})
    elif kind == "video" or (video and not photos):
        extra = {k: video[k] for k in ("width", "height", "duration") if video.get(k)}
        tg("sendVideo", {**base, "caption": caption, "supports_streaming": "true", **extra},
           files={"video": (video["name"], video["data"], "video/mp4")})
    elif len(photos) == 1 and not video:
        tg("sendPhoto", {**base, "caption": caption},
           files={"photo": (photos[0]["name"], photos[0]["data"], "image/jpeg")})
    else:
        items = [("photo", p) for p in photos] + ([("video", video)] if video else [])
        media, files = [], {}
        for i, (t, item) in enumerate(items[:10]):
            key = f"f{i}"
            entry = {"type": t, "media": f"attach://{key}"}
            if i == 0:
                entry.update(caption=caption, parse_mode="HTML")
            if t == "video":
                entry["supports_streaming"] = True
            media.append(entry)
            files[key] = (item["name"], item["data"], "video/mp4" if t == "video" else "image/jpeg")
        tg("sendMediaGroup", {"chat_id": CHANNEL, "media": json.dumps(media, ensure_ascii=False)},
           files=files)


QUIZ_COVER = pathlib.Path(__file__).with_name("assets") / "quiz-cover.jpg"


def send_quiz(quiz):
    if QUIZ_COVER.exists():                       # обложка с совой — узнаваемый стиль
        tg("sendPhoto", {"chat_id": CHANNEL, "disable_notification": "true"},
           files={"photo": ("quiz.jpg", QUIZ_COVER.read_bytes(), "image/jpeg")})
        time.sleep(2)
    options = [o.strip()[:100] for o in quiz["options"]]
    correct = options[quiz["correct"]]
    random.shuffle(options)                       # нейросети любят ставить ответ первым
    data = {
        "chat_id": CHANNEL, "type": "quiz", "is_anonymous": "true",
        "question": "🧩 " + quiz["question"].strip()[:290],
        "options": json.dumps([{"text": o} for o in options], ensure_ascii=False),
        "correct_option_id": options.index(correct),
    }
    if isinstance(quiz.get("explanation"), str) and quiz["explanation"].strip():
        data["explanation"] = quiz["explanation"].strip()[:200]
    tg("sendPoll", data)


# ---------------------------------------------------------------- главное
def choose_type(forced):
    if forced and forced != "auto":
        return forced
    kinds, weights = zip(*POST_TYPE_WEIGHTS.items())
    return random.choices(kinds, weights)[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", default="auto",
                    choices=["auto", "photo", "album", "video", "mixed", "text"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-quiz", action="store_true")
    args = ap.parse_args()

    if not args.dry_run and not (TG_TOKEN and CHANNEL):
        sys.exit("Не заданы TELEGRAM_BOT_TOKEN и TELEGRAM_CHANNEL")
    if not (GIGACHAT_KEY or GEMINI_KEY or GROQ_KEY):
        log("Внимание: нет ключей нейросетей, посты будут без обработки")

    state = load_state()
    kind = choose_type(args.type)
    src = pick_source(state)
    log(f"Формат: {kind}. Материал: {src['title']}")
    post = write_post(src)

    photos, video = [], None
    if kind in ("photo", "album", "mixed"):
        photos = media.collect_photos(src, post, state, {"photo": 1, "album": 3, "mixed": 2}[kind])
    if kind in ("video", "mixed"):
        video = media.collect_video(post, state)
        if not video and kind == "video":           # видео не нашлось — хотя бы фото
            photos = media.collect_photos(src, post, state, 1)

    caption = build_caption(post, src, media_credits(photos, video),
                            limit=4000 if not photos and not video else CAPTION_LIMIT)

    if args.dry_run:
        print("\n" + "=" * 60 + f"\nФормат: {kind} | тема: {post['category']} | фото: {len(photos)} | видео: {bool(video)}")
        print(caption)
        if post["quiz"]:
            print("\nВикторина:", json.dumps(post["quiz"], ensure_ascii=False, indent=1))
        return

    send_post(kind, caption, photos, video)
    log("Пост опубликован")
    if post["quiz"] and not args.no_quiz and random.random() < QUIZ_CHANCE:
        time.sleep(5)
        try:
            send_quiz(post["quiz"])
            log("Викторина опубликована")
        except RuntimeError as e:
            log(f"Викторина не отправлена: {e}")

    state["used_sources"].append(src["id"])
    state["used_media"] += [p["id"] for p in photos] + ([video["id"]] if video else [])
    state["posts"] = state.get("posts", 0) + 1
    state["last_post"] = datetime.now(MSK).isoformat(timespec="minutes")
    save_state(state)


if __name__ == "__main__":
    main()
