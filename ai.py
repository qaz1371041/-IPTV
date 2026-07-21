#!/usr/bin/env python3
"""AI 辅助 (NVIDIA NIM) - 分类 + 标准化"""
import os, json, time, logging
from typing import Dict, Optional
import requests

logger = logging.getLogger(__name__)
API_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
API_KEY = os.environ.get("NVIDIA_API_KEY", "")
MODEL = "nvidia/step-3.5-flash"
VALID_CATS = ["央视","卫视","港澳台","数字付费","少儿","体育","电影","电视剧","综艺","其他"]

class AIHelper:
    def __init__(self, cache: Dict, cache_path: str):
        self.cache = cache
        self.cache_path = cache_path
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "IPTV/1.0"
        self._last = 0.0

    def save(self):
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump(self.cache, f, ensure_ascii=False, indent=2)

    def _call(self, prompt: str) -> Optional[str]:
        if not API_KEY:
            return None
        wait = 0.2 - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        try:
            r = self.s.post(API_URL, json={
                "model": MODEL,
                "messages": [
                    {"role": "system", "content": "你是电视频道分类专家，只输出结果，不解释。"},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 64,
                "temperature": 0.1,
            }, headers={"Authorization": f"Bearer {API_KEY}"}, timeout=15)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logger.debug(f"AI: {e}")
            return None

    def classify(self, name: str) -> str:
        key = f"c:{name}"
        if key in self.cache:
            return self.cache[key]
        result = self._call(f"频道「{name}」属于哪个分类？可选: {','.join(VALID_CATS)}\n只输出分类名:")
        if result and result in VALID_CATS:
            self.cache[key] = result
            return result
        return self._rule(name)

    def normalize(self, name: str) -> str:
        key = f"n:{name}"
        if key in self.cache:
            return self.cache[key]
        result = self._call(f"标准化频道名「{name}」，去除4K/HD/高清等，只输出标准名:")
        if result:
            result = result.strip().strip('"')
            self.cache[key] = result
            return result
        return name

    def _rule(self, name: str) -> str:
        n = name.lower()
        if "cctv" in n or "央视" in n or "中央" in n: return "央视"
        if "卫视" in n: return "卫视"
        if any(k in n for k in ["凤凰","tvb","翡翠","明珠","viu","hoy","澳视","莲花"]): return "港澳台"
        if any(k in n for k in ["chc","求索","付费","华数"]): return "数字付费"
        if any(k in n for k in ["卡通","少儿","动画"]): return "少儿"
        if "体育" in n: return "体育"
        if any(k in n for k in ["电影","影院","剧场"]): return "电影"
        if any(k in n for k in ["电视剧","剧集"]): return "电视剧"
        return "其他"
