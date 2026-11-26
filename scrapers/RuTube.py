# -*- coding: utf-8 -*-
"""
RuTube.py — TVLINK scraper
Version: 0.3.1
"""
import os
import re
import sys
import urllib.parse
from pathlib import Path
import requests

root_dir = os.path.dirname(sys.argv[0])
if root_dir and root_dir not in sys.path:
    sys.path.append(root_dir)
local_dir = os.path.dirname(os.path.abspath(__file__))
if local_dir not in sys.path:
    sys.path.append(local_dir)

import utils
from utils import DEF_BROWSER

BASE = "https://rutube.ru"
LIVE_PAGE = BASE + "/feeds/live/"
LIVE_FEED = BASE + "/api/feeds/live/?format=json"
GROUP_TV = "Федеральные каналы"
GROUP_LIVE = "ТВ трансляции"

CUSTOM_CHANNELS = [
    ("ЛУЧШИЕ ТРИЛЛЕРЫ", "f7c1387e288223779b2b53937843233a"),
]
FALLBACK_CHANNELS = [
    ("Первый канал", "c58f502c7bb34a8fcdd976b221fca292"),
]
MAX_LIVE_FEEDS = 10
MAX_PAGES_PER_FEED = 5
TIMEOUT = 15
ID_RE = re.compile(r"([0-9a-f]{32})", re.I)


def _clean_title(value):
    value = (value or "").strip()
    if not value:
        return ""
    if value.lower().startswith("иконка канала"):
        value = value[len("иконка канала"):].strip(" :·-–—")
    return re.sub(r"\s+", " ", value).strip()[:120]


