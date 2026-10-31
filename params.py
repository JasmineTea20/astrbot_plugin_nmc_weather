"""指令、类别与参数表。

只放常量与纯函数，方便离线测试。
"""

from __future__ import annotations

import re

BULLETIN_ENTRY = "/publish/weather-bulletin/index.htm"

CATEGORIES: dict[str, dict] = {
    "chart": {
        "name": "天气图",
        "entry": "/publish/observations/china/dm/weatherchart-h000.htm",
        "args": "[区域] [层次] [类型]",
        "default": "中国 · 地面 · 基本天气分析",
        "examples": "/天气图 ／ /天气图 亚欧 ／ /天气图 850 ／ /天气图 亚欧 500 叠加",
    },
    "sat": {
        "name": "卫星云图",
        "entry": "/publish/satellite/fy4b-visible.htm",
        "args": "[类别] [子类型]",
        "default": "FY4B",
        "examples": "/卫星云图 ／ /卫星云图 FY4C ／ /卫星云图 圆盘 水汽",
    },
    "radar": {
        "name": "雷达图",
        "entry": "/publish/radar/chinaall.html",
        "args": "[类型] [区域]",
        "default": "雷达拼图 · 全国",
        "examples": "/雷达图 ／ /雷达图 华北 ／ /雷达图 华东",
    },
    "precip": {
        "name": "降水量",
        "entry": "/publish/observations/hourly-precipitation.html",
        "args": "[类型] [时效]",
        "default": "1小时降水量",
        "examples": "/降水量 ／ /降水量 6 ／ /降水量 24 ／ /降水量 近30天 近10天",
    },
    "temp": {
        "name": "气温",
        "entry": "/publish/observations/hourly-temperature.html",
        "args": "[类型] [子类型]",
        "default": "全国逐时气温",
        "examples": "/气温 ／ /气温 逐日 日最高 ／ /气温 近30天最高 近10天",
    },
    "wind": {
        "name": "风",
        "entry": "/publish/observations/hourly-winds.html",
        "args": "",
        "default": "全国逐时风",
        "examples": "/风",
    },
    "vis": {
        "name": "能见度",
        "entry": "/publish/sea/seaplatform1.html",
        "args": "",
        "default": "能见度",
        "examples": "/能见度",
    },
    "conv": {
        "name": "强对流",
        "entry": "/publish/observations/lighting.html",
        "args": "[类型]",
        "default": "雷电（地闪）",
        "examples": "/强对流 ／ /强对流 冰雹 ／ /强对流 雷暴大风",
    },
}

# 指令名 → 别名
COMMAND_ALIASES: dict[str, set[str]] = {
    "天气实况": {"实况", "实况图"},
    "天气图": {"天气分析"},
    "卫星云图": {"云图", "卫星"},
    "雷达图": {"雷达"},
    "降水量": {"降水"},
    "气温": {"温度"},
    "风": {"风场"},
    "能见度": set(),
    "强对流": set(),
    "天气公报": {"公报"},
}

# 指令名 → 类别 key
COMMAND_CATEGORY: dict[str, str] = {
    "天气图": "chart",
    "卫星云图": "sat",
    "雷达图": "radar",
    "降水量": "precip",
    "气温": "temp",
    "风": "wind",
    "能见度": "vis",
    "强对流": "conv",
}

_SPLIT_RE = re.compile(r"[\s,，、/／|]+")

TEXT_WORDS = {"文字", "文本", "text", "txt"}


def split_args(message_str: str) -> list[str]:
    """去掉指令名本身，切出后续参数。"""
    parts = [p for p in _SPLIT_RE.split((message_str or "").strip()) if p]
    return parts[1:] if parts else []


def is_text_request(tokens: list[str]) -> bool:
    return any(t.lower() in TEXT_WORDS for t in tokens)


MENU = (
    "🌤 中央气象台 · 天气实况图\n"
    "━━━━━━━━━━━━\n"
    "· /天气图     区域 层次 类型（默认 中国·地面·基本天气分析）\n"
    "· /卫星云图   类别 子类型（默认 FY4B）\n"
    "· /雷达图     类型 区域（默认 雷达拼图·全国）\n"
    "· /降水量     类型 时效（默认 1小时降水量）\n"
    "· /气温       类型 子类型（默认 全国逐时气温）\n"
    "· /风         全国逐时风\n"
    "· /能见度     全国能见度\n"
    "· /强对流     雷电/短时强降水/雷暴大风/冰雹\n"
    "· /天气公报   最新天气公报（加「文字」发纯文本）\n"
    "━━━━━━━━━━━━\n"
    "参数可省略，也可用数字/简称：/天气图 亚欧 850 叠加\n"
    "数据来源：www.nmc.cn"
)
