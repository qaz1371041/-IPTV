import os
from datetime import datetime, timezone, timedelta

def generate_output(results, output_dir, min_speed, min_resolution, max_keep):
    os.makedirs(output_dir, exist_ok=True)
    now = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')

    txt_lines = []
    m3u_lines = ['#EXTM3U x-tvg-url="https://live.fanmingming.com/e.xml"']
    stats = {'channels': 0, 'valid': 0, 'filtered': 0, 'vod': 0}

    for category, channels in results.items():
        txt_lines.append(f"{category},#genre#")
        for ch_name, tested in channels.items():
            stats['channels'] += 1
            valid_sources = []

            for src in tested:
                r = src['result']
                # 1. 失效源直接丢弃 (不标记，不输出)
                if not r['playable'] or r['speed'] < min_speed:
                    continue

                # 2. 分辨率过滤 (点播源豁免)
                is_vod = r.get('is_vod', False)
                w, h = r['resolution']
                
                if not is_vod:
                    if h > 0 and h < min_resolution:
                        stats['filtered'] += 1
                        continue
                else:
                    stats['vod'] += 1

                valid_sources.append(src)
                stats['valid'] += 1

            # 写入有效源
            for i, src in enumerate(valid_sources[:max_keep]):
                if i == 0:
                    txt_lines.append(f"{ch_name},{src['url']}")
                else:
                    txt_lines.append(f"{ch_name} #{i+1},{src['url']}")

                m3u_name = ch_name if i == 0 else f"{ch_name} #{i+1}"
                m3u_lines.append(f'#EXTINF:-1 group-title="{category}",{m3u_name}')
                m3u_lines.append(src['url'])
        txt_lines.append("")

    header = [
        f"# IPTV直播源",
        f"# 更新时间: {now}",
        f"# 有效源: {stats['valid']} | 720p以下过滤: {stats['filtered']} | 点播源保留: {stats['vod']}",
        "",
    ]
    txt_lines = header + txt_lines

    with open(os.path.join(output_dir, 'result.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(txt_lines))
    with open(os.path.join(output_dir, 'result.m3u'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(m3u_lines))

    print(f"\n{'='*50}")
    print(f"📊 统计: 频道 {stats['channels']} | 有效源 {stats['valid']}")
    print(f"   720p以下过滤: {stats['filtered']} | 点播源保留: {stats['vod']}")
    print(f"{'='*50}")
