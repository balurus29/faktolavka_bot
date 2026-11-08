"""
Откуда бот берёт материал для постов и какие темы пропускает.

Источники (доли меняются в SOURCE_WEIGHTS):
  wiki_ru      избранные и хорошие статьи русской Википедии
  onthisday    «В этот день» из русской Википедии
  wiki_en      избранные и хорошие статьи английской Википедии (бот переводит)
  rss_ru       научно-популярные сайты на русском (Naked Science, N+1)
  rss_en       необычные новости науки на английском (ScienceDaily, Atlas Obscura, Smithsonian)
  apod         «Космическая картинка дня» NASA

Фильтр тем:
  - политика, религиозные учения, войны и насилие, сексуальность, наркотики — не публикуются никогда;
  - кино, сериалы, персонажи, спорт, шоу-бизнес — не чаще одного поста из POP_EVERY
    и никогда два раза подряд.
"""
import html
import os
import random
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

import requests

MSK = timezone(timedelta(hours=3))
NASA_KEY = os.getenv("NASA_API_KEY", "") or "DEMO_KEY"

SOURCE_WEIGHTS = {"wiki_ru": 25, "onthisday": 15, "wiki_en": 10,
                  "rss_ru": 20, "rss_en": 20, "apod": 10}

RSS_FEEDS = {
    "rss_ru": [
        "https://naked-science.ru/allrss",
        "https://nplus1.ru/rss",
    ],
    "rss_en": [
        "https://www.sciencedaily.com/rss/strange_offbeat.xml",
        "https://www.sciencedaily.com/rss/strange_offbeat/weird_world.xml",
        "https://www.sciencedaily.com/rss/strange_offbeat/odd_creatures.xml",
        "https://www.sciencedaily.com/rss/strange_offbeat/bizarre_things.xml",
        "https://www.atlasobscura.com/feeds/latest",
        "https://www.smithsonianmag.com/rss/smart-news/",
    ],
}

WIKI_CATEGORIES = {
    "ru": ["Категория:Википедия:Избранные статьи по алфавиту",
           "Категория:Википедия:Хорошие статьи по алфавиту"],
    "en": ["Category:Featured articles", "Category:Good articles"],
}
WIKI_LETTERS = {"ru": "АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШЭЮЯ", "en": "ABCDEFGHIJKLMNOPRSTUVW"}

POP_EVERY = 5               # поп-культура — не чаще 1 поста из 5

# ---------------------------------------------------------------- фильтр тем
# Никогда: темы сексуальности (ищем по всему началу материала).
BLOCK_ANYWHERE = re.compile(
    r"гомосексу|гей[\s-]|геев|лгбт|lgbt|трансгендер|transgender|однопол|бисексу|bisexual|"
    r"лесбиян|lesbian|квир|queer|\bgay\b|сексуальн\w* ориентац|sexual orientation|каминг-аут|coming out|"
    r"наркоти|каннабис|марихуан|гашиш|кокаин|героин|cannabis|marijuana|cocaine|heroin|narcotic",
    re.IGNORECASE)
# Никогда: если с этого начинается статья (определение темы).
BLOCK_IN_LEAD = re.compile(
    r"политик|политическ|партия|выборы|депутат|парламент|санкци|митинг|оппозиц|"
    r"politician|political party|election|parliament|sanction|"
    r"религиоз|богослов|теолог|секта|ислам\b|христианств|иудаизм|буддизм|проповед|"
    r"religious|theolog|\bsect\b|"
    r"войн[аыуе]|сражени|битв[аы]|вооружённ|теракт|убийств|расстрел|казн[иья]|геноцид|резн|"
    r"катастроф|самоубий|массов\w+ гибел|"
    r"\bwar\b|battle|terroris|murder|massacre|genocide|execution|suicide",
    re.IGNORECASE)
# Поп-культура — можно, но дозированно.
POP_WORDS = re.compile(
    r"фильм|кинофильм|телесериал|сериал|мультфильм|мультсериал|аниме|персонаж|актёр|актер|актриса|"
    r"режиссёр|режиссер|футбол|хоккеист|теннисист|баскетбол|спортсмен|олимпийск|чемпион|боксёр|"
    r"певец|певица|рэпер|музыкальн\w+ групп|альбом|сингл|видеоигр|компьютерн\w+ игр|комикс|"
    r"\bfilm\b|movie|tv series|television series|sitcom|\bcharacter\b|\bactor|actress|footballer|"
    r"athlete|olympic|singer|rapper|\balbum|video game|\bcomic\b|comic book",
    re.IGNORECASE)


