import re
from utils import load_alias_map, load_demo_template, logger

# 内置来源分类规则（替代 source-cat.txt）
DEFAULT_RULES = [
    (r'cctv|央视', '央视'),
    (r'卫视|satellite', '卫视'),
    (r'体育|sports|espn', '体育'),
    (r'电影|movie', '电影'),
    (r'少儿|kids|cartoon', '少儿'),
    (r'新闻|news', '新闻'),
    (r'纪录片|doc', '纪录片'),
]

class Categorizer:
    def __init__(self):
        self.alias = load_alias_map()
        self.demo_cats, self.demo_ch2cat = load_demo_template()
        self.rules = DEFAULT_RULES

    def resolve(self, raw_name, source_url=''):
        # 1. 精确匹配 demo 频道列表
        if raw_name in self.demo_ch2cat:
            return raw_name, self.demo_ch2cat[raw_name]

        # 2. 别名匹配
        for std, aliases in self.alias.items():
            if raw_name in aliases:
                if std in self.demo_ch2cat:
                    return std, self.demo_ch2cat[std]
                # 若标准名不在 demo，归于“其他”
                return std, '其他'

        # 3. 来源 URL 推断
        for pattern, cat in self.rules:
            if re.search(pattern, source_url, re.I):
                return raw_name, cat

        # 4. 无法匹配 -> 其他
        return raw_name, '其他'
