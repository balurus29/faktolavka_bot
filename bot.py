#!/usr/bin/env python3
"""
Автопостинг для Telegram-канала с интересными фактами.

Один запуск = один пост. Расписание задаётся в .github/workflows/autopost.yml.

Цепочка: источник фактов (Википедия, научпоп-сайты, NASA — см. sources.py) -> бесплатная нейросеть (GigaChat, Gemini или Groq) пишет пост
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
import sources

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

# Доли форматов постов (в процентах). Меняйте как хотите.
POST_TYPE_WEIGHTS = {"photo": 55, "album": 15, "video": 15, "mixed": 15}
QUIZ_CHANCE = 0.3          # вероятность добавить викторину после поста
# Публиковать ли выдержку из статьи без обработки, если нейросеть недоступна. По умолчанию нет:
# лучше пропустить запуск, чем выложить сырой текст.
ALLOW_RAW_POSTS = os.getenv("ALLOW_RAW_POSTS", "") == "1"

# Источники фактов, их доли и фильтр тем — в sources.py, источники картинок — в media.py.

STATE_FILE = pathlib.Path(__file__).with_name("state.json")
UA = "FactsChannelAutopost/1.0 (Telegram channel bot; python-requests)"
MSK = timezone(timedelta(hours=3))
CAPTION_LIMIT = 1024       # лимит подписи к медиа в Telegram

session = requests.Session()
session.headers["User-Agent"] = UA


def log(*args):
    print(datetime.now(MSK).strftime("%H:%M:%S"), *args, flush=True)


media.setup(session, log)
sources.setup(session, log)


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


# ---------------------------------------------------------------- нейросеть
PROMPT = """Ты автор популярного Telegram-канала с интересными фактами. Тон живой и лёгкий, с интригой, без канцелярита и без лживого кликбейта.

Сначала оцени материал:
- fact: одной фразой самый удивительный факт из материала, от которого обычный человек скажет «ого, не знал!».
- interest: насколько он удивителен, от 1 до 10. Ставь 1-4, если материал — рецепт, кулинарная или бытовая история,
  личный опыт, обзор товара, совет, реклама, анонс, сухая биография или новость без удивительного факта.
  7 и выше — только если факт правда неожиданный (необычное явление, рекорд, странный обычай, открытие, парадокс).
  Будь строгим. Примеры оценок:
  3 — «дорога проходит через несколько городов и поворачивает на север»; «музей открылся в 1929 году»;
  4 — «в турнире играют на траве, призовой фонд — миллионы»;
  3 — учебный материал без простого «ого»: биохимические реакции, формулы, термины, устройство механизмов;
  3 — общеизвестное, что проходят в школе или знает почти каждый: «теория относительности изменила физику», «Земля вращается вокруг Солнца»;
  8 — «тихоходки выживают в открытом космосе»; «осьминоги меняют цвет во сне»;
  9 — «в Средние века животных судили в суде, с адвокатами и приговорами».
  Не придумывай удивительность там, где её нет, и не натягивай заголовок.

