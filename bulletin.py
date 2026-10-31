"""天气公报：从 nmc.cn 抓正文，用 Pillow 渲染成竖版长图（服务器无 Chromium）。

页面结构（实测）：
    <div id=text>
        <div class=title> 天 气 公 报 </div>
        <div class=author> 制作：… 签发：… 2026 年 10 月 31 日 18 时 </div>
        <hr color=red>
        <div class=writing> <p>…</p> <p><img src="…"></p> … </div>
    </div>
正文段落带 inline 样式：标题行 font-size:14pt + 加粗 + text-align:center，
小标题（一、天气实况）加粗，正文 12pt 且 text-indent。
"""

from __future__ import annotations

import io
import os
import re
from html import unescape

from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = [
    "/usr/share/fonts/chinese/msyh.ttc",
    "/usr/share/fonts/chinese/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
]

CLOSING_PUNCT = "，。、；：？！）】》」』”’,.;:?!)]}%"
OPENING_PUNCT = "（【《「『“‘([{"


# ---------------------------------------------------------------- 抓取解析


def _balanced_div(html: str, marker: re.Pattern) -> str | None:
    m = marker.search(html)
    if not m:
        return None
    start = m.start()
    depth = 0
    for mm in re.finditer(r"<div\b|</div>", html[start:]):
        if mm.group(0) == "<div":
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return html[start : start + mm.end()]
    return None


def _text_of(fragment: str) -> str:
    txt = re.sub(r"<br\s*/?>", "\n", fragment)
    txt = re.sub(r"<[^>]+>", "", txt)
    txt = unescape(txt).replace("\u3000", " ").replace("\xa0", " ")
    txt = re.sub(r"[ \t]+", " ", txt)
    return re.sub(r"\s*\n\s*", "\n", txt).strip()


_PT_RE = re.compile(r"font-size:\s*([\d.]+)pt", re.I)

# 只按 <p> 切块：若把 <div> 也纳入，正文容器 div 会把里面所有 <p> 一口吞掉，
# 导致整篇公报变成几个巨型段落（实测踩过）。没有 <p> 的旧页面走 extract() 的兜底。
BLOCK_RE = re.compile(r"(?P<img><img[^>]*>)|(?P<p><p\b[^>]*>(?P<pinner>.*?)</p>)", re.S | re.I)


def _img_url(tag: str) -> str | None:
    src = re.search(r'src="([^"]+)"', tag)
    if not src:
        return None
    url = src.group(1)
    if "assets/img" in url or url.startswith("data:"):
        return None
    wh = re.search(r'height=["\']?(\d+)', tag), re.search(r'width=["\']?(\d+)', tag)
    h = int(wh[0].group(1)) if wh[0] else 0
    w = int(wh[1].group(1)) if wh[1] else 0
    if 0 < h <= 6 or 0 < w <= 6:  # 排版用的分隔线/占位小图
        return None
    return url


def _classify(text: str, style: str) -> dict:
    bold = bool(re.search(r"<(b|strong)\b|font-weight:\s*bold", style, re.I))
    center = "text-align:center" in style.replace(" ", "")
    pts = [float(x) for x in _PT_RE.findall(style)]
    pt = max(pts) if pts else 12.0
    if center and len(text) <= 60:
        kind = "subtitle"
    elif re.match(r"^[一二三四五六七八九十]+、", text):
        kind = "heading"
    elif re.match(r"^\d+[.．、]", text) and len(text) <= 40:
        kind = "subheading"
    elif bold and pt >= 13 and len(text) <= 40 and not text.endswith("："):
        kind = "heading"
    elif len(text) <= 40 and text.startswith("图"):
        kind = "figcap"
    else:
        kind = "para"
    return {"kind": kind, "text": text, "bold": bold, "pt": pt}


def parse_blocks(fragment: str) -> list[dict]:
    """把正文 fragment 切成有序块：段落 / 图片。

    注意公报里的图通常包在 ``<p><span><img><br></span></p>`` 里，必须在段落内按
    出现顺序拆出「文字 → 图 → 文字」，否则图片会被 _text_of 丢掉。
    """
    blocks: list[dict] = []
    for m in BLOCK_RE.finditer(fragment):
        if m.group("img"):
            url = _img_url(m.group("img"))
            if url:
                blocks.append({"kind": "img", "url": url})
            continue
        inner = m.group("pinner")
        if inner is None:
            continue
        style = inner
        if "<img" in inner.lower():
            for part in re.split(r"(<img[^>]*>)", inner, flags=re.I):
                if part.lower().startswith("<img"):
                    url = _img_url(part)
                    if url:
                        blocks.append({"kind": "img", "url": url})
                    continue
                text = _text_of(part)
                if text:
                    blocks.append(_classify(text, style))
            continue
        text = _text_of(inner)
        if text:
            blocks.append(_classify(text, style))
    return blocks


