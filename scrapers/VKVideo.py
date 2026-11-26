# -*- coding: utf-8 -*-
"""Провайдер VK Видео — телеканалы через api.vkvideo.ru. VKVideo 0.3 для TVLINK.

Принципы:
- один запрос каталога catalog.getVideo{'url':'/tvchannels'}, секции
  добираем постранично catalog.getSection с next_from;
- токен: сначала в память, потом на диск — сеть не штормит;
- прогрев не чаще раза в минуту;
- холодный старт не блокирует GUI: get_channels сразу вернёт пусто,
  каталог докачается в фоновой нити, о готовности скажет on_loaded;
- кеш каталога бессрочный, обновление только по «Обновить».
"""
import base64
import gzip
import http.cookiejar
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import zlib
import sys

# TVLINK добавляет папку scrapers в sys.path; сохраняем совместимость
# с запуском скрапера напрямую.
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

import utils


ADDON_ID = 'script.virut'
VER = '0.3'

UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                    'AppleWebKit/605.1.15 (KHTML, like Gecko) '
                    'Version/17.4 Safari/605.1.15',
      'Accept': 'application/json, text/plain, */*',
      'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8',
      'Accept-Encoding': 'gzip',
      'Origin': 'https://vkvideo.ru',
      'Referer': 'https://vkvideo.ru/tvchannels'}

API = 'https://api.vkvideo.ru/method/'
TOKEN_URL = 'https://login.vk.ru/?act=get_anonym_token'
CLIENT_SECRET = 'o557NLIkAErNhakXrQ7A'
CLIENT_ID = '52461373'
SCOPES = ('audio_anonymous,video_anonymous,photos_anonymous,'
          'profile_anonymous')
APP_ID = '6287487'

WARMUP_URL = 'https://vkvideo.ru/tvchannels'
AL_URLS = ('https://vk.com/al_video.php', 'https://vkvideo.ru/al_video.php')

MAX_CHANNELS = 2000
MAX_PAGES = 30

PROVIDER_ID = 'vkvideo'
GROUP_ALL = 'Телеканалы VK'
ACTIVITY_TV = 'Телеканал'

# --- синхронизация ---
_STOP = threading.Event()
_LOCK = threading.Lock()
_WARM_TS = [0.0]

_CATALOG = None
_LOAD_THREAD = None
_LOAD_DONE = threading.Event()


def set_stop():
    _STOP.set()


# ------------------------------------------------------------- база
def _resolve_base():
    import sys
    for name, mod in list(sys.modules.items()):
        if name.split('.')[-1] == 'provider_base' and mod is not None:
            b = getattr(mod, 'BaseProvider', None)
            if b is not None:
                return b
    for pkg in ('lib.provider_base', 'script.virut.lib.provider_base'):
        try:
            m = __import__(pkg, fromlist=['BaseProvider'])
            b = getattr(m, 'BaseProvider', None)
            if b is not None:
                return b
        except Exception:
            pass
    return object


# ------------------------------------------------------------- профиль
def profile_dir():
    """Отдельное хранилище анонимного токена и каталога VK для TVLINK."""
    path = '/storage/.config/tvlink/vkvideo'
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass
    return path


def _log(message, level=None):
    print(str(message))


_OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def _cookie_jar():
    for h in _OPENER.handlers:
        if isinstance(h, urllib.request.HTTPCookieProcessor):
            return h.cookiejar
    return None


def _fetch(url, timeout=15, data=None):
    body = urllib.parse.urlencode(data).encode('utf-8') if data else None
    req = urllib.request.Request(url, data=body, headers=UA,
                                 method='POST' if data else 'GET')
    resp = _OPENER.open(req, timeout=timeout)
    raw = resp.read()
    if 'gzip' in (resp.headers.get('Content-Encoding') or '').lower() \
            or raw[:2] == b'\x1f\x8b':
        raw = gzip.decompress(raw)
    return raw.decode('utf-8', 'ignore')


def _warmup():
    """Греем сайт не чаще раза в минуту."""
    if time.time() - _WARM_TS[0] < 60:
        return
    _WARM_TS[0] = time.time()
    try:
        _fetch(WARMUP_URL, timeout=10)
        jar = _cookie_jar()
        _log('[virut-vk] %s прогрев: куки %d'
                 % (VER, len(list(jar)) if jar else 0))
    except Exception as e:
        _log('[virut-vk] %s прогрев: %r' % (VER, e))


