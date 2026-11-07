"""
Поиск картинок и видео сразу в нескольких бесплатных источниках.

Под постами авторы медиа не подписываются, поэтому бот берёт только то,
что разрешено использовать БЕЗ указания автора:
  Pexels        фото + видео        PEXELS_API_KEY     (лицензия Pexels)
  Pixabay       фото + видео        PIXABAY_API_KEY    (лицензия Pixabay)
  NASA          космос              без ключа          (общественное достояние)
  Wikimedia     исторические фото   без ключа          только CC0 и общественное достояние
  Openverse     открытые фото       без ключа          только CC0 и общественное достояние
  Pollinations  ИИ-иллюстрация      без ключа          выключено; AI_IMAGE_FALLBACK=1 включает
Картинка из статьи Википедии используется, только если она тоже в общественном достоянии.

Порядок источников зависит от темы поста (category), которую определяет нейросеть.
"""
import os
import random
import re
import urllib.parse

import requests

PEXELS_KEY = os.getenv("PEXELS_API_KEY", "")
PIXABAY_KEY = os.getenv("PIXABAY_API_KEY", "")
AI_FALLBACK = os.getenv("AI_IMAGE_FALLBACK", "0") == "1"   # ИИ-картинки: выключены, включить = "1"

MAX_PHOTO_BYTES = 9_500_000
MAX_VIDEO_BYTES = 48_000_000

# Какие источники пробовать первыми для каждой темы.
PHOTO_ORDER = {
    "space":   ["nasa", "wikimedia", "pixabay", "pexels", "openverse"],
    "history": ["wikimedia", "openverse", "pixabay", "pexels"],
    "art":     ["wikimedia", "openverse", "pixabay", "pexels"],
    "nature":  ["pexels", "pixabay", "wikimedia", "openverse"],
    "science": ["pixabay", "pexels", "wikimedia", "openverse"],   # современные фото, не архив
    "other":   ["pixabay", "pexels", "wikimedia", "openverse"],
}
CATEGORIES = tuple(PHOTO_ORDER)

session = requests.Session()
log = print


def setup(http_session, logger):
    global session, log
    session, log = http_session, logger


def _get(url, **kw):
    r = session.get(url, timeout=30, **kw)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- фото-источники
# Каждая функция возвращает список {"id", "url", "credit", "tags"}.
# tags — описание картинки у источника: по нему бот проверяет, что картинка правда по теме.
# Найденное по точному названию статьи помечается trusted: сверка слов не нужна,
# но фильтр мрачных картинок действует всегда.

def photos_pexels(q):
    if not PEXELS_KEY:
        return []
    data = _get("https://api.pexels.com/v1/search", headers={"Authorization": PEXELS_KEY},
                params={"query": q, "per_page": 10, "orientation": "landscape"})
    return [{"id": f"pexels-photo:{p['id']}", "url": p["src"]["large2x"], "credit": "Pexels",
             "tags": p.get("alt", "")}
            for p in data.get("photos", [])]


def photos_pixabay(q):
    if not PIXABAY_KEY:
        return []
    data = _get("https://pixabay.com/api/", params={
        "key": PIXABAY_KEY, "q": q[:100], "image_type": "photo", "orientation": "horizontal",
        "per_page": 10, "safesearch": "true"})
    return [{"id": f"pixabay-photo:{h['id']}", "url": h["largeImageURL"], "credit": "Pixabay",
             "tags": h.get("tags", "")}
            for h in data.get("hits", [])]


FREE_LICENSE = re.compile(r"\bcc0\b|public domain|\bpd\b|\bpd-|\bpdm\b|no restrictions")  # без указания автора


