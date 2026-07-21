import re
import json
from config.settings import *
from utils.config import logger

def load_text_lines(path):
    if not path.exists():
        return []
    with open(path, 'r', encoding='utf-8') as f:
        return [line.strip() for line in f if line.strip()]

def load_alias_map():
    """别名映射：标准名 -> 别名列表"""
    alias_map = {}
    lines = load_text_lines(ALIAS_FILE)
    for line in lines:
        if ':' in line:
            std, aliases = line.split(':', 1)
            alias_map[std.strip()] = [a.strip() for a in aliases.split('|') if a.strip()]
    return alias_map

def load_blacklist():
    return set(load_text_lines(BLACKLIST_FILE))

def load_whitelist():
    return set(load_text_lines(WHITELIST_FILE))

def load_source_cat_rules():
    """来源 URL -> 分类的规则列表 (正则, 分类)"""
    rules = []
    for line in load_text_lines(SOURCE_CAT_FILE):
        if '::' in line:
            pattern, cat = line.split('::', 1)
            rules.append((re.compile(pattern.strip()), cat.strip()))
    return rules

def load_demo_template():
    """解析 demo.txt 得到 OrderedDict {分类: [频道名列表]} 和 频道->分类映射"""
    from collections import OrderedDict
    categories = OrderedDict()
    channel_to_cat = {}
    current_cat = None
    if not DEMO_FILE.exists():
        logger.warning("demo.txt not found")
        return categories, channel_to_cat
    
    with open(DEMO_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') and not line.endswith(',#genre#'):
                continue
            # 匹配分类头：分类名,#genre# 或 # 分类名,#genre#
            cat_match = re.match(r'^(?:#\s*)?(.+?),#genre#$', line)
            if cat_match:
                current_cat = cat_match.group(1).strip()
                categories[current_cat] = []
            elif current_cat:
                ch_name = re.sub(r'#.*', '', line).strip()
                if ch_name:
                    categories[current_cat].append(ch_name)
                    channel_to_cat[ch_name] = current_cat
    logger.info(f"Loaded demo template with {len(categories)} categories")
    return categories, channel_to_cat

def load_icons_index():
    """图标索引：频道名 -> URL"""
    icons = {}
    for line in load_text_lines(ICONS_INDEX_FILE):
        if ',' in line:
            name, url = line.split(',', 1)
            icons[name.strip()] = url.strip()
    return icons
