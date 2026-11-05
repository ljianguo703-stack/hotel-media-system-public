# 原生下载入口

从影院后台“NAS/下载”打开原生 qBittorrent、AriaNg、Prowlarr、Jackett；LAN 入口由后台会话网关保护。首次完整安装由根 `install.sh --profile full` 编排本目录脚本。

- qBittorrent：8098；默认容器 `/downloads` → 主机 `/srv/hotel-tv/media/.downloads`。
- Prowlarr：9696；连接同一 qB 网络，预建本地下载客户端。
- Jackett：9117；提供 qB 官方 Jackett 插件的统一索引。
- AriaNg：6801；aria2 上游仅 loopback 6800。

全新安装的第三方站点可达性受外网影响，未配置/不可达应在原生站点管理中处理，不阻塞本地影院。不是把任意搜索结果自动上架；自动发布默认关闭。

镜像和静态包固定摘要；密码/API key 保存在目标机私有文件，通过 stdin/本地 API 配置，不打印或打进程序包。不要脱离安装状态随意重复 commission，保留原有任务和下载。
