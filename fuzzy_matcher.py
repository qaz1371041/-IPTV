"""
模糊匹配器 v3 - 彻底解决 CCTV-2 匹配到 CCTV-12 的问题
"""
import difflib
import re
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)


def extract_cctv_number(name):
    """
    提取 CCTV 频道号
    
    "CCTV-2"       → 2
    "CCTV-12"      → 12
    "CCTV2财经"     → 2
    "CCTV12社会与法" → 12
    "CCTV-5+"      → 5
    """
    match = re.search(r'cctv[-\s]?(\d+)', name, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def extract_weishi_name(name):
    """
    提取卫视地名
    
    "湖南卫视"     → "湖南"
    "北京卫视"     → "北京"
    "湖南卫视频道"  → "湖南"
    """
    match = re.search(r'(.+?)卫视', name)
    if match:
        return match.group(1).strip()
    return None


def normalize_channel_name(name):
    """标准化频道名称（保守清洗）"""
    name = name.strip().lower()
    name = re.sub(r'[\s·.．]', '', name)
    name = re.sub(r'[_]', '-', name)
    name = re.sub(r'(频道|台|高清|hd|fhd|4k|标清|sd)$', '', name)
    return name


def is_same_channel(template_name, source_name):
    """
    判断两个频道名是否真的是同一个频道
    
    核心逻辑：
    1. CCTV数字频道：数字必须完全一致
    2. 卫视：地名必须完全一致
    3. 其他：用相似度兜底
    """
    t = template_name.strip().lower()
    s = source_name.strip().lower()
    
    # 完全一致
    if t == s:
        return True
    
    # ===== CCTV 数字频道保护 =====
    t_cctv_num = extract_cctv_number(t)
    s_cctv_num = extract_cctv_number(s)
    
    if t_cctv_num is not None and s_cctv_num is not None:
        # 都是CCTV频道，数字必须完全一致
        if t_cctv_num != s_cctv_num:
            return False  # CCTV-2 和 CCTV-12 数字不同，直接拒绝
        # 数字相同，进一步检查（防止 CCTV-5 和 CCTV-5+ 混淆）
        t_plus = '+' in t
        s_plus = '+' in s
        if t_plus != s_plus:
            return False  # CCTV-5 和 CCTV-5+ 不是同一个
        return True  # 数字相同且+号一致
    
    # 一个是CCTV，另一个不是 → 不是同一个频道
    if (t_cctv_num is not None) != (s_cctv_num is not None):
        return False
    
    # ===== 卫视保护 =====
    t_weishi = extract_weishi_name(t)
    s_weishi = extract_weishi_name(s)
    
    if t_weishi and s_weishi:
        # 都是卫视，地名必须完全一致
        return t_weishi == s_weishi
    
    # 一个是卫视，另一个不是 → 不是同一个频道
    if (t_weishi is not None) != (s_weishi is not None):
        return False
    
    # ===== 其他频道：用相似度兜底 =====
    norm_t = normalize_channel_name(t)
    norm_s = normalize_channel_name(s)
    
    # 子串包含
    if norm_t in norm_s or norm_s in norm_t:
        return True
    
    # 相似度检查（提高阈值）
    similarity = difflib.SequenceMatcher(None, norm_t, norm_s).ratio()
    return similarity >= 0.85  # 其他频道用更高的阈值


def match_channels(template, all_channels, cutoff=0.85, max_matches_per_channel=3):
    """
    精准匹配策略:
    1. 先尝试精确匹配（标准化后完全一致）
    2. 再遍历所有源，用 is_same_channel 判断
    3. 最后才用模糊匹配兜底
    
    返回: {"分类": {"频道名": [{"name", "url", "source", "similarity"}, ...]}}
    """
    # 构建源数据索引
    source_names_unique = list(set(ch["name"] for ch in all_channels))
    source_map = defaultdict(list)
    for ch in all_channels:
        source_map[ch["name"]].append(ch)
    
    # 预处理：标准化所有源名称
    normalized_sources = {}
    for sn in source_names_unique:
        norm = normalize_channel_name(sn)
        normalized_sources[norm] = sn
    
    matched_result = {}
    total_matched = 0
    total_template = 0
    match_log = []

    for category, channel_names in template.items():
        matched_result[category] = {}

        for template_name in channel_names:
            total_template += 1
            matches = []
            
            norm_template = normalize_channel_name(template_name)

            # ===== 策略1：精确匹配 =====
            if norm_template in normalized_sources:
                original_name = normalized_sources[norm_template]
                for ch_data in source_map[original_name]:
                    matches.append({
                        "name": original_name,
                        "url": ch_data["url"],
                        "source": ch_data["source"],
                        "similarity": 1.0,
                        "match_type": "精确"
                    })

            # ===== 策略2：智能匹配（带频道保护）=====
            if not matches:
                for source_name in source_names_unique:
                    if is_same_channel(template_name, source_name):
                        similarity = difflib.SequenceMatcher(
                            None, norm_template, normalize_channel_name(source_name)
                        ).ratio()
                        for ch_data in source_map[source_name]:
                            matches.append({
                                "name": source_name,
                                "url": ch_data["url"],
                                "source": ch_data["source"],
                                "similarity": round(similarity, 4),
                                "match_type": "智能"
                            })

            # 去重 + 排序
            seen_urls = set()
            unique_matches = []
            for m in matches:
                if m["url"] not in seen_urls:
                    seen_urls.add(m["url"])
                    unique_matches.append(m)
            
            unique_matches.sort(key=lambda x: x["similarity"], reverse=True)
            unique_matches = unique_matches[:max_matches_per_channel]

            if unique_matches:
                matched_result[category][template_name] = unique_matches
                total_matched += 1
                best = unique_matches[0]
                match_log.append(
                    f"  ✅ [{category}] {template_name} → {best['name']} "
                    f"({best['match_type']}, {best['similarity']})"
                )
            else:
                matched_result[category][template_name] = []
                match_log.append(f"  ❌ [{category}] {template_name} → 无匹配")

    # 打印匹配日志
    for log in match_log:
        logger.info(log)

    logger.info(f"匹配结果: {total_matched}/{total_template} 个频道找到匹配")
    return matched_result