# ------------------------------------------------------------- токен
_TOKEN_PATH = None
_TOKEN = None      # (token, exp) в памяти — диск вторичен
_TOKEN_ERR = 0     # время последней неудачи: пауза 5 мин


def _jwt_exp(token):
    try:
        part = token.split('.')[1]
        part += '=' * (-len(part) % 4)
        return int(json.loads(base64.urlsafe_b64decode(part)
                              .decode('utf-8')).get('exp') or 0)
    except Exception:
        return 0


def _token_get():
    """-> токен из памяти или диска, либо None."""
    global _TOKEN
    if _TOKEN:
        tok, exp = _TOKEN
        if exp > time.time() + 60:
            return tok
        _TOKEN = None
    p = os.path.join(profile_dir(), 'vk_anonym_token.json')
    try:
        if p and os.path.isabs(p) and os.path.exists(p):
            with open(p, 'r', encoding='utf-8') as fh:
                data = json.load(fh)
            tok, exp = data.get('token'), int(data.get('exp') or 0)
            if tok and exp > time.time() + 60:
                _TOKEN = (tok, exp)
                return tok
    except Exception:
        pass
    return None


def _token_new(force=False):
    """Получить свежий токен (или взять из кеша)."""
    global _TOKEN_ERR
    if not force:
        tok = _token_get()
        if tok:
            return tok
        if _TOKEN_ERR and time.time() - _TOKEN_ERR < 300:
            return None
    _warmup()
    body = {'client_secret': CLIENT_SECRET, 'client_id': CLIENT_ID,
            'scopes': SCOPES, 'isApiOauthAnonymEnabled': 'false',
            'version': '1', 'app_id': APP_ID}
    old = _token_get() or ''
    if old:
        body['anonymous_token'] = old
    try:
        text = _fetch(TOKEN_URL, data=body, timeout=15)
    except Exception as e:
        _log('[virut-vk] %s токен: сеть: %r' % (VER, e))
        _TOKEN_ERR = time.time()
        return None
    tok = None
    m = re.search(r'anonym\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+',
                  text)
    if m:
        tok = m.group(0)
    else:
        try:
            for d in _walk(json.loads(text)):
                for v in d.values():
                    if isinstance(v, str) and v.startswith('anonym.'):
                        tok = v
                        break
                if tok:
                    break
        except Exception:
            pass
    if not tok:
        _TOKEN_ERR = time.time()
        return None
    exp = _jwt_exp(tok) or int(time.time()) + 20 * 3600
    global _TOKEN
    _TOKEN = (tok, exp)                     # в память сразу
    try:                                    # на диск — по возможности
        p = os.path.join(profile_dir(), 'vk_anonym_token.json')
        if p and os.path.isabs(p):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, 'w', encoding='utf-8') as fh:
                json.dump({'token': tok, 'exp': exp}, fh)
        _log('[virut-vk] %s токен сохранён' % VER)
    except Exception as e:
        _log('[virut-vk] %s токен не сохранён на диск: %r'
                 % (VER, e))
    _TOKEN_ERR = 0
    return tok


# ------------------------------------------------------------- утилиты
def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


# ------------------------------------------------------------- API
def api_call(method, extra=None, timeout=15, force_post=False):
    base = {'v': '5.289', 'client_id': CLIENT_ID,
            'https': '1', 'lang': 'ru', 'device': 'web'}
    base.update(extra or {})
    tok = _token_new()
    if tok:
        base['access_token'] = tok
    text = _api_raw(method, base, force_post)
    data = _api_parse(method, text)
    if data is None:
        return None
    if isinstance(data, dict) and data.get('error') \
            and data['error'].get('error_code') == 5:
        tok = _token_new(force=True)
        if tok:
            base['access_token'] = tok
            text = _api_raw(method, base, force_post)
            data = _api_parse(method, text)
    return data.get('response') if isinstance(data, dict) \
        and 'response' in data else (None if isinstance(data, dict)
                                     and 'error' in data else data)


def _api_raw(method, base, force_post):
    try:
        if force_post:
            return _fetch(API + method, data=base, timeout=15)
        return _fetch('%s%s?%s' % (API, method,
                                   urllib.parse.urlencode(base)))
    except Exception as e:
        _log('[virut-vk] %s %s: сеть: %r' % (VER, method, e))
        return None


def _api_parse(method, text):
    if not text:
        return None
    try:
        data = json.loads(text)
    except Exception:
        _log('[virut-vk] %s %s: не-JSON: %r' % (VER, method,
                                                    text[:200]))
        return None
    if isinstance(data, dict) and 'error' in data:
        err = data['error']
        _log('[virut-vk] %s %s: ошибка %s: %s'
                 % (VER, method, err.get('error_code'),
                    err.get('error_msg')))
    return data


