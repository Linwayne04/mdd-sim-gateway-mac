# macOS 12 (Monterey) 移植適配指引

> 給在目標機器上執行的 Claude Code session。任務：把 mdd-sim-gateway 的 macOS
> native port 在 macOS 12 上從零 build 起來並跑通一條 VoWiFi 線路。
> 開發基準機 = macOS 15 (Darwin 24.6, Intel x86_64, MacPorts)，本指引列出兩者
> 已知的差異與風險點。完整背景見 `docs/macos-port/PLAN.md`。

## 0. 背景一句話

mdd-sim-gateway = VoWiFi SIM 閘道（GPL-3.0）。native macOS port 用 LaunchDaemon
跑引擎（Asterisk per-line + swu_ike userspace ESP over utun），user LaunchAgent
跑 FastAPI 控制面，WebUI 為靜態 assets（已內建於 repo，不需 node build）。

## 1. 環境確認（先做）

- `sw_vers`（應為 12.x，Darwin 21）、`uname -m`（x86_64 或 arm64，腳本皆有處理）。
- Xcode CLT：Monterey 最終版是 CLT 14.2。`clang --version` 確認；
  太舊就 `xcode-select --install` 或 `softwareupdate --list`。
- **套件管理器強烈建議用 MacPorts，不要用 Homebrew**：Homebrew 官方只保留
  最近三個 macOS 版本的 bottles，Monterey 上 `brew install` 會退化成
  source build 或直接找不到 formula。MacPorts 對舊系統支援期長、bottles 齊。
- ⚠️ MacPorts 裝套件需要 sudo，而 **Claude Code 環境 sudo 沒有 tty**
  （`! sudo ...` 會失敗）。解法：請使用者在一般終端機先跑
  `sudo port -N install python312 nodejs22 autoconf automake libtool bison flex
  pkgconfig jansson libxml2 sqlite3 openssl3 ncurses`，之後再讓 Claude 繼續；
  或每次用 `osascript -e 'do shell script "..." with administrator privileges'`
  （會跳 GUI 授權框，需要人在電腦前）。

## 2. 取得程式碼

```bash
git clone https://github.com/Linwayne04/mdd-sim-gateway-mac.git
cd mdd-sim-gateway-mac && git checkout macos-port
```

- 若上游已合併 PR #220（macOS body），可改用上游 `MddIdd/mdd-sim-gateway`。
- 若之後要 push：repo 很大，`git config http.postBuffer 1GB`、
  `git config http.version HTTP/1.1`；裝 MacPorts 的 `gh` 並 `gh auth login`。
- **永遠不要把 API key / token / 密碼印進對話或 commit**；push 前先掃描 diff。

## 3. 安裝

```bash
./install-macos.sh install        # 冪等；--no-autostart = 裝好先不載入
./install-macos.sh status
```

- 腳本會自動偵測 brew/port；deps 已含 python312、autoconf/automake/libtool。
- venv 建在 `~/mdd-macos-build/venv`，用 **python3.12**——系統 python3
  （3.9.6）不行，控制面與 engine daemon 都以 venv 解釋器跑。
- `install-macos.sh` **沒有 macOS 最低版本檢查**（只檢查 Darwin 與 ARCH）。
  適配完成、驗收通過後，應回補一個 `sw_vers` 版本閘門（例如 ≥ 12），
  避免在更舊系統上跑出半吊子狀態。

## 4. 高風險點（預期需要實測修正）

1. **Asterisk / pjproject 在 clang 14 下編譯 —— 最高風險**。
   pinned rev：pjproject `20537ab1`、asterisk `d231cb2c`（+cherry-pick `f1b60dc`，
   tag 判定 `mdd-f1b60dc-pick`）。流程見 `host/macos/build/build-asterisk.sh`
   檔頭註解：distclean → py patches → main/Makefile、main/xml.c 內聯補丁 →
   `patches/asterisk/*.patch`（冪等 marker = 每個 patch 第一個 `+` 行）→
   `sh bootstrap.sh`（必須，02 patch 改了 configure.ac）→ configure →
   menuselect → make。遇錯先看**第一個 error**；常見解法是補 include 或
   `-Wno-error=...`。**修正必須寫進 `patches/asterisk/*.patch`**，不能只改
   build tree（clean build 會丟）。
