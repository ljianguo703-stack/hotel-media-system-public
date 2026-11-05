# 全新 Ubuntu 施工步骤

## 操作系统与网络

从 Ubuntu 官方 24.04 镜像目录下载 Desktop amd64 ISO，制作启动 U 盘。核对系统盘身份后安装，创建可使用 sudo 的管理员，登录桌面并完成安全更新。磁盘选取按 Ubuntu 官方安装教程操作。[官方安装步骤](https://ubuntu.com/tutorials/install-ubuntu-desktop)

通过路由器固定服务器有线网卡的 DHCP 租约，核对本机地址与业务时区。服务器保持常驻运行，在 Ubuntu 电源设置中关闭自动挂起。

```bash
cat /etc/os-release
dpkg --print-architecture
ip -4 -br address
ip route
timedatectl
```

## 影片盘

先核对型号、容量、序列号和文件系统，已有影片盘保留原资料。新空盘根据本店规划建立 ext4 分区。以下示例适用于已确认的 ext4 分区。

```bash
lsblk -o NAME,SIZE,MODEL,SERIAL,FSTYPE,UUID,MOUNTPOINTS
sudo mkdir -p /mnt/hotel-media
sudo cp -an /etc/fstab /etc/fstab.before-hotel-media
sudoedit /etc/fstab
```

把下面占位符换成真实分区 UUID，在 fstab 中添加对应记录。同一分区已有记录时先核对原记录。

```text
UUID=REPLACE_WITH_ACTUAL_UUID /mnt/hotel-media ext4 defaults 0 2
```

```bash
sudo systemctl daemon-reload
sudo mount /mnt/hotel-media
findmnt -M /mnt/hotel-media
df -hT /mnt/hotel-media
```

已挂载时可跳过 mount。新机重启后再次确认准确的挂载点与磁盘。安装数据根目录选挂载点下的新空子目录，例如 `/mnt/hotel-media/data`。[Ubuntu fstab 手册](https://manpages.ubuntu.com/manpages/noble/man5/fstab.5.html)

## 联网首装

按仓库首页的联网命令执行。按店址选择实际业务时区，中国可用 `Asia/Shanghai`，缅甸可用 `Asia/Yangon`，新加坡可用 `Asia/Singapore`。`--timezone` 作用于影院业务设置，Ubuntu 主机时区另行核对。

需要先检查安装计划时，在命令末尾追加 `--plan`。正式安装需等待终端明确显示“安装完成”。本次中断恢复保留原参数，加 `--resume`。终端会显示已校验安装包的下载位置，便于复查与恢复。

## 后台与媒体

私下读取 `/etc/hotel-tv/setup-credentials.txt`，在 `http://服务器IP:18791` 登录影院后台。核对店名、时区和 NAS 应用连接，在“加片与下载”配置本店使用的来源与字幕账号，再以一部真实影片验证下载、归档、上架和字幕。

```bash
systemctl is-active hotel-tv
sudo docker ps --format 'table {{.Names}}\t{{.Status}}'
findmnt -M /mnt/hotel-media
sudo journalctl -u hotel-tv -n 50 --no-pager
```

下载或字幕来源需要的账号、Token 由各酒店业主提供。已纳入管理的新片按任务流程搜索、下载和校时，旧片使用“本地导入”流程处理。

## 电视与验收

用本店独立的 2.0.2 工具目录逐台安装、登记房号与纳管。先验一台，再扩到 2-3 台、10 台及实际目标规模。

- [ ] 服务器重启后 IP、影片盘与服务恢复。
- [ ] 电视身份、房号、实际应用版本和后台登记一致。
- [ ] 真实电影/剧集、实际声音、字幕、拖动、退出和续播通过。
- [ ] 广告按配置触发。暂停广告默认播一组，结束保留画面，视频约回退一秒定格。
- [ ] 原遥控 HOME、返回、员工 PIN、待机与唤醒通过。
- [ ] 按精确机型能力实测断电来电和后台 ADB 重连。
- [ ] 本店 24 小时连播与目标并发记录起止时间、样本和异常处理结果。

交付时保存本店私有凭据、挂载 UUID、工具 data/备份、房号表与验收结果。日常更新通过后台“软件与更新”使用匹配基线的更新包。
