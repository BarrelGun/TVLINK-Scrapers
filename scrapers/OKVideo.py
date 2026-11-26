# -*- coding: utf-8 -*-
"""
OKVideo.py — TVLINK scraper
Version: 0.2
"""
import re
import html
from pathlib import Path

import requests
import utils

BASE = "https://ok.ru"
LIVE_PAGE = BASE + "/video/live/"
GROUP = "Телеканалы OK"
TIMEOUT = 20

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36"
)

RADIO_RE = re.compile(
    r"\bрадио\b|\bradio\b|\bfm\b|радиостанц",
    re.I
)


def _clean_title(value):
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:120]


class Scraper:
    def __init__(self):
        self.source = Path(__file__).stem
        self.link = "ext:%s:" % self.source

        self.headers = {
            "User-Agent": UA,
            "Referer": BASE + "/",
        }

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,*/*",
            "Referer": LIVE_PAGE,
        })

    def getHeaders(self):
        return self.headers

    def Channels(self):
        try:
            response = self.session.get(
                LIVE_PAGE,
                timeout=TIMEOUT
            )
            response.raise_for_status()
            page = html.unescape(response.text)
        except Exception as exc:
            print("[OKVideo] Ошибка загрузки каталога: %s" % exc)
            return False

        blocks = re.findall(
            r'<div class="video-card_n-w">(.*?)(?=<div class="video-card_n-w">|$)',
            page,
            re.S
        )

        LL = []
        seen_ids = set()
        skipped_radio = 0

        for block in blocks:
            mid = re.search(r'openMovie\("(\d+)"', block)
            title_match = re.search(
                r'class="video-card_n[^"]*"[^>]*title="([^"]*)"',
                block
            )

            if not mid or not title_match:
                continue

            video_id = mid.group(1).strip()
            title = _clean_title(title_match.group(1))

            if not video_id or not title:
                continue

            if video_id in seen_ids:
                continue

            seen_ids.add(video_id)

            if RADIO_RE.search(title):
                skipped_radio += 1
                continue

            LL.append((
                utils.title_to_crc32(title),
                title,
                GROUP,
                self.link + "http://" + video_id,
                "",
            ))

        if not LL:
            print("[OKVideo] Каналы не найдены")
            return False

        try:
            utils.ch_inputs_DB(self.source, LL)
            print("[OKVideo] Загружено каналов: %d" % len(LL))
            print("[OKVideo] Исключено радиостанций: %d" % skipped_radio)
            return True
        except Exception as exc:
            print("[OKVideo] Ошибка записи каталога: %s" % exc)
            return False

    def getLink(self, lnk):
        value = str(lnk or "")

        match = re.search(r"(\d+)", value)
        if not match:
            print("[OKVideo] Некорректная ссылка: %s" % value)
            return ""

        video_id = match.group(1)
        page_url = BASE + "/video/" + video_id

        try:
            response = self.session.get(
                page_url,
                timeout=TIMEOUT
            )
            response.raise_for_status()

            page = html.unescape(response.text)
            page = page.replace("\\/", "/")
            page = page.replace("\\u0026", "&")

            match = re.search(
                r'"hlsMasterPlaylistUrl"\s*:\s*"([^"]+)"',
                page
            )

            if match:
                stream_url = match.group(1)
                stream_url = stream_url.replace("\\/", "/")
                stream_url = stream_url.replace("\\u0026", "&")

                if stream_url.startswith("http"):
                    print("[OKVideo] HLS получен: %s" % video_id)
                    return stream_url

            print("[OKVideo] HLS не найден: %s" % video_id)

        except Exception as exc:
            print("[OKVideo] Ошибка получения HLS: %s" % exc)

        return ""


print("[OKVideo] Scraper 0.2 загружен")
