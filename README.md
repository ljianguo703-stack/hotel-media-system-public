# 酒店影院 IN20

一台 Ubuntu 服务一家酒店，提供局域网媒体、广告、字幕、下载、电视与设备维护。

当前交付：后台首装 `v1-0-0-rc5-in20`，已有后台直升 `IN20u2`；四平台施工工具 `2.0.6`，内置标准/守护电视 `0.1.18/code19`。固定首装仍内置code18并复用14份镜像，电视新版由施工工具独立安装。2.0.6的M1重复启停和当前电视HOME接管仍待实机复验，详见升级说明。

[安装教程](docs/INSTALL.md) · [日常使用教程](docs/USAGE.md) · [升级说明](docs/CHANGELOG-IN20.md) · [构建](docs/BUILD.md)

首次安装使用下面的固定入口，下载及 SHA256 校验成功后执行；完整国内/海外/本地说明见安装教程。完成后无需另补RQ02/RQ04。

```bash
sudo apt-get update &&
sudo apt-get install -y python3 wget ca-certificates &&
wget --timeout=20 --tries=3 \
  --header='Accept: application/octet-stream' \
  -O hotel-install.py \
  https://api.github.com/repos/ljianguo703-stack/hotel-media-system-public/releases/assets/720625080 &&
echo '1682ee506be6c33512659afa7279cdc7600510dd2f9f5fa2a77c2992eb827504  hotel-install.py' |
  sha256sum -c - &&
sudo python3 hotel-install.py
```

已有系统只使用 [hotel-tv-upgrade-latest.deb](https://github.com/ljianguo703-stack/hotel-media-system-public/releases/latest/download/hotel-tv-upgrade-latest.deb)，目标IN20u2，修复程序目录权限；自动识别已支持起点，保留原数据。详见 [统一升级说明](docs/UPGRADE.md)，不要重跑首装覆盖已有服务。

A版分组菜单保留41个功能入口。运行概览显示在线电视、影院业务流量、Linux资源和网口总量；这些流量范围不同，未采样时明确显示未采集。

本版本机隔离验证与酒店现场验收分别记录。连续24小时、真实目标房数并发和实体电源行为仍需现场验收。未知机型按施工工具支持级别单独验证。

源码、许可证和第三方来源见 LICENSE、NOTICE、third-party 与 PUBLIC_RELEASE.json。公开源不包含本店密码、设备凭据、私钥或业务数据库。

文件整理入口：[只认最新交付目录](docs/DELIVERY.md)。旧候选包已清理；固定首装资源与依赖单独标注，由安装器自动读取。
