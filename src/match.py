import re, difflib

def clean_name(name):
    name = re.sub(r'[\s《》\[\]【】]+', '', name)
    name = name.replace('高清完整版', '').replace('完整版', '').replace('4K高清', '')
    return name.upper()

def match_template(template, all_channels):
    # URL去重
    seen = set()
    unique = []
    for name, url in all_channels:
        base = url.split('?')[0]
        if base not in seen:
            seen.add(base)
            unique.append((name, url))
    print(f"   去重后: {len(unique)} 条上游源")

    results = {}
    total_matched = 0

    for category, channel_names in template:
        results[category] = {}
        for target in channel_names:
            matched = []
            t_clean = clean_name(target)
            t_core = t_clean[:6] if len(t_clean) > 6 else t_clean

            for src_name, src_url in unique:
                s_clean = clean_name(src_name)
                score = 0
                
                if t_clean and (t_clean in s_clean or s_clean in t_clean):
                    score = 100
                elif t_core and t_core in s_clean:
                    score = 90
                else:
                    # difflib 模糊匹配
                    matches = difflib.get_close_matches(t_clean, [s_clean], n=1, cutoff=0.6)
                    if matches: score = 80

                if score >= 80:
                    matched.append((src_name, src_url))

            # 结果去重
            seen_urls = set()
            final_matched = []
            for n, u in matched:
                if u not in seen_urls:
                    seen_urls.add(u)
                    final_matched.append((n, u))

            results[category][target] = final_matched
            if final_matched: total_matched += 1

    print(f"   匹配成功: {total_matched} 个频道有源")
    return results
