# 酒店影院公开安装版

一台 Ubuntu 服务一家酒店，提供局域网选片、电影/剧集播放、广告、字幕、下载与电视管理。

当前版本为 IN19，后台 `v1-0-0-rc5-pl04`，电视 `0.1.16 / code17`，员工安装工具 `2.0.2`。首装基线为 Ubuntu 24.04 LTS amd64，首次安装需要联网下载系统依赖与容器。

## 一条命令安装

全新 Ubuntu 接通网络后，复制下面这一条命令执行即可。安装使用已有默认值，终端会在需要时提示输入 Ubuntu 管理员密码。

```bash
sudo apt-get update && sudo apt-get install -y python3 wget ca-certificates && wget --timeout=20 --tries=3 --header='Accept: application/octet-stream' -O hotel-install.py https://api.github.com/repos/ljianguo703-stack/hotel-media-system-public/releases/assets/718751190 && echo '2768ee80d86393b7af4e653d8efb9a1e0d43ead70c0f5e9427f4431add897251  hotel-install.py' | sha256sum -c - && sudo python3 hotel-install.py
```

入口从本仓库 Release 下载固定依赖，校验 SHA256 后导入镜像并执行 full 首装，已有安装记录自动续装。Ubuntu 系统包和 Docker 软件包使用 APT。完成后终端显示后台地址与初始凭据的位置，登录后台再完成本店设置。

| 项目 | 自动处理 | 安装后设置 |
|---|---|---|
| 店名 | 默认“酒店影院” | 后台“系统设置 → 酒店与播放”修改 |
| 服务器 IP | 根据本机网络路由自动识别 | 后台按当前访问地址显示电视接口 |
| 业务时区 | 默认沿用 Ubuntu 主机时区 | 后台“系统设置 → 酒店与播放”修改 |
| 存储 | 自动创建标准目录 `/srv/hotel-tv/media/.automation` | 后台管理下载保存位置，或在“影片片库”接入已有影片目录 |

系统会为本店生成独立账号、设备凭据和员工 PIN。初始凭据在服务器本机私下查看。

```bash
sudo cat /etc/hotel-tv/setup-credentials.txt
```

后台使用 `http://服务器IP:18791`，电视接口使用 `http://服务器IP:18790`。完整操作见[施工教程](docs/INSTALL.md)。需要把完整媒体目录搬到新盘时，按[手动存储切换指南](docs/STORAGE-MIGRATION.md)操作。

查看固定依赖清单可运行 `python3 hotel-install.py --plan`。中断后重跑同一条命令，或运行 `sudo python3 hotel-install.py`。需要先准备依赖时使用 `sudo python3 hotel-install.py --cache-only`。详见[仓库依赖与续装说明](docs/RELEASE-DEPENDENCIES.md)。

## 电视接入与下载

从发布页下载施工电脑对应的完整 `hotel-tv-installer-2.0.2-*` ZIP，解压并启动工具。新酒店创建自己的工具资料目录。开启电视调试后检测精确型号、固件与设备身份，填写本店服务器地址、当前后台账号和唯一房号，选择“已有员工 PIN”复用新服务器生成的 PIN。

需要后台 ADB 维护时勾选“纳入后台全程维护”，完成真实遥控、播放、声音、字幕、待机及断电恢复验收。机型支持范围以工具检测结果和随包说明为准。

公开发布包含 Ubuntu 首装、源码、标准/守护电视 APK、Mac ARM/Intel、Linux x64、Windows x64 安装工具及 SHA256 清单。[电视 APK 与四平台工具下载](https://github.com/ljianguo703-stack/hotel-media-system-public/releases/tag/IN19-public-20261106) / [当前 Ubuntu 首装发布](https://github.com/ljianguo703-stack/hotel-media-system-public/releases/tag/IN19-dependencies-20261107)

## 源码与验证范围

运行源码基线为 `6548049e3df8a1f81b82e243153733c62fde47e0`。公开安装包的运行文件与该版本逐项核对，来源及校验记录见 `PUBLIC_RELEASE.json`。本仓库以经筛选的当前源码快照建立，内部施工记录单独管理。

连续播放满 24 小时和实际目标房数并发，需要在使用酒店记录结果。已验电视为 MiTV-ASTP0/mulan、Android 9/SDK28、OS3.0.104.0.PSTAATV，其他型号按工具支持级别单独验证。

开发构建见 [构建说明](docs/BUILD.md)。项目许可证见 [LICENSE](LICENSE)，第三方来源与许可见 [NOTICE](NOTICE) 和 `third-party/`。
