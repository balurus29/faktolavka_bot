"""
Откуда бот берёт материал для постов и какие темы пропускает.

Источники (доли меняются в SOURCE_WEIGHTS):
  dyk_ru       рубрика «Знаете ли вы?» русской Википедии — тысячи проверенных удивительных фактов
  dyk_en       рубрика «Did you know…?» английской Википедии — десятки тысяч фактов (бот переводит)
  wiki_ru      избранные и хорошие статьи русской Википедии
  onthisday    «В этот день» из русской Википедии
  wiki_en      избранные и хорошие статьи английской Википедии (бот переводит)
  rss_ru       научпоп на русском: Naked Science, N+1, Элементы, Популярная механика, Наука и жизнь,
               Кот Шрёдингера, научно-популярные хабы Хабра (без программирования)
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

SOURCE_WEIGHTS = {"dyk_ru": 25, "dyk_en": 20, "wiki_ru": 12, "onthisday": 8, "wiki_en": 5,
                  "rss_ru": 13, "rss_en": 10, "apod": 7}

RSS_FEEDS = {
    # Элемент — адрес ленты или кортеж из нескольких вариантов адреса: бот пробует их по очереди.
    "rss_ru": [
        "https://naked-science.ru/allrss",
        "https://nplus1.ru/rss",
        "https://elementy.ru/rss/news",
        ("https://www.popmech.ru/out/public-all.xml", "https://www.popmech.ru/rss/"),
        ("https://www.nkj.ru/rss/", "https://www.nkj.ru/news/rss/"),
        ("https://kot.sh/rss.xml", "https://kot.sh/rss"),
        # Хабр — только научно-популярные хабы, без программирования
        *[(f"https://habr.com/ru/rss/hubs/{hub}/articles/?fl=ru",
           f"https://habr.com/ru/rss/hub/{hub}/all/?fl=ru")
          for hub in ("popular_science", "space", "biology", "physics", "history", "astronomy")],
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


# Статьи энциклопедии, в которых почти не бывает удивительного факта.
BORING_WIKI = re.compile(
    r"\b(U\.?S\.? Route|Interstate|State Route|State Highway|Highway \d|Route \d|County Road|"
    r"County|Township|Municipality|Borough|Parish|Census|Electoral|District)\b|^List of|"
    r"шоссе|трасса|автомобильная дорога|автодорога|\bмаршрут|район\b|округ\b|муниципал|"
    r"сельское поселение|городское поселение|^Список",
    re.IGNORECASE)

# Заметки, в которых обычно нет удивительного факта: рецепты, покупки, советы, обзоры.
BORING_TITLE = re.compile(
    r"recipe|how to\b|\btips\b|gift guide|\bdeals?\b|\bsale\b|review|best .* to buy|podcast|newsletter|"
    r"webinar|giveaway|sponsored|рецепт|как приготовить|советы|скидк|распродаж|обзор|подкаст|вебинар|"
    r"конкурс|розыгрыш|реклам",
    re.IGNORECASE)


# Насилие в готовом тексте поста — такой пост не публикуем, берём другой материал.
VIOLENT_TEXT = re.compile(
    r"казн[иеё]\w*|казнь|\bубил[аио]?\b|\bубит[аыо]?\b|убийств[оаеу]?\b|убийц|расстрел|резн[яиюе]|"
    r"обезглав|повесил|пытк|растерзал|"
    r"executed|execution|murder|killed|massacre|beheaded|tortur",
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
        if sid in state["used_sources"] or BORING_WIKI.search(title):
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
            if GENERIC_TITLE.search(title) or BORING_WIKI.search(title):
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
    for feed in feeds[:4]:
        items = None
        for url in (feed if isinstance(feed, tuple) else (feed,)):
            try:
                r = session.get(url, timeout=30)
                r.raise_for_status()
                items = parse_feed(r.content)
                break
            except (requests.RequestException, ET.ParseError) as e:
                log(f"Лента {url} недоступна: {e.__class__.__name__}")
        if not items:
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


# ---------------------------------------------------------------- «Знаете ли вы?»
# Каждый факт рубрики — строка вида «* … что '''[[Статья]]''' ...?». Жирная ссылка — статья,
# из которой взят факт. Бот берёт сам факт и начало этой статьи для подробностей.
EN_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
             "September", "October", "November", "December"]
HOOK_LINE = re.compile(r"^[*#:]*\s*(?:\.\.\.|…)\s*(?:что|that)\b", re.IGNORECASE)
BOLD_LINK = re.compile(r"'''\s*\[\[([^\]|#]+)")
ANY_LINK = re.compile(r"\[\[([^\]|#]+)")


def wikitext(lang, title):
    data = wiki_api(lang, {"action": "query", "titles": title, "prop": "revisions",
                           "rvprop": "content", "rvslots": "main", "redirects": 1})
    page = (data.get("query", {}).get("pages") or [{}])[0]
    if page.get("missing") or "revisions" not in page:
        return ""
    return page["revisions"][0]["slots"]["main"]["content"]


def clean_wiki(s):
    s = re.sub(r"<ref[^>]*?/>|<ref.*?</ref>", "", s, flags=re.DOTALL)
    for _ in range(3):
        s = re.sub(r"\{\{[^{}]*\}\}", "", s)                      # шаблоны
    s = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", s)       # [[a|b]] -> b
    s = re.sub(r"\[https?://\S+\s*([^\]]*)\]", r"\1", s)
    s = re.sub(r"'{2,}|<[^>]+>", "", s)
    s = re.sub(r"\((?:на илл\.|на фото|изображ[её]н\w*|pictured|illustrated)\)", "", s, flags=re.I)
    return " ".join(s.split()).lstrip("*#: ").strip()


def parse_hooks(text):
    hooks = []
    for line in text.splitlines():
        line = line.strip()
        if not HOOK_LINE.match(line):
            continue
        link = BOLD_LINK.search(line) or ANY_LINK.search(line)
        if not link or link.group(1).startswith(("Файл:", "File:", "Image:", "Изображение:")):
            continue
        hook = clean_wiki(line)
        if 40 <= len(hook) <= 400:
            hooks.append({"hook": hook, "article": link.group(1).strip()})
    return hooks


_ru_archive_pages = []


def dyk_pages_ru():
    """Находит страницы архива рубрики через API (названия меняются — не зашиваем их в код)."""
    if _ru_archive_pages:
        return _ru_archive_pages
    for ns in (4, 104, 10):                       # Википедия:, Проект:, Шаблон:
        for prefix in ("Знаете ли вы/Архив рубрики/", "Знаете ли вы/Архив/", "Знаете ли вы/"):
            data = wiki_api("ru", {"action": "query", "list": "allpages", "apnamespace": ns,
                                   "apprefix": prefix, "aplimit": 500})
            titles = [p["title"] for p in data.get("query", {}).get("allpages", [])
                      if re.search(r"\d{4}", p["title"]) and "Обсуждение" not in p["title"]]
            if len(titles) > 5:
                _ru_archive_pages.extend(titles)
                return _ru_archive_pages
    log("Архив «Знаете ли вы?» не найден")
    return _ru_archive_pages


def dyk(state, lang):
    if lang == "ru":
        pages = dyk_pages_ru()[:]
        random.shuffle(pages)
        pages = pages[:3]
    else:
        now = datetime.now(MSK)
        pages = [f"Wikipedia:Recent additions/{random.randint(2012, now.year - 1)}/{random.choice(EN_MONTHS)}"
                 for _ in range(3)]
    for page in pages:
        try:
            hooks = parse_hooks(wikitext(lang, page))
        except (requests.RequestException, KeyError, IndexError) as e:
            log(f"Архив {page} недоступен: {e.__class__.__name__}")
            continue
        random.shuffle(hooks)
        for h in hooks[:15]:
            sid = f"dyk:{lang}:{h['hook'][:80]}"
            if (sid in state["used_sources"] or is_blocked(h["hook"], lead_chars=len(h["hook"]))
                    or BORING_WIKI.search(h["article"])):
                continue
            art = wiki_article(h["article"], lang)
            if not art or len(art["text"]) < 300:
                continue
            fact = ("Знаете ли вы, " if lang == "ru" else "Did you know ") + h["hook"].lstrip(".… ")
            yield {"id": sid, "kind": "проверенный удивительный факт из рубрики «Знаете ли вы?» Википедии",
                   "material": f"ГЛАВНЫЙ ФАКТ: {fact}\n\nПодробности из статьи «{art['title']}»:\n{art['text']}",
                   **art}


# ---------------------------------------------------------------- выбор
GENERATORS = {
    "dyk_ru": lambda st: dyk(st, "ru"),
    "dyk_en": lambda st: dyk(st, "en"),
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