def _walk_dicts(obj):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk_dicts(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk_dicts(value)


def _extract_logo(item):
    if not isinstance(item, dict):
        return ""
    for key in ("thumbnail_url", "picture", "logo", "poster", "image"):
        val = item.get(key)
        if isinstance(val, str) and val.startswith("http"):
            return val
    for sub_key in ("video_channel", "author"):
        sub = item.get(sub_key)
        if isinstance(sub, dict):
            for key in ("picture", "logo", "thumbnail", "poster"):
                val = sub.get(key)
                if isinstance(val, str) and val.startswith("http"):
                    return val
    return ""


class Scraper:
    def __init__(self):
        self.source = Path(__file__).stem
        self.link = f"ext:{self.source}:"
        self.headers = {"User-Agent": DEF_BROWSER, "Referer": BASE + "/"}
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": DEF_BROWSER,
            "Accept": "application/json, text/plain, */*",
            "Referer": LIVE_PAGE,
        })

    def getHeaders(self):
        return self.headers

    def _get_json(self, url, params=None, referer=None):
        headers = {"Referer": referer} if referer else None
        response = self.session.get(url, params=params, headers=headers, timeout=TIMEOUT)
        response.raise_for_status()
        return response.json()

    def _collect_from_widget(self, start_url, require_live=False):
        items, seen_ids, seen_urls = [], set(), set()
        url, page = start_url, 0
        while url and page < MAX_PAGES_PER_FEED:
            if url in seen_urls:
                break
            seen_urls.add(url)
            page += 1
            try:
                data = self._get_json(url)
            except Exception as exc:
                print(f"[RuTube] Widget request failed: {exc}")
                break
            for entry in _walk_dicts(data):
                vid = entry.get("id")
                title = _clean_title(entry.get("title") or entry.get("name"))
                if not (isinstance(vid, str) and ID_RE.fullmatch(vid) and title):
                    continue
                if vid in seen_ids:
                    continue
                # Reject explicitly non-live entries only for live-feed collection.
                if require_live and entry.get("is_live") is False:
                    continue
                seen_ids.add(vid)
                items.append((title, vid, _extract_logo(entry)))
            next_url = data.get("next") if isinstance(data, dict) else None
            if isinstance(next_url, str) and next_url.strip():
                url = urllib.parse.urljoin(BASE, next_url)
            else:
                break
        return items

    def _get_federal_channels(self):
        endpoints = [
            f"{BASE}/api/v1/feeds/tabwidget/maintvbroadcasttabs_no-favs/?page=1&per_page=50&show_hidden_videos=False",
            f"{BASE}/api/feeds/autowidget/3?limit=50",
        ]
        result, seen = [], set()
        for ep in endpoints:
            for title, vid, logo in self._collect_from_widget(ep, require_live=False):
                if vid not in seen:
                    seen.add(vid)
                    result.append((title, vid, logo))
        if not result:
            result = [(title, vid, "") for title, vid in FALLBACK_CHANNELS]
        return result

    def _get_live_channels(self):
        try:
            data = self._get_json(LIVE_FEED, referer=BASE + "/feeds/stream/")
        except Exception as exc:
            print(f"[RuTube] Live index request failed: {exc}")
            return []
        feed_urls, seen_urls = [], set()
        excluded = ("banner", "tvfavorites", "history", "tvarchive", "cardgroup")
        for item in _walk_dicts(data):
            url = item.get("url")
            if not isinstance(url, str):
                continue
            if "/api/" not in url or not ("/video/" in url or "/feeds/" in url):
                continue
            if any(term in url.lower() for term in excluded):
                continue
            full_url = urllib.parse.urljoin(BASE, url)
            if full_url not in seen_urls:
                seen_urls.add(full_url)
                feed_urls.append(full_url)
        channels, seen_ids = [], set()
        for feed_url in feed_urls[:MAX_LIVE_FEEDS]:
            for title, vid, logo in self._collect_from_widget(feed_url, require_live=True):
                if vid not in seen_ids:
                    seen_ids.add(vid)
                    channels.append((title, vid, logo))
        return channels

    def Channels(self):
        LL, seen_titles, seen_ids = [], set(), set()

        def add_entry(title, vid, group, logo=""):
            clean = _clean_title(title)
            if not clean or not isinstance(vid, str) or not ID_RE.fullmatch(vid):
                return
            title_key, id_key = clean.casefold(), vid.lower()
            if title_key in seen_titles or id_key in seen_ids:
                return
            seen_titles.add(title_key)
            seen_ids.add(id_key)
            ch_id = utils.title_to_crc32(clean)
            LL.append((ch_id, clean, group, f"{self.link}http://{vid}", logo or ""))

        for title, vid in CUSTOM_CHANNELS:
            add_entry(title, vid, GROUP_LIVE)
        for title, vid, logo in self._get_federal_channels():
            add_entry(title, vid, GROUP_TV, logo)
        for title, vid, logo in self._get_live_channels():
            add_entry(title, vid, GROUP_LIVE, logo)
        if LL:
            utils.ch_inputs_DB(self.source, LL)
            print(f"[RuTube] Catalog loaded: {len(LL)} unique entries")
            return True
        print("[RuTube] No channels collected")
        return False

    def getLink(self, lnk):
        match = ID_RE.search(str(lnk))
        if not match:
            return lnk
        video_id = match.group(1)
        query = urllib.parse.urlencode({"no_404": "true", "referer": LIVE_PAGE})
        url = f"{BASE}/api/play/options/{video_id}/?{query}"
        try:
            data = self._get_json(url, referer=LIVE_PAGE)
        except Exception as exc:
            print(f"[RuTube] Playback options request failed: {exc}")
            return ""
        live = data.get("live_streams") or {}
        hls_list = live.get("hls") or []
        for stream in hls_list:
            if isinstance(stream, dict) and isinstance(stream.get("url"), str):
                return stream["url"]
        balancer = data.get("video_balancer") or {}
        for key in ("hls_url", "default"):
            stream_url = balancer.get(key)
            if isinstance(stream_url, str) and stream_url.startswith("http"):
                return stream_url
        return ""
