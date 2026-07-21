import json
import aiohttp
import re
from config.settings import *
from utils.config import logger

class AIHelper:
    def __init__(self):
        self.cache = {}
        if AI_CACHE_FILE.exists():
            with open(AI_CACHE_FILE) as f:
                self.cache = json.load(f)
        self.session = None

    async def _call_ai(self, prompt):
        if not AI_API_KEY:
            return None
        if not self.session:
            self.session = aiohttp.ClientSession()
        headers = {
            "Authorization": f"Bearer {AI_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": AI_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 100
        }
        try:
            async with self.session.post(AI_ENDPOINT, json=payload, headers=headers, timeout=20) as resp:
                data = await resp.json()
                return data['choices'][0]['message']['content'].strip()
        except Exception as e:
            logger.error(f"AI call failed: {e}")
            return None

    async def standardize_and_classify(self, raw_name):
        """返回 (标准名, 分类) 或 None"""
        if raw_name in self.cache:
            return self.cache[raw_name]
        
        prompt = f"你是一个电视节目频道名称标准化助手。请将以下频道名称标准化为最通用的中文名称，并给出分类（如：央视、卫视、体育、电影、少儿等）。\n输入：{raw_name}\n输出格式：标准名|分类"
        result = await self._call_ai(prompt)
        if result and '|' in result:
            parts = result.split('|', 1)
            std_name = parts[0].strip()
            category = parts[1].strip()
            self.cache[raw_name] = (std_name, category)
            # 更新缓存文件
            with open(AI_CACHE_FILE, 'w') as f:
                json.dump(self.cache, f, ensure_ascii=False, indent=2)
            return (std_name, category)
        return None
