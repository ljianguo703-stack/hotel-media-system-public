# 软件与更新

完整安装通过根 `install.sh --profile full` 接入后台、systemd path → oneshot 受控执行器、APT/DEB 校验和电视 APK 检查。core 仅提供影院业务，不安装这套特权执行器。

后台程序升级包只包含 server/admin：

```bash
python3 deploy/software/package.py --source . --output ../system-update.zip --version 新版本号 --base-release 当前版本号
```

版本必须与目标 current 匹配；先在开发环境检查/演练，备份业务数据，再在后台明确应用。源码首装 tar.gz 不能作为后台升级 ZIP。

APK 必须签名和版本校验通过；通过明确目标的 ADB 覆盖安装，不卸载、不清数据、不改 HOME、不扫描网段。第三方 APK 不保证所有电视兼容；影院自更新不能跨签名。

DEB 使用 Ubuntu 原生包管理，保留检查、确认、日志和回退边界。不要绕过队列从浏览器执行任意 shell。详见 ../../docs/安装与维护.md。

IN11起程序ZIP与执行器分组件更新：已有店先在后台安装`package_agent.py`生成的原生DEB，等待任务完成/进程退出，再上传程序ZIP。DEB只更新五个固定模块并原子切换原服务读取的链接，不重启当前执行器；新full首装直接复制完整五模块。操作与验收见[批量运维与公共通知](../../docs/management/批量运维与公共通知.md)。
