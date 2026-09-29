# mdd-sim-gateway macOS 原生移植方案

> 目標：在 macOS(Intel x86_64 驗證,Apple Silicon 可後續適配)上原生運行完整功能:
> VoWiFi 通話、SMS/MMS、4G 行動數據、每國出口路由,最多 10 條 SIM 線路。

## 1. 現狀盤點(探索結論)

| 層 | 組件 | Linux 依賴 |
|---|---|---|
| WebUI | React+Vite+jsSIP (~2500 行) | 無,純跨平台 |
| 控制面 | FastAPI + uvicorn (~39k 行 Python) | `mmcli`/`busctl`/`udevadm`(cellular_*, mms_transport)、sysfs/proc(sysinfo, usbreader)、`/run/pcscd` socket、Docker SDK(engine.py)、`/etc/resolv.conf` |
| 引擎 | entrypoint.sh 監督 render.py→pin_keeper→swu_ike→ami_usim→Asterisk | `/dev/net/tun`+TUNSETIFF、`ip route/rule/netns`、sysfs、pcsc-lite socket、Fedora 容器 |
| 宿主 | mdd_orchestrator.py + install.sh (2000 行) + mdd_update.py | systemd(systemd-run/units/journalctl)、ModemManager、NetworkManager、udev、TUN、`ip`/`ss`、apt/dnf |
| 外部件 | sing-box、Xray、lpac、Asterisk(sysmocom fork + 9 patches)、pcsc-lite、libccid、vpcd | 前 3 個有官方 darwin 二進位;後 4 個需源码編譯 |

## 2. macOS 目標架構

```
macOS host (launchd 取代 systemd)
├── mdd-control          FastAPI+React,venv 原生跑,8443 HTTPS
├── mdd-orchestrator     sing-box/xray (darwin)+ route/pfctl 路由 + modem 發現
├── pcscd                pcsc-lite 源码編譯於 macOS,libccid(USB 讀卡器/modem)
├── mdd-modem-bridge×N   AT+CSIM → VPCD 虛擬讀卡器(modem SIM 線路)
└── engine-<line>×N      每線一組 launchd 監督的程序(取代 Docker 容器):
    render.py → pin_keeper.py → swu_ike.py(utun)→ ami_usim.py → asterisk(原生編譯)
```

控制面↔引擎契約不變:instance.json、run 目錄狀態檔(pin/swu/usim_status.json、pcscf)、
notify HTTP 回調、AMI 5038、軟電話 WS——只是「容器管理」換成「進程管理」。

## 3. 分層改寫計畫

### A. 可直接運行/微調(工作量小)
- **WebUI**:`npm ci && npm run build`,需安裝 Node。
- **控制面主體**(main.py/store.py/config.py/auth.py/egress.py/ami.py/softphone_ws.py…):純 Python,直接跑。
- **pyscard 相關**(sim.py/card.py/lpa.py):macOS 走 PCSC.framework,基本相容。
- **sing-box / Xray**:官方 darwin 版;install.sh 的下載 URL 寫死 `-linux-`,需參數化。

### B. 需要平台抽象層(中等工作量)
1. **`engine.py` 引擎後端抽象**:新增 `NativeEngineBackend`(launchd/subprocess 監督),
   與 `DockerEngineBackend` 並存,按 `sys.platform` 或設定選擇。容器名→線路 run 目錄;
   `cap_add`/`sysctls`/port publish 等 Docker 參數在原生模式忽略;RTP 端口直接綁本機。
2. **`swu_ike.py` TUN dataplane**:`/dev/net/tun`+TUNSETIFF → macOS utun
   (PF_SYSTEM/SYSPROTO_CONTROL,4-byte AF header);`ip link/route/rule/netns` →
   `route`/`ifconfig`/`pfctl`/scutil;`/proc/net/route` → `route -n get default`;
   netns 在 macOS 不啟用(原生模式本就不需要);`ipsec0` 名字 → `utunN`。