# ------------------------------------------------------------- каталог
GID_RE = re.compile(r'^(-?\d+)_(\d+)$')


def _is_group(d):
    return (isinstance(d, dict) and isinstance(d.get('id'), int)
            and d.get('name') and any(k.startswith('photo_') for k in d))


def _is_media(g):
    if not isinstance(g, dict):
        return False
    return (g.get('activity') or '').strip() == ACTIVITY_TV \
        or bool(g.get('verified'))


class _Acc:
    """Накопитель: группы, секции, каналы (id -> [имя, oid, vid, лого,
    множество секций])."""

    def __init__(self):
        self.groups = {}
        self.sections = []
        self.seen = set()
        self.chans = {}
        self.media = set()

    def feed(self, resp, sid=''):
        if not resp:
            return None
        for d in _walk(resp):
            if _is_group(d):
                self.groups.setdefault(d['id'], d)
            if (isinstance(d.get('id'), str) and d.get('title')
                    and 'tvchannels' in str(d.get('url') or '')
                    and d['id'] not in self.seen):
                self.seen.add(d['id'])
                self.sections.append((d['id'], d['title']))
        nf = None
        for d in _walk(resp):
            vids = d.get('videos_ids')
            if isinstance(vids, list):
                for pair in vids:
                    m = GID_RE.match(str(pair))
                    if not m:
                        continue
                    oid, vid = int(m.group(1)), m.group(2)
                    g = self.groups.get(abs(oid))
                    if not g:
                        continue
                    name = (g.get('name') or '').strip()
                    if not name:
                        continue
                    row = self.chans.get((oid, vid))
                    if row:
                        if sid:
                            row[4].add(sid)
                    else:
                        self.chans[(oid, vid)] = [
                            name, oid, vid,
                            g.get('photo_200') or g.get('photo_100') or '',
                            {sid} if sid else set()]
                    if _is_media(g):
                        self.media.add(oid)
            v = d.get('next_from')
            if isinstance(v, str) and v.strip() \
                    and (nf is None or len(v) > len(nf)):
                nf = v
        return nf

    def out(self):
        rows = [(r[0], r[1], r[2], r[3], tuple(sorted(r[4])))
                for (oid, _v), r in self.chans.items() if oid in self.media]
        if not rows:
            rows = [(r[0], r[1], r[2], r[3], tuple(sorted(r[4])))
                    for r in self.chans.values()]
        return rows

    def count(self):
        return len(self.out())


def _cache_path():
    return os.path.join(profile_dir(), 'vk_channels_tv14.json')


def _cache_load():
    try:
        p = _cache_path()
        if p and os.path.isabs(p) and os.path.exists(p):
            with open(p, 'r', encoding='utf-8') as fh:
                data = json.load(fh)
            if isinstance(data, dict) and data.get('chans'):
                for r in data['chans']:
                    if isinstance(r[4], list):
                        r[4] = set(r[4])
                return data
    except Exception:
        pass
    return None


def _cache_save(data):
    try:
        p = _cache_path()
        if p and os.path.isabs(p):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, 'w', encoding='utf-8') as fh:
                json.dump(data, fh, ensure_ascii=False)
    except Exception as e:
        _log('[virut-vk] %s cache save: %r' % (VER, e))


def _paginate(sid, acc):
    """Добираем секцию постранично. Между страницами — проверка стопа."""
    r = api_call('catalog.getSection', {'section_id': sid})
    nf = acc.feed(r, sid)
    pages, prev = 1, None
    while nf and pages < MAX_PAGES and acc.count() < MAX_CHANNELS:
        if _STOP.is_set():
            break
        if nf == prev:
            break
        prev = nf
        nf = acc.feed(api_call('catalog.getSection',
                               {'section_id': sid, 'next_from': nf}), sid)
        pages += 1
    return pages


