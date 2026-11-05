完整 NAS 已由根 install.sh --profile full 编排。
动态识别本机地址与操作用户，使用 images.lock.json 锁定镜像，凭据安装时生成。
媒体 /srv/hotel-tv/media，应用数据 /srv/nas/appdata，Compose 栈 /opt/stacks。
配置/初始化脚本属于内部阶段，不宜脱离首装状态重复运行。
详见 ../../docs/安装与维护.md 与 ../../docs/使用与存储.md。