def extract(html: str) -> dict:
    """返回 {"title", "author", "blocks", "images"}；images 为正文内图片 URL 列表。"""
    frag = _balanced_div(html, re.compile(r'<div[^>]*\bid=["\']?text["\']?[^>]*>'))
    if frag is None:
        raise ValueError("未找到公报正文容器")
    title = ""
    m = re.search(r'<div[^>]*class=["\']?title["\']?[^>]*>(.*?)</div>', frag, re.S | re.I)
    if m:
        title = _text_of(m.group(1))
    author = ""
    m = re.search(r'<div[^>]*class=["\']?author["\']?[^>]*>(.*?)</div>', frag, re.S | re.I)
    if m:
        author = _text_of(m.group(1))
    body = frag
    if m:
        body = body.replace(m.group(0), " ")
    if title:
        body = re.sub(
            r'<div[^>]*class=["\']?title["\']?[^>]*>.*?</div>', " ", body, flags=re.S | re.I
        )
    blocks = parse_blocks(body)
    # 页面上报头和签发行各出现两次（正文区 + 打印版），去掉重复文本块
    seen_head = {title.strip(), author.strip()} - {""}
    blocks = [b for b in blocks if not (b["kind"] != "img" and b.get("text", "").strip() in seen_head)]
    if not any(b["kind"] != "img" for b in blocks):  # 老页面没有 <p>：整段兜底
        raw = _text_of(body)
        if raw:
            blocks.append(_classify(raw, ""))
    images = [b["url"] for b in blocks if b["kind"] == "img"]
    return {"title": title or "天 气 公 报", "author": author, "blocks": blocks, "images": images}


def plain_text(doc: dict) -> str:
    lines = [doc["title"].strip(), doc["author"].strip(), ""]
    for b in doc["blocks"]:
        if b["kind"] == "img":
            lines.append("［图］")
        elif b["kind"] == "figcap":
            lines.append(f"（{b['text']}）")
        else:
            lines.append(b["text"])
            lines.append("")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------- 渲染


def pick_font(configured: str = "") -> str:
    for p in ([configured] if configured else []) + FONT_CANDIDATES:
        if p and os.path.exists(p):
            return p
    raise ValueError("找不到可用的中文字体（请在插件配置里指定 font_path）")


