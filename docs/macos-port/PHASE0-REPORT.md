# Phase 0 可行性驗證報告(spike 結果)

> 日期:2026-09-28。機器:macOS 15 (Darwin 24.6.0),Intel x86_64,Apple clang 17。
> 結論:**四項 spike 全部通過,原生移植路線可行,無須退回 Linux VM 混合方案。**
> 最終狀態:**310 個模組編出並安裝,白名單達成 125/126**(唯一缺席 `res_timing_timerfd.so`,
> macOS 無 timerfd,`res_timing_pthread.so` 已編出作為 fallback)。

## 1. 建置環境

- Homebrew 在此機器不可用(拒絕非 Apple Silicon),改用 **MacPorts 2.12.6**(Sequoia pkg)。
- 已裝 23 個 ports,關鍵:`openssl3 jansson unbound libsrtp2 libuuid speex libvorbis libxml2 sqlite3 libusb libtool autoconf automake pkgconfig ncurses libedit`。
- 建置環境腳本:`~/mdd-macos-build/env.sh`(`/opt/local` 進 PATH/PKG_CONFIG_PATH,`CC=cc`)。
- **為日後 macOS 12 向下相容,所有編譯已加 `-mmacosx-version-min=12.0`。**

## 2. Spike A:Asterisk(sysmocom fork 20.7.0)原生編譯 ✅

結果:configure 通過、**310 個模組**編出、`asterisk -V` 正常。

踩過並修正的坑(全部位於 `~/mdd-macos-build/asterisk` 工作樹,未動 upstream repo):

| 問題 | 修法 |
|---|---|
| autoconf 2.73 先探 C23 → `-std=gnu23` 破壞 db1-ast K&R 程式碼 | configure 加 `ac_cv_prog_cc_c23=no` |
| Makefile.rules 給 clang 下 GNU 旗標 `-fno-partial-inlining` | `CC=cc`(/usr/bin/gcc 是 clang,`-dumpversion` 回 17.x 誤導版本檢查) |
| `main/Makefile` 硬編 `/usr/lib/bundle1.o`(10.4 時代遺跡) | 刪除,順便把 `-mmacosx-version-min=10.6` 改成 `12.0` |
| `codec_vevs.c` 用 Linux-only `SOCK_CLOEXEC` | 加 `#ifndef SOCK_CLOEXEC #define SOCK_CLOEXEC 0` shim |
| configure.ac darwin 區塊無條件定義 `AST_POLL_COMPAT`(10.4 時代 select 版 poll)與現代原生 poll 衝突 | 移除該 `AC_DEFINE`,重跑 bootstrap.sh |
| `strcompat.c` 缺 poll 宣告 | `#ifdef __APPLE__ #include <poll.h>` |
| GNU `install -D`(Makefile.moddir_rules) | 改 `mkdir -p && install -m 755` |
| `res_geolocation` 用 GNU ld `-b binary`/`-Wl,-znoexecstack` | menuselect 停用 `res_geolocation` + `res_pjsip_geolocation`(兩者皆不在 126 白名單;後者 depend 前者,只停一個會被 menuselect 以 BUILD_DEPS 自動加回) |
| pjproject `dependency_utils` 用 GNU `realpath -L --relative-to=`(BSD realpath 無此選項) | `mdd-macos-build/bin/realpath` shim(GNU 子集,純 lexical relpath),經 env.sh PATH 生效 |
| staging 後 dyld 找不到 `libasteriskssl.dylib` | 驗證時帶 `DYLD_LIBRARY_PATH=<stage>/usr/local/lib` |
| 9 個 MDD python patch 路徑寫死 `/home/asterisk-build` | sed 改寫後複製到 `mdd-macos-build/patches-asterisk/`,9/9 套用成功 |
| pjproject 第三方連結 | `ln -sfn ../../../pjproject third-party/pjproject/source` |

模組目錄(darwin 預設含空白):`<stage>/Library/Application Support/Asterisk/Modules`。

## 3. Spike B:PC/SC 智慧卡棧 ✅

- pcsc-lite 2.3.3(meson,`-Dpolkit=false -Dlibsystemd=false -Dlibudev=false -Dlibusb=true`)+ CCID 1.6.2 編譯成功,pcscd 原生啟動:
  `Enabled features: USB serial filter_names libusb MacOS x86_64`。
- CCID tarball 取自 `install.sh` 使用的 archive URL(GitHub releases 會 404)。

