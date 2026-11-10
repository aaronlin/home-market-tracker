# 房市資料 API 比較

公開資料查核日期：2026-11-09（美國太平洋時間）。價格均為美元；不含未確認的稅額、MLS 費用與合約條件。沒有購買、註冊或呼叫付費資料 API，也沒有證明任何替代來源在追蹤區域比 RentCast 更完整。

## 公開價格與取捨

| 來源 | 價格與額度 | 優點 | 限制與自動化負擔 |
| --- | --- | --- | --- |
| RentCast | Developer $0／月含 50 次，超額 $0.20／次；Foundation $74／月含 1,000 次，超額 $0.06；Growth $199／月含 5,000 次，超額 $0.03；Scale $449／月含 25,000 次，超額 $0.015。[官方價格](https://www.rentcast.io/api) | 現有程式已整合，包含房屋紀錄、AVM、掛牌與區域統計，維護負擔最低。 | 掛牌並非直接 MLS，僅 Active／Inactive 不能分辨成交、撤回或到期。業者宣稱每日更新，新掛牌通常 12–24 小時上線；實際當地完整率仍未知。[掛牌文件](https://developers.rentcast.io/reference/property-listings) |
| HouseCanary Pro | $79／月，或 $790／年（年付平均約 $65.83／月）。另收 API 費用：Pro Basic $0.40、Premium $3、Premium Plus $5／成功呼叫。$19 Basic 方案不能視為已包含適用 API 權限。[官方價格](https://www.housecanary.com/pricing) | 可自動補充估值、特徵、交易歷史與預測。 | Data Explorer 不是即時 MLS 掛牌 feed，不含即時掛牌狀態及 DOM；AVM 每月更新。交易端點的適用級別與當地資料品質須確認，不能單獨取代掛牌來源。[開發指南](https://www.housecanary.com/blog/real-estate-data-api-for-developers) |
| ATTOM | Basic Property API $500／月起；Premium 詢價。公開頁未列足以計算完整用途的額度與超額費用。[官方價格](https://www.attomdata.com/solutions/delivery/property-data-api/pricing/) | 結構化房屋與成交歷史，API 可自動排程。 | 實際端點、進階成交比較與覆蓋依授權而異。不能把 $500 Basic 視為包含所有需求，也未證實其當地更新較快。對兩間房的固定成本偏高。 |
| MLSListings 授權 feed＋SimplyRETS Premium | SimplyRETS Premium $99／月，加每個 feed 一次性 $99 設定費；MLS 本身的授權費另待確認。Basic $49／月不含 Closed，不適合直接當成交資料解法。[SimplyRETS 價格](https://simplyrets.com/) | 若取得合適授權，較有機會提供 active、pending、closed 等完整掛牌生命週期；Premium 宣稱約 30 分鐘刷新，受 feed 限制。 | 必須先取得合法 RETS／RESO 憑證。買 API 介接服務不等於取得 MLS 資料或私人分析／寄信用途授權。前期資格確認與整合負擔最大，建立後可自動化。[SimplyRETS FAQ](https://simplyrets.com/faq)、[MLSListings 存取說明](https://mlsl.my.site.com/community/s/article/IDX-FAQ) |

MLSListings 公開流程以參與經紀商、資料授權與核准供應商為核心。一般屋主不能假設有自行申請完整 feed 的資格。SimplyRETS 能接標準 feed，不代表已證實此用途的 MLSListings 接線或使用權。Bridge／Trestle 也只是授權資料的傳輸途徑，並不繞過資格限制。

## 兩間房、每週一次的費用示例

這些是條件式估算，不是目前帳單，也不是資料品質等價的方案報價。

- RentCast：若每間房每輪剛好各呼叫三個端點，四至五週月份為 24–30 次。分頁會增加請求，例如一間房需要兩頁 listing 時，兩屋一輪合計七次，四至五週為 28–35 次。這些簡化情境低於免費 50 次，但預覽、重跑及其他用量仍須計入。實際訂閱與帳單未檢查。
- HouseCanary：若每屋每週恰好成功呼叫一次 $3 的 Premium 端點，月付 Pro 下四至五週為 $103–109。這不代表該端點足以完成週報，也不含另一個掛牌來源。
- SimplyRETS Premium：單一 feed 的服務費首月 $198，之後 $99／月，另加未知的 MLS 費用。取得資料權限之前，不可視為可直接購買使用的完整方案。
- ATTOM：只能可靠列出 $500／月起，完整需求總價仍待確認。

## 建議

先保留 RentCast，修正成交價／開價／日期的解讀，讓資料缺口在報表上清楚呈現。接著用獨立增量測試更合理的查詢條件，再決定是否需要新來源。

若仍有影響判斷的重要成交或掛牌狀態缺口，優先確認授權 MLS feed 的資格、用途與總成本；無法取得時，再評估 HouseCanary 等自動補充來源。ATTOM 需有足夠額外效益才能合理化固定費用。不要以更高筆數、兩家資料一致或四週時間經過，就認定品質更好。

試用前一次確認預算、權限與比較標準。比較可用成交日期／價格、相關待售物件、單位配對錯誤、觀察延遲及維護成本。沒有獨立參考資料時，完整率與真實供應延遲維持未知。正常流程不需要屋主持續填寫可比屋或人工核對表。