2. **pip wheels**：`control/requirements.txt` 釘了新版（cryptography 50、
   uvicorn 0.52、fastapi 0.141…）。這些多半有 `macosx_11_0` wheel 可在 12 跑；
   若 pip 誤判去本機編譯（cryptography 需要 Rust，必炸），先
   `pip download <pkg>==<ver> --no-deps -d /tmp/w` 檢查 wheel tag，不行就降版
   並在 PLAN.md 註記原因。
3. **sing-box / Xray egress**（可選，`--no-egress` 略過）：Go 編譯、最低
   macOS 11，12 可用；`fetch-egress.sh` 有 sha256 + version 自檢。
4. **launchd jobs**：engine / control / orchestrator / update 四個 plist 只用
   10.11+ API（bootstrap、enable/disable、kickstart -k），Monterey 直接可用。
   ⚠️ `install-launchd.sh` 的 case 只吃 `$1`——多個 job 要**分開呼叫**
   （`--daemon` 一次、`--orchestrator` 一次…）。
5. 若 Intel 機：`--no-egress` 之外的步驟與開發機相同。若 Apple Silicon：
   一切走 arm64 路徑（scripts 已分支），但 **arm64 從未實機驗證過**。

## 5. 操作守則（沿用開發機的約束）

- root 操作 = osascript administrator prompt 或 engine daemon socket；不要試 `! sudo`。
- zsh：`echo ===X` 會炸（`=` word expansion），改用 `echo NEXT` 之類。
- foreground `sleep` 會被擋；等待請用 background command 或 Monitor。
- 測試一律 targeted：
  `~/mdd-macos-build/venv/bin/python -m unittest tests.test_<module>`
  ——**不要跑 `unittest discover`（會 hang）**；venv 沒裝 pytest。
- engine daemon：root LaunchDaemon `local.mdd.engine`，Unix socket
  `~/mdd-macos-build/data/run/engine.sock`（0600，owner = build-root owner），
  協議 JSON + "\n"，actions: start/stop/status/exec/logs/pcap。
  daemon 重啟會忘掉 RUNNING 表 → 線路要 stop+start 才重生。
- Asterisk per-line：`-C ~/mdd-macos-build/data/instances/<iid>/etc/asterisk`。
  **不要用 `core restart now`**（有 heap corruption 前科且會重置 debug flags）；
  要換 binary 就 stop 線路 → 起新樹。
- tcpdump 需要 root → 一律走 daemon 的 pcap action（BPF filter 有白名單，
  非白名詞彙會被拒）。
- 這台機器的 `~/mdd-macos-build/data/` 是全新狀態：線路、SIM、config 都要
  重新設定；SIM 卡 / 讀卡機狀態不會從開發機帶過來。

## 6. 驗收標準（比照開發機已驗證項目）

1. `./install-macos.sh status`：engine（root daemon）+ control（user agent）
   綠；orchestrator 若裝了也要綠（update job 可有可無）。
2. venv 下 control 的 targeted unittest 全綠。
3. 一條 VoWiFi 線路端到端：IMS Registered → 600 echo test 通 →
   MO SMS 進歷史 → MT SMS 收得到 → WebUI 內建軟電話 ICE/DTLS 通。
4. 讓線路跑過一次 rekey（supervisor 已 setdefault
   `SWU_REKEY_TIMEOUT=20` / `SWU_REKEY_RETRANSMITS=8`，總預算 180s）。
5. 修任何東西前，先判斷是「macOS 12 特有」還是「通用 bug」：
   - 12 特有 → runtime gate（如 `os.uname().release` 主版本），不能影響 15 的行為；
   - 通用 → 照上游慣例處理。

## 7. 回饋與提交

- 平台無關 bug：基於上游 **develop** 開 `fix/...` 分支、一題一 PR、附復現
  步驟（參考 fork 上 fix/sms-result-ignore-softphone-ws-405 等五個 PR 的格式，
  描述用簡體中文，結尾 `🤖 Generated with [Claude Code](https://claude.com/claude-code)`）。
- macOS 特有修正：推到 fork 的 `macos-port` 分支（若上游 PR #220 已合，
  改開新分支）。commit 結尾 `Co-Authored-By: Claude Code <noreply@anthropic.com>`。
- 每個修正同步更新 `docs/macos-port/PLAN.md`。
