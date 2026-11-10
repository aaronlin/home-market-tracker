# Home Market Tracker

以 API 資料產生設定房屋的每週市場證據報告。郵件分開呈現來源記錄的成交與近期觀察到的待售開價，用於追蹤市場，不是房屋估值或房價增值估算。

每次執行會使用三類 RentCast 資料來源。待售／歷史掛牌分開搜尋，加上分頁後，每間房可能產生超過三次計費請求：

- `/avm/value`：估值背景與可比候選。
- `/listings/sale`：狀態、開價、上市日期、下架日期、最後觀察日期與 DOM。
- `/properties`：可取得的房屋紀錄、成交歷史與 HOA 欄位。

## 報告內容

每間房先顯示摘要，再列逐筆資料，以證據協助買賣判斷，不自動提供買賣建議或追蹤房屋的估值。相符的 SFH 或 Townhouse 成交與近期開價分開呈現。已知 Condo 與未釐清的產權／房型衝突排除。在相容事件中優先採已辨認的 Redfin MLS 資料，兩邊原始來源均保留。

成交價必須搭配房屋紀錄或 Redfin MLS 歷史支持的交易日期。每個房型的成交先各自查近 90 天，少於三筆時擴大至設定的回溯期間，最長 365 天。報告標示窗口與筆數，每類最多顯示最近三筆，不混算成交價／開價中位數，也不提出未證實的增值或新增庫存結論。舊版成功執行狀態維持相容，不作為新指標的依據。

待售資料必須具正數開價，且來源在七天內仍有待售觀察。速覽依房型與來源定義顯示待售 DOM 中位數及納入筆數；合法的零會保留，不把它當成成交速度。開價參考落差須符合房源身分、日期、明確 MLS 週期及成交前 90 天等條件。後續改價金額未知時，仍採最後已知售前價格並標明非最終開價，不稱為議價折扣。同 MLS 日期可核對時，另外顯示上市至首次 Pending、上市至成交的日曆天數，與來源 DOM 分開。

房屋紀錄依地理與交易日期範圍搜尋，不把追蹤房屋的房數、衛浴、年份或土地當成精確條件。明確設定的面積／土地範圍與最終篩選條件仍在本機套用。掛牌依追蹤房型分別查詢 Active 與 Inactive，可另設房數／衛浴發現範圍。每類搜尋預設最多三頁；未讀完或缺少取得紀錄時會明示。請求失敗仍會中止，不能假裝沒有房源。

已核對的地址優先連至精準 Redfin 房源頁；否則提供明確標示的 Google Redfin 站內搜尋。產生郵件時不發送外部請求。目前未設定自動 Redfin 資料擷取。詳見[市場指標定義與搜尋診斷](docs/market-evidence.md)、[API 價格與取捨](docs/api-comparison.md)、[產品說明](PRODUCT.md)及[郵件設計](DESIGN.md)。

## 設定

安裝依賴：

```bash
python3 -m pip install -r requirements.txt
```

建立本機環境設定：

```bash
cp .env.example .env.local
```

建立私有追蹤設定：

```bash
cp config/settings.example.yaml config/settings.yaml
```

在 `settings.yaml` 設定房屋、地址、購入參考、可比條件、註記及 HOA 提示。此檔案由 Git 忽略，避免真實地址進入公開儲存庫。也可用 `HOME_MARKET_SETTINGS_PATH` 指向其他設定檔。

必要環境變數：

```text
HOME_MARKET_RENTCAST_API_KEY
HOME_MARKET_EMAIL_TO
HOME_MARKET_EMAIL_FROM
HOME_MARKET_SMTP_HOST
HOME_MARKET_SMTP_PORT
HOME_MARKET_SMTP_USER
HOME_MARKET_SMTP_PASSWORD
```

## 執行指令

預覽，不寄信也不更新狀態：

```bash
python3 src/run_weekly.py --preview
```

寄送週報，僅在郵件寄送成功後更新狀態：

```bash
python3 src/run_weekly.py --send
```

## 資料邊界

公開範例設定在 `config/settings.example.yaml`。

私有設定留在本機，並由 Git 忽略：

```text
config/settings.yaml
```

執行資料也留在本機，並由 Git 忽略：

```text
data/
```

包括供應商原始快照、狀態、預覽產物與日誌。

## 排程

隨附的 LaunchAgent 在每月 1、8、15、22 日，本機時間上午 8:00 執行，每月固定四個排程時點。現有 `weekly` 標籤與檔名保留，以便直接取代已安裝的工作：

```text
bin/send-weekly
launchd/example.weekly.plist
```

完成設定後，用已安裝專案依賴的 Python 環境安裝 LaunchAgent：

```bash
python3 bin/install-launchagent
```

若使用儲存庫的 `.venv`，改執行 `.venv/bin/python bin/install-launchagent`。

