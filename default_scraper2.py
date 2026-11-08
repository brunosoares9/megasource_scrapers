# -*- coding: utf-8 -*-
"""MegaSource - WatchPlay scraper (v1.3.0).

A origem exige Referer e Origin também nas requisições HLS. O Stremio
recebe esses cabeçalhos por behaviorHints.proxyHeaders.

Variáveis de ambiente opcionais:
    TMDB_API_KEY          chave da API do TMDB
    WATCHPLAY_BASE_URL    domínio base do WatchPlay
    WATCHPLAY_VALIDATE    "1" para validar o M3U8 antes de retornar
"""

import http.cookiejar
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request


TITLE = "WatchPlay Scraper"
VERSION = "1.3.0"
DESCRIPTION = "Filmes e séries dublados do WatchPlay"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0 Safari/537.36"
)

TMDB_API_KEY = os.getenv(
    "TMDB_API_KEY",
    "1865f43a0549ca50d341dd9ab8b29f49",
)

BASE_URL = os.getenv(
    "WATCHPLAY_BASE_URL",
    "https://v2.watchplay.shop",
).rstrip("/")

VALIDATE_M3U8 = os.getenv("WATCHPLAY_VALIDATE", "0") == "1"

TIMEOUT = 20
RETRIES = 2
VALIDATE_MAX_BYTES = 64 * 1024

TMDB_CACHE_TTL = 24 * 60 * 60
STREAM_CACHE_TTL = 5 * 60

IMDB_RE = re.compile(r"tt\d+")


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

_cookiejar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(_cookiejar)
)


def _request(url, method="GET", data=None, headers=None, max_bytes=None):
    """Retorna (status, texto). Status 0 indica falha de rede."""
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    }

    if headers:
        request_headers.update(headers)

    body = None

    if method.upper() == "POST":
        if isinstance(data, dict):
            body = urllib.parse.urlencode(data).encode("utf-8")
            request_headers.setdefault(
                "Content-Type",
                "application/x-www-form-urlencoded; charset=UTF-8",
            )
        elif data is not None:
            body = data

    status, text = 0, ""

    for attempt in range(1, RETRIES + 1):
        req = urllib.request.Request(
            url,
            data=body,
            headers=request_headers,
            method=method.upper(),
        )

        try:
            with _opener.open(req, timeout=TIMEOUT) as resp:
                raw = resp.read(max_bytes) if max_bytes else resp.read()
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.status, raw.decode(charset, errors="replace")

        except urllib.error.HTTPError as exc:
            try:
                text = exc.read().decode("utf-8", errors="replace")
            except Exception:
                text = ""

            status = exc.code

            # Só repete em erro de servidor
            if status < 500:
                return status, text

        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logging.warning(
                "Falha HTTP em %s (tentativa %d/%d): %s",
                url, attempt, RETRIES, exc,
            )
            status, text = 0, ""

        if attempt < RETRIES:
            time.sleep(0.5 * attempt)

    return status, text


def _json_response(body):
    try:
        value = json.loads(body)
        return value if isinstance(value, dict) else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Cache simples em memória
# --------------------------------------------------------------------------

_cache = {}


def _cache_get(key):
    entry = _cache.get(key)

    if not entry:
        return None

    expires_at, value = entry

    if expires_at < time.time():
        _cache.pop(key, None)
        return None

    return value


def _cache_set(key, value, ttl):
    _cache[key] = (time.time() + ttl, value)


# --------------------------------------------------------------------------
# TMDB
# --------------------------------------------------------------------------

def imdb_to_tmdb(imdb_id):
    if not isinstance(imdb_id, str):
        return None

    imdb_id = imdb_id.strip()

    if not IMDB_RE.fullmatch(imdb_id):
        return None

    cache_key = "tmdb:" + imdb_id
    cached = _cache_get(cache_key)

    if cached:
        return cached

    query = urllib.parse.urlencode(
        {
            "api_key": TMDB_API_KEY,
            "external_source": "imdb_id",
            "language": "pt-BR",
        }
    )

    url = (
        "https://api.themoviedb.org/3/find/"
        + urllib.parse.quote(imdb_id)
        + "?" + query
    )

    status, body = _request(url)

    if status != 200:
        logging.error("TMDB retornou HTTP %s", status)
        return None

    data = _json_response(body)

    if not data:
        return None

    result = None

    if data.get("movie_results"):
        item = data["movie_results"][0]
        result = {
            "type": "movie",
            "tmdb_id": item.get("id"),
            "title": (
                item.get("title")
                or item.get("original_title")
                or TITLE
            ),
        }

    elif data.get("tv_results"):
        item = data["tv_results"][0]
        result = {
            "type": "tv",
            "tmdb_id": item.get("id"),
            "title": (
                item.get("name")
                or item.get("original_name")
                or TITLE
            ),
        }

    if result:
        _cache_set(cache_key, result, TMDB_CACHE_TTL)

    return result


# --------------------------------------------------------------------------
# Extração do player
# --------------------------------------------------------------------------

def _decode_js_string(value):
    """Decodifica o conteúdo de uma string JS (sem as aspas externas)."""
    value = value.replace("\\/", "/")

    try:
        return json.loads('"' + value.replace('"', '\\"') + '"')
    except ValueError:
        return value


