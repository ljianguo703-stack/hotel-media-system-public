# 酒店影院公开安装版

一台 Ubuntu 服务一家酒店，提供局域网选片、电影/剧集播放、广告、字幕、下载与电视管理。

当前版本为 IN19，后台 `v1-0-0-rc5-pl04`，电视 `0.1.16 / code17`，员工安装工具 `2.0.2`。首装基线为 Ubuntu 24.04 LTS amd64，首次安装需要联网下载系统依赖与容器。

## 联网一键安装

先安装 Ubuntu，配置服务器固定局域网地址，并确认影片盘已经挂载。本例使用 `/mnt/hotel-media`，影院数据使用其下新空子目录 `/mnt/hotel-media/data`。完整施工步骤见 [安装教程](docs/INSTALL.md)。

Ubuntu 如需准备下载工具，先执行这一条。

```bash
sudo apt update && sudo apt install -y curl ca-certificates
```

把店名、IP、业务时区和存储路径换成本店实际值，再整段复制执行。入口会匿名下载固定版本的安装包，校验 SHA256，随后调用 full 首装。

```bash
curl -fL --retry 3 -o hotel-install.sh \
  https://raw.githubusercontent.com/ljianguo703-stack/hotel-media-system-public/main/install-online.sh \
  && bash hotel-install.sh \
    --profile full --hotel "本店影院" \
    --lan-ip 192.168.10.10 --timezone Asia/Shanghai \
    --storage-root /mnt/hotel-media/data
```

先看计划可在命令末尾加 `--plan`。本次首装中断后，使用原参数加 `--resume`。完成后查看终端显示的后台地址，在本店私有环境读取初始凭据。

```bash
sudo cat /etc/hotel-tv/setup-credentials.txt
```

后台使用 `http://服务器IP:18791`，电视接口使用 `http://服务器IP:18790`。每次新店安装生成本店独立凭据和员工 PIN。

## 电视接入与下载

从发布页下载施工电脑对应的完整 `hotel-tv-installer-2.0.2-*` ZIP，解压并启动工具。新酒店创建自己的工具资料目录。开启电视调试后检测精确型号、固件与设备身份，填写本店服务器地址、当前后台账号和唯一房号，选择“已有员工 PIN”复用新服务器生成的 PIN。

需要后台 ADB 维护时勾选“纳入后台全程维护”，完成真实遥控、播放、声音、字幕、待机及断电恢复验收。机型支持范围以工具检测结果和随包说明为准。

公开发布包含 Ubuntu 首装、源码、标准/守护电视 APK、Mac ARM/Intel、Linux x64、Windows x64 安装工具及 SHA256 清单。[下载当前发布](https://github.com/ljianguo703-stack/hotel-media-system-public/releases/tag/IN19-public-20261106)

## 源码与验证范围

运行源码基线为 `6548049e3df8a1f81b82e243153733c62fde47e0`。公开安装包的运行文件与该版本逐项核对，来源及校验记录见 `PUBLIC_RELEASE.json`。本仓库以经筛选的当前源码快照建立，内部施工记录单独管理。

连续播放满 24 小时和实际目标房数并发，需要在使用酒店记录结果。已验电视为 MiTV-ASTP0/mulan、Android 9/SDK28、OS3.0.104.0.PSTAATV，其他型号按工具支持级别单独验证。

开发构建见 [构建说明](docs/BUILD.md)。项目许可证见 [LICENSE](LICENSE)，第三方来源与许可见 [NOTICE](NOTICE) 和 `third-party/`。
