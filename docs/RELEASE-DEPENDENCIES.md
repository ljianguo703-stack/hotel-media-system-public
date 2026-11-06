# GitHub 依赖与续装

2026-11-07，IN19-dependencies-20261107。适用于 Ubuntu 24.04 amd64 的 full 首装及同版本断点续装。

## 安装入口

复制 README 首页的一条命令。入口使用公开 GitHub REST 附件接口，校验入口脚本与依赖 SHA256，准备 Docker，导入镜像，再执行配套 r3 安装包。

店名默认“酒店影院”，IP 自动识别，业务时区沿用 Ubuntu，媒体根默认 `/srv/hotel-tv/media/.automation`。本店资料安装后在后台设置。

## 发布内容

- 13 个上游固定镜像，覆盖 Jellyfin、Dockge、FileBrowser、qBittorrent、NPC、Prowlarr、Radarr、Sonarr、Bazarr、Seerr、Alpine、Jackett 和 Nginx。
- 从安装器同一 Dockerfile 和固定 Alpine 根构建的 aria2 1.37.0 镜像。入口校验其固定身份后复用。
- AriaNg 1.3.15 与 qBittorrent Jackett 搜索插件，保持原安装器锁定字节。
- 配套 r3 安装包、精确安装器输入源码、公开依赖清单、上游来源元数据、22 份主项目及容器构建源码归档和许可证。

镜像归档合计 4,696,456,704 字节，约 4.37 GiB。入口逐项核对本机镜像身份，已满足的镜像直接复用。Ubuntu 系统包从 APT 安装，首次准备 Docker 时使用官方签名公钥及清华 Docker CE APT 镜像。

## Docker 与缓存

本 OCI 入口使用 Docker 29+ 的 containerd 镜像存储，导入平台为 linux/amd64。新机由入口准备 Docker。已有 Docker 会先检查版本、Compose 和镜像存储能力。

已校验文件保存在 `/var/cache/hotel-tv-dependencies/IN19-dependencies-20261107`。下载过程保留 `.partial`，再次运行时续传。文件完整且 SHA256 一致后才导入。终端每约 10 秒显示下载进度。

`sudo python3 hotel-install.py --cache-only` 会准备 Docker、导入镜像和缓存附件及安装包，随后退出。`python3 hotel-install.py --plan` 显示内置清单。

## 续装与状态

原安装记录保存在 `/etc/hotel-tv/install-state.json`。配套安装包按原 release 和 full profile 续装，已完成阶段按记录复用。新版安装器源码目录按 payload 摘要区分，分别保存同版本安装包的来源。

日常应用维护继续通过后台“软件与更新”使用匹配基线的更新包。依赖版本维护应建立新的 Release，并生成对应的固定清单和入口脚本。

## 核验记录

70 项安装、部署、在线入口和依赖测试通过，包含 REST 附件编号、断点续传、损坏文件拒绝、缓存复用、预载 aria2 身份及正常入口参数传递。Ubuntu Python 环境已通过公开 API 实际下载 AriaNg、搜索插件和配套安装包，三份 SHA256 一致。

GitHub 云端 14 项任务全部完成 OCI 导出、linux/amd64 导入及固定摘要识别。安装包在 Ubuntu 环境完成 plan 验证，同版本不同 payload 并存与重复执行通过。整店业务和真实设备验收由安装酒店完成。[镜像发布核验](https://github.com/ljianguo703-stack/hotel-media-system-public/actions/runs/42298617912)
