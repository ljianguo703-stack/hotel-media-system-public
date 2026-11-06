# 手动切换影院存储目录

IN19-DOC03，2026-11-06。本指南适用于默认一键安装后的完整媒体目录迁移。

本指南章节号对应完整教程第 13–21 页。文内第 4 页为新数据盘 UUID 持久挂载步骤，先按施工教程接好新盘。迁移期间暂停影院与相关服务，按顺序逐段核验。

- [完整 Ubuntu 教程 PDF](https://github.com/ljianguo703-stack/hotel-media-system-public/releases/download/IN19-public-20261106-r2/ubuntu-install-IN19.pdf)
- [完整教程可复制文字版](https://github.com/ljianguo703-stack/hotel-media-system-public/releases/download/IN19-public-20261106-r2/ubuntu-install-IN19.md)
- [Ubuntu 安装与影片盘挂载](INSTALL.md)

## 13 手动切换存储：范围与准备

本章将默认媒体目录整体复制到新盘，再使用 Ubuntu 绑定挂载。后台继续使用影院统一入口，实际文件保存到新盘，覆盖广告、下载、片库、字幕及兼容副本。旧副本先保留验收，释放旧盘空间见第 21 页。

适用默认一键首装、专用影院 Ubuntu 和已按第 4 页持久挂载的 ext4 数据盘。操作会中断影院、SMB 和本机 Docker 容器，请安排维护窗口，并暂停新增业务。自定义数据根、已有绑定或额外子挂载需先另行规划。

### 1. 读取当前自动化数据根

```bash
sudo python3 - <<'PY'
import json
from pathlib import Path
config = json.loads(Path('/srv/hotel-tv/data/media-automation.json').read_text())
print(config['storage_root'])
PY
```

本流程预期结果为 `/srv/hotel-tv/media/.automation`。迁移其父目录 `/srv/hotel-tv/media`，可同时覆盖默认媒体和广告文件。已另外接入的外部片库按各自目录管理。

### 2. 在同一个 Bash 终端设置本次路径

新目录选择新盘下尚未使用的独立目录，示例路径使用英文且无空格。后续命令连续使用这些变量，记录打印的备份路径。

```bash
hotel_old=/srv/hotel-tv/media
hotel_disk=/mnt/hotel-media
hotel_new="$hotel_disk/hotel-store"
hotel_stamp=$(date +%Y%m%d-%H%M%S)
hotel_backup="/var/backups/hotel-tv/storage-$hotel_stamp"
hotel_dropin="90-hotel-storage-$hotel_stamp.conf"
findmnt -M "$hotel_disk"
findmnt --fstab --evaluate -M "$hotel_disk"
printf '%s\n' "$hotel_backup"
df -hT "$hotel_old" "$hotel_disk" /var/backups
sudo du -sh "$hotel_old" /srv/nas/appdata /srv/hotel-tv/data
```

确认设备、UUID、文件系统和容量正确，新盘及备份盘空间足够。`findmnt -M "$hotel_old"` 应无挂载记录。用 `findmnt -rn -o TARGET` 核对媒体目录内部也无独立子挂载。

## 14 手动切换存储：停写与备份

先在后台及原生下载器确认，下载、导入、兼容转换、片库扫描和软件发布等任务已完成或取消。已暂停的未完成任务也要处理，因为任务可能保存原磁盘身份。关闭文件管理写入，暂停共享访问。

### 1. 保存状态并停止标准部署服务

```bash
sudo mkdir -p /var/backups/hotel-tv
sudo mkdir -m 0700 "$hotel_backup"
printf '%s\n' "$hotel_old" "$hotel_disk" "$hotel_new" \
  "$hotel_dropin" | sudo tee "$hotel_backup/paths.txt" >/dev/null
systemctl show -p Id -p ActiveState hotel-tv.service smbd.service \
  docker.service docker.socket hotel-tv-installer.path \
  | sudo tee "$hotel_backup/services.before" >/dev/null
sudo stat -c '%d:%i' "$hotel_old" \
  | sudo tee "$hotel_backup/media-before.stat" >/dev/null
sudo systemctl stop hotel-tv-installer.path
systemctl is-active hotel-tv-installer.service
```

最后一条应显示 inactive。若仍为 active，先等待升级任务结束。本节后续会停止全部正在运行的容器，先查看并确认它们属于这台专用影院主机。

```bash
sudo docker ps --format 'table {{.Names}}\t{{.Status}}'
sudo docker ps -q | sudo tee "$hotel_backup/containers.running" >/dev/null
sudo xargs -r -a "$hotel_backup/containers.running" \
  docker inspect --format '{{.Name}} {{.HostConfig.RestartPolicy.Name}}'
```

标准部署的重启策略为 unless-stopped。若有其它业务容器、不同策略或 Swarm 部署，由该主机运维先调整维护计划。

```bash
sudo systemctl stop hotel-tv smbd
sudo xargs -r -a "$hotel_backup/containers.running" docker stop --time 60
sudo systemctl stop docker.socket docker.service
sudo tar --acls --xattrs -C / -cpf "$hotel_backup/config-and-state.tar" \
  etc/fstab etc/hotel-tv srv/hotel-tv/data srv/nas/appdata \
  opt/stacks etc/systemd/system
```

备份成功后再复制数据。备份含本店私有凭据和数据库，存放在本店受保护位置。每一步出现错误先处理，相关服务在切换完成前保持停止。

## 15 手动切换存储：复制与校验

### 1. 创建目标目录并完整复制

如主机缺少工具，先执行 `sudo apt-get install -y rsync acl`。下面 mkdir 若提示目录已存在，先核对内容并改用未使用的新目录，保留已有资料。

```bash
sudo mkdir "$hotel_new"
sudo rsync -aHAX --numeric-ids --info=progress2 -- \
  "$hotel_old/" "$hotel_new/"
```

`-H` 保留传输范围内的硬链接，`-A` 保留 ACL，`-X` 保留扩展属性，数值 UID/GID 按同一主机保留。完整父目录一起复制，使下载区和片库之间的硬链接得到覆盖。路径末尾的斜杠代表复制目录内容。

### 2. 使用校验和进行只读复核

```bash
sudo rsync -aHAXnc --numeric-ids --itemize-changes -- \
  "$hotel_old/" "$hotel_new/"
sudo test -f "$hotel_new/.automation/.hotel-media-install.json"
sudo ls -ld "$hotel_new" "$hotel_new/.automation"
```

rsync 复核应无差异输出，退出码为 0。发现权限、校验、硬链接或空间错误，停留在本步骤排查。复制完整且校验成功后，进入挂接阶段。

### 本次迁移涉及的位置

| 位置 | 处理方式 |
|---|---|
| /srv/hotel-tv/media | 整体复制到新盘，作为影院统一入口挂接 |
| .automation 内的下载、片库和 .hotel-compat | 随媒体父目录一起复制 |
| /srv/hotel-tv/data 及 /etc/hotel-tv | 已在停服时备份，存放主机配置与业务元数据 |
| 其它接入的外部影片目录 | 按各自挂载和目录维护 |

原系统盘副本继续作为本次验收的回退来源，验收完成后按第 21 页清理。当前阶段先核对副本完整性。

## 16 手动切换存储：挂接与开机持久化

### 1. 编辑本机挂载表

备份当前 fstab，再打印本次绑定记录。将终端打印的一整行加入 fstab，目标路径只登记一次。

```bash
sudo cp -a /etc/fstab "$hotel_backup/fstab.before"
printf '%s %s none bind,nofail,x-systemd.requires-mounts-for=%s 0 0\n' \
  "$hotel_new" "$hotel_old" "$hotel_disk"
sudoedit /etc/fstab
```

新数据盘原有 UUID 记录保留，可采用 `defaults,nofail`。绑定记录通过 x-systemd.requires-mounts-for 依赖数据盘挂载。第 17 页会为影院相关服务加入强制挂载依赖，让主机维护入口与媒体服务分别受控。

两处配置都完成并核对前，相关服务保持停止。若编辑器为 nano，Ctrl+O、回车保存，Ctrl+X 退出。

### 2. 复核并挂接

```bash
sudo findmnt --verify --verbose
sudo systemctl daemon-reload
sudo mount "$hotel_old"
findmnt -M "$hotel_old" -o TARGET,SOURCE,FSTYPE,OPTIONS
df -hT "$hotel_old" "$hotel_new"
sudo stat -c '%d:%i %n' "$hotel_old" "$hotel_new"
```

最后一条的两个目录应显示相同设备号和 inode，表明影院入口确实对应新盘目录。df 应显示新数据盘的容量和空间。

### 后台会看到什么

后台使用 `/srv/hotel-tv/media/.automation` 作为统一入口，实际文件位于新盘 `hotel-store/.automation`。通过 findmnt 和 df 核对实际设备。各应用、共享和容器仍通过这个入口访问文件。

遇到挂载失败，先核对 UUID、目录、fstab 和文件系统，按第 19-20 页处理回退。当前阶段保留两份完整数据。

## 17 手动切换存储：服务启动保护

为影院、Docker、SMB 和软件安装执行器设置挂载依赖。主机启动时先取得媒体挂载，再启动这些服务。数据盘未就绪时，管理员可从主机维护入口处理故障。

### 1. 创建本次独立配置片段

以下文件名含本次时间戳，记录在第 14 页的 paths.txt 中。若终端曾关闭，先恢复本次变量再执行。

```bash
for hotel_unit in hotel-tv.service docker.service smbd.service \
  hotel-tv-installer.service; do
  sudo mkdir -p "/etc/systemd/system/$hotel_unit.d"
  printf '[Unit]\nRequiresMountsFor=%s\n' "$hotel_old" \
    | sudo tee "/etc/systemd/system/$hotel_unit.d/$hotel_dropin" >/dev/null
done
sudo systemctl daemon-reload
```

片段内容相同，RequiresMountsFor 会为对应挂载建立依赖和启动顺序。

```ini
[Unit]
RequiresMountsFor=/srv/hotel-tv/media
```

### 2. 逐项检查配置

```bash
systemctl show hotel-tv.service docker.service smbd.service \
  hotel-tv-installer.service -p Id -p RequiresMountsFor
systemctl cat hotel-tv.service docker.service smbd.service \
  hotel-tv-installer.service
```

四个服务的 RequiresMountsFor 均应包含 `/srv/hotel-tv/media`，并能看到本次新建的片段。核对挂载记录和服务依赖均正确后再启动。

依赖保护针对开机和服务启动顺序。运行中发生拔盘、文件系统错误或断连时，应进入维护状态，先停写再排查挂载和磁盘健康。

## 18 手动切换存储：启动与验收

### 1. 先确认 Docker 能看到新目录

标准 unless-stopped 容器已在第 14 页手动停止。重新启动 Docker 后，先用本机已有镜像进行只读挂载探针。

```bash
sudo systemctl start docker.socket docker.service
hotel_probe_image=$(sudo docker inspect --format '{{.Image}}' hotel-qbittorrent)
hotel_check=$(sudo mktemp "$hotel_new/.storage-check.XXXXXX")
hotel_check_name=${hotel_check##*/}
sudo docker run --rm --network none --read-only --user 0:0 \
  --mount "type=bind,src=$hotel_old,dst=/probe,readonly" \
  --env "HOTEL_CHECK=$hotel_check_name" --entrypoint /bin/sh \
  "$hotel_probe_image" -c 'test -f "/probe/$HOTEL_CHECK"'
sudo rm -f -- "$hotel_check"
```

探针退出码应为 0，证明 Docker 新建的挂载能读取本次新盘标记。

### 2. 按原运行记录恢复

```bash
sudo xargs -r -a "$hotel_backup/containers.running" docker start
sudo systemctl start hotel-tv smbd
systemctl is-active hotel-tv smbd
sudo docker ps --format 'table {{.Names}}\t{{.Status}}'
```

按第 14 页服务状态记录恢复需要运行的项目，软件发布暂停到本次验收结束。

- [ ] 后台登录、原账号、员工 PIN、片库和广告资料正常。
- [ ] 真实影片有声音、字幕正常，拖动和续播可用。
- [ ] 用小样本验证新增下载、归档、字幕和兼容副本写入新盘。
- [ ] findmnt、df、后台磁盘余量和实际文件一致。
- [ ] 维护窗口内重启主机，再确认挂载、共享、容器和影院恢复。

验收完成后按原状态启动 `hotel-tv-installer.path`，恢复营业。检查或下载产生的新文件也属于新盘数据，回退按第 19 页进行同步。

## 19 手动切换存储：回退与新数据同步

本页用于新盘可读且旧盘空间足够时。终端关闭过的，按 paths.txt 重新填写路径和本次 hotel_backup、hotel_dropin，沿用原记录时间。新盘损坏时从可用备份恢复。

### 1. 再次进入维护状态

按第 14 页完成任务清理，确认安装执行器空闲。重新采集本次正在运行的容器，避免使用已过期的容器记录。

```bash
sudo systemctl stop hotel-tv-installer.path
sudo systemctl stop hotel-tv smbd
sudo docker ps -q \
  | sudo tee "$hotel_backup/rollback-containers.running" >/dev/null
sudo xargs -r -a "$hotel_backup/rollback-containers.running" docker stop --time 60
sudo systemctl stop docker.socket docker.service
sudo umount "$hotel_old"
```

### 2. 核对旧入口已回到原磁盘，再预览反向同步

```bash
sudo stat -c '%d:%i' "$hotel_old"
sudo cat "$hotel_backup/media-before.stat"
findmnt -M "$hotel_disk"
sudo test -f "$hotel_new/.automation/.hotel-media-install.json"
sudo rsync -aHAXnc --numeric-ids --delete --itemize-changes -- \
  "$hotel_new/" "$hotel_old/"
```

前两条应相同。rsync 的 -n 为只读预览，逐项检查新增、修改和删除。新盘数据为本次同步来源，旧盘接收同步。确认旧盘能容纳当前数据，并为临时文件和备份预留空间。

### 3. 保存旧文件差异，再同步新盘数据

```bash
hotel_rollback="$hotel_disk/hotel-rollback-$(date +%Y%m%d-%H%M%S)"
sudo mkdir -m 0700 "$hotel_rollback"
(
  set -e
  test "$hotel_old" = /srv/hotel-tv/media
  if mountpoint -q "$hotel_old"; then exit 1; fi
  findmnt -M "$hotel_disk" >/dev/null
  test "$(sudo stat -c '%d:%i' "$hotel_old")" = \
    "$(sudo cat "$hotel_backup/media-before.stat")"
  sudo test -f "$hotel_new/.automation/.hotel-media-install.json"
  sudo rsync -aHAX --checksum --numeric-ids --delete-delay \
    --backup --backup-dir="$hotel_rollback/overwritten" -- \
    "$hotel_new/" "$hotel_old/"
)
```

备份目录在新盘、且位于复制来源之外，保存被替换或删除的旧文件。失败时先停留排查，新盘副本继续作为数据来源。

## 20 手动切换存储：完成回退与排错

### 1. 核验反向同步完成

```bash
sudo rsync -aHAXnc --numeric-ids --delete --itemize-changes -- \
  "$hotel_new/" "$hotel_old/"
```

应无差异输出且退出码为 0。同步失败或旧盘空间不足时，保留新盘为权威来源，修正原因后继续同步，或重新挂接新盘并恢复服务。

### 2. 移除本次绑定与依赖片段

```bash
sudoedit /etc/fstab
```

删除本次目标为 `/srv/hotel-tv/media` 的 bind 记录。逐行核对本次 fstab.before，保留数据盘 UUID 及其它后来合法增加的配置。按 paths.txt 恢复本次 hotel_dropin 变量。

```bash
for hotel_unit in hotel-tv.service docker.service smbd.service \
  hotel-tv-installer.service; do
  sudo rm -f -- "/etc/systemd/system/$hotel_unit.d/$hotel_dropin"
done
sudo findmnt --verify --verbose
sudo systemctl daemon-reload
sudo systemctl start docker.socket docker.service
sudo xargs -r -a "$hotel_backup/rollback-containers.running" docker start
sudo systemctl start hotel-tv smbd
```

复验播放、字幕、广告、下载和文件权限，再按原状态恢复软件发布。配置和数据库按当前业务继续使用，备份供需要时的专项恢复。

| 现象 | 处理 |
|---|---|
| umount 提示 busy | 查看仍在访问目录的进程，退出相关文件管理和工作目录后再卸载 |
| 新盘挂载或依赖失败 | 通过主机维护入口检查 UUID、fstab、挂载单元和磁盘状态 |
| 容器读到错误目录 | 停止相关容器和 Docker，核对挂载，再按第 18 页探针验证 |
| rsync 或权限校验失败 | 保留两份数据，解决空间、文件系统或权限问题后重新核对 |

## 21 验收后释放旧盘空间

绑定完成后，旧盘副本仍占空间。完成第 18 页重启和业务验收，并具备独立备份后，再安排维护窗口清理旧副本。清理期间继续保留新盘目录和本次配置备份。

### 1. 再次停写并卸载影院入口

按第 19 页第 1 步停止服务、记录本次容器并卸载 `$hotel_old`。随后执行下列检查，输出应与最初 media-before.stat 一致。

```bash
sudo stat -c '%d:%i' "$hotel_old"
sudo cat "$hotel_backup/media-before.stat"
findmnt -M "$hotel_disk"
sudo du -sh "$hotel_old" "$hotel_new"
```

### 2. 只清理已暴露的旧副本内容

下面的保护检查确认目标为本教程的旧入口、旧磁盘身份匹配、绑定已卸载且新盘数据仍存在。检查失败即停止本段。

```bash
(
  set -eu
  test "$hotel_old" = /srv/hotel-tv/media
  if mountpoint -q "$hotel_old"; then exit 1; fi
  findmnt -M "$hotel_disk" >/dev/null
  test "$(sudo stat -c '%d:%i' "$hotel_old")" = \
    "$(sudo cat "$hotel_backup/media-before.stat")"
  sudo test -f "$hotel_new/.automation/.hotel-media-install.json"
  sudo find "$hotel_old" -mindepth 1 -maxdepth 1 \
    -exec rm -rf --one-file-system -- {} +
)
```

### 3. 挂回新盘并恢复运行

```bash
sudo mount "$hotel_old"
findmnt -M "$hotel_old"
sudo systemctl start docker.socket docker.service
sudo xargs -r -a "$hotel_backup/rollback-containers.running" docker start
sudo systemctl start hotel-tv smbd
```

复验后按原状态恢复软件发布。旧盘释放空间以 df 为准，原来另有硬链接的文件可能仍被其它目录引用。

依据为 [Ubuntu mount](https://manpages.ubuntu.com/manpages/noble/man8/mount.8.html)、[systemd 挂载与依赖](https://manpages.ubuntu.com/manpages/noble/man5/systemd.mount.5.html)、[rsync](https://manpages.ubuntu.com/manpages/noble/man1/rsync.1.html) 和 [Docker 重启策略](https://docs.docker.com/engine/containers/start-containers-automatically/)。本节命令已作语法、临时文件夹和配置解析核验，正式存储切换按维护窗口实机验收。