def get_catalog():
    """-> (данные, ok). Под _LOCK, между запросами — проверка _STOP.
    При стопе ok=False и кеш не пишется."""
    global _CATALOG
    if _CATALOG:
        return _CATALOG, True
    cached = _cache_load()
    if cached:
        _CATALOG = {'sections': cached.get('sections') or [],
                    'chans': cached.get('chans') or []}
        _log('[virut-vk] %s каталог из кеша: %d каналов'
                 % (VER, len(_CATALOG['chans'])))
        return _CATALOG, True

    with _LOCK:
        if _CATALOG:
            return _CATALOG, True
        if _STOP.is_set():
            return {'sections': [], 'chans': []}, False

        acc = _Acc()
        acc.feed(api_call('catalog.getVideo', {'url': '/tvchannels'}))

        total, done = 1, set()
        for sid, _ in list(acc.sections):
            if sid in done or _STOP.is_set():
                continue
            done.add(sid)
            pages_for_section = _paginate(sid, acc)
            total += pages_for_section
            if acc.count() >= MAX_CHANNELS:
                break

        if _STOP.is_set():
            _log('[virut-vk] %s каталог: стоп, кеш не пишем' % VER)
            return {'sections': [], 'chans': []}, False

        chans = acc.out()
        _log('[virut-vk] %s каталог загружен: %d каналов, секций %d, страниц %d'
                 % (VER, len(chans), len(acc.sections), total))
        if not chans:
            return {'sections': [], 'chans': []}, False
        _cache_save({'sections': acc.sections, 'chans': chans,
                     'ts': time.time()})
        _CATALOG = {'sections': acc.sections, 'chans': chans}
        return _CATALOG, True


def _start_background(on_loaded=None):
    """Фоновая закачка каталога. True — запустили."""
    global _LOAD_THREAD
    if _CATALOG is not None:
        return False
    t = _LOAD_THREAD
    if t is not None and t.is_alive():
        if not _LOAD_DONE.is_set():
            return False
        t.join(timeout=3)
    _LOAD_THREAD = None
    _STOP.clear()
    _LOAD_DONE.clear()

    def _run():
        try:
            data, ok = get_catalog()
            if ok and on_loaded:
                try:
                    on_loaded()
                except Exception as e:
                    _log('[virut-vk] %s on_loaded: %r' % (VER, e))
        except Exception as e:
            _log('[virut-vk] %s фоновая закачка: %r' % (VER, e))
        finally:
            _LOAD_DONE.set()

    _LOAD_THREAD = threading.Thread(target=_run, daemon=True)
    _LOAD_THREAD.start()
    return True


# ------------------------------------------------------------- поток
KEYS = ('hls_live', 'hls_live_playback', 'hls_ondemand', 'hls',
        'dash_live', 'dash_ondemand')


def _unescape(s):
    try:
        return json.loads('"%s"' % s)
    except Exception:
        return s