Затем напиши пост по материалу.
Правила:
- Используй ТОЛЬКО факты из материала. Не выдумывай цифры, даты, имена и подробности. Если не уверен в детали, просто не упоминай её.
- Пиши для обычного человека, а не для специалиста: научные термины (коннектом, апоптоз, перцептрон…) либо объясни в двух-трёх словах при первом упоминании, либо обойдись без них.
- Не давай советов, что купить или как поступить («выбирайте…», «используйте…») — канал рассказывает факты, а не консультирует. Не пиши рубленым рекламным стилем («Итог? … Решение? …»).
- Пиши своими словами, не копируй фразы из материала. Материал может быть на английском, пост всегда на русском.
- Не упоминай источник, сайт, журнал или автора материала. Не пиши «учёные недавно выяснили», если в материале нет даты.
- Не касайся политики, религии, войн и тем сексуальности.
- Для исторических событий не начинай со слова «Сегодня»: пиши «8 ноября 1793 года…» или «В этот день в 1793 году…».
- Сегодня {today}. Не считай сам, сколько лет прошло: не пиши «исполнилось N лет» или «N лет назад». Пиши «в этот день в 1929 году» или просто год.
- Если в материале есть строка «ГЛАВНЫЙ ФАКТ» — построй пост именно вокруг него, а статью используй для подробностей.
- Иначе найди в материале самый неожиданный или удивительный факт и построй пост вокруг него. Строй пост вокруг жизни, открытий и достижений, а не вокруг смерти, могил, болезней или несчастий, даже если они есть в материале.
- title: цепляющий заголовок до 70 символов, можно с одним эмодзи в начале.
- text: 2-4 коротких абзаца, всего 350-650 символов, абзацы разделяй пустой строкой. 1-3 эмодзи на весь текст. Первая фраза текста должна быть понятна без заголовка: НЕ начинай с «Именно так», «Именно такую», «Это», «Так» и других слов, которые отсылают к заголовку. Закончи ОДНИМ коротким вопросом «на подумать» или неожиданным выводом — не двумя. Вывод должен прямо следовать из факта, не притягивай мораль. НЕ призывай писать в комментариях, делиться мнением, ставить реакции или пересылать пост — у канала нет комментариев.
- hashtags: 2-3 тега на русском, одним словом каждый. В text хэштеги НЕ пиши.
- media_queries: 3 запроса НА АНГЛИЙСКОМ для фотостока, по 1-3 слова (одно слово — только если предмет уникален сам по себе, например "octopus"; иначе уточняй: "strawberry shortcake", а не "cake"): главный предмет поста, который буквально видно на фото (существительное). Первый запрос — самый точный и короткий. Примеры: "octopus", "opera house", "subway station", "comet", "tennis grass". Никаких абстракций вроде "history", "science", "atmosphere", "mystery". Без имён людей.
- video_query: 1-2 английских слова, ТОЛЬКО если главный предмет поста можно буквально показать на коротком видео (животное, растение, природное явление, космос, город или место, транспорт). Для событий, людей, искусства, истории, медицины и абстрактных тем — пустая строка "".
- category: тема поста, одно слово из списка: space, history, art, nature, science, other.
- image_prompt: описание иллюстрации к посту на английском, 1 предложение, без текста на картинке.
- quiz: викторина ТОЛЬКО по тому, что прямо написано в text (не по остальному материалу) — читатель должен найти ответ в посте; правильный ответ должен дословно встречаться в text. Формулируй вопрос словами из text: не используй в вопросе и вариантах термины и названия, которых нет в тексте поста. question до 200 символов, options ровно 4 варианта до 90 символов каждый, correct — индекс правильного (0-3), explanation до 180 символов.

Ответь строго одним JSON-объектом:
{{"fact": "...", "interest": 0, "title": "...", "text": "...", "hashtags": ["..."], "media_queries": ["..."], "video_query": "...", "category": "...", "image_prompt": "...", "quiz": {{"question": "...", "options": ["...", "...", "...", "..."], "correct": 0, "explanation": "..."}}}}

Материал ({kind}, «{title}»):
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


MIN_INTEREST = int(os.getenv("MIN_INTEREST", "") or 7)   # ниже — материал пропускается


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
    today = datetime.now(MSK)
    prompt = PROMPT.format(kind=src["kind"], title=src["title"], material=src["material"][:6000],
                           today=f"{today.day} {sources.MONTHS[today.month - 1]} {today.year} года")
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
                interest = post.get("interest")
                if isinstance(interest, (int, float)) and interest < MIN_INTEREST:
                    return post                  # всё равно отбракуем — проверка фактов не нужна
                return fix_opening(fact_check(post, src, name, fn), fn)
            except requests.HTTPError as e:
                code = e.response.status_code if e.response is not None else 0
                detail = (e.response.text[:200] if e.response is not None else "")
                log(f"{name}: ошибка HTTP {code} {detail}")
                if code == 429 or code >= 500:   # лимит или сбой сервера — подождать и повторить
                    time.sleep(20 * (attempt + 1))
                    continue
                break
            except (ValueError, KeyError, IndexError, requests.RequestException) as e:
                log(f"{name}: {e}")
                time.sleep(3)
    if ALLOW_RAW_POSTS:
        log("Нейросети недоступны — публикую выдержку из статьи как есть")
        return fallback_post(src)
    raise LLMUnavailable("нейросеть не ответила")


