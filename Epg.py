# Epg.py
import logging
import time
import math
import queue
import threading
from typing import List, Dict, Any
from datetime import datetime
from typing import Dict, Any, List, Tuple, Optional

import requests
from requests.adapters import HTTPAdapter
try:
    # requests>=2.32
    from urllib3.util.retry import Retry
except Exception:
    # fallback for some environments
    from requests.packages.urllib3.util.retry import Retry

from PyQt5.QtCore import QThread, pyqtSignal


# ----------------------------- Utils -----------------------------

def _safe_int(x):
    try:
        return int(x)
    except Exception:
        return None


def _epoch_to_local(ts: Optional[int]) -> Optional[datetime]:
    """
    Converts epoch seconds to local datetime. Protect against ms values.
    """
    if ts is None:
        return None
    if ts > 10_000_000_000:  # ms?
        ts = ts / 1000.0
    return datetime.fromtimestamp(ts)


def _parse_dt_str(s: Optional[str]) -> Optional[datetime]:
    """
    Parse 'YYYY-mm-dd HH:MM:SS' into naive local datetime.
    """
    if not s:
        return None
    try:
        # Many portals return this exact format
        return datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _hhmm_from_dt(dt: Optional[datetime]) -> str:
    if not dt:
        return "??:??"
    return dt.strftime("%I:%M %p").lstrip("0")  # e.g., 3:05 PM


def _dow_mon_day(dt: Optional[datetime]) -> str:
    return dt.strftime("%a, %b %d") if dt else ""


def _first_non_empty(d: Dict[str, Any], keys) -> Optional[str]:
    for k in keys:
        v = d.get(k)
        if v is not None and str(v).strip():
            return str(v)
    return None


def _truncate(s: Optional[str], max_chars: int) -> str:
    if not s:
        return ""
    s = " ".join(s.split())  # normalize whitespace/newlines
    if len(s) <= max_chars:
        return s
    cut = s[: max_chars - 1]
    # avoid breaking in the middle of a word
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "…"