3. **cellular 後端**:`cellular_sms/call/modem_ims/modem_voice/mms_transport` 的
   mmcli/busctl → 直接 pyserial AT 命令(`AT+CIMI`、`AT+CPMS`、`AT+CMGS`、`AT+CSIM`、
   `ATD`…),即 vpcd_modem_bridge 已有的 AT 邏輯下沉為共用模組。modem 偵測改走 IOKit
   (PyObjC)枚舉 `/dev/cu.usbserial*`、`/dev/cu.wdm*`。
4. **sysinfo/usbreader**:sysfs/proc → PyObjC IOKit + `sysctl` + `nettop`/`route`。
5. **install.sh → install-macos.sh**:launchd plist 取代 systemd units
   (control、orchestrator、pcscd、per-engine、update);
   套件依賴用 brew 或源码編譯;webui 用本機 Node。

### C. 高風險,需先行驗證(不確定性高)
1. **sysmocom Asterisk fork 的 macOS 編譯**:pjproject 可編;Asterisk 主體含大量
   Linux-only res_*;`asterisk-keep-modules.txt` 白名單(126 模組)需按 macOS 可編
   模組修剪;pjsip.conf 模板 `bind=ipsec0` → utun 介面名;9 個 patch 本身與平台無關。
   → **Phase 0 先做編譯驗證(spike)**。
2. **USB modem 的 macOS 驅動**:Quectel EC25 等 QMI modem 無官方 macOS 驅動
   (AT 串口可能可見,網卡不行);Android 手機 USB 網路共享(CDC-ECM/NCM)macOS
   原生支援。→ 影響「4G 行動數據」功能的可行性,需確認你的硬體。
3. **pcsc-lite + libccid + vpcd 的 macOS 源码編譯**(用 libusb 後端),以及 pyscard
   對自編 pcsc-lite 的綁定(vs 系統 PCSC.framework)——為讓 modem 虛擬讀卡器
   (VPCD)工作,傾向整組 pcsc-lite 自編方案。

## 4. 分期執行

- **Phase 0 — 可行性驗證(spike,不寫正式碼)** — ✅ **完成(2026-09-28)**
  1. ~~裝好 brew / Python 3.12 / Node;~~ → Homebrew 不支援此 Intel 機,改用 MacPorts 2.12.6;
  2. ~~編譯 sysmocom Asterisk fork(先 pjproject)在 macOS 上 `asterisk -V` 成功;~~
     → Asterisk 20.7.0 編譯安裝成功,310 模組,白名單 125/126(僅缺 `res_timing_timerfd`);
  3. ~~pcsc-lite + libccid 編譯跑起,pyscard 列出 USB 讀卡器;~~
     → pcsc-lite 2.3.3 + CCID 1.6.2 原生啟動(pyscard 綁定留待 Phase 1);
  4. ~~Python 建立 utun 介面讀寫 IP 封包 PoC。~~ → utun PoC ping 3/3 通。
  結果:四項全過,免退回混合方案。詳見 `PHASE0-REPORT.md`。
- **Phase 1 — 控制面原生** — ✅ **完成(2026-09-28)**
  1. venv(MacPorts py3.12)+ 全部依賴;
  2. pyscard 2.3.1 自 PyPI 編譯(補 `PYSCARD_PCSC_LIB` override + SCardControl fallback,
     `LIBPCSCLITE_DELEGATE` 繞過 pcsc-lite shim 的 Linux soname 硬編);
  3. WebUI 建置(MacPorts node 無 npm,用 npm registry tarball + wrapper);
  4. 控制面原生啟動(`host/macos/run-control.sh`),0 錯誤,Docker/modem 警告級降級;
  5. **實機驗證:插入 USB 讀卡器(Generic USB2.0-CRW)成功熱插拔偵測。**
  6. 修復:儲存 IMEI 時 500 —— `api_device_hardware` → `engine.is_running` →
     `container_runtime` 只捕獲 `NotFound`,`docker.from_env()` 的 `DockerException`
     直接炸穿;已在 `engine.py:616` 補捕獲(原生模式無 daemon → 一律 not-running)。
  7. 修復:讀卡 APDU 全失敗(0x80100016)—— macOS CryptoTokenKit 協商出 T=1 但
     對部分讀卡機(Generic USB2.0-CRW)每個 APDU 都回 CARD_UNSUPPORTED;T=0 正常。
     新建 `control/app/platform/pcsc.py`(`connect()` 包協議選擇,darwin 強制 T=0,
     失敗回退預設遮罩),`sim.py`×2、`estkme.py`×1 三個連線點改走此邊界。
     **引擎側已在 Phase 2 處理:`engine/pcsc_platform.py` 複製同一邏輯,
     pin_keeper.py / ami_usim.py / swu_ike.py 的全部連線點改走此邊界。**
