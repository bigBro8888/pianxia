# 片匣

本地媒体下载小工具：粘贴链接 → 识别 → 取走。基于 yt-dlp，支持视频 / 仅音频。

## 运行

```powershell
# 需已安装 Python 3.11+、ffmpeg、yt-dlp 依赖
pip install -r requirements.txt
.\start.ps1
```

打开 http://127.0.0.1:8765/

## 说明

- 仅用于你有权保存的公开内容
- 不经第三方托管站；云服务器 IP 常被 YouTube 限流，推荐本机或自建出口
- 文件保存在 `downloads/`