# Заметки, в которых обычно нет удивительного факта: рецепты, покупки, советы, обзоры.
BORING_TITLE = re.compile(
    r"recipe|how to\b|\btips\b|gift guide|\bdeals?\b|\bsale\b|review|best .* to buy|podcast|newsletter|"
    r"webinar|giveaway|sponsored|рецепт|как приготовить|советы|скидк|распродаж|обзор|подкаст|вебинар|"
    r"конкурс|розыгрыш|реклам",
    re.IGNORECASE)


def is_blocked(text, lead_chars=500, any_chars=4000):
    return bool(BLOCK_ANYWHERE.search(text[:any_chars]) or BLOCK_IN_LEAD.search(text[:lead_chars]))


def is_pop(title, text):
    return bool(POP_WORDS.search(title + " " + text[:400]))


def pop_allowed(state):
    recent = state.get("recent_topics", [])[-(POP_EVERY - 1):]
    return "pop" not in recent


# ---------------------------------------------------------------- общие
session = requests.Session()
log = print


def setup(http_session, logger):
    global session, log
    session, log = http_session, logger


def _json(url, **kw):
    r = session.get(url, timeout=30, **kw)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- Википедия
def wiki_api(lang, params):
    params = {**params, "format": "json", "formatversion": 2}
    return _json(f"https://{lang}.wikipedia.org/w/api.php", params=params)


def wiki_article(title, lang="ru"):
    data = wiki_api(lang, {
        "action": "query", "titles": title, "redirects": 1,
        "prop": "extracts|pageimages|info", "explaintext": 1, "exchars": 5000,
        "piprop": "thumbnail|name", "pithumbsize": 1280, "inprop": "url"})
    pages = data.get("query", {}).get("pages", [])
    if not pages or pages[0].get("missing"):
        return None
    p = pages[0]
    return {
        "title": p["title"],
        "text": re.sub(r"\n{2,}", "\n", p.get("extract", "")).strip(),
        "url": p.get("fullurl", ""),
        "image": (p.get("thumbnail") or {}).get("source"),
        "image_file": p.get("pageimage"),
        "lang": lang,
    }


def wiki_good(state, lang):
    category = random.choice(WIKI_CATEGORIES[lang])
    data = wiki_api(lang, {
        "action": "query", "list": "categorymembers", "cmtitle": category,
        "cmnamespace": 0, "cmlimit": 50,
        "cmstartsortkeyprefix": random.choice(WIKI_LETTERS[lang])})
    titles = [m["title"] for m in data.get("query", {}).get("categorymembers", [])]
    random.shuffle(titles)
    for title in titles[:12]:
        sid = f"wiki:{lang}:{title}" if lang != "ru" else f"wiki:{title}"
        if sid in state["used_sources"]:
            continue
        art = wiki_article(title, lang)
        if art and len(art["text"]) > 800:
            yield {"id": sid, "kind": "интересная статья энциклопедии", "material": art["text"], **art}


GENERIC_TITLE = re.compile(
    r"^\d{1,4}( год)?( до н\. ?э\.)?$|^\d{1,2} [а-я]+$|^\d{3,4}-е( годы)?|\bвек\b|тысячелети|"
    r"^(Январь|Февраль|Март|Апрель|Май|Июнь|Июль|Август|Сентябрь|Октябрь|Ноябрь|Декабрь)$",
    re.IGNORECASE)
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]


def on_this_day(state):
    now = datetime.now(MSK)
    data = _json(f"https://ru.wikipedia.org/api/rest_v1/feed/onthisday/selected/{now:%m}/{now:%d}")
    events = data.get("selected", [])
    random.shuffle(events)
    for ev in events:
        text, year = ev.get("text", ""), ev.get("year")
        sid = f"otd:{year}:{text[:60]}"
        if sid in state["used_sources"] or is_blocked(text):
            continue
        for page in ev.get("pages") or []:          # статья о самом событии, а не о годе или дате
            title = ((page.get("titles") or {}).get("normalized") or page.get("title", "")).replace("_", " ")
            if GENERIC_TITLE.search(title):
                continue
            art = wiki_article(title)
            if art and len(art["text"]) >= 400:
                material = (f"В этот день, {now.day} {MONTHS[now.month - 1]} {year} года: {text}\n\n"
                            f"Подробнее о теме:\n{art['text']}")
                yield {"id": sid, "kind": f"событие этого дня в истории ({year} год)",
                       "material": material, **art}
                break


# ---------------------------------------------------------------- RSS
class _TextExtractor(HTMLParser):
    """Достаёт текст абзацев <p> со страницы статьи."""
    def __init__(self):
        super().__init__()
        self.parts, self.cur, self.depth, self.skip = [], [], 0, 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "nav", "footer", "aside"):
            self.skip += 1
        elif tag == "p":
            self.depth += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "footer", "aside"):
            self.skip = max(0, self.skip - 1)
        elif tag == "p" and self.depth:
            self.depth -= 1
            txt = " ".join("".join(self.cur).split())
            if len(txt) > 60:
                self.parts.append(txt)
            self.cur = []

    def handle_data(self, data):
        if self.depth and not self.skip:
            self.cur.append(data)