- **Phase 2 — 單線 VoWiFi 端到端** — 🔨 **程式完成(2026-09-28),待 sudo 安裝 daemon 後 E2E**:
  引擎進程化 + swu_ike utun + Asterisk;外接 PC/SC 讀卡器一張 SIM,
  完成註冊/收發簡訊/瀏覽器通話。
  - 已完成:render/notify/模板 `MDD_PREFIX` 前綴化(Linux 行為逐位不變)、
    `engine/utun_darwin.py`(PF_SYSTEM utun + AF header + ifconfig/route)、
    swu_ike darwin 全套(utun、scoped 路由不做 /1 全捕獲、NAT-T 強制、
    fork start method、媒體路由流量學習 journal 至 media_routes)、
    `engine/pcsc_platform.py` + 引擎側三檔 T=0 邊界、
    `host/macos/engine_supervisor.py`(entrypoint.sh 的 Python 移植)、
    `host/macos/mdd_engine_daemon.py` + LaunchDaemon plist + install 腳本、
    控制面 `engine_native.py` socket 後端(engine.py/main.py/runtime.py 分派)。
  - ~~待使用者:(a) `sudo host/macos/install-engine-daemon.sh`;~~
    (a) 已完成（daemon plist 已由 install-launchd.sh 模板化取代）；
    ~~(b) 批准安裝 mitshell/card~~(已裝,`card-0.3`,swu_ike.USIM 驗證可匯入);
    (c) 之後跑 E2E(啟動線路 → 註冊 → 簡訊 → 瀏覽器通話)。
- **Phase 3 — 多線 + 國家出口**:10 線並發、sing-box darwin TUN、`route` 固定 ePDG。
- **Phase 4 — modem 整合**:AT+CSIM 橋接 modem SIM、AT 簡訊/通話、4G 數據(networksetup)。
- **Phase 5 — 安裝/更新/打包**：install-macos.sh、launchd 全套、mdd_update 的 launchd 化、
  備份/診斷/日誌(journalctl → log show / 檔案日誌)。
  - ✅ **install-macos.sh + launchd 安裝（2026-09-29）**：`install-macos.sh`
    （install/status/logs/uninstall，冪等，brew/port 自動偵測，Intel+arm64）
    + `host/macos/build/{fetch-sources,build-support-libs,build-asterisk}.sh`
    （版次/sha256 全鎖定）+ `host/macos/install-launchd.sh`
    （plist 模板渲染，取代寫死路徑的 install-engine-daemon.sh）。
    注意：全新機器與 arm64 尚未實機驗證（僅本機冪等重跑驗證）。
  - 待辦：mdd_update 的 launchd 化、備份/診斷。

## 5. 程式組織原則

- 平台分支集中在邊界:新增 `control/app/platform/`(darwin/linux 適配)與
  `host/macos/`,既有檔案只做最小 `if sys.platform == "darwin"` 分派,保住 upstream 可合性。
- 引擎↔控制面契約(instance.json、狀態檔、notify、AMI)完全不動。
- 所有 SHA256 校驗、安全預設(0600/0700、TLS、密碼 scrypt)維持原樣。