class Renderer:
    def __init__(
        self,
        font_path: str,
        width: int = 900,
        margin: int = 45,
        split_height: int = 3000,
        content_width: int | None = None,
    ):
        self.font_path = font_path
        self.W = width
        self.M = margin
        self.CW = content_width or (width - 2 * margin)
        self.split_height = split_height
        self._fonts: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}

    def font(self, size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
        key = (size, bold)
        if key not in self._fonts:
            self._fonts[key] = ImageFont.truetype(self.font_path, size)
        return self._fonts[key]

    # -- 排版原语

    @staticmethod
    def _lh(font: ImageFont.FreeTypeFont, ratio: float) -> int:
        return int(font.size * ratio)

    def wrap(self, text: str, font: ImageFont.FreeTypeFont, width: int, draw) -> list[str]:
        lines: list[str] = []
        for para in text.split("\n"):
            cur = ""
            for ch in para:
                if draw.textlength(cur + ch, font=font) <= width:
                    cur += ch
                    continue
                if ch in CLOSING_PUNCT and cur:
                    cur += ch
                    lines.append(cur)
                    cur = ""
                    continue
                if cur and cur[-1] in OPENING_PUNCT:
                    lines.append(cur[:-1])
                    cur = cur[-1] + ch
                    continue
                lines.append(cur)
                cur = ch
            if cur:
                lines.append(cur)
        return lines or [""]

    def _draw(self, draw, lines, font, x, y, fill, lh, bold):
        for ln in lines:
            draw.text((x, y), ln, font=font, fill=fill, stroke_width=1 if bold else 0, stroke_fill=fill)
            y += lh
        return y

    def render(self, doc: dict, images: dict[str, bytes], max_pages: int = 6) -> list[bytes]:
        pages: list[bytes] = []
        canvas = None
        draw = None
        y = self.M

        def new_page():
            nonlocal canvas, draw, y
            canvas = Image.new("RGB", (self.W, self.split_height + self.M), "white")
            draw = ImageDraw.Draw(canvas)
            y = self.M

        def flush():
            nonlocal canvas, draw, y
            if canvas is None:
                return
            h = min(canvas.height, max(self.M * 2, y + self.M))
            buf = io.BytesIO()
            canvas.crop((0, 0, self.W, h)).save(buf, "PNG", optimize=True)
            pages.append(buf.getvalue())
            canvas = None

        def ensure(need: int):
            """块放不下就换页"""
            nonlocal y
            if y + need > self.split_height + self.M and y > self.M + 60:
                flush()
                new_page()

        new_page()

        # 报头
        f_title = self.font(34, True)
        ensure(int(34 * 1.6))
        tw = draw.textlength(doc["title"], font=f_title)
        draw.text(((self.W - tw) / 2, y), doc["title"], font=f_title, fill="#111111")
        y += int(34 * 1.7)

        if doc.get("author"):
            f_auth = self.font(16)
            for ln in self.wrap(doc["author"], f_auth, self.CW - 40, draw):
                lw = draw.textlength(ln, font=f_auth)
                draw.text(((self.W - lw) / 2, y), ln, font=f_auth, fill="#666666")
                y += self._lh(f_auth, 1.5)
        y += 8
        draw.line([(self.M, y), (self.W - self.M, y)], fill="#C62828", width=3)
        y += 22

        for b in doc["blocks"]:
            kind = b["kind"]
            if kind == "img":
                data = images.get(b["url"])
                if not data:
                    continue
                try:
                    im = Image.open(io.BytesIO(data)).convert("RGB")
                except Exception:  # noqa: BLE001
                    continue
                nw = self.CW
                nh = max(1, int(im.height * nw / im.width))
                if nh > 1500:
                    nh = 1500
                    nw = max(1, int(im.width * nh / im.height))
                im = im.resize((nw, nh), Image.LANCZOS)
                if y + nh + 10 > self.split_height + self.M and y > self.M + 60:
                    flush()
                    new_page()
                canvas.paste(im, ((self.W - nw) // 2, y))
                y += nh + 14
                continue

            text = b["text"]
            if kind == "subtitle":
                f = self.font(23, True)
                lines = self.wrap(text, f, self.CW - 20, draw)
                ensure(len(lines) * self._lh(f, 1.6) + 10)
                for ln in lines:
                    lw = draw.textlength(ln, font=f)
                    draw.text(((self.W - lw) / 2, y), ln, font=f, fill="#111111", stroke_width=1, stroke_fill="#111111")
                    y += self._lh(f, 1.6)
                y += 6
            elif kind == "heading":
                f = self.font(24, True)
                lines = self.wrap(text, f, self.CW, draw)
                ensure(len(lines) * self._lh(f, 1.7) + 16)
                y += 8
                y = self._draw(draw, lines, f, self.M, y, "#111111", self._lh(f, 1.7), True)
                y += 4
            elif kind == "subheading":
                f = self.font(21, True)
                indent = int(f.size * 2)
                lines = self.wrap(text, f, self.CW - indent, draw)
                lh = self._lh(f, 1.68)
                ensure(lh * min(len(lines), 3) + 10)
                y += 4
                first_line = True
                for ln in lines:
                    if y + lh > self.split_height + self.M:
                        flush()
                        new_page()
                        first_line = False
                    draw.text(
                        (self.M + (indent if first_line else 0), y),
                        ln,
                        font=f,
                        fill="#111111",
                        stroke_width=1,
                        stroke_fill="#111111",
                    )
                    y += lh
                    first_line = False
                y += 2
            elif kind == "figcap":
                f = self.font(17)
                lines = self.wrap(text, f, self.CW, draw)
                ensure(len(lines) * self._lh(f, 1.6) + 8)
                for ln in lines:
                    lw = draw.textlength(ln, font=f)
                    draw.text(((self.W - lw) / 2, y), ln, font=f, fill="#555555")
                    y += self._lh(f, 1.6)
                y += 8
            else:
                f = self.font(21)
                bold = bool(b.get("bold")) and len(text) <= 12
                indent = int(f.size * 2)
                lines = self.wrap(text, f, self.CW - indent, draw)
                lh = self._lh(f, 1.68)
                ensure(lh * min(len(lines), 4) + 8)
                first = True
                for ln in lines:
                    if y + lh > self.split_height + self.M:
                        flush()
                        new_page()
                        first = False
                    x = self.M + (indent if first else 0)
                    draw.text((x, y), ln, font=f, fill="#000000", stroke_width=1 if bold else 0, stroke_fill="#000000")
                    y += lh
                    first = False
                y += 6
            if len(pages) >= max_pages:
                break

        flush()
        return pages
