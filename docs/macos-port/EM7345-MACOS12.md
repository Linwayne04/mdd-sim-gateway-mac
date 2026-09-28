# EM7345 在 macOS 12 (T450 Hackintosh) 適配可行性研究

> 日期:2026-09-28。結論:**可行,無需任何 kernel/kext 代碼**,走「用戶空間 MBIM + utun」路線。
> 當前決策:先購買 WWAN M.2→USB 適配板,在開發機(Intel Mac, macOS 15)上做 MBIM spike,把 hackintosh 變量留到最後一步。

## 1. 結論摘要

- macOS(含 Monterey)**沒有** EM7345 的任何官方或第三方可用驅動;但 EM7345 走標準 USB + MBIM/QMI,可在純用戶空間驅動
- 不需要 modem 成為系統網卡:解 NCM 幀後餵進已有的 `engine/utun_darwin.py` utun PoC + pf NAT 即可,與網關架構天然吻合
- T450 上卡是內建 FRU(如 04X6014),天線/SIM 槽/M.2 槽全部就位,零額外硬件;唯一 hackintosh 特有的是 **USB 端口映射**
- 明確排除 kext 路線([EigenTom EM74xx kext](https://github.com/EigenTom/Sierra-Wireless-EM74xx-Series-WWAN-Card-Driver-for-macOS-Catalina) 只支持 Qualcomm EM7455/EM7430,不匹配 Intel XMM7160;Monterey 裝未簽名 kext 還要降 SIP)

## 2. EM7345 硬事事實

| 項 | 值 |
|---|---|
| 芯片 | Intel XMM7160(**非 Qualcomm**,社區 QMI kext 不適用) |
| USB ID | VID `1199`(Sierra Wireless),PID 常見 `a001`,變體 `9063` |
| 接口形態 | M.2 但對外暴露 **USB 2.0** → M.2→USB 適配板可外接 |
| 默認 USB composition | #8 = DM + NMEA + AT + MBIM(AT 口現成) |
| 可切 composition | #6=加 QMI;#9=僅 MBIM(沒 AT 口,避免);切換用 `AT!UDUSBCOMP=8` 或 QMI 經 MBIM 隧道(`qmicli --device-open-mbim`),切完需復位 |

## 3. macOS 12 驅動現狀

| 方案 | 狀態 |
|---|---|
| Sierra/Lenovo 官方驅動 | 僅 Windows,無 macOS |
| macOS 內建 CDC MBIM 類驅動 | 不存在(`cdc_mbim` 是 Linux 專有;libmbim 無 macOS 移植) |
| AT 口(CDC-ACM, class 02/02/01) | **內建 AppleUSBCDCACM 自動綁定** → `/dev/cu.usbmodem*`,免費 |
| MBIM 口(control 02/0d/00 + data 02/00/07) | 無驅動無人搶佔 → pyusb/libusb 直接 claim |
| 社區 hackintosh 記錄 | X250 同平台 EFI 標「WWAN 有驅動未測試」——實際只是端口 map 後枚舉出串口,無數據通路 |

## 4. 技術路線(路線 A:用戶空間 MBIM)

1. **控制面**:pyserial 走 AT 口——註冊/信號/SIM/短信,與 Phase 1 已有方案零改動
2. **數據面**:Python 實現最小 MBIM 客戶端
   - control 通道:MBIM 消息封裝(UUID + NTL/TLV),`Open`/`Connect`/`DeviceServices`/`RadioState`
   - data 通道:NCM NDP 封裝/解封裝(NTB 參數按設備回應動態協商)
   - 參考實現:[arachsys/tinymbim](https://github.com/arachsys/tinymbim)(單文件,最適合移植)、[openwrt/umbim](https://github.com/openwrt/umbim)、[khvalera/mbim-network](https://github.com/khvalera/mbim-network)、[wwan-go/mbim](https://pkg.go.dev/github.com/voorz/wwan-go/mbim)
   - 工作量估計:400–600 行 Python
3. **網絡面**:IP 包 → `engine/utun_darwin.py` utun + pf NAT;不需要 kernel NIC / NEPacketTunnelProvider / 代碼簽名
4. **兼容性**:不依賴任何新 API,天然滿足 macOS 12 向下兼容目標;組件日後可回用 Linux 側

## 5. T450 Hackintosh 特定事項

- BIOS 啟用 Wireless WAN;EM7345 是 Lenovo FRU,白名單直接通過
- **USB 端口映射是 hackintosh 上唯一的「驅動」問題**:卡掛內部 USB 2.0 口,若 EFI 的 port map 沒包含該口,macOS 裡完全不枚舉(15 端口限制)。用 USBToolBox/USBMap 找出 HS 口並標 **internal (type 255)**,否則睡眠異常
- T450(Broadwell)+ OpenCore + Monterey 是成熟組合,平台本身無風險
- SIM:microSIM 槽在電池倉一側

## 6. 開發/驗證分工(不搬家)

| 工作 | 機器 |
|---|---|
| 構建、控制面、engine、utun 開發 | 現有 Intel Mac(macOS 15)環境現成 |
| macOS 12 向下兼容驗證 | T450(本來就需要真機:utun AF header 字節序按 OS 版本驗證、MacPorts 依賴為目標 OS 重建) |
| EM7345 spike | 開發機 + USB 適配板(見下) |
| E2E 集成 + 長期運行 | T450(部署二進制在 T450 的 MacPorts 環境重建) |

## 7. Spike 計畫(路徑 B:適配板先行)

1. 購買:**帶 SIM 卡槽引腳的 WWAN M.2→USB 適配板**(普通 SSD 適配板的 SIM 走線不通;3.3V 由適配板處理);EM7345 確認 unlocked/generic 版本
2. 開發機上 `system_profiler SPUSBDataType` 確認枚舉 `1199:a001`;`ls /dev/cu.usbmodem*` 確認 AT 口
3. pyserial:`AT+CPIN?` / `AT+CREG?` / `AT+CESQ` 驗證基礎通路
4. pyusb claim MBIM 接口,`MBIM_OPEN_MSG` → 拿到 `MBIM_DEVICE_CAPS` = 綠燈
5. `Connect` + NCM 收發 → utun → ping 通 = Phase 4 硬件鏈路就緒
6. 之後卡裝回 T450,只需補一步 USB mapping 驗證內建卡枚舉一致

風險點:① 品牌拆機卡可能帶鎖(EM7345 比 EM7455 輕,但買前確認);② AppleUSBCDCACM 佔 AT 口後需驗證不獨佔整個 configuration(理論上接口級匹配不會);③ MBIM 的 NTB/NDP 協商細節(tinymbim 已趟過)。

## 8. 參考來源

- [ThinkWiki — Sierra Wireless EM7345](https://www.thinkwiki.org/wiki/Sierra_Wireless_EM7345)
- [m2msupport — EM7345 module](https://m2msupport.net/m2msupport/sierra-wireless-em7345-4g-lte-m2-module/)
- [mavstuff/swi_setusbcomp — composition 切換](https://github.com/mavstuff/swi_setusbcomp)
- [0xf8.org — EM7305 USB composition (MBIM/QMI/AT/NMEA)](https://www.0xf8.org/2016/04/changing-dell-wireless-5809e-sierra-wireless-em7305-usb-composition-mbim-qmi-at-interface-nmea/)
- [Linux Mint Forums — EM7345 排障](https://forums.linuxmint.com/viewtopic.php?t=262003)
- [BakaMamizou — X250 Hackintosh EFI(EM7345「有驅動未測試」)](https://github.com/BakaMamizou/ThinkPad-X250-Hackintosh-EFI)
- [racka98 — T450/T450s OpenCore 指南](https://github.com/racka98/Lenovo-Thinkpad-T450-T450s-Hackintosh-Guide-Opencore)
- [InsanelyMac — USB 端口映射討論](https://www.insanelymac.com/forum/topic/352311-mapping-usb-ports-discussions/)
- [EliteMacx86 — USB 端口映射工具比較](https://elitemacx86.com/threads/how-to-map-your-usb-ports-on-macos.581/)
- [EigenTom — EM74xx kext(僅 Qualcomm,不適用)](https://github.com/EigenTom/Sierra-Wireless-EM74xx-Series-WWAN-Card-Driver-for-macOS-Catalina)
- [Linux kernel cdc_mbim 文檔](https://docs.kernel.org/networking/cdc_mbim.html)
- [Reddit r/hackintosh — EM7345 在 macOS 不工作](https://www.reddit.com/r/hackintosh/comments/1eu4z8d/sierra_wireless_em7345_i_know_it_doesnt_work_but/)
