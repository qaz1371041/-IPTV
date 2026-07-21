# deepseek-IPTV


# 📺 IPTV 纯净源聚合

自动抓取、测速、分类并输出高质量 IPTV 播放列表与 EPG 节目单。

## 特性
- 多源并发抓取
- 50 线程高速测速，自动剔除死链、弱流
- 严格遵循 `config/demo.txt` 自定义分类与频道顺序
- 别名引擎 + AI 辅助标准化频道名称
- EPG 多源聚合，自动去重
- GitHub Actions 每 6 小时全自动更新

## 使用方法
1. 编辑 `config/sources.txt` 添加上游 M3U 链接
2. 编辑 `config/demo.txt` 定义你想要的分类和频道顺序
3. （可选）在 `config/alias.txt` 中添加别名
4. 推送到 GitHub，Actions 将自动运行并生成 `output/live.m3u`

播放地址：`https://raw.githubusercontent.com/<你的用户名>/<仓库名>/main/output/live.m3u`
