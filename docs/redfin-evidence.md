# Redfin 與 RentCast 的來源比對

報告優先採用可核對的 Redfin MLS 資料，RentCast 補充缺值並保留差異。這是欄位與事件層級的政策，不是「Redfin 每個值永遠正確」。Redfin 的 MLS 掛牌、公共紀錄、估值及搜尋摘要必須分開。

## 房型與資格

只呈現與追蹤房屋相符的 SFH 或 Townhouse。任一來源明確標示 Condo、已知 condominium ownership，或 Condo／Townhouse 衝突未解決時，排除主要筆數、逐筆參考及統計。Townhome 建築形式不能當成 fee-simple 產權證明。不另設 Condo 參考區。一般來源標為 Townhouse 且沒有已知衝突的候選仍可呈現，但產權與社區可比性未查核。

舊房的範例條件為最終 3–4 房、2–3.5 衛、1700–3000 sqft、1.5 英里。掛牌發現放寬為 2–4 房，讓屬性修正有機會在最終篩選前套用。房屋紀錄先保留地理／日期廣搜，避免 Condo 分類或舊尺寸造成漏查。這些是可調整的相關性條件，不是經統計驗證的市場門檻。

搜尋每類最多三頁、每頁 500 筆。讀到短頁才確認讀完；第三頁仍滿頁時明確標示不完整。官方文件說掛牌依 lastSeenDate 由新至舊排序，並非依可比性排序。分頁讀完也只涵蓋該查詢，不能保證沒有其他房源。

## 日期與事件

同地址、戶別及相容成交事件的價格衝突，優先採 Redfin MLS。較舊 Redfin 事件不能蓋掉較新 RentCast 事件。無法核對時保留兩邊主張，將受影響的成交／目前掛牌指標標為待核對，不把較新的 RentCast 單方紀錄當成已確認成交，也不把舊 Redfin 事件描述為目前狀態。

事件日期、掛牌觀察日、來源欄位日期與本次查核日是不同日期。今天查到舊資料不會讓它變成今天的市場資料。Pending 不算成交，估值不算成交價。新 MLS 掛牌週期不能沿用舊週期開價或 DOM。更換成交或掛牌資料時清除其他來源的開價配對，證據不足就不計算落差。

Redfin 的 Time on Redfin 保留原名稱與觀察日期，不能直接轉成 RentCast DOM。管理費只有明確月繳時才填入月管理費；年繳、季繳或週期未知的值保留原單位，不假設為月繳。

## 本機保存與更新限制

`tracker.redfin_reference_path` 預設 `data/references/redfin.json`。索引保存已核對的完整地址、戶號、ZIP、實際 canonical 房源網址、查核日期、來源欄位群組、簡短事實、原資料與可用值。不存在索引時正常產生報告；存在但格式錯誤、戶號矛盾或重複對應時回報設定錯誤，不能忽略後寄信。

每次執行在 `tracker.evidence_root`、預設 `data/evidence` 保存時間戳記快照，包含 RentCast 原始 payload、雙來源原值、採用來源、有效值、差異與被排除的原因。包含最終未入選的候選，方便日後回放及比較 API。這是第三方主張的驗證候選資料集，不是獨立認證的 ground truth。所有真實資料都在 Git 忽略的私有目錄。

排程會自動重用保存的 Redfin 索引，但尚未自動查找新地址或每週刷新 Redfin 頁面。本次地址由 agent 查核，不需要使用者逐筆輸入。報告摘要顯示匹配筆數／所有取得的獨立候選，以及其中採用 Redfin 欄位的候選筆數。沒有匹配也顯示零。查核日期不承諾即時更新。沒有核對的地址保留明確標示的搜尋備援。

本版沒有新增付費搜尋服務、訂閱、模型、無人值守 Redfin 爬蟲或 CAPTCHA 繞過。直接連結與來源資料保存，不代表已建立穩定的長期 Redfin 資料服務。

## 來源依據

- [Redfin 資料品質](https://www.redfin.com/about/data-quality-on-redfin)：MLS 直接來源及收到資料後的刊登速度，不是獨立準確率證明。
- [Redfin 公共紀錄](https://support.redfin.com/hc/en-us/articles/360001454531-Public-Records)：MLS 與縣政府資料的差異與配對限制。
- [Time on Redfin](https://support.redfin.com/hc/en-us/articles/360024234952-Time-on-Redfin)：依 MLS 號碼計算的廣告天數。
- [RentCast 掛牌](https://developers.rentcast.io/reference/property-listings)：公開來源、每日更新與新掛牌延遲。
- [RentCast 房屋紀錄](https://developers.rentcast.io/reference/property-data)：縣政府紀錄可能延遲數週至數月。
- [RentCast Sale Listings](https://developers.rentcast.io/reference/sale-listings)：排序與 limit／offset 分頁。