def _pick_current_only(items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Pick exactly ONE item to display:
      1) The item airing *now* (start_dt <= now < end_dt)
      2) Else the first upcoming item (now < start_dt)
      3) Else the most recent past item
    """
    if not items:
        return None

    now = datetime.now()
    first_upcoming = None
    last_past = None

    for it in items:
        sd = it.get("start_dt")
        ed = it.get("end_dt")
        if sd and ed:
            if sd <= now < ed:
                return it  # current
            if now < sd and first_upcoming is None:
                first_upcoming = it
            if ed <= now:
                last_past = it

    return first_upcoming or last_past or items[0]


def format_epg_tooltip(items: List[Dict[str, Any]]) -> str:
    """
    Tooltip contains ONLY the current show's info (or next upcoming if none currently airing).
    Uses HTML so Qt will wrap text in a fixed width box.
    """
    it = _pick_current_only(items)
    if not it:
        return "No EPG."

    name = (it.get("name") or it.get("title") or "—").strip()
    sd = it.get("start_dt")
    ed = it.get("end_dt")

    start_s = _hhmm_from_dt(sd)
    end_s   = _hhmm_from_dt(ed)
    date_s  = _dow_mon_day(sd)

    header = f"<b>{name}</b> {start_s} – {end_s}"
    if date_s:
        header += f" ({date_s})"

    # Meta
    meta_bits = []
    cat = (it.get("category") or "").strip()
    if cat:
        meta_bits.append(cat)
    mins = it.get("duration_min")
    if isinstance(mins, int) and mins > 0:
        meta_bits.append(f"{mins} min")

    descr = (it.get("descr") or "").strip()
    if descr:
        descr = " ".join(descr.split())
        if len(descr) > 500:
            cut = descr[:499]
            cut = cut.rsplit(" ", 1)[0] if " " in cut else cut
            descr = cut + "…"

    parts = [header]
    if meta_bits:
        parts.append(f"<i>{' • '.join(meta_bits)}</i>")
    if descr:
        parts.append(descr)

    # return HTML with wrapping and fixed width (~300px)
    return (
        "<qt><div style='white-space: normal; "
        "width: 300px;'>" + "<br>".join(parts) + "</div></qt>"
    )

# ---------------------- HTTP session builders --------------------

def _build_session(max_retries: int, backoff_factor: float) -> requests.Session:
    """
    HTTP session with keep-alive, gzip and robust retry/backoff.
    """
    s = requests.Session()
    # Default headers to keep connections hot and payloads small
    s.headers.update({
        "Connection": "keep-alive",
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
        "User-Agent": ("Mozilla/5.0 (QtEmbedded; U; Linux; C) "
                       "AppleWebKit/533.3 (KHTML, like Gecko) "
                       "MAG200 stbapp ver: 2 rev: 250 Safari/533.3")
    })

    retry = Retry(
        total=max_retries,
        connect=max_retries,
        read=max_retries,
        status=max_retries,
        backoff_factor=backoff_factor,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    # Pool sizes roughly match our gentle, batched prefetcher
    adapter = HTTPAdapter(max_retries=retry, pool_connections=32, pool_maxsize=64)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


# ----------------------------- EPG -------------------------------

class EpgManager(QThread):
    """
    Lightweight EPG worker with in-memory cache and robust networking.

    Signals:
      epg_ready(channel_key: str, items: list[dict])

    Public API:
      - request(channel_dict, size=6)
      - reconfigure(mode, base_url, session, mac, token_provider)
    """

    epg_ready = pyqtSignal(str, list)

    def __init__(
        self,
        mode: str,
        base_url: str,
        session: Optional[requests.Session] = None,
        mac: Optional[str] = None,
        token_provider=None,
        connect_timeout: float = 0.6,   # faster connect fail
        read_timeout: float = .5,       # don't hang long on slow portals
        max_retries: int = 0,           # light but helpful
        backoff_factor: float = 0.2,    # gentle ramp
        cache_ttl: float = 180.0,       # seconds; reuse while navigating
        max_items_default: int = 6
    ):
        super().__init__()
        self.mode = mode
        self.base_url = base_url.rstrip("/")
        self.session = session or _build_session(max_retries, backoff_factor)
        self.mac = mac or ""
        self.token_provider = token_provider
        self.connect_timeout = float(connect_timeout)
        self.read_timeout = float(read_timeout)
        self.cache_ttl = float(cache_ttl)
        self.max_items_default = int(max_items_default)

        # Queue entries: (key, channel_dict, requested_size)
        self._queue: "queue.Queue[Tuple[str, Dict[str, Any], int]]" = queue.Queue()
        self._stop = False

        # Cache: key -> (timestamp, normalized_items_full)
        # We store the FULL normalized list returned by the portal (not truncated),
        # then slice on emit. This lets us serve future smaller requests from cache
        # and only refetch if caller asks for MORE than we currently have.
        self._cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
        self._cache_lock = threading.Lock()

        # Debounce repeated requests for same key
        self._last_requested: Dict[str, float] = {}
        self._debounce_sec = 0.15

        self.start()


    def _is_stalker(self) -> bool:
        return (self.mode or "").lower() == "stalker"

    def _epg_endpoints(self) -> list:
        """
        Return EPG endpoint(s) to try.
        Stalker: /stalker_portal/server/load.php (then /stalker_portal/load.php)
        Generic: /portal.php
        """
        base = self.base_url.rstrip("/")
        if self._is_stalker():
            return [
                f"{base}/stalker_portal/server/load.php",
                f"{base}/stalker_portal/load.php",
            ]
        return [f"{base}/portal.php"]


    def cancel_pending(self):
        """
        Drop any queued EPG requests so we don't keep fetching for stale lists.
        """
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass


    # ----------------- Public API -----------------

    def reconfigure(
        self,
        mode: Optional[str] = None,
        base_url: Optional[str] = None,
        session: Optional[requests.Session] = None,
        mac: Optional[str] = None,
        token_provider=None,
    ):
        if mode is not None:
            self.mode = mode
        if base_url is not None:
            self.base_url = base_url.rstrip("/")
        if session is not None:
            self.session = session
        if mac is not None:
            self.mac = mac
        if token_provider is not None:
            self.token_provider = token_provider

    def stop(self):
        self._stop = True

    def request(self, channel: Dict[str, Any], size: Optional[int] = None):
        """
        Enqueue a fetch for this channel. If cached & fresh, emits immediately.
        Will only refetch if cache expired OR caller asks for more items than cached.
        """
        req_size = int(size or self.max_items_default)
        key = self._channel_key(channel)
        if not key:
            return

        # Debounce identical rapid requests
        now = time.time()
        last = self._last_requested.get(key, 0.0)
        if (now - last) < self._debounce_sec:
            return
        self._last_requested[key] = now

        # Serve from cache if fresh & large enough
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit:
                ts, cached_full = hit
                fresh = (now - ts) <= self.cache_ttl
                if fresh and len(cached_full) >= req_size:
                    self.epg_ready.emit(key, cached_full[:req_size])
                    return
                # If fresh but not enough items, we'll try to refetch below

        self._queue.put((key, channel, req_size))

    # ----------------- Thread loop -----------------

    def run(self):
        while not self._stop:
            try:
                key, channel, req_size = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue

            try:
                token, headers, cookies = self._get_auth()
                # Ask server for at least req_size, but some endpoints ignore 'size'
                items_raw = self._try_fetch_epg(channel, req_size, token, headers, cookies)
            except Exception as e:
                logging.warning(f"[EPG] worker unexpected error: {e}")
                items_raw = []

            # Normalize (no truncation here)
            normalized_full = self._normalize_items(items_raw)

            # Cache full list
            with self._cache_lock:
                self._cache[key] = (time.time(), normalized_full)

            # Emit only what caller asked for
            self.epg_ready.emit(key, normalized_full[:req_size])

    # ----------------- Fetching -----------------

    def _get_auth(self) -> Tuple[Optional[str], Dict[str, str], Dict[str, str]]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (QtEmbedded; U; Linux; C) "
                "AppleWebKit/533.3 (KHTML, like Gecko) "
                "MAG200 stbapp ver: 2 rev: 250 Safari/533.3"
            )
        }
        cookies = {"mac": self.mac or "", "stb_lang": "en", "timezone": "Europe/London"}
        token = None

        if callable(self.token_provider):
            try:
                t, h, c = self.token_provider()
                token = t or None
                if isinstance(h, dict): headers.update(h)
                if isinstance(c, dict): cookies.update(c)
            except Exception as e:
                logging.debug(f"EPG token_provider error (non-fatal): {e}")

        if token and "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {token}"
        if token and "token" not in cookies:
            cookies["token"] = token

        if self._is_stalker():
            headers.setdefault("X-User-Agent", "Model: MAG254; Link: WiFi")
            base = self.base_url.rstrip("/")
            headers.setdefault("Referer", f"{base}/stalker_portal/c/")

        return token, headers, cookies


    def _portal_get(self, params: Dict[str, Any], headers: Dict[str, str], cookies: Dict[str, str]) -> Dict[str, Any]:
        """
        Safe GET to portal endpoint with proper timeouts + retry session.
        Tries multiple endpoints for Stalker variants.
        """
        last_err = None
        for url in self._epg_endpoints():
            try:
                r = self.session.get(
                    url,
                    params=params,
                    headers=headers,
                    cookies=cookies,
                    timeout=(self.connect_timeout, self.read_timeout),
                )
                r.raise_for_status()
                if not r.content:
                    return {}
                return r.json()
            except requests.exceptions.HTTPError as e:
                last_err = e
                status = getattr(e.response, "status_code", None)
                # only try next endpoint for path/5xx-ish errors
                if status not in (404, 405, 500, 502, 503, 504):
                    logging.warning(f"EPG request error action={params.get('action')} {url}: {e}")
                    return {}
                logging.debug(f"EPG retrying with alternate endpoint after {status} at {url}")
                continue
            except requests.exceptions.Timeout as e:
                last_err = e
                continue
            except requests.exceptions.RequestException as e:
                logging.warning(f"EPG request error action={params.get('action')} at {url}: {e}")
                last_err = e
                continue
            except ValueError as e:
                logging.warning(f"EPG JSON decode failed from {url}: {e}")
                last_err = e
                continue

        if last_err:
            logging.warning(f"EPG failed on all endpoints for action={params.get('action')}: {last_err}")
        return {}


    def _try_fetch_epg(
        self,
        channel: Dict[str, Any],
        size: int,
        token: Optional[str],
        headers: Dict[str, str],
        cookies: Dict[str, str],
    ) -> List[Dict[str, Any]]:
        """
        Attempts a couple of known EPG endpoints. Returns raw items (to be normalized).
        """
        ch_id = self._choose_channel_id(channel)
        if not ch_id:
            return []

        # Strategy:
        # 1) get_short_epg (fast, limited size)
        # 2) fallback get_epg_info

        # --- 1) get_short_epg ---
        params1 = {
            "type": "itv",
            "action": "get_short_epg",
            "JsHttpRequest": "1-xml",
            "ch_id": ch_id,
            "size": str(max(1, size)),
        }
        js1 = self._portal_get(params1, headers, cookies)
        items = self._extract_items(js1)
        if items:
            return items

        # --- 2) get_epg_info ---
        params2 = {
            "type": "itv",
            "action": "get_epg_info",
            "JsHttpRequest": "1-xml",
            "ch_id": ch_id,
        }
        js2 = self._portal_get(params2, headers, cookies)
        items = self._extract_items(js2)
        return items

    # ----------------- Helpers -----------------

    def _extract_items(self, js: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not isinstance(js, dict):
            return []
        data = js.get("js")
        if isinstance(data, list):
            return data or []
        if isinstance(data, dict):
            if isinstance(data.get("epg"), list):
                return data.get("epg") or []
            if isinstance(data.get("data"), list):
                return data.get("data") or []
        return []

    def _choose_channel_id(self, ch: Dict[str, Any]) -> Optional[str]:
        """
        Pick the most likely channel id field. Return string.
        """
        for k in ("ch_id", "id", "number", "channel_id", "cmd"):
            v = ch.get(k)
            if v is None:
                continue
            if isinstance(v, (int, str)):
                s = str(v).strip()
                if s:
                    # Strip non-digits but keep a fallback if all digits got removed
                    digits = "".join([c for c in s if c.isdigit()])
                    return digits or s
        return None

    def _normalize_items(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Normalize raw portal items to a consistent shape:
          {
            "name": str,
            "start_dt": datetime | None,
            "end_dt": datetime | None,
            "descr": str,
            "category": str,
            "duration": int | None,       # seconds if known
            "duration_min": int | None
          }
        No truncation here; caller slices on emit.
        """
        norm: List[Dict[str, Any]] = []
        for it in items:
            name = (_first_non_empty(it, ["name", "title", "progname", "program"]) or "").strip()

            # timestamps first
            start_ts = (
                _safe_int(it.get("start"))
                or _safe_int(it.get("start_timestamp"))
                or _safe_int(it.get("from"))
            )
            end_ts = (
                _safe_int(it.get("end"))
                or _safe_int(it.get("stop_timestamp"))
                or _safe_int(it.get("to"))
            )

            # Optional string datetimes
            if not start_ts:
                start_dt_str = _first_non_empty(it, ["time", "start_time"])
                start_dt = _parse_dt_str(start_dt_str)
            else:
                start_dt = _epoch_to_local(start_ts)

            if not end_ts:
                end_dt_str = _first_non_empty(it, ["time_to", "end_time"])
                end_dt = _parse_dt_str(end_dt_str)
            else:
                end_dt = _epoch_to_local(end_ts)

            # duration
            duration = _safe_int(_first_non_empty(it, ["duration", "prog_duration", "length"]))
            if not duration and start_ts and end_ts and isinstance(start_dt, datetime) and isinstance(end_dt, datetime):
                # derive if both timestamps exist and look sane
                delta = int(end_dt.timestamp() - start_dt.timestamp())
                if 0 < delta < 24 * 3600:
                    duration = delta

            # or derive end_dt if start present + duration present
            if not end_dt and isinstance(start_dt, datetime) and duration and duration < 24 * 3600:
                end_dt = _epoch_to_local(int(start_dt.timestamp()) + duration)

            duration_min = (duration // 60) if isinstance(duration, int) and duration > 0 else None

            descr = _first_non_empty(
                it,
                ["descr", "description", "desc", "short_description", "long_description", "plot", "overview"]
            ) or ""
            category = _first_non_empty(it, ["category", "genre", "categories"]) or ""

            norm.append({
                "name": (name or "—").strip() or "—",
                "start_dt": start_dt,
                "end_dt": end_dt,
                "descr": descr.strip(),
                "category": category.strip(),
                "duration": duration,
                "duration_min": duration_min,
            })

        def _key(x):
            dt = x.get("start_dt")
            return dt.timestamp() if isinstance(dt, datetime) else math.inf

        norm.sort(key=_key)
        return norm

    # -------------------------------------------------

    def _channel_key(self, channel):
        """
        Produce a stable string key for caching/de-duping EPG requests.
        Accepts either a channel dict or a plain id/str.
        """
        try:
            if isinstance(channel, dict):
                for k in ("ch_id", "id", "cid", "ch", "number", "cmd", "name"):
                    v = channel.get(k)
                    if v is not None and str(v).strip():
                        return str(v)
                return "|".join(f"{k}={channel[k]}" for k in sorted(channel))
            return str(channel)
        except Exception:
            logging.exception("[EPG] _channel_key failed; falling back to str")
            return str(channel)

    def __del__(self):
        try:
            self.stop()
        except Exception:
            pass