class LLMUnavailable(Exception):
    pass


CHECK_PROMPT = """Ты строгий редактор-фактчекер. Сверь пост с исходным материалом.

Найди каждое утверждение в заголовке, тексте и викторине, которое НЕ подтверждается материалом,
искажает его (перепутаны даты, причины и следствия, «всегда» вместо «с какого-то года», «первый» вместо «единственный»
и т. п.) или преувеличивает. Подсчёты лет («исполнилось N лет», «N лет назад») удали — оставь только сам год. Заголовок не должен обещать больше, чем подтверждает материал (например, «дерево, которое строит железную дорогу», если из него просто делают шпалы) — перепиши его честно, но интересно. Если текст начинается с «Именно так…» или другой отсылки к заголовку — перепиши первую фразу, чтобы она была понятна сама по себе. Если в конце несколько вопросов — оставь один. Притянутую мораль убери. Исправь такие места по материалу или убери их. Стиль, длину, эмодзи и вопрос в конце сохрани. Призывы писать в комментариях, делиться мнением или ставить реакции удали.
Проверь викторину: правильный ответ должен прямо содержаться в тексте поста. Если его там нет — добавь этот факт в текст одной фразой или замени вопрос на вопрос по тексту. Если в вопросе есть термин или название, которого нет в тексте поста, — перефразируй вопрос словами из текста.
Если ошибок нет — верни пост без изменений.

Ответь строго одним JSON-объектом:
{{"errors": ["что было неверно", ...], "title": "...", "text": "...", "quiz": {{"question": "...", "options": ["...", "...", "...", "..."], "correct": 0, "explanation": "..."}}}}
Если в посте нет викторины, верни "quiz": null.

ПОСТ:
{post}

МАТЕРИАЛ:
{material}
"""


# Первая фраза, которая без заголовка непонятна: «Она была создана…», «Именно так…», «Это…»
DANGLING = re.compile(r"^\W*(она|он|оно|они|её|его|их|это|этот|эта|эти|этим|именно|так|такой|такая|"
                      r"такое|такие|таким|там|тогда|здесь|поэтому)\b", re.IGNORECASE)

PRONOUN = re.compile(r"^(он|она|оно|они|его|её|ее|их|ему|ей|им|него|неё|нее|них|"
                     r"этот|эта|это|эти|этого|этой|этому|этим|этих|эту|"
                     r"тот|та|те|того|той|тем|тех|ту|"
                     r"такой|такая|такое|такие|таким|такого|такую|таких)$", re.IGNORECASE)


def needs_new_opening(para):
    """Первая фраза непонятна без заголовка: начинается с отсылки, с «А/Но/И…» или в первых шести
    словах есть местоимение («А оказывается, что его листья…»)."""
    first = re.split(r"(?<=[.!?])\s", para.strip(), 1)[0]
    words = re.findall(r"[а-яёa-z]+", first.lower())[:6]
    return bool(DANGLING.match(first) or re.search(r"\bименно так\w*", first, re.IGNORECASE)
                or (words and words[0] in ("а", "и", "но", "да", "ведь", "же", "вот", "ну", "зато"))
                or any(PRONOUN.match(w) for w in words))


