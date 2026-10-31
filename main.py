"""中央气象台（nmc.cn）天气实况图 + 天气公报。

指令：
    /天气实况                      分类菜单
    /天气图 [区域] [层次] [类型]    默认 中国·地面·基本天气分析
    /卫星云图 [类别] [子类型]       默认 FY4B
    /雷达图 [类型] [区域]           默认 雷达拼图·全国
    /降水量 [类型] [时效]           默认 1小时降水量
    /气温 [类型] [子类型]           默认 全国逐时气温
    /风                            全国逐时风
    /能见度                         全国能见度
    /强对流 [类型]                  默认 雷电（地闪）
    /天气公报 [文字]                渲染为图片，加「文字」发纯文本
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import Image as ImgComp
from astrbot.core.message.components import Plain, Reply

from . import bulletin as bulletin_mod
from . import params
from .nmc import NmcClient, NmcError, absolutize

PLUGIN_NAME = "nmcWeather"
DEFAULT_FONT = "/usr/share/fonts/chinese/msyh.ttc"


@register(
    PLUGIN_NAME,
    "Hermes",
    "中央气象台天气实况图（天气图/卫星云图/雷达图/降水量/气温/风/能见度/强对流）与天气公报",
    "1.0.0",
)
class NmcWeather(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self.client = NmcClient(
            cache_ttl=self._cfg_int("cache_ttl", 60),
            timeout=self._cfg_int("timeout", 15),
        )
        self.cooldown = self._cfg_int("cooldown", 5)
        self.max_pages = self._cfg_int("max_pages", 4)
        self.split_height = self._cfg_int("page_split_height", 3000)
        self.render_width = self._cfg_int("render_width", 900)
        self.text_limit = self._cfg_int("bulletin_text_limit", 1200)
        self.font_path = str(self.config.get("font_path") or DEFAULT_FONT)
        self._cd: dict[str, float] = {}
        self._render_cache: tuple[str, float, list[bytes]] | None = None
        try:
            self._cache_dir = Path(StarTools.get_data_dir(PLUGIN_NAME)) / "cache"
        except Exception:  # noqa: BLE001 - 离线测试时没有 Star 上下文
            self._cache_dir = Path("./plugin_data/nmcWeather/cache")
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ 基础设施

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _hit_cooldown(self, umo: str) -> int:
        if self.cooldown <= 0:
            return 0
        left = self._cd.get(umo, 0) - time.time()
        if left > 0:
            return int(left) + 1
        self._cd[umo] = time.time() + self.cooldown
        if len(self._cd) > 500:
            now = time.time()
            self._cd = {k: v for k, v in self._cd.items() if v > now}
        return 0

    def _write(self, data: bytes, suffix: str) -> Path:
        path = self._cache_dir / f"{int(time.time())}_{uuid.uuid4().hex[:8]}{suffix}"
        path.write_bytes(data)
        self._prune()
        return path

    def _prune(self, keep_seconds: int = 3600) -> None:
        now = time.time()
        try:
            for f in self._cache_dir.iterdir():
                if f.is_file() and now - f.stat().st_mtime > keep_seconds:
                    f.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _quoted_result(event: AstrMessageEvent, caption: str, path: Path):
        """每页都显式引用原消息。

        显式带 Reply 后 chain 里出现非 Plain/Image 组件，框架的 reply_with_quote
        就不会再插一条引用（避免双重引用）；也因此每页都能吃到引用。
        """
        chain = []
        mid = getattr(getattr(event, "message_obj", None), "message_id", None)
        if mid:
            chain.append(Reply(id=mid))
        if caption:
            chain.append(Plain(caption))
        chain.append(ImgComp.fromFileSystem(str(path)))
        return event.chain_result(chain)

    @staticmethod
    def _ext(url: str) -> str:
        low = url.lower().split("?")[0]
        for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
            if low.endswith(ext):
                return ".jpg" if ext == ".jpeg" else ext
        return ".jpg"

    # ------------------------------------------------------------ 实况图

    async def _send(self, event: AstrMessageEvent, cat_key: str, tokens: list[str] | None = None):
        cat = params.CATEGORIES[cat_key]
        left = self._hit_cooldown(event.unified_msg_origin)
        if left:
            yield event.plain_result(f"⏳ 请求太快了，{left} 秒后再试")
            return

        toks = params.split_args(event.message_str) if tokens is None else tokens
        try:
            res = await self.client.resolve(cat["entry"], toks)
        except NmcError as e:
            yield event.plain_result(f"❌ {cat['name']} 取图失败：{e}")
            return
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[{PLUGIN_NAME}] resolve error")
            yield event.plain_result(f"❌ {cat['name']} 取图异常：{e}")
            return

        if res.unmatched:
            yield event.plain_result(self._unknown_help(cat, res))
            return
        if not res.image_url:
            yield event.plain_result(
                f"😶 {cat['name']}：源站当前没有可用图片（产品可能未更新或已下线）"
            )
            return

        caption = res.caption
        if res.image_time:
            caption = f"{caption}　{res.image_time}"

        path: Path | None = None
        try:
            data = await self.client.fetch_bytes(res.image_url)
            path = self._write(data, self._ext(res.image_url))
        except Exception as e:  # noqa: BLE001 - 下载失败就直发 URL
            logger.warning(f"[{PLUGIN_NAME}] 图片下载失败，改为直发链接：{e}")

        result = event.make_result().message(caption)
        if path is not None:
            result = result.file_image(str(path))
        else:
            result = result.url_image(res.image_url)
        yield result

    def _unknown_help(self, cat: dict, res) -> str:
        lines = [f"❓ 「{res.unmatched}」不是{cat['name']}的可选参数。", ""]
        if cat["args"]:
            lines.append(f"用法：/{cat['name']} {cat['args']}")
            lines.append(f"默认：{cat['default']}")
            lines.append(f"示例：{cat['examples']}")
        for nav in res.navs:
            labels = " / ".join(o["label"] for o in nav["opts"])
            lines.append(f"· {nav['key']}：{labels}")
        if not res.navs:
            lines.append("（该类别没有可调参数）")
        return "\n".join(lines)

    # ------------------------------------------------------------ 指令

    @filter.command("天气实况", alias={"实况", "实况图"})
    async def cmd_menu(self, event: AstrMessageEvent):
        toks = params.split_args(event.message_str)
        if not toks:
            yield event.plain_result(params.MENU)
            return
        first = toks[0]
        for name, key in params.COMMAND_CATEGORY.items():
            if first in ({name} | params.COMMAND_ALIASES.get(name, set())):
                async for r in self._send(event, key, toks[1:]):
                    yield r
                return
        yield event.plain_result(f"❓ 没听懂「{first}」。\n\n{params.MENU}")

    @filter.command("天气图", alias=params.COMMAND_ALIASES["天气图"])
    async def cmd_chart(self, event: AstrMessageEvent):
        async for r in self._send(event, "chart"):
            yield r

    @filter.command("卫星云图", alias=params.COMMAND_ALIASES["卫星云图"])
    async def cmd_sat(self, event: AstrMessageEvent):
        async for r in self._send(event, "sat"):
            yield r

    @filter.command("雷达图", alias=params.COMMAND_ALIASES["雷达图"])
    async def cmd_radar(self, event: AstrMessageEvent):
        async for r in self._send(event, "radar"):
            yield r

    @filter.command("降水量", alias=params.COMMAND_ALIASES["降水量"])
    async def cmd_precip(self, event: AstrMessageEvent):
        async for r in self._send(event, "precip"):
            yield r

    @filter.command("气温", alias=params.COMMAND_ALIASES["气温"])
    async def cmd_temp(self, event: AstrMessageEvent):
        async for r in self._send(event, "temp"):
            yield r

    @filter.command("风", alias=params.COMMAND_ALIASES["风"])
    async def cmd_wind(self, event: AstrMessageEvent):
        async for r in self._send(event, "wind"):
            yield r

    @filter.command("能见度")
    async def cmd_vis(self, event: AstrMessageEvent):
        async for r in self._send(event, "vis"):
            yield r

    @filter.command("强对流")
    async def cmd_conv(self, event: AstrMessageEvent):
        async for r in self._send(event, "conv"):
            yield r

    @filter.command("天气公报", alias=params.COMMAND_ALIASES["天气公报"])
    async def cmd_bulletin(self, event: AstrMessageEvent):
        left = self._hit_cooldown(event.unified_msg_origin)
        if left:
            yield event.plain_result(f"⏳ 请求太快了，{left} 秒后再试")
            return

        toks = params.split_args(event.message_str)
        try:
            html = await self.client.fetch_html(params.BULLETIN_ENTRY)
            doc = bulletin_mod.extract(html)
        except NmcError as e:
            yield event.plain_result(f"❌ 天气公报获取失败：{e}")
            return
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[{PLUGIN_NAME}] bulletin parse error")
            yield event.plain_result(f"❌ 天气公报解析失败：{e}")
            return

        if params.is_text_request(toks):
            text = bulletin_mod.plain_text(doc)
            if len(text) > self.text_limit:
                text = text[: self.text_limit] + "\n……（已截断，完整内容请看图片版）"
            yield event.plain_result(text)
            return

        fingerprint = f"{doc['title']}|{doc['author']}|{len(doc['blocks'])}"
        pages: list[bytes] = []
        if self._render_cache and self._render_cache[0] == fingerprint and time.time() - self._render_cache[1] < 600:
            pages = self._render_cache[2]

        if not pages:
            images: dict[str, bytes] = {}
            for url in doc["images"][:5]:
                try:
                    images[url] = await self.client.fetch_bytes(absolutize(url))
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[{PLUGIN_NAME}] 公报内嵌图片下载失败：{e}")
            try:
                renderer = bulletin_mod.Renderer(
                    bulletin_mod.pick_font(self.font_path),
                    width=self.render_width,
                    split_height=self.split_height,
                )
                pages = renderer.render(doc, images, max_pages=self.max_pages)
            except Exception as e:  # noqa: BLE001
                logger.exception(f"[{PLUGIN_NAME}] bulletin render error")
                text = bulletin_mod.plain_text(doc)
                yield event.plain_result(f"⚠️ 渲染失败，先发文字版：\n\n{text[: self.text_limit]}")
                return
            self._render_cache = (fingerprint, time.time(), pages)

        total = len(pages)
        for i, png in enumerate(pages, 1):
            path = self._write(png, ".png")
            head = f"📢 天气公报（{i}/{total}）" if total > 1 else "📢 天气公报"
            if i == 1 and doc.get("author"):
                head += f"\n{doc['author'][:80]}"
            yield self._quoted_result(event, head, path)

    async def terminate(self):
        await self.client.close()