def _normalize_url(raw):
    url = _decode_js_string(raw).strip()

    if url.startswith("//"):
        url = "https:" + url

    if url.startswith(("http://", "https://")):
        return url

    return ""


_PLAYER_PATTERNS = [
    # 1) createMyPlayer({ ... url: "..." })
    re.compile(
        r"createMyPlayer\s*\(\s*\{.*?\burl\s*:\s*([\"'])(.*?)\1",
        re.IGNORECASE | re.DOTALL,
    ),
    # 2) url: "..." / url = "..."
    re.compile(
        r"\burl\s*[:=]\s*([\"'])(.*?)\1",
        re.IGNORECASE | re.DOTALL,
    ),
]

_M3U8_FALLBACK = re.compile(
    r"(https?:)?(?:\\?/){2}[^\"'\s<>]+?\.m3u8[^\"'\s<>]*",
    re.IGNORECASE,
)


def _extract_player_url(html):
    if not html:
        return ""

    for pattern in _PLAYER_PATTERNS:
        for match in pattern.finditer(html):
            url = _normalize_url(match.group(2))

            if url:
                return url

    # Último recurso: qualquer .m3u8 solto no HTML
    match = _M3U8_FALLBACK.search(html)

    if match:
        return _normalize_url(match.group(0))

    return ""


def _validate_playlist(stream_url, page_url):
    status, playlist = _request(
        stream_url,
        headers={
            "Referer": page_url,
            "Origin": BASE_URL,
            "Accept": (
                "application/vnd.apple.mpegurl,"
                "application/x-mpegURL,*/*"
            ),
        },
        max_bytes=VALIDATE_MAX_BYTES,
    )

    if status != 200 or not playlist.lstrip().startswith("#EXTM3U"):
        logging.error(
            "M3U8 inválido ou bloqueado: HTTP %s para %s",
            status, stream_url,
        )
        return False

    return True


def _page_stream(page_url):
    cached = _cache_get("stream:" + page_url)

    if cached:
        return cached

    status, body = _request(
        page_url,
        headers={
            "Referer": BASE_URL + "/",
            "Accept": "text/html,application/xhtml+xml",
        },
    )

    if status != 200:
        logging.error(
            "WatchPlay retornou HTTP %s para %s", status, page_url
        )
        return ""

    stream_url = _extract_player_url(body)

    if not stream_url:
        logging.error("M3U8 não encontrado em %s", page_url)
        return ""

    if VALIDATE_M3U8 and not _validate_playlist(stream_url, page_url):
        return ""

    _cache_set("stream:" + page_url, stream_url, STREAM_CACHE_TTL)

    return stream_url


# --------------------------------------------------------------------------
# URLs das páginas
# --------------------------------------------------------------------------

def _movie_page_url(imdb_id):
    return f"{BASE_URL}/movie/{urllib.parse.quote(imdb_id, safe='')}"


def _series_page_url(imdb_id, season, episode):
    tmdb_info = imdb_to_tmdb(imdb_id)

    if not tmdb_info or not tmdb_info.get("tmdb_id"):
        return ""

    tmdb_id = urllib.parse.quote(str(tmdb_info["tmdb_id"]), safe="")

    return f"{BASE_URL}/tvshow/{tmdb_id}/{int(season)}/{int(episode)}"


# --------------------------------------------------------------------------
# API pública
# --------------------------------------------------------------------------

def movie(imdb_id):
    if not isinstance(imdb_id, str) or not IMDB_RE.fullmatch(imdb_id.strip()):
        return ""

    return _page_stream(_movie_page_url(imdb_id.strip()))


def series(imdb_id, season, episode):
    if not isinstance(imdb_id, str) or not IMDB_RE.fullmatch(imdb_id.strip()):
        return ""

    page_url = _series_page_url(imdb_id.strip(), season, episode)

    return _page_stream(page_url) if page_url else ""


def _positive_int(value):
    try:
        number = int(str(value).strip())
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


def _build_stream(stream_url, page_url, title):
    return {
        "name": TITLE,
        "title": title,
        "url": stream_url,
        "behaviorHints": {
            "notWebReady": False,
            "notMyMetadata": True,
            "proxyHeaders": {
                "request": {
                    "User-Agent": USER_AGENT,
                    "Origin": BASE_URL,
                    "Referer": page_url,
                }
            },
        },
    }


def get_streams(media_type, media_id, config=None):
    if media_type not in ("movie", "series"):
        return []

    if not isinstance(media_id, str):
        return []

    parts = media_id.strip().split(":")
    imdb_id = parts[0].strip()

    if not IMDB_RE.fullmatch(imdb_id):
        return []

    if media_type == "movie":
        page_url = _movie_page_url(imdb_id)
        title = "Dublado"

    else:
        if len(parts) != 3:
            return []

        season = _positive_int(parts[1])
        episode = _positive_int(parts[2])

        if season is None or episode is None:
            return []

        page_url = _series_page_url(imdb_id, season, episode)

        if not page_url:
            return []

        title = f"Dublado S{season:02d}E{episode:02d}"

    stream_url = _page_stream(page_url)

    if not stream_url:
        return []

    return [_build_stream(stream_url, page_url, title)]
