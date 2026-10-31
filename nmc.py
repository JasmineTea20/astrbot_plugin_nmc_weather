"""中央气象台（nmc.cn）天气实况图抓取。

核心思路：**不硬编码图片地址**。
  1. 抓入口页 HTML（服务端渲染，无需 JS）
  2. 页内 ``div.p-nav`` 暴露「同级参数选项」及其跳转链接 → 按用户参数逐级跳转
  3. 终点页 ``div.time[data-img]`` 的第一个就是最新一张图

注意：NMC 的导航链接只保留同组上下文，跨组会重置（例如在 850hPa 页点「亚欧」会跳回地面层）。
所以采用「多轮迭代 + 跳过已选中项(class=actived)」的方式收敛多组参数（区域/层次/类型）。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field

import aiohttp

BASE = "https://www.nmc.cn"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": BASE + "/",
}

MAX_JUMPS = 12


class NmcError(Exception):
    """抓取/解析失败。"""


# ---------------------------------------------------------------- 解析工具


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("&nbsp;", " ")).strip()


def parse_navs(html: str) -> list[dict]:
    """解析页内的参数导航组。

    返回 ``[{"key": "区域", "opts": [{"href", "label", "active"}, ...]}, ...]``
    """
    navs: list[dict] = []
    for seg in re.split(r'<div class="?p-nav', html)[1:]:
        seg = re.split(r'<div class="?row"?', seg)[0]
        key = re.search(r"class=sl-key>\s*([^<]*)", seg)
        if not key:
            continue
        opts = []
        for m in re.finditer(r'<a\s+href="([^"]+)"([^>]*)>([^<]+)</a>', seg):
            href = m.group(1)
            if not href.startswith("/"):
                continue
            opts.append(
                {
                    "href": href,
                    "active": "actived" in m.group(2),
                    "label": _clean(m.group(3)),
                }
            )
        if opts:
            navs.append({"key": _clean(key.group(1)).rstrip("：:"), "opts": opts})
    return navs


def parse_times(html: str) -> list[dict]:
    """解析时次列表（按页面顺序，第一个即最新）。"""
    out: list[dict] = []
    for m in re.finditer(r'<div[^>]*class="col-xs-12 time[^"]*"([^>]*)>', html):
        attrs = m.group(1)
        url = re.search(r'data-img="([^"]+)"', attrs)
        if not url:
            continue
        t = re.search(r'data-time="([^"]+)"', attrs)
        out.append({"time": t.group(1) if t else "", "url": url.group(1)})
    if not out:  # 极少数页面只有主图 img#imgpath
        m = re.search(r'<img[^>]*id=imgpath[^>]*>', html)
        if m:
            url = re.search(r'src="([^"]+)"', m.group(0))
            t = re.search(r'data-time="([^"]+)"', m.group(0))
            if url:
                out.append({"time": t.group(1) if t else "", "url": url.group(1)})
    return out


def page_title(html: str) -> str:
    m = re.search(r"<title>([^<]*)", html)
    return _clean(m.group(1)) if m else ""


def caption_of(title: str) -> str:
    """「天气实况_天气图_中国_地面_基本天气分析」→「天气图 · 中国 · 地面 · 基本天气分析」"""
    parts = [p for p in title.split("_") if p.strip()]
    if parts and parts[0] in ("天气实况", "天气预报"):
        parts = parts[1:]
    return " · ".join(parts)


def _norm(s: str) -> str:
    return s.replace("hPa", "").replace("HPA", "").replace(" ", "").lower()


def match_option(navs: list[dict], token: str) -> tuple[str, dict] | None:
    """在一个页面的全部导航组里为 token 找最合适的选项。

    优先级：精确 → 数字/前缀（850→850hPa）→ 组内序号 → 子串。
    """
    t = _norm(token)
    cands: list[tuple[str, int, dict]] = []
    for nav in navs:
        for i, opt in enumerate(nav["opts"]):
            cands.append((nav["key"], i, opt))

    for key, _, opt in cands:
        if _norm(opt["label"]) == t:
            return key, opt
    for key, _, opt in cands:
        label = _norm(opt["label"])
        if label.startswith(t) or (t and t.startswith(label)):
            return key, opt
    if token.isdigit():
        for key, i, opt in cands:
            if i + 1 == int(token):
                return key, opt
    for key, _, opt in cands:
        if t and t in _norm(opt["label"]):
            return key, opt
    return None


def absolutize(href: str) -> str:
    return href if href.startswith("http") else BASE + href


# ---------------------------------------------------------------- 结果对象


@dataclass
class Resolved:
    url: str
    title: str
    caption: str
    image_url: str | None
    image_time: str
    navs: list[dict] = field(default_factory=list)
    unmatched: str | None = None
    jumps: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 客户端


class NmcClient:
    def __init__(self, cache_ttl: int = 60, timeout: int = 15):
        self.cache_ttl = max(0, int(cache_ttl))
        self.timeout = max(3, int(timeout))
        self._session: aiohttp.ClientSession | None = None
        self._page_cache: dict[str, tuple[float, str]] = {}
        self._img_cache: dict[str, tuple[float, bytes]] = {}

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers=HEADERS, timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _raw(self, url: str, binary: bool = False, tries: int = 2):
        last: Exception | None = None
        sess = await self.session()
        for _ in range(tries):
            try:
                async with sess.get(url) as r:
                    if r.status != 200:
                        raise NmcError(f"HTTP {r.status}")
                    return await (r.read() if binary else r.text(errors="replace"))
            except Exception as e:  # noqa: BLE001 - 网络异常统一重试
                last = e
                await asyncio.sleep(0.6)
        raise NmcError(f"请求失败：{last}")

    async def fetch_html(self, url: str) -> str:
        url = absolutize(url)
        now = time.time()
        hit = self._page_cache.get(url)
        if hit and now - hit[0] < self.cache_ttl:
            return hit[1]
        html = await self._raw(url)
        if "404错误页面" in html:
            raise NmcError("源站返回 404（页面或产品已下线）")
        if self.cache_ttl:
            self._page_cache[url] = (now, html)
            if len(self._page_cache) > 200:
                self._page_cache.clear()
        return html

    async def fetch_bytes(self, url: str) -> bytes:
        url = absolutize(url)
        now = time.time()
        hit = self._img_cache.get(url)
        if hit and now - hit[0] < 600:
            return hit[1]
        data = await self._raw(url, binary=True)
        if not data:
            raise NmcError("图片内容为空")
        self._img_cache[url] = (now, data)
        if len(self._img_cache) > 60:
            self._img_cache.clear()
        return data

    async def resolve(self, entry: str, tokens: list[str]) -> Resolved:
        """按参数逐级跳转，返回终点页的最新图片。"""
        url = absolutize(entry)
        html = await self.fetch_html(url)
        jumps: list[str] = []
        total = 0

        for _ in range(6):
            changed = False
            for tok in tokens:
                navs = parse_navs(html)
                hit = match_option(navs, tok)
                if hit is None:
                    return Resolved(
                        url=url,
                        title=page_title(html),
                        caption=caption_of(page_title(html)),
                        image_url=None,
                        image_time="",
                        navs=navs,
                        unmatched=tok,
                        jumps=jumps,
                    )
                key, opt = hit
                new_url = absolutize(opt["href"])
                # 已选中（actived）或已在当前页：不跳，否则会来回震荡
                if opt["active"] or new_url == url:
                    continue
                if total >= MAX_JUMPS:
                    break
                html = await self.fetch_html(new_url)
                url = new_url
                total += 1
                changed = True
                jumps.append(f'{key}:{opt["label"]}')
            if not changed:
                break

        title = page_title(html)
        times = parse_times(html)
        latest = times[0] if times else {"time": "", "url": None}
        return Resolved(
            url=url,
            title=title,
            caption=caption_of(title),
            image_url=latest["url"],
            image_time=latest["time"],
            navs=parse_navs(html),
            jumps=jumps,
        )