def resolve_video(oid, vid):
    """Поток: POST al_video.php act=show -> hls; фолбэк — discover."""
    pair = '%s_%s' % (oid, vid)
    for base in AL_URLS:
        try:
            text = _fetch(base, data={'act': 'show', 'al': '1',
                                      'video': pair, 'list': ''})
        except Exception as e:
            _log('[virut-vk] al_video %s: %r' % (base, e))
            continue
        m = re.search(r'"(hls_live|hls)"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        if m and '.m3u8' in m.group(2):
            return _unescape(m.group(2))
    for extra in ({'video_id': pair}, {'videos': pair}):
        resp = api_call('video.getVideoDiscover', extra)
        for d in _walk(resp):
            for k in KEYS:
                u = d.get(k)
                if isinstance(u, str) and u and '.m3u8' in u:
                    return u
    return None


# ------------------------------------------------------------- EPG
_EPG_MAP = {
    'первыйканал': 'pervy', 'общественноетелевидениероссииотр': 'otr',
    'твцентр': 'tvcentr', 'стс': 'sts', 'домашний': 'domashny',
    'телерадиокомпаниязвезда': 'zvezda', 'музтв': 'muztv', 'думатв': 'dumatv',
    'телеканал2х2': '2na2', 'телеканалсолнце': 'solnce',
    'телеканалспас': 'spas', 'мир24': 'mir-24',
    'телеканалсуббота': 'super', 'твстарт': 'start', 'телеканалче': 'che',
    'стсlove': 'sts-love', 'москва24': 'moskva-24', 'ртд': 'rtd-ru',
    'большаяазия': 'bolshaya-azia', 'телеканалмы': 'myy',
    'русскоерадио': 'russkoe-radio', 'европаплюс': 'europa-plus-tv',
    'лентв24': 'lot-47', 'астрахань24': 'astraxan24', 'телеканалдон24': 'don-24',
    'япервый': 'pervy-yaroslavsky', 'телеканалтольятти24': 'tolyatti24',
    'таврия': 'tavria', 'твпоиск': 'poisk-tv',
    'телеканалинфо24': 'inf24shadr', 'ямаl1': 'vremia-arktiki',
    'арктика24': 'arktika-24', 'гтркбашкортостан': 'bashkortostan24',
    'краснаялиния': 'krasnaya-linia', 'телеканалмегаполис': 'megapolis',
    'телеканаленисей': 'enisey-region', 'прима': 'prima',
    'кузбасспервый': 'kuzbas1', '10канал': '10kanal-novokuzneck',
    'телеканалволга24': 'vremia-n', 'первыйтульский': 'pervy-tulsky',
    'сейм': 'seym-kursk', '41каналворонеж': 'vtk-argus', 'телеканалю': 'yu',
    'православнаятелекомпаниясоюз': 'soyuz',
    'люберецкоетелевидение': 'ltv-lubercy', 'отв': 'otv-primorie',
    'изру': 'izvestia',
}
_LAT2CYR = {'a': 'а', 'b': 'в', 'c': 'с', 'd': 'д', 'e': 'е', 'h': 'н',
            'k': 'к', 'm': 'м', 'o': 'о', 'p': 'р', 't': 'т', 'x': 'х',
            'y': 'у'}
_NOISE = ('телеканал', 'телерадиокомпания', 'канал')


def _epg_for(title):
    k = ''.join(_LAT2CYR.get(ch, ch)
                for ch in re.sub(r'[^a-zа-яё0-9]', '',
                                 (title or '').lower()))
    if k in _EPG_MAP:
        return _EPG_MAP[k]
    for w in _NOISE:
        if k.startswith(w) and k[len(w):] in _EPG_MAP:
            return _EPG_MAP[k[len(w):]]
        if k.endswith(w) and k[:-len(w)] in _EPG_MAP:
            return _EPG_MAP[k[:-len(w)]]
    return ''


# ------------------------------------------------------------- TVLINK scraper
class Scraper:
    """Публичные телеканалы VK для TVLINK 5.2.6."""

    def __init__(self):
        self.source = os.path.splitext(os.path.basename(__file__))[0]
        self.link = "ext:%s:" % self.source
        self.headers = dict(UA)

    def getHeaders(self):
        return self.headers

    def Channels(self):
        """Загружает каталог VK и передаёт каналы в базу TVLINK."""
        try:
            data, ok = get_catalog()
        except Exception as error:
            print("[VKVideo] Ошибка получения каталога: %s" % error)
            return False

        if not ok or not data:
            print("[VKVideo] Каталог VK пока недоступен")
            return False

        rows = []
        seen = set()
        for item in data.get('chans') or []:
            try:
                title, oid, vid, logo = item[:4]
                oid = int(oid)
                vid = str(vid)
                title = re.sub(r'\s+', ' ', str(title or '')).strip()[:120]
                if not title or not re.match(r'^\d+$', vid):
                    continue
                channel_id = '%s_%s' % (oid, vid)
                if channel_id in seen:
                    continue
                seen.add(channel_id)
                channel_url = self.link + 'http://' + channel_id
                rows.append((
                    utils.title_to_crc32(title),
                    title,
                    self.source,
                    channel_url,
                    logo or '',
                ))
            except Exception as error:
                print("[VKVideo] Пропуск записи каталога: %s" % error)

        if not rows:
            print("[VKVideo] В каталоге нет подходящих телеканалов")
            return False

        try:
            utils.ch_inputs_DB(self.source, rows)
            print("[VKVideo] В TVLINK передано каналов: %d" % len(rows))
            return True
        except Exception as error:
            print("[VKVideo] Ошибка записи каналов в TVLINK: %s" % error)
            return False

    def getLink(self, link):
        """Разрешает внутренний oid_vid в HLS URL VK."""
        value = (link or '').strip()
        if value.startswith(self.link):
            value = value[len(self.link):]
        value = re.sub(r'^https?://', '', value, flags=re.I)
        match = re.search(r'(-?\d+)_(\d+)', value)
        if not match:
            print("[VKVideo] Некорректный идентификатор: %r" % value)
            return ''
        oid, vid = int(match.group(1)), match.group(2)
        try:
            stream_url = resolve_video(oid, vid)
            if stream_url and '.m3u8' in stream_url:
                print("[VKVideo] HLS получен: %s_%s" % (oid, vid))
                return stream_url
            print("[VKVideo] VK не вернул HLS для %s_%s" % (oid, vid))
        except Exception as error:
            print("[VKVideo] Ошибка получения HLS %s_%s: %s"
                  % (oid, vid, error))
        return ''


print('[VKVideo] %s модуль TVLINK загружен' % VER)
