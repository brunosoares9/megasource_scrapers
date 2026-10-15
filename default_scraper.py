# -*- coding: utf-8 -*-
"""MegaSource - WatchPlay scraper corrigido.

A origem exige Referer e Origin também nas requisições HLS. O Stremio
recebe esses cabeçalhos por behaviorHints.proxyHeaders.
"""

import http.cookiejar
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request


TITLE = "WatchPlay Scraper"
VERSION = "1.2.0"
DESCRIPTION = "Filmes e séries dublados do WatchPlay"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0 Safari/537.36"
)

TMDB_API_KEY = "1865f43a0549ca50d341dd9ab8b29f49"

BASE_URL = "https://v2.watchplay.shop"
TIMEOUT = 20


_cookiejar = http.cookiejar.CookieJar()

_opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(_cookiejar)
)


def _request(url, method="GET", data=None, headers=None):
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

    req = urllib.request.Request(
        url,
        data=body,
        headers=request_headers,
        method=method.upper(),
    )

    try:
        with _opener.open(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
            charset = resp.headers.get_content_charset() or "utf-8"

            return resp.status, raw.decode(
                charset,
                errors="replace",
            )

    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read().decode(
                "utf-8",
                errors="replace",
            )

        except Exception:
            return exc.code, ""

    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
    ) as exc:
        logging.warning(
            "Falha HTTP em %s: %s",
            url,
            exc,
        )

        return 0, ""


def _json_response(body):
    try:
        value = json.loads(body)

        return value if isinstance(value, dict) else None

    except (
        TypeError,
        ValueError,
    ):
        return None


def imdb_to_tmdb(imdb_id):
    if (
        not isinstance(imdb_id, str)
        or not re.fullmatch(r"tt\d+", imdb_id.strip())
    ):
        return None

    imdb_id = imdb_id.strip()

    find_url = (
        "https://api.themoviedb.org/3/find/"
        + urllib.parse.quote(imdb_id)
    )

    query = urllib.parse.urlencode(
        {
            "api_key": TMDB_API_KEY,
            "external_source": "imdb_id",
            "language": "pt-BR",
        }
    )

    status, body = _request(
        find_url + "?" + query
    )

    if status != 200:
        logging.error(
            "TMDB retornou HTTP %s",
            status,
        )

        return None

    data = _json_response(body)

    if not data:
        return None

    if data.get("movie_results"):
        item = data["movie_results"][0]

        return {
            "type": "movie",
            "tmdb_id": item.get("id"),
            "title": (
                item.get("title")
                or item.get("original_title")
                or TITLE
            ),
        }

    if data.get("tv_results"):
        item = data["tv_results"][0]

        return {
            "type": "tv",
            "tmdb_id": item.get("id"),
            "title": (
                item.get("name")
                or item.get("original_name")
                or TITLE
            ),
        }

    return None


def _decode_js_string(value):
    if (
        len(value) >= 2
        and value[0] in ('"', "'")
        and value[-1] == value[0]
    ):
        value = value[1:-1]

    value = value.replace("\\/", "/")

    try:
        value = bytes(
            value,
            "utf-8",
        ).decode("unicode_escape")

    except UnicodeDecodeError:
        pass

    return value


def _extract_player_url(html):
    if not html:
        return ""

    patterns = [
        (
            r"createMyPlayer\s*\(\s*\{.*?"
            r"\burl\s*:\s*([\"'])(.*?)\1"
        ),
        r"\burl\s*[:=]\s*([\"'])(.*?)\1",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )

        if not match:
            continue

        url = _decode_js_string(
            '"' + match.group(2) + '"'
        ).strip()

        if url.startswith("//"):
            url = "https:" + url

        if url.startswith(
            (
                "http://",
                "https://",
            )
        ):
            return url

    return ""


def _page_stream(page_url):
    status, body = _request(
        page_url,
        headers={
            "Referer": BASE_URL + "/",
            "Accept": "text/html,application/xhtml+xml",
        },
    )

    if status != 200:
        logging.error(
            "WatchPlay retornou HTTP %s para %s",
            status,
            page_url,
        )

        return ""

    stream_url = _extract_player_url(body)

    if not stream_url:
        logging.error(
            "M3U8 não encontrado em %s",
            page_url,
        )

        return ""

    check_status, playlist = _request(
        stream_url,
        headers={
            "Referer": page_url,
            "Origin": BASE_URL,
            "Accept": (
                "application/vnd.apple.mpegurl,"
                "application/x-mpegURL,*/*"
            ),
        },
    )

    if (
        check_status != 200
        or not playlist.lstrip().startswith("#EXTM3U")
    ):
        logging.error(
            "M3U8 inválido ou bloqueado: HTTP %s para %s",
            check_status,
            stream_url,
        )

        return ""

    return stream_url


def movie(imdb_id):
    page_url = (
        f"{BASE_URL}/movie/"
        f"{urllib.parse.quote(imdb_id, safe='')}"
    )

    return _page_stream(page_url)


def series(imdb_id, season, episode):
    tmdb_info = imdb_to_tmdb(imdb_id)

    if (
        not tmdb_info
        or not tmdb_info.get("tmdb_id")
    ):
        return ""

    page_url = (
        f"{BASE_URL}/tvshow/"
        f"{urllib.parse.quote(str(tmdb_info['tmdb_id']), safe='')}"
        f"/{int(season)}/{int(episode)}"
    )

    return _page_stream(page_url)


def _positive_int(value):
    try:
        number = int(str(value).strip())

        return number if number > 0 else None

    except (
        TypeError,
        ValueError,
    ):
        return None


def get_streams(media_type, media_id, config=None):
    if (
        media_type not in ("movie", "series")
        or not isinstance(media_id, str)
    ):
        return []

    parts = media_id.strip().split(":")
    imdb_id = parts[0].strip()

    if not re.fullmatch(r"tt\d+", imdb_id):
        return []

    if media_type == "movie":
        page_url = (
            f"{BASE_URL}/movie/"
            f"{urllib.parse.quote(imdb_id, safe='')}"
        )

        stream_url = _page_stream(page_url)
        title = "Dublado"

    else:
        if len(parts) != 3:
            return []

        season = _positive_int(parts[1])
        episode = _positive_int(parts[2])

        if season is None or episode is None:
            return []

        tmdb_info = imdb_to_tmdb(imdb_id)

        if (
            not tmdb_info
            or not tmdb_info.get("tmdb_id")
        ):
            return []

        page_url = (
            f"{BASE_URL}/tvshow/"
            f"{tmdb_info['tmdb_id']}/"
            f"{season}/{episode}"
        )

        stream_url = _page_stream(page_url)
        title = f"Dublado S{season:02d}E{episode:02d}"

    if not stream_url:
        return []

    return [
        {
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
    ]