## 4. Spike C:utun 使用者態 dataplane ✅(`~/mdd-macos-build/utun_poc.py`)

- PF_SYSTEM/SYSPROTO_CONTROL 連 `com.apple.net.utun_control`;CPython 用 tuple API `fd.connect((ctl_id, unit))`,unit N → `utun(N-1)`。
- **關鍵發現(影響 swu_ike 移植)**:
  1. utun 封包 4-byte AF header 在 darwin 24 x86_64 上**讀寫皆為 big-endian**(`00 00 00 AF`);寫錯 byte order 不會報錯,封包被靜默丟棄。macOS 12 上需實機複驗。
  2. checksum 必須以 network byte order(`!H`)unpack。
- PoC 結果:ping `10.99.99.2` 3/3 收到 echo reply,RTT ~0.2ms。

## 5. 白名單(126 模組)比對

| 模組 | 狀態 | 處理 |
|---|---|---|
| `res_timing_timerfd.so` | 缺席(預期) | macOS 無 timerfd;`res_timing_pthread` 已編出作為 fallback |
| `res_pjsip_outbound_registration.so` | ✅ 移植完成 | 見下 |
| `codec_amr.so` | ✅ 已解決 | opencore-amr 0.1.6 + vo-amrwbenc 0.1.3 自 SourceForge 原始碼編入 `stage/`(MacPorts/Homebrew 皆無此 port)。注意:Asterisk 的 `AST_EXT_LIB_CHECK` **不走 pkg-config**,必須下 `--with-opencore-amrnb/--with-opencore-amrwb/--with-vo-amrwbenc=<prefix>` 才會加 `-L/-I` |

### res_pjsip_outbound_registration 的 Linux 耦合與 macOS 對策

此模組含 sysmocom 的 VoLTE 擴充(`volte.c` 的 IMS AKA 註冊狀態機、`milenage.c`),並透過
`netlink_xfrm.c`(libmnl + `<linux/xfrm.h>`)向**核心**安裝 IPsec SA/SP。

- 本 gateway 的 ESP dataplane 本來就是**使用者態**(swu_ike.py 走 tun/utun,`pjsip.conf` 的
  `bind_interface=ipsec0`),kernel XFRM 只是 fork 裡的備援路徑;`volte_set_xfrm()` 的回傳值
  在呼叫端被忽略,失敗僅記 log。
- 對策:`netlink_xfrm.c` 全檔包 `#ifdef __linux__`,非 Linux 編 stub
  (回 `-EOPNOTSUPP`);`netlink_xfrm.h` 在 `__APPLE__` 下自備 `struct xfrm_algo`
  (固定 160-byte key buffer,避免 flexible array member 嵌入問題);
  移除 MODULEINFO 的 `<depend>libmnl</depend>`。三個 .c 均通過獨立編譯驗證。

## 6. 產出物(~/mdd-macos-build/)

- `env.sh`、`build-asterisk.sh`(完整 distclean→configure→menuselect→make→install→verify 一條龍)
- `utun_poc.py`(含上述 byte-order 結論)
- `compare-modules.sh`(對照白名單)
- `patches-asterisk/`(路徑修正版 9 patch)
- `stage/`(pcsc-lite、CCID、opencore-amr、vo-amrwbenc 等自編 prefix)

## 7. 結論

Phase 0 四項驗證全數通過,繼續 Phase 1(控制面原生)。後續注意事項:

1. utun AF header byte order 需在 macOS 12 實機複驗(向下相容階段)。
2. MacPorts dylib 以 macOS 15 target 編譯;macOS 12 相容需在 12 的環境重編或驗證。
3. Quectel QMI modem 無 macOS 驅動(已知風險,Phase 4 處理;Android USB 網路共享原生支援)。
4. **pyscard 要連自編 pcscd**(vpcd 虛擬讀卡器需要),但 pyscard 在 darwin 預設連 PCSC.framework——Phase 1 需讓 pyscard 改連自編 pcsc-lite(改 setup 的 include/lib 指向 stage,或修補其 darwin 分支),pcscd socket 路徑與 Linux 的 `/run/pcscd` 不同,控制面若寫死路徑需適配。
5. 控制面 requirements 全是純 Python 或有 macOS wheel(fastapi/uvicorn/pyscard/cryptography/pyserial…),Python 3.12 已由 MacPorts 提供;`docker` 套件在原生模式用不到但可安裝(僅 import)。
