#!/usr/bin/env bash
set -euo pipefail

# 取得執行此腳本的非 root 使用者名稱與 UID
ACTUAL_USER="${SUDO_USER:-$USER}"
ACTUAL_HOME=$(eval echo "~${ACTUAL_USER}")
ACTUAL_UID=$(id -u "${ACTUAL_USER}")

INSTALL_DIR="${ACTUAL_HOME}/mdd-macos-build"
VENV_DIR="${INSTALL_DIR}/venv"
DATA_DIR="${INSTALL_DIR}/data"
RUN_DIR="${DATA_DIR}/run"
LOGS_DIR="${INSTALL_DIR}/logs"

DAEMON_PLIST="/Library/LaunchDaemons/local.mdd.engine.plist"
AGENT_PLIST="${ACTUAL_HOME}/Library/LaunchAgents/local.mdd.manager.plist"

log() { echo -e "\033[1;32m[MDD-MAC]\033[0m $*"; }
err() { echo -e "\033[1;31m[ERROR]\033[0m $*" >&2; exit 1; }

check_system() {
    [[ "$(uname -s)" == "Darwin" ]] || err "此腳本僅支援 macOS 系統！"
    [[ $EUID -eq 0 ]] || err "請以 sudo 執行此安裝腳本: sudo $0 install"
    
    # 檢查 Xcode Command Line Tools
    if ! xcode-select -p &>/dev/null; then
        log "尚未安裝 Xcode Command Line Tools，正在嘗試觸發安裝..."
        xcode-select --install
        err "請先完成 Xcode Command Line Tools 安裝後再重新執行本腳本。"
    fi
}

do_install() {
    log "開始安裝 mdd-sim-gateway macOS 移植版..."
    check_system

    # 1. 建立必要目錄並修復使用者權限
    mkdir -p "${RUN_DIR}" "${LOGS_DIR}"
    chown -R "${ACTUAL_USER}:staff" "${INSTALL_DIR}"

    # 2. 建立 Python 虛擬環境
    if [[ ! -d "${VENV_DIR}" ]]; then
        log "正在以使用者 ${ACTUAL_USER} 建立 Python 虛擬環境..."
        sudo -u "${ACTUAL_USER}" python3 -m venv "${VENV_DIR}"
    fi

    log "安裝/更新 Python 相依套件..."
    sudo -u "${ACTUAL_USER}" "${VENV_DIR}/bin/pip" install --upgrade pip
    sudo -u "${ACTUAL_USER}" "${VENV_DIR}/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"

    # 3. 確保關鍵腳本具備執行權限（修復研究報告 3.4 問題）
    [[ -f "${INSTALL_DIR}/engine/notify.py" ]] && chmod +x "${INSTALL_DIR}/engine/notify.py"
    [[ -f "${INSTALL_DIR}/build-asterisk.sh" ]] && chmod +x "${INSTALL_DIR}/build-asterisk.sh"

    # 4. 原生編譯 Asterisk（若尚未編譯）
    if [[ ! -f "${INSTALL_DIR}/staging/sbin/asterisk" && ! -f "/usr/local/sbin/asterisk" ]]; then
        log "正在執行 Asterisk 20.7.0 原生編譯與打補丁 (build-asterisk.sh)..."
        sudo -u "${ACTUAL_USER}" "${INSTALL_DIR}/build-asterisk.sh"
    else
        log "偵測到已存在的 Asterisk binary，跳過重複編譯。"
    fi

    # 5. 修復設定檔中的 manager_url (移除 host.docker.internal 遺留值)
    for conf in "${INSTALL_DIR}/config.yaml" "${DATA_DIR}/instance.json"; do
        if [[ -f "$conf" ]]; then
            sed -i '' 's|https://host.docker.internal:8443|https://127.0.0.1:8443|g' "$conf" 2>/dev/null || true
        fi
    done

    # 6. 配置 Root LaunchDaemon (local.mdd.engine)
    log "配置底層引擎 LaunchDaemon (${DAEMON_PLIST})..."
    cat <<EOF > "${DAEMON_PLIST}"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>local.mdd.engine</string>
    <key>ProgramArguments</key>
    <array>
        <string>${VENV_DIR}/bin/python</string>
        <string>${INSTALL_DIR}/engine_supervisor.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${INSTALL_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${LOGS_DIR}/engine.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${LOGS_DIR}/engine.stderr.log</string>
</dict>
</plist>
EOF
    chown root:wheel "${DAEMON_PLIST}"
    chmod 644 "${DAEMON_PLIST}"

    # 7. 配置 User LaunchAgent (local.mdd.manager)
    log "配置 Web 控制面 LaunchAgent (${AGENT_PLIST})..."
    mkdir -p "$(dirname "${AGENT_PLIST}")"
    cat <<EOF > "${AGENT_PLIST}"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>local.mdd.manager</string>
    <key>ProgramArguments</key>
    <array>
        <string>${VENV_DIR}/bin/python</string>
        <string>${INSTALL_DIR}/manager.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${INSTALL_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${LOGS_DIR}/manager.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${LOGS_DIR}/manager.stderr.log</string>
</dict>
</plist>
EOF
    chown "${ACTUAL_USER}:staff" "${AGENT_PLIST}"
    chmod 644 "${AGENT_PLIST}"

    # 8. 載入並啟動服務
    log "載入並啟動服務..."
    launchctl unload "${DAEMON_PLIST}" 2>/dev/null || true
    launchctl load -w "${DAEMON_PLIST}"

    sudo -u "${ACTUAL_USER}" launchctl unload "${AGENT_PLIST}" 2>/dev/null || true
    sudo -u "${ACTUAL_USER}" launchctl load -w "${AGENT_PLIST}"

    log "安裝完成！"
    echo -e "\n======================================================="
    echo -e " 管理控制台已在背景啟動："
    echo -e " 瀏覽器訪問：\033[1;34mhttps://127.0.0.1:8443\033[0m"
    echo -e " WebRTC 軟電話埠：WSS 8088"
    echo -e " 引擎 Socket：${RUN_DIR}/engine.sock"
    echo -e " 日誌目錄：${LOGS_DIR}"
    echo -e "=======================================================\n"
}

do_uninstall() {
    check_system
    log "正在解除安裝與移除服務..."
    launchctl unload "${DAEMON_PLIST}" 2>/dev/null || true
    rm -f "${DAEMON_PLIST}"
    
    sudo -u "${ACTUAL_USER}" launchctl unload "${AGENT_PLIST}" 2>/dev/null || true
    rm -f "${AGENT_PLIST}"
    
    pkill -9 swu_ike 2>/dev/null || true
    log "服務已移除並停止。"
}

case "${1:-install}" in
    install)   do_install ;;
    uninstall) do_uninstall ;;
    *)         echo "用法: sudo $0 {install|uninstall}" ;;
esac
