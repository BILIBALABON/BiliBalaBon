"""連結檢查：模型很容易編出看似合理、實際不存在的網址，所以由閘門實際連線確認。

結果快取在 <project>/.state/linkcache.json，同一個網址在一天內不重複連線。
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

UA = "Mozilla/5.0 (compatible; LearnLoop-linkcheck/1.0)"
CACHE_TTL = 24 * 3600
_YT = re.compile(r"^https?://((www|m)\.)?(youtube\.com/(watch|shorts|live|embed)|youtu\.be/)", re.I)


def is_http_url(url: object) -> bool:
    if not isinstance(url, str):
        return False
    try:
        p = urllib.parse.urlparse(url.strip())
    except ValueError:
        return False
    return p.scheme in ("http", "https") and "." in (p.netloc or "")


class PrivateAddress(Exception):
    """網址指向本機或內部網路。"""


def to_ascii_url(url: str) -> str:
    """把含中文等非 ASCII 字元的網址轉成可送出的形式（例如 zh.wikipedia.org/wiki/熱傳導）。"""
    p = urllib.parse.urlsplit(url.strip())
    host = p.hostname or ""
    try:
        host_ascii = host.encode("idna").decode("ascii") if host else ""
    except UnicodeError:
        host_ascii = host
    netloc = host_ascii if ":" not in host_ascii else f"[{host_ascii}]"
    if p.port:
        netloc += f":{p.port}"
    if p.username:
        netloc = urllib.parse.quote(p.username) + (":" + urllib.parse.quote(p.password) if p.password else "") + "@" + netloc
    safe = "%/:@!$&'()*+,;=~-._"
    return urllib.parse.urlunsplit((p.scheme, netloc, urllib.parse.quote(p.path, safe=safe),
                                    urllib.parse.quote(p.query, safe=safe + "?"), ""))


def assert_public(url: str) -> None:
    """網址是模型給的，不能讓它借這台電腦去敲內部網路（127.0.0.1、192.168.x.x、雲端 metadata…）。"""
    host = urllib.parse.urlsplit(url).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return  # 解析不到：交給後面的連線去失敗
    for info in infos:
        if not ipaddress.ip_address(info[4][0].split("%")[0]).is_global:
            raise PrivateAddress(host)


class _Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, allow_private: bool):
        self.allow_private = allow_private

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not self.allow_private:
            assert_public(newurl)  # 轉址後的目的地也要檢查
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch(url: str, timeout: int, method: str, allow_private: bool = False) -> int:
    url = to_ascii_url(url)
    if not allow_private:
        assert_public(url)
    req = urllib.request.Request(url, method=method, headers={"User-Agent": UA, "Accept": "*/*"})
    opener = urllib.request.build_opener(_Redirects(allow_private))
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


PROBES = ("https://www.google.com/generate_204", "https://www.cloudflare.com/cdn-cgi/trace", "https://example.com/")


def is_online(timeout: int = 10) -> bool:
    """連得到任何一個常見網站就算有網路。用來分辨「連結壞了」與「這台電腦沒有網路」。"""
    for u in PROBES:
        try:
            _fetch(u, timeout, "GET")
            return True
        except Exception:
            continue
    return False


def check_one(url: str, timeout: int = 10, allow_private: bool = False) -> tuple[str, str]:
    """回傳 (狀態, 說明)。狀態：ok｜blocked（站方擋機器人，無法判斷）｜dead（站方回應不存在）｜
    unreachable（連不上：網域不存在、逾時，或這台電腦沒有網路）。"""
    try:
        if _YT.match(url):
            # YouTube 影片頁對不存在的影片也回 200，要用 oEmbed 才分得出來
            o = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(url, safe="")
            code = _fetch(o, timeout, "GET", allow_private)
            if code == 200:
                return "ok", "YouTube 影片存在"
            if code in (401, 403):
                return "blocked", f"影片存在但不允許嵌入或需登入（HTTP {code}）"
            return "dead", f"YouTube 查無此影片（HTTP {code}）"
        code = _fetch(url, timeout, "HEAD", allow_private)
        if code in (403, 405, 406, 501) or code >= 500:
            code = _fetch(url, timeout, "GET", allow_private)  # 有些站不支援 HEAD
        if code < 400:
            return "ok", f"HTTP {code}"
        if code in (401, 403, 429, 451) or code >= 500:
            return "blocked", f"HTTP {code}（站方拒絕自動檢查，無法確認）"
        return "dead", f"HTTP {code}"
    except PrivateAddress:
        return "dead", "這是本機或內部網路的位址，不能當作來源"
    except Exception as e:  # DNS 失敗、逾時、憑證錯誤…
        reason = getattr(e, "reason", e)
        return "unreachable", f"無法連線（{str(reason)[:80]}）"


def check_urls(urls: list[str], project_dir: Path, mode: str = "strict", timeout: int = 10,
               allow_private: bool = False) -> tuple[list[str], list[str], bool]:
    """回傳 (errors, warnings, offline)。mode=off 不檢查；warn 全部降為警告。

    offline=True 表示這台電腦連不到外部網站，檢查無法進行（此時 errors 為空，由呼叫端決定怎麼處理）。
    """
    if mode == "off":
        return [], [], False
    urls = sorted({u.strip() for u in urls if is_http_url(u)})
    cache_path = Path(project_dir) / ".state" / "linkcache.json"
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}
    now = time.time()
    todo = [u for u in urls if not (u in cache and now - cache[u]["at"] < CACHE_TTL
                                    and cache[u]["status"] in ("ok", "blocked"))]
    if todo:
        with ThreadPoolExecutor(max_workers=8) as ex:
            fresh = dict(zip(todo, ex.map(lambda x: check_one(x, timeout, allow_private), todo)))
        # 這一批沒有任何一個拿到網站的回應 → 先確認是不是本機沒有網路
        if all(s == "unreachable" for s, _ in fresh.values()) and not is_online(timeout):
            return [], [], True
        for u, (status, detail) in fresh.items():
            cache[u] = {"status": status, "detail": detail, "at": now}
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass
    errors, warnings = [], []
    for u in urls:
        c = cache.get(u, {"status": "dead", "detail": "未檢查"})
        if c["status"] in ("dead", "unreachable"):
            (errors if mode == "strict" else warnings).append(
                f"連結無法開啟：{u} —— {c['detail']}。請換成實際查得到的網址，不要憑印象寫。")
        elif c["status"] == "blocked":
            warnings.append(f"連結無法自動確認：{u} —— {c['detail']}")
    return errors, warnings, False
