import re, difflib

def clean_name(name):
    # 清洗特殊符号和多余后缀
    name = re.sub(r'[\s《》\[\]【】\(\)（）]+', '', name)
    name = name.replace('高清完整版', '').replace('完整版', '').replace('4K高清', '').replace('高清', '')
    return name.upper()

def match_template(template, all_channels):
    # 1. 上游源 URL 去重
    seen = set()
    unique = []
    for name, url in all_channels:
        base = url.split('?')[0]
        if base not in seen:
            seen.add(base)
            unique.append((name, url))
    print(f"   去重后: {len(unique)} 条上游源")

    # 2. 提前清洗上游源名称，建立索引（极大提升匹配速度）
    cleaned_sources = []
    for src_name, src_url in unique:
        cleaned_sources.append((clean_name(src_name), src_name, src_url))

    results = {}
    total_matched = 0

    # 3. 开始匹配
    for category, channel_names in template:
        results[category] = {}
        for target in channel_names:
            t_clean = clean_name(target)
            # 提取核心词（前6个字符），用于快速初筛
            t_core = t_clean[:6] if len(t_clean) > 6 else t_clean
            
            matched = []
            
            for s_clean, src_name, src_url in cleaned_sources:
                score = 0
                
                # 策略 A：完全包含（最快，优先级最高）
                if t_clean and (t_clean in s_clean or s_clean in t_clean):
                    score = 100
                # 策略 B：核心词包含
                elif t_core and t_core in s_clean:
                    score = 90
                # 策略 C：difflib 极速模糊匹配（替代 fuzzywuzzy）
                else:
                    # difflib 是 C 实现的，比 fuzzywuzzy 快 100 倍以上
                    ratio = difflib.SequenceMatcher(None, t_clean, s_clean).ratio()
                    if ratio >= 0.75:
                        score = int(ratio * 100)

                if score >= 80:
                    matched.append((src_name, src_url, score))

            # 4. 结果去重并按相似度排序
            seen_urls = set()
            final_matched = []
            # 先按分数降序排列，保证最相似的排在前面
            matched.sort(key=lambda x: x[2], reverse=True)
            
            for n, u, s in matched:
                if u not in seen_urls:
                    seen_urls.add(u)
                    final_matched.append((n, u))

            results[category][target] = final_matched
            if final_matched: total_matched += 1

    print(f"   匹配成功: {total_matched} 个频道有源")
    return results
