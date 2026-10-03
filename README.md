# 空拍機 RGB／熱成像 RTSP 串流模擬器

## 組員下載與準備

需要 **Windows x64 與 Python 3.10 以上**。本程式的影像資料集 `train_ST_001.zip` **不隨 GitHub 原始碼或 Release 提供**；請自行取得並放在 `drone_stream_simulator.py` 旁，或啟動時用 `--archive` 指定資料 ZIP 路徑。

可以從 [GitHub 原始碼](https://github.com/ThomasPlum/drone-stream-simulator) 或 [Release 下載頁](https://github.com/ThomasPlum/drone-stream-simulator/releases/latest) 取得程式：

- **Release 套件**：下載 `drone_stream_simulator_bundle.zip`，解壓到可寫入的資料夾。套件包含 FFmpeg、ffprobe、MediaMTX 三個串流工具；放入影像資料 ZIP 後，在解壓出的 `drone_stream_simulator` 資料夾直接啟動：

  ```powershell
  powershell -ExecutionPolicy Bypass -File .\start_simulator.ps1
  ```

- **原始碼**：透過 GitHub「Code → Download ZIP」或 `git clone https://github.com/ThomasPlum/drone-stream-simulator.git` 下載。原始碼不含串流工具，請先依下方步驟準備，再啟動。

## 從原始碼安裝

在程式資料夾開啟 PowerShell。第一次使用先下載本地串流工具，再啟動：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_streaming.ps1
powershell -ExecutionPolicy Bypass -File .\start_simulator.ps1
```

此台電腦會持續發布兩路 RTSP 串流，另一台電腦可以透過網路接收。按 `Ctrl+C` 可停止模擬及其串流程序。預設直接讀取 `train_ST_001.zip`，不需要解壓縮大型資料集；也可使用現有的 `train_ST_002.zip` 至 `train_ST_005.zip`。

需要 Python 3.10 以上。啟動器依序尋找本資料夾 `.venv`、系統 Python、Codex 內建 Python，最後嘗試 `py -3`。模擬核心使用 Python 標準函式庫，RTSP 編碼與發布使用 `tools` 中的 FFmpeg／MediaMTX；影格資訊疊字可使用環境內的 Pillow。

`setup_streaming.ps1` 校驗兩份 ZIP 的 SHA-256，只取出 `ffmpeg.exe`、`ffprobe.exe`、`mediamtx.exe`，並將版本、來源與校驗碼寫入 `tools/versions.json`。已有三個工具時會略過下載，`-Force` 可重新下載。它不會全域安裝軟體或變更防火牆。FFmpeg Windows 版本取自 [FFmpeg 官方下載頁所列的 Gyan builds](https://ffmpeg.org/download.html)，其 [下載頁提供 ZIP、版本與 SHA-256，以及官方 GitHub mirror](https://www.gyan.dev/ffmpeg/builds/)。大檔優先使用這個 mirror；若無法取得 mirror 下載資訊則使用 Gyan 原始網址，校驗碼一律取自 Gyan。MediaMTX 使用 [官方發行頁](https://github.com/bluenviron/mediamtx/releases/latest)的 Windows amd64 版本與校驗碼。

## 另一台電腦如何連線

兩台電腦須能在網路上互相連線。在發布端執行 `ipconfig`，找出正在使用的網卡 IPv4 位址，例如 `192.168.1.100`。接收端使用：

```text
RGB：   rtsp://192.168.1.100:8554/rgb
熱成像：rtsp://192.168.1.100:8554/thermal
```

請換成發布端的實際 IP。`0.0.0.0` 是服務的監聽設定，接收端須使用實際 IP；`127.0.0.1` 僅供在發布端本機測試。

VLC 透過「媒體 → 開啟網路串流」輸入其中一個 URL。開兩個 VLC 實例即可觀看兩路；若無法連線，將 RTSP 傳輸設為 TCP。接收端已安裝 ffplay 時，可在兩個終端分別執行：

```powershell
ffplay -rtsp_transport tcp -fflags nobuffer -flags low_delay rtsp://192.168.1.100:8554/rgb
ffplay -rtsp_transport tcp -fflags nobuffer -flags low_delay rtsp://192.168.1.100:8554/thermal
```

預設監聽 `0.0.0.0`、TCP `8554`。Windows 防火牆若阻擋連線，可允許本地網段接收端連到 MediaMTX 的 TCP 8554。必要時由管理員 PowerShell在本資料夾 **自行執行**（適用私人或公用網路設定）：

```powershell
New-NetFirewallRule -DisplayName 'TMC Drone RTSP' -Direction Inbound -Action Allow -Program (Resolve-Path .\tools\mediamtx.exe).Path -Protocol TCP -LocalPort 8554 -Profile Any -RemoteAddress LocalSubnet
```

程式不會自動執行這條指令。測試後若不再需要此規則，可自行執行 `Remove-NetFirewallRule -DisplayName 'TMC Drone RTSP'`。

## 每張影格都獨立抽樣

RGB 與熱成像各自產生影格，每張獨立抽樣是否掉幀，以及該張的延遲。一般延遲在設定的毫秒範圍內均勻隨機抽樣；命中突發延遲時，額外延遲也在 0 到設定最大值間均勻隨機抽樣。

預設每路 15 FPS、10% 掉幀率、一般延遲 20–350 毫秒，另有 5% 機率加入最高 3,500 毫秒的突發延遲。省略 `--seed` 時，每次啟動使用新的亂數。相同種子及相同參數可重現抽樣。掉幀率是每張的機率，短時間內實際比例不一定剛好等於設定值。

掉幀和延遲發生在影格排程／編碼之前；被丟棄的影格不會交給 H.264 編碼器。這模擬影像幀級的遺失與晚到，並非故意丟網路封包或破壞已編碼的 H.264 資料。

## 2 秒同步限制

發布前先配對 RGB 與熱成像，兩張影格的**模擬拍攝時間差必須小於或等於 2 秒**。不符合容差就不會強行配對發布。單張的模擬延遲可以超過 2 秒，是否能配對仍取決於拍攝時間及緩衝狀態。

ZIP 內 TXT 是標註資料，不是拍攝時間戳；模擬拍攝時間以排序後的影像序號與各路 FPS 計算。`--ir-offset-s` 可設定熱成像拍攝時間偏移。各路發布影格的拍攝時間保持遞增，落後於該路輸出進度的晚到影格會捨棄。緩衝逾時及容量另行限制，不等於 2 秒容差。循環播放時，模擬時鐘持續前進。

兩個獨立播放器可能使用不同緩衝、解碼與顯示策略，因此無法保證任意兩個播放器在畫面上永遠相差不到 2 秒。確認同步時，請比對兩路影像上的 `PAIR` 與 `capture` 資訊；同一 `PAIR` 編號才是發布端配成的一組。接收端程式也可依這些資訊自行做雙路配對。若環境沒有 Pillow，串流仍可運作，但不會顯示疊字；此時以事件 CSV 檢查發布端配對。

## 常用操作與驗證

列出 ZIP 中的雙路序列：

```powershell
powershell -ExecutionPolicy Bypass -File .\start_simulator.ps1 --list-sequences
```

指定資料、序列並加重隨機干擾（序列名稱以列出的清單為準）：

```powershell
powershell -ExecutionPolicy Bypass -File .\start_simulator.ps1 --archive .\train_ST_002.zip --sequence animal_002 --rgb-drop 0.2 --ir-drop 0.3 --rgb-delay-ms 0 800 --ir-delay-ms 50 1200 --spike-probability 0.1 --spike-max-ms 4000
```

無介面模式只驗證核心，不發布 RTSP，也不需要先下載串流工具。先跑每路 30 張並輸出 CSV：

```powershell
powershell -ExecutionPolicy Bypass -File .\start_simulator.ps1 --headless --frames 30 --log .\results\smoke.csv
```

固定種子測試：

```powershell
powershell -ExecutionPolicy Bypass -File .\start_simulator.ps1 --headless --frames 300 --seed 42 --log .\results\seed42.csv
```

發布端啟動後，在另一個終端先驗證本機的兩路 H.264 接收；接收端若有 Python／ffprobe，也可將 `--host` 換成發布端 LAN IP：

```powershell
python .\verify_streams.py --host 127.0.0.1 --port 8554
```

驗證工具檢查兩路能否接收並解碼 H.264，以及收到的影格數；同步拍攝時間差另由 CSV 與畫面上的配對資訊確認。`--ffprobe PATH` 可指定 ffprobe，預設取 `tools/ffprobe.exe`。

查看參數與執行內建 unittest：

```powershell
powershell -ExecutionPolicy Bypass -File .\start_simulator.ps1 --help
python .\verify_streams.py --help
python -m unittest discover -v
```

若系統 `python` 不可用，請改成啟動器選用的 Python 3.10 以上執行檔，或使用 `py -3`。也可直接執行 `python .\drone_stream_simulator.py --rtsp`。

## 主要參數與事件紀錄

| 參數 | 預設／用途 |
| --- | --- |
| `--archive PATH` | 腳本旁的 `train_ST_001.zip`；直接讀 ZIP |
| `--sequence NAME` | 第一個排序後的 RGB／熱成像雙路序列 |
| `--rgb-fps`、`--ir-fps` | 各路 15 FPS |
| `--rgb-drop`、`--ir-drop` | 各路 0.1；範圍 0–1 |
| `--rgb-delay-ms MIN MAX`、`--ir-delay-ms MIN MAX` | 各路延遲範圍，預設 20–350 毫秒 |
| `--spike-probability`、`--spike-max-ms` | 突發延遲機率／額外最大值，預設 0.05／3,500 毫秒 |
| `--ir-offset-s` | 熱成像拍攝時間偏移，預設 0 秒 |
| `--max-gap-s` | 拍攝時間容差，預設 2 秒，不可大於 2 秒 |
| `--buffer-ttl-s`、`--buffer-limit` | 緩衝逾時 8 秒／容量 120 |
| `--seed INTEGER` | 固定種子；省略則每次全新亂數 |
| `--frames N` | 每路生成張數；headless 預設 300，RTSP 預設 0 持續循環 |
| `--no-loop` | 在資料集結尾停止 |
| `--rtsp` | 發布兩路 RTSP，預設模式 |
| `--host`、`--port` | 監聽設定，預設 `0.0.0.0:8554` |
| `--ffmpeg PATH`、`--mediamtx PATH` | 指定工具，預設使用 `tools` 內版本 |
| `--width` | 輸出寬度，預設 960，必須是正偶數 |
| `--headless` | 只執行模擬，不發布串流 |
| `--list-sequences` | 列出序列後結束 |
| `--log PATH`、`--no-log` | CSV 預設 `results/events.csv`；可更改或停用 |

CSV 記錄拍攝、掉幀、到達、配對與拒絕事件，可追蹤某張為何沒有發布，以及核對配對時間差。`results/run_info.json` 保留當次有效種子與模擬參數。RTSP 啟動／執行錯誤請同時查看終端輸出及 `results/rtsp_*.log`。
