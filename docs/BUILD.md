# 源码构建

本源码快照包含服务器、管理界面、Android 客户端和电视安装工具。依赖与镜像版本由项目锁定文件管理。公开安装包运行源码的基线和 SHA 见根目录 `PUBLIC_RELEASE.json`。

## Python 与管理界面

使用 Python 3.9+ 和 Node.js。安装器入口为 `deploy/bootstrap.py`，全量部署还会拉取锁定的 Docker 镜像。

```bash
python3 -m unittest discover -s server -p 'test_*.py'
python3 -m unittest discover -s tests -p 'test_*.py'
node --test admin/tests/*.test.cjs
```

## Android

准备 JDK 21、Android SDK 及 Gradle 所需依赖。调试构建可使用项目自身的标准配置。发行签名由构建者自行保管，应用升级需要与已装版本签名相符。

```bash
bash ./gradlew --max-workers=1 \
  :app:testStandardDebugUnitTest :app:testGuardDebugUnitTest \
  :app:lintStandardRelease :app:lintGuardRelease \
  :app:assembleStandardRelease :app:assembleGuardRelease
```

签名配置格式见 `app/build.gradle.kts`，本店服务器和设备凭据由安装时配置。

## 电视安装工具

准备 Go 1.24+，构建原厂电源 helper 时还需 JDK 21、Android SDK。平台 ADB、运行依赖和经核验的签名 APK 由构建者准备。

```bash
python3 tv-installer/tools/build_catalog.py
python3 tools/build_connection_guide.py
(cd tv-installer && go test -race ./...)
```

四平台完整便携包的构建参数见 `tools/build_tv_bundle.py --help`，产物核验见 `tools/verify_tv_bundle.py --help`。机型支持由精确身份与固件条件决定。


## 单文件服务器直升DEB

在Ubuntu准备Python3.9+及dpkg-deb。程序包不含本店数据、签名材料、APK或容器镜像。

```bash
python3 deploy/software/package_upgrade.py --source . \
  --output dist/hotel-tv-upgrade-latest.deb
python3 -m unittest discover -s tests -p test_unified_upgrade.py
```

构建器仅收集server/admin允许的109个程序文件和统一安装控制器，生成Architecture all的DEB。隔离原IN17执行器/HTTP/dpkg闭环脚本为tools/unified_upgrade_e2e.py，必须在一次性root Docker环境运行，不用于现场。