FIX_OPENING_PROMPT = """Перед тобой первый абзац поста. Его первая фраза непонятна без заголовка
(начинается с местоимения или слова-отсылки). Перепиши ТОЛЬКО первую фразу так, чтобы она была понятна сама
по себе и прямо называла, о чём речь. Остальное оставь без изменений. Ответь только исправленным абзацем, без кавычек.

Заголовок поста: {title}
Абзац: {para}"""


def quiz_answer_in_text(quiz, text):
    """Есть ли правильный ответ викторины в тексте поста (числа — по цифрам, слова — по корням)."""
    answer = quiz["options"][quiz["correct"]].lower()
    plain = re.sub(r"(?<=\d)[\s\u00a0](?=\d)", "", text.lower())        # «138 639» -> «138639»
    numbers = re.findall(r"\d+", re.sub(r"(?<=\d)[\s\u00a0](?=\d)", "", answer))
    if numbers:
        return all(n in plain for n in numbers)
    words = [w for w in re.findall(r"[а-яёa-z]+", answer) if len(w) > 3]
    if not words:
        return answer.strip() in plain
    return sum(w[:5] in plain for w in words) >= max(1, (len(words) + 1) // 2)


def fix_opening(post, fn):
    """Если первая фраза отсылает к заголовку — просим переписать её, а при неудаче подставляем заголовок."""
    post = {**post, "text": clean_text(post["text"], post["title"])}   # сначала убираем мусор сверху
    paras = post["text"].strip().split("\n\n", 1)
    first_sentence = re.split(r"(?<=[.!?])\s", paras[0], 1)[0]
    if not needs_new_opening(paras[0]):
        return post
    new = ""
    for attempt in range(2):                         # две попытки переписать
        try:
            cand = fn(FIX_OPENING_PROMPT.format(title=post["title"], para=paras[0])).strip().strip('"«»')
        except (requests.RequestException, ValueError, KeyError, IndexError):
            continue
        first_new = re.split(r"(?<=[.!?])\s", cand, 1)[0]
        # для переписанной фразы проверка мягче: главное — не начинаться с отсылки
        if cand and not DANGLING.match(cand) and not re.search(r"\bименно так\w*", first_new, re.I) \
                and not re.match(r"^(а|и|но|да|ведь|вот)\b", cand, re.I) and 0.4 < len(cand) / max(len(paras[0]), 1) < 4:
            new = cand
            break
    if new:
        log("Первая фраза переписана: была отсылка к заголовку")
        paras[0] = new
    else:
        # Запасной вариант: убираем фразу-отсылку, если без неё остаётся связный текст
        rest = re.split(r"(?<=[.!?])\s+", paras[0], 1)
        tail = rest[1] if len(rest) > 1 else ""
        others = paras[1] if len(paras) > 1 else ""
        if len(tail + others) > 250 and not needs_new_opening(tail or others):
            log("Первая фраза отсылала к заголовку — убрал её")
            paras = [p for p in (tail, others) if p]
        else:
            title = re.sub(r"^[^\w«]+", "", post["title"]).rstrip(".!?")
            log("Первая фраза отсылала к заголовку — добавил заголовок в начало")
            paras[0] = f"{title}. {paras[0][0].upper()}{paras[0][1:]}"
    return {**post, "text": "\n\n".join(paras)}


def fact_check(post, src, name, fn):
    """Второй проход: нейросеть сверяет пост с материалом и исправляет искажения."""
    draft = {"title": post["title"], "text": post["text"], "quiz": post["quiz"]}
    prompt = CHECK_PROMPT.format(post=json.dumps(draft, ensure_ascii=False, indent=1),
                                 material=src["material"][:6000])
    try:
        fixed = parse_json(fn(prompt))
    except (ValueError, KeyError, IndexError, requests.RequestException) as e:
        log(f"Проверка фактов не удалась ({e.__class__.__name__}), публикую как есть")
        return post
    errors = [e for e in fixed.get("errors") or [] if isinstance(e, str) and e.strip()]
    candidate = {**post, "title": fixed.get("title"), "text": fixed.get("text"),
                 "quiz": fixed.get("quiz") if post["quiz"] else None}
    try:
        candidate = validate_post(candidate)
    except ValueError:
        log("Проверка фактов вернула негодный ответ, публикую как есть")
        return post
    if errors:
        log(f"Проверка фактов ({name}) исправила: " + "; ".join(errors)[:500])
    else:
        log("Проверка фактов: ошибок нет")
    return candidate


def fallback_post(src):
    if src.get("lang", "ru") != "ru":
        return None                                # без нейросети английский материал не перевести
    paras = [s for s in src["text"].split("\n") if len(s) > 80][:2]
    return {"title": f"📚 {src['title']}", "text": "\n\n".join(paras),
            "hashtags": ["интересное", "факты"], "media_queries": [src["title"]], "video_query": "", "category": src.get("category", "history"), "image_prompt": "",
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


def clean_text(text, title=""):
    """Чистит текст поста: хэштеги, повтор заголовка, призывы комментировать, лишние вопросы."""
    text = re.sub(r"(?m)^[ \t]*(#\w+[ \t]*)+$", "", text)    # нейросеть иногда сама ставит теги
    # страховка: убираем предложения с призывами комментировать, даже если нейросеть их написала
    text = re.sub(r"[^.!?\n]*(?:в комментари|комментируйте|делитесь|поделитесь|пишите,|напишите,|"
                  r"ставьте|поставьте реакц|перешлите)[^.!?\n]*(?:[.!?]+|$)[ \t]*[^\s\w]*", "", text,
                  flags=re.IGNORECASE | re.MULTILINE)
    # «Сегодня, 8 ноября 1793 года, произошло…» звучит так, будто это происходит сейчас
    text = re.sub(r"(?m)^Сегодня,\s*(\d{1,2}\s+[а-я]+\s+\d{3,4}\s+года),?\s*", r"\1 ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    paras = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    if len(paras) >= 3 and paras[-1].rstrip().endswith("?") and paras[-2].rstrip().rstrip("🌊🤔😉🙂").rstrip().endswith("?"):
        paras.pop(-1)                                   # два вопроса подряд в конце — оставляем первый
    if paras:                                           # два вопроса в одном последнем абзаце — оставляем последний
        qs = re.findall(r"[^.!?]*\?", paras[-1])
        if len(qs) >= 2 and paras[-1].rstrip().endswith("?"):
            head = paras[-1][:paras[-1].find(qs[0].strip())].strip() if qs[0].strip() else ""
            paras[-1] = (head + " " + qs[-1].strip()).strip()
    text = "\n\n".join(paras)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    paras = [p for p in text.split("\n\n") if p.strip()]
    norm = lambda s: re.sub(r"[\W_]+", " ", s).strip().lower()
    while paras and title and norm(paras[0]) == norm(title):     # нейросеть повторила заголовок
        paras.pop(0)
    return "\n\n".join(paras)


def build_caption(post, limit=CAPTION_LIMIT, media_items=()):
    """Заголовок, текст, хэштеги, подпись авторов фото (если лицензия требует) и подпись канала."""
    tags = " ".join("#" + re.sub(r"[^\w]", "", t.lstrip("#")) for t in post["hashtags"])
    tags = " ".join(t for t in tags.split() if len(t) > 1 and re.search(r"[а-яё]", t, re.IGNORECASE))
    if not tags:                                        # нейросеть дала теги не по-русски — ставим тег темы
        tags = {"space": "#космос", "history": "#история", "nature": "#природа", "science": "#наука",
                "art": "#искусство"}.get(post.get("category"), "#факты")
    attribs = list(dict.fromkeys(m["attrib"] for m in media_items if m and m.get("attrib")))
    credit = ("📷 " + "; ".join(attribs[:3])) if attribs else ""
    footer = "\n\n".join(x for x in [tags, credit, SIGNATURE] if x)
    title = trim(post["title"].strip(), 100)
    text = clean_text(post["text"], post["title"])
    body = trim(text, min(max(limit - len(title) - len(footer) - 6, 100), 900))   # длиннее 900 — уже лонгрид
    out = f"<b>{html.escape(title)}</b>\n\n{html.escape(body)}"
    return out + ("\n\n" + html.escape(footer) if footer else "")


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
    elif video and (kind == "video" or not photos):
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
    tried = set()
    for attempt in range(4):
        src = sources.pick_source(state, exclude=tried)
        log(f"Формат: {kind}. Источник: {src['source']}. Материал: {src['title']}")
        try:
            post = write_post(src)
        except LLMUnavailable:
            sys.exit("Нейросеть недоступна — пост пропущен, следующий запуск попробует снова. "
                     "Причина — в строках выше.")
        if post is None:
            tried.add(src["id"])
            continue
        interest = post.get("interest")
        if isinstance(interest, (int, float)) and interest < MIN_INTEREST:
            log(f"Скучный материал (интерес {interest}/10: {str(post.get('fact'))[:120]}) — беру другой")
        elif not sources.is_blocked(post["title"] + " " + post["text"], lead_chars=0) \
                and not sources.VIOLENT_TEXT.search(post["title"] + " " + post["text"]):
            break
        else:
            log("Готовый текст касается запрещённой темы — беру другой материал")
        tried.add(src["id"])
        state["used_sources"].append(src["id"])
    else:
        sys.exit("Не удалось подобрать подходящий материал")
    if src.get("image_free"):                       # снимок NASA и т. п. — свободный
        src["image_ok"], src["image_attrib"] = True, None
    elif src.get("image"):
        src["image_ok"], src["image_attrib"] = media.image_license(src)

    photos, video = [], None
    if kind in ("album", "mixed") and post["category"] not in ("nature", "space"):
        # На абстрактных темах (технологии, общество, история) второе и третье фото почти всегда
        # случайные. Несколько картинок — только для природы и космоса, где фотостоки надёжны.
        log(f"Тема «{post['category']}» — вместо альбома одно фото")
        kind = "photo"
    if kind in ("photo", "album", "mixed"):
        photos = media.collect_photos(src, post, state, {"photo": 1, "album": 3, "mixed": 2}[kind])
    if kind in ("video", "mixed"):
        video = media.collect_video(post, state)
        if not video and kind == "video":           # видео не нашлось — хотя бы фото
            photos = media.collect_photos(src, post, state, 1)

    caption = build_caption(post, limit=4000 if not photos and not video else CAPTION_LIMIT,
                            media_items=photos + [video])

    if args.dry_run:
        print("\n" + "=" * 60 + f"\nФормат: {kind} | тема: {post['category']} | {src['topic']} | фото: {len(photos)} | видео: {bool(video)}")
        print(caption)
        if post["quiz"]:
            print("\nВикторина:", json.dumps(post["quiz"], ensure_ascii=False, indent=1))
        return

    send_post(kind, caption, photos, video)
    log("Пост опубликован")
    if post["quiz"] and not quiz_answer_in_text(post["quiz"], post["title"] + " " + clean_text(post["text"], post["title"])):
        log("Викторина пропущена: ответа на вопрос нет в тексте поста")
        post["quiz"] = None
    if post["quiz"] and not args.no_quiz and random.random() < QUIZ_CHANCE:
        time.sleep(5)
        try:
            send_quiz(post["quiz"])
            log("Викторина опубликована")
        except RuntimeError as e:
            log(f"Викторина не отправлена: {e}")

    state["used_sources"].append(src["id"])
    state["recent_topics"] = (state.get("recent_topics", []) + [src["topic"]])[-20:]
    state["used_media"] += [p["id"] for p in photos] + ([video["id"]] if video else [])
    state["posts"] = state.get("posts", 0) + 1
    state["last_post"] = datetime.now(MSK).isoformat(timespec="minutes")
    save_state(state)


if __name__ == "__main__":
    main()
