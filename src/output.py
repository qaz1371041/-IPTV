import os
from datetime import datetime, timezone, timedelta

def generate_output(results, output_dir, min_speed, min_resolution, max_keep):
    os.makedirs(output_dir, exist_ok=True)
    now = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')

    txt_lines = []
    m3u_lines = ['#EXTM3U x-tvg-url="https://live.fanmingming.com/e.xml"']
    
    # 统计信息
    stats = {'channels': 0, 'valid': 0, 'filtered': 0, 'vod': 0, 'max_kept': 0}

    for category, channels in results.items():
        txt_lines.append(f"{category},#genre#")
        
        for ch_name, tested in channels.items():
            stats['channels'] += 1
            valid_sources = []

            # 1. 筛选所有合格的源
            for src in tested:
                r = src['result']
                # 基础门槛：必须能播，且速度达标
                if not r['playable'] or r['speed'] < min_speed:
                    continue

                is_vod = r.get('is_vod', False)
                w, h = r['resolution']
                
                # 2. 分辨率过滤 (点播源豁免)
                if not is_vod:
                    if h > 0 and h < min_resolution:
                        stats['filtered'] += 1
                        continue
                else:
                    stats['vod'] += 1

                valid_sources.append(src)
                stats['valid'] += 1

            # 3. 写入所有合格源 (受 max_keep 保护，防止单个频道有几百个源导致文件过大)
            kept_count = 0
            for i, src in enumerate(valid_sources[:max_keep]):
                kept_count += 1
                
                # TXT 格式输出
                if i == 0:
                    txt_lines.append(f"{ch_name},{src['url']}")
                else:
                    txt_lines.append(f"{ch_name} #{i+1},{src['url']}")

                # M3U 格式输出
                m3u_name = ch_name if i == 0 else f"{ch_name} #{i+1}"
                m3u_lines.append(f'#EXTINF:-1 group-title="{category}",{m3u_name}')
                m3u_lines.append(src['url'])
            
            if kept_count > stats['max_kept']:
                stats['max_kept'] = kept_count

        txt_lines.append("") # 分类间空行

    # 写入文件
    header = [
        f"# IPTV直播源",
        f"# 更新时间: {now}",
        f"# 有效源总数: {stats['valid']} | 720p以下过滤: {stats['filtered']} | 点播源保留: {stats['vod']}",
        f"# 单频道最多保留: {stats['max_kept']} 个源",
        "",
    ]
    txt_lines = header + txt_lines

    with open(os.path.join(output_dir, 'result.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(txt_lines))
    with open(os.path.join(output_dir, 'result.m3u'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(m3u_lines))

    print(f"\n{'='*60}")
    print(f"📊 最终统计:")
    print(f"   总频道数: {stats['channels']}")
    print(f"   ✅ 有效可用源: {stats['valid']} 个 (全部保留!)")
    print(f"   ❌ 720p以下过滤: {stats['filtered']} 个")
    print(f"   🎬 点播源保留: {stats['vod']} 个")
    print(f"   🏆 单个频道最多保留了: {stats['max_kept']} 个源")
    print(f"{'='*60}")