def strip_html(s):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s or "")).split())


def page_text(url, limit=6000):
    r = session.get(url, timeout=30)
    r.raise_for_status()
    p = _TextExtractor()
    p.feed(r.text)
    return "\n".join(p.parts)[:limit]


def parse_feed(xml_text):
    root = ET.fromstring(xml_text)
    items = []
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        get = {c.tag.split("}")[-1]: c for c in el}
        link = get.get("link")
        href = (link.get("href") or link.text or "").strip() if link is not None else ""
        # элементы без вложенных тегов в ElementTree «ложные», поэтому проверяем на None явно
        desc = next((get[k] for k in ("encoded", "description", "summary", "content") if get.get(k) is not None), None)
        items.append({
            "title": strip_html(get["title"].text if "title" in get else ""),
            "link": href,
            "summary": strip_html(desc.text if desc is not None else ""),
        })
    return items


def rss(state, group):
    feeds = RSS_FEEDS[group][:]
    random.shuffle(feeds)
    lang = "ru" if group == "rss_ru" else "en"
    for feed in feeds:
        try:
            r = session.get(feed, timeout=30)
            r.raise_for_status()
            items = parse_feed(r.content)
        except (requests.RequestException, ET.ParseError) as e:
            log(f"Лента {feed} недоступна: {e.__class__.__name__}")
            continue
        random.shuffle(items)
        for it in items[:8]:
            sid = f"rss:{it['link']}"
            if not it["link"] or sid in state["used_sources"] or BORING_TITLE.search(it["title"]):
                continue
            try:
                text = page_text(it["link"])
            except requests.RequestException:
                text = ""
            material = text if len(text) > len(it["summary"]) else it["summary"]
            if len(material) < 400:
                continue
            yield {"id": sid, "kind": "научно-популярная заметка", "title": it["title"],
                   "material": f"{it['title']}\n\n{material}", "text": material,
                   "url": it["link"], "image": None, "lang": lang}


# ---------------------------------------------------------------- NASA
def apod(state):
    data = _json("https://api.nasa.gov/planetary/apod",
                 params={"api_key": NASA_KEY, "count": 5, "thumbs": "true"})
    for d in data:
        sid = f"apod:{d.get('date')}"
        if sid in state["used_sources"] or len(d.get("explanation", "")) < 300:
            continue
        free = d.get("media_type") == "image" and not d.get("copyright")   # снимки NASA — свободные
        yield {"id": sid, "kind": "космический снимок дня NASA с пояснением астрономов",
               "title": d.get("title", ""), "material": f"{d.get('title')}\n\n{d['explanation']}",
               "text": d["explanation"], "url": d.get("url", ""), "lang": "en",
               "image": d.get("url") if free else None, "image_free": free, "category": "space"}


# ---------------------------------------------------------------- выбор
GENERATORS = {
    "wiki_ru": lambda st: wiki_good(st, "ru"),
    "onthisday": on_this_day,
    "wiki_en": lambda st: wiki_good(st, "en"),
    "rss_ru": lambda st: rss(st, "rss_ru"),
    "rss_en": lambda st: rss(st, "rss_en"),
    "apod": apod,
}


def check(src, state):
    """'ok', 'blocked' (запрещённая тема) или 'later' (поп-культура, квота сейчас исчерпана)."""
    if is_blocked(src["material"]):
        return "blocked"
    src["topic"] = "pop" if is_pop(src["title"], src["text"]) else "general"
    if src["topic"] == "pop" and not pop_allowed(state):
        return "later"
    return "ok"


def pick_source(state, exclude=()):
    names = [n for n in SOURCE_WEIGHTS if SOURCE_WEIGHTS[n] > 0]
    order = []
    while names:                                  # случайный порядок с учётом долей
        n = random.choices(names, [SOURCE_WEIGHTS[x] for x in names])[0]
        order.append(n)
        names.remove(n)
    for name in order:
        try:
            for src in GENERATORS[name](state):
                if src["id"] in exclude:
                    continue
                verdict = check(src, state)
                if verdict == "ok":
                    src["source"] = name
                    return src
                if verdict == "blocked":                     # к запрещённым темам не возвращаемся
                    state["used_sources"].append(src["id"])
        except (requests.RequestException, ValueError, KeyError) as e:
            log(f"Источник {name} недоступен: {e.__class__.__name__}")
    raise RuntimeError("Не удалось найти материал для поста")