def photos_wikimedia(q):
    data = _get("https://commons.wikimedia.org/w/api.php", params={
        "action": "query", "format": "json", "formatversion": 2,
        "generator": "search", "gsrnamespace": 6, "gsrlimit": 12,
        "gsrsearch": f"{q} filetype:bitmap",
        "prop": "imageinfo", "iiprop": "url|extmetadata|size|mime", "iiurlwidth": 1280})
    out = []
    for page in data.get("query", {}).get("pages", []):
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata", {})
        lic = meta.get("LicenseShortName", {}).get("value", "").lower() + " " + \
            meta.get("License", {}).get("value", "").lower()
        if info.get("mime") not in ("image/jpeg", "image/png") or (info.get("width") or 0) < 600:
            continue
        if not FREE_LICENSE.search(lic):
            continue
        desc = " ".join(re.sub(r"<[^>]+>", " ", meta.get(k, {}).get("value", ""))
                        for k in ("ImageDescription", "ObjectName", "Categories"))
        out.append({"id": f"commons:{page['pageid']}", "url": info.get("thumburl") or info["url"],
                    "credit": "Wikimedia Commons",
                    "tags": f"{page.get('title', '')} {desc}"[:2000].replace("_", " ")})
    return out


def photos_openverse(q):
    data = _get("https://api.openverse.org/v1/images/", params={
        "q": q, "license": "cc0,pdm", "page_size": 12, "mature": "false",
        "aspect_ratio": "wide,square"})
    return [{"id": f"openverse:{r['id']}", "url": r["url"],
             "credit": f"{r.get('creator') or 'автор'} / {r.get('source', 'Openverse')}, "
                       f"{(r.get('license') or '').upper()}",
             "tags": (r.get("title") or "") + " " + " ".join(t.get("name", "") for t in r.get("tags") or [])}
            for r in data.get("results", []) if r.get("url")]


def photos_nasa(q):
    data = _get("https://images-api.nasa.gov/search", params={"q": q, "media_type": "image"})
    out = []
    for item in data.get("collection", {}).get("items", [])[:12]:
        links, meta = item.get("links") or [], (item.get("data") or [{}])[0]
        if not links:
            continue
        href = links[0]["href"].replace("~thumb.", "~medium.")
        out.append({"id": f"nasa:{meta.get('nasa_id', href)}", "url": href, "credit": "NASA",
                    "tags": f"{meta.get('title', '')} {' '.join(meta.get('keywords') or [])} "
                            f"{(meta.get('description') or '')[:300]}"})
    return out


PHOTO_SOURCES = {
    "pexels": photos_pexels, "pixabay": photos_pixabay,
    "wikimedia": photos_wikimedia, "openverse": photos_openverse, "nasa": photos_nasa,
}


def photo_ai(prompt):
    """Запасной вариант: бесплатная генерация картинки по описанию."""
    seed = random.randint(1, 10**6)
    q = urllib.parse.quote(f"{prompt}, detailed illustration, no text")
    return {"id": f"ai:{seed}", "url": f"https://image.pollinations.ai/prompt/{q}"
            f"?width=1280&height=720&nologo=true&seed={seed}", "credit": "ИИ-иллюстрация"}


# ---------------------------------------------------------------- видео-источники
def videos_pexels(q):
    if not PEXELS_KEY:
        return []
    data = _get("https://api.pexels.com/videos/search", headers={"Authorization": PEXELS_KEY},
                params={"query": q, "per_page": 10, "orientation": "landscape"})
    out = []
    for v in data.get("videos", []):
        if not 4 <= v.get("duration", 0) <= 40:
            continue
        files = [f for f in v.get("video_files", [])
                 if f.get("file_type") == "video/mp4" and 640 <= (f.get("width") or 0) <= 1280]
        if files:
            f = max(files, key=lambda f: f["width"])
            out.append({"id": f"pexels-video:{v['id']}", "url": f["link"], "credit": "Pexels",
                        "tags": v.get("url", "").replace("-", " "),
                        "duration": v.get("duration", 0), "width": f["width"], "height": f.get("height")})
    return out


def videos_pixabay(q):
    if not PIXABAY_KEY:
        return []
    data = _get("https://pixabay.com/api/videos/", params={
        "key": PIXABAY_KEY, "q": q[:100], "per_page": 10, "safesearch": "true"})
    out = []
    for h in data.get("hits", []):
        if not 4 <= h.get("duration", 0) <= 40:
            continue
        for size in ("medium", "small"):
            f = (h.get("videos") or {}).get(size) or {}
            if f.get("url") and 0 < (f.get("size") or 0) < MAX_VIDEO_BYTES:
                out.append({"id": f"pixabay-video:{h['id']}", "url": f["url"], "credit": "Pixabay",
                            "tags": h.get("tags", ""),
                            "duration": h.get("duration", 0), "width": f.get("width"),
                            "height": f.get("height")})
                break
    return out