安裝程式會偵測儲存庫路徑與 Python 目錄，並建立日誌目錄。固定標籤為 `com.home-market-tracker.weekly`，不需要個人姓名。產生的 plist 位於儲存庫外的 `~/Library/LaunchAgents`。搬移儲存庫或更換 Python 環境後，重新執行安裝程式。安裝會取代同標籤的現有工作，不會寄信。

四個排程時點不等於 API 用量硬上限。依目前兩間房的實測，每輪房源查詢 11 次，加上兩個 ZIP 每月各一次歷史查詢，正常月份約 46 次。額外分頁、手動預覽、重跑、失敗請求與其他共用帳戶的程式仍可能增加用量。正式剩餘額度與計費週期以 RentCast 儀表板為準；安裝排程不會清除已用額度，也不補跑過去日期。

立即執行寄送：

```bash
launchctl kickstart gui/$(id -u)/com.home-market-tracker.weekly
```

## 公開發布檢查

將專案移至公開儲存庫前：

- 不複製被忽略的 `data/`，其中含原始 API 快照、預覽、狀態與日誌。
- 不提交 `.env.local` 或 `settings.yaml`。
- 掃描已追蹤檔案是否含私人資料：

```bash
git ls-files | xargs rg -n "real-street-name|real-city|local-username|absolute-home-path"
```

- 如果私人資料曾進入 Git 歷史，公開前先清理歷史。

## 測試

```bash
python3 -m unittest discover tests
```

## 來源比對

可選的私有 `tracker.redfin_reference_path` 預設為 `data/references/redfin.json`。由 agent 查核的參考提供直接房源連結與已辨認的 MLS 資料。排程重用保存參考，不會自動發現新 Redfin 頁面或刷新既有頁面。沒有參考時保留明確標示的搜尋備援。

Redfin MLS 資料在相容事件中優先採用。尚未核對的新舊交易或狀態主張均保留，受影響指標暫不使用。已知 Condo 與未釐清的 Condo／Townhouse 衝突排除，預設只呈現主屋房型；可設定 `comparable_property_types: [Townhouse, Single Family]` 同時搜尋並分組呈現兩種住宅，保留主屋實際房型，且不混算中位數。掛牌發現可使用 `listing_bedrooms` 與 `listing_bathrooms`，和最終 `ranked_comp_*` 條件分開；房屋紀錄保留廣搜，以取得可能需要修正屬性的候選。

每次執行將比較證據寫入 `tracker.evidence_root`，預設為 `data/evidence`。包含原始 RentCast payload、正規化原值與 Redfin 主張、欄位來源、有效值、差異及最終篩選前的排除原因。預覽可寫入這些診斷產物，但不寄信，也不更新成功執行狀態。第三方主張不是獨立確認的真值。

來源與資料格式規則詳見[來源政策](docs/redfin-evidence.md)。

## 區域掛牌歷史與固定條件基線

每間房的速覽現在分開提供附近固定條件觀察，以及同 ZIP、同房型的區域掛牌背景。地域與房型不混算；區域資料不是主屋估值。

- 以既有 RentCast `/markets` 回補月度歷史，預設 36 個月。同一 ZIP 的成功資料每月快取重用，不新增每日排程或模型呼叫。
- 比較最近已結束月份與去年同月，分開列來源 DOM、當月新增掛牌／曾待售數與開價中位數。來源缺月不補零，不拿全房型或房數分組代替指定房型。
- 開價中位數不是成交價；當月曾待售數不是月底庫存，也不是 DOM 有效分母。供應商沒有提供指標有效分母時會明示未知。
- 每次完整擷取保存固定地域、房型、尺寸等條件版本與全部候選，建立往後可比較的基線。首次觀察不等於新上市，房源消失不等於成交；條件或來源屬性修正時重建基線。
- `--preview` 將新快取與基線寫到預覽目錄的 `research`，不改正式歷史或寄送狀態。`--send` 可保存已完成擷取的歷史，即使之後 SMTP 失敗；成功寄送狀態仍只在郵件成功後更新。
- 區域取得失敗、資料過舊或歷史讀寫失敗會顯示限制，既有逐筆報告仍可運作。缺少可比基期不會產生房市回升、價格預測或買賣時點建議。

`tracker.market_history` 可設定 `enabled`、`history_months`、`max_age_days`、`cache_root` 與 `capture_root`。預設啟用，來源／擷取最長資料年齡為 45 天；關閉時不呼叫區域端點。ZIP 自動取自追蹤主屋的 AVM，其次為地址末尾郵遞區號；只有來源缺漏時，才需要選填每屋 `market_zip_code`，不要求日常人工輸入。

正式月度來源快取預設位於原始快照目錄同層的 `cache/markets`，固定條件歷史位於 `history/cohorts`。這些與實際地址、來源原文、預覽一樣都屬私有資料，不應提交。來源取捨與後續每日模式見 [方案](docs/market-heat-plan.md)。
