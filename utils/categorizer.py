from config.settings import DEMO_FILE, SOURCE_CAT_FILE
from utils.loaders import load_alias_map, load_demo_template, load_source_cat_rules, load_icons_index
from utils.config import logger

class Categorizer:
    def __init__(self, ai_helper=None):
        self.alias_map = load_alias_map()
        self.demo_cats, self.demo_ch2cat = load_demo_template()
        self.source_rules = load_source_cat_rules()
        self.icons = load_icons_index()
        self.ai = ai_helper
        self.missing_channels = []  # 无法匹配的频道

    def resolve(self, raw_name, source_url=''):
        """将原始频道名解析为 (标准名, 分类, logo)"""
        # 1. 精确匹配 demo 频道列表
        if raw_name in self.demo_ch2cat:
            return raw_name, self.demo_ch2cat[raw_name], self.icons.get(raw_name, '')
        
        # 2. 别名匹配
        for std, aliases in self.alias_map.items():
            if raw_name in aliases:
                if std in self.demo_ch2cat:
                    return std, self.demo_ch2cat[std], self.icons.get(std, '')
                # 标准名不在 demo 中，但我们可以尝试用其分类
                # 先尝试从 demo 中找到类似分类
                # 这里简单返回标准名和未知分类，后续可追加到其他分类
                return std, '其他', self.icons.get(std, '')
        
        # 3. 来源 URL 推断分类
        for pattern, cat in self.source_rules:
            if pattern.search(source_url):
                # 直接使用推断的分类，不要求必须存在 demo
                return raw_name, cat, self.icons.get(raw_name, '')
        
        # 4. AI 兜底
        if self.ai:
            # 这里需要异步，调用方应传入异步运行的结果
            # 为简化，我们假设 ai 提供同步方法（通过缓存）
            # 实际使用时改为 asyncio.run() 或在顶层处理
            pass
        
        # 无法匹配
        self.missing_channels.append(raw_name)
        return raw_name, '其他', self.icons.get(raw_name, '')

    async def async_resolve(self, raw_name, source_url=''):
        """异步版本，支持 AI"""
        # 重复上述1-3步骤，若失败且AI启用，则调用AI
        # 省略重复代码，可封装
        # 这里实现逻辑
        pass