VIDEO_SOURCES = {"pexels": videos_pexels, "pixabay": videos_pixabay}


def image_is_free(src):
    """Проверяет лицензию главной картинки статьи Википедии: можно ли без указания автора."""
    name = src.get("image_file")
    if not name:
        return False
    try:
        data = _get(f"https://{src.get('lang', 'ru')}.wikipedia.org/w/api.php", params={
            "action": "query", "format": "json", "formatversion": 2,
            "titles": f"File:{name}", "prop": "imageinfo", "iiprop": "extmetadata"})
        meta = data["query"]["pages"][0]["imageinfo"][0]["extmetadata"]
    except (requests.RequestException, KeyError, IndexError, ValueError):
        return False
    lic = (meta.get("LicenseShortName", {}).get("value", "") + " " +
           meta.get("License", {}).get("value", "")).lower()
    return bool(FREE_LICENSE.search(lic))


# ---------------------------------------------------------------- сбор
def download(url, limit, kind="image"):
    with session.get(url, stream=True, timeout=90) as r:
        r.raise_for_status()
        ctype = r.headers.get("Content-Type", "")
        if kind == "image" and not ctype.startswith("image/"):
            raise ValueError(f"это не картинка ({ctype})")
        buf = bytearray()
        for chunk in r.iter_content(256 * 1024):
            buf += chunk
            if len(buf) > limit:
                raise ValueError("файл слишком большой")
    return bytes(buf)


STOP = {"the", "and", "with", "for", "from", "of", "in", "on", "a", "an", "old", "new", "big",
        "small", "close", "up", "view", "photo", "image", "picture", "beautiful", "background"}


# Мрачные изображения не ставим никогда: покойники, похороны, кровь, казни, нищета, катастрофы, война.
MORBID = re.compile(
    r"coffin|casket|corpse|cadaver|dead body|deceased|funeral|post.?mortem|deathbed|lying in state|"
    r"memento mori|execution|hanged|gallows|blood|wound|autopsy|"
    r"cemetery|graveyard|\bgraves?\b|gravestone|tombstone|\btombs?\b|burial|mausoleum|crypt|"
    r"кладбищ|могил|надгроб|склеп|"
    r"great depression|refugee|migrant|famine|starv|poverty|homeless|destitute|"
    r"september 11|9/11|9-11|ground zero|world trade center|terror|attack|bomb|explosion|blast|"
    r"ruin|rubble|debris|wreck|disaster|catastroph|destroy|devastat|collaps|crash|burning|fire|flood|"
    r"\bwar\b|wartime|soldier|military|army|troops|weapon|tank|battle|"
    r"теракт|руин|обломк|разруш|катастроф|взрыв|пожар|авари|крушени|наводнени|войн|солдат|оружи|"
    r"гроб|покойн|похорон|труп|мертв|посмертн|казн[иь]|висел|кров[ьи]|ран[аеы]\b|вскрыт",
    re.IGNORECASE)


# Откровенные картинки тоже никогда: обнажёнка, пин-ап, обложки бульварных журналов.
RACY = re.compile(
    r"nud[ei]|naked|erotic|pin.?up|bikini|lingerie|topless|burlesque|sexy|seductive|"
    r"pulp|magazine cover|nsfw|"
    r"обнажён|обнажен|голая|эроти|бельё",
    re.IGNORECASE)


def is_morbid(item):
    """Мрачная или откровенная картинка — не ставим ни при каких условиях."""
    tags = item.get("tags") or ""
    return bool(MORBID.search(tags) or RACY.search(tags))


# Стоковые фото людей-моделей — самая частая картинка «не в тему». Берём их, только если
# в запросе есть люди.
PEOPLE = re.compile(r"\b(model|fashion|portrait|beauty|girl|woman|women|man|men|boy|lady|guy|"
                    r"person|people|selfie|pose|posing|glamour|young)\b", re.IGNORECASE)


def _words(query):
    return [w for w in re.findall(r"[a-zа-яё]+", query.lower()) if len(w) > 2 and w not in STOP]


def relevant(item, query):
    """Картинка подходит, если в её описании есть ВСЕ ключевые слова запроса (с учётом окончаний)."""
    if is_morbid(item):
        return False
    if item.get("trusted"):
        return True
    words = _words(query)
    if not words or PEOPLE.search(item.get("tags") or "") and not PEOPLE.search(query):
        return False
    return score(item, query) == len(words)


def score(item, query):
    """Сколько ключевых слов запроса есть в описании картинки."""
    if item.get("trusted"):
        return 1
    tags = (item.get("tags") or "").lower()
    return sum(w[:5] in tags for w in _words(query))


def _search(sources, order, queries, used, per_source=2):
    """Опрашивает источники по очереди и оставляет только то, что подходит по теме."""
    found, seen = [], set(used)
    for name in order:
        fn = sources.get(name)
        if not fn:
            continue
        taken = 0
        for q in queries:
            try:
                items = fn(q)
            except (requests.RequestException, ValueError, KeyError) as e:
                log(f"{name}: недоступен ({e.__class__.__name__})")
                break
            fresh = [i for i in items[:10] if i["id"] not in seen and relevant(i, q)]
            random.shuffle(fresh)
            fresh.sort(key=lambda i: -score(i, q))   # сначала те, где совпали все слова запроса
            for i in fresh[:per_source - taken]:
                seen.add(i["id"])
                found.append(i)
                taken += 1
            if taken >= per_source:
                break                       # с этого источника достаточно, разнообразим следующим
    return found


def collect_photos(src, post, state, n):
    category = post.get("category") if post.get("category") in CATEGORIES else "other"
    if category == "other" and src.get("category") in CATEGORIES:
        category = src["category"]
    candidates = []
    if src.get("image") and src.get("image_free"):   # картинка из самой статьи — точно по теме
        candidates.append({"id": f"wiki-img:{src['image']}", "url": src["image"],
                           "credit": "Википедия"})
    queries = [q for q in post.get("media_queries", []) if q][:3]
    if category in ("history", "art", "science") and src.get("source") in ("wiki_ru", "wiki_en", "onthisday"):
        # Wikimedia точно находит картинки по названию статьи — им сверка слов не нужна
        try:
            for item in photos_wikimedia(src["title"])[:6]:
                if item["id"] not in state["used_media"] and not is_morbid(item):
                    candidates.append({**item, "trusted": True})
                    if len(candidates) >= n:
                        break
        except (requests.RequestException, ValueError, KeyError) as e:
            log(f"wikimedia: недоступен ({e.__class__.__name__})")
    if queries and len(candidates) < n:
        candidates += _search(PHOTO_SOURCES, PHOTO_ORDER[category], queries, state["used_media"],
                              per_source=max(1, n - 1))
    if AI_FALLBACK and not candidates and queries:   # ничего подходящего — рисуем иллюстрацию
        candidates.append(photo_ai(post.get("image_prompt") or queries[-1]))

    photos = []
    for c in candidates:
        if len(photos) >= n:
            break
        try:
            data = download(c["url"], MAX_PHOTO_BYTES)
        except (requests.RequestException, ValueError) as e:
            log(f"Пропускаю картинку ({c['credit']}): {e}")
            continue
        photos.append({**c, "data": data, "name": f"p{len(photos)}.jpg"})
    return photos


def collect_video(post, state):
    # Видео ищем только по video_query: нейросеть оставляет его пустым, если тему нельзя
    # буквально показать на видео. Тогда пост будет с фото — лучше так, чем видео не в тему.
    q = (post.get("video_query") or "").strip()
    if not q:
        log("Видео не подходит к теме — будет фото")
        return None
    order = ["pexels", "pixabay"]
    random.shuffle(order)                    # чередуем источники видео
    for v in _search(VIDEO_SOURCES, order, [q], state["used_media"]):
        if not 4 <= (v.get("duration") or 0) <= 40:
            continue
        try:
            return {**v, "data": download(v["url"], MAX_VIDEO_BYTES, kind="video"), "name": "v.mp4"}
        except (requests.RequestException, ValueError) as e:
            log(f"Пропускаю видео ({v['credit']}): {e}")
    return None
