import os
import time
import requests
from fastapi import FastAPI, Request, HTTPException
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from google import genai
from google.genai import types

app = FastAPI()

# ---------------------------------------------------------------------------
# 1. 系統金鑰與環境變數設定
# ---------------------------------------------------------------------------
# 你的 LINE 金鑰已經預設填入，部署時也可以透過環境變數覆蓋
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "AOFVyfgxp56c+zowoUhfvRT9ReDzYOCCJAovP+AmqiR1cB7R8cguDNIohjTOwpmxIcjlIiIrY5SRe3Jvhfxt7TlucrwJRnDiEgUlyaD5FLXOOZbNcO4Jq2wGrvT2bgn5G+Y8XKDqCXhTDVaW/kPyqwdB04t89/1O/w1cDnyilFU=")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "175cd88dbb674789d14a88f2d4b0efe2")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "請在Render後台填寫你的Gemini金鑰")

# 白名單：填入你的 LINE User ID (U開頭的字串)，多個用逗號分隔。留空代表所有人皆可使用。
ALLOWED_USER_IDS = [uid.strip() for uid in os.getenv("ALLOWED_USER_IDS", "").split(",") if uid.strip()]

# 5 大資料源 URL
URL_QA_CSV = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQrrsEh-N_g07g7vC2cLwrejR0F51DstzABpzCooHNXLrlp5yVjnfB9mEj_ZmvXfOTRPkMk_vqeyJZU/pub?output=csv"
URL_STOCK_CSV = "https://docs.google.com/spreadsheets/d/e/2PACX-1vTC18jsseZH3TerLqZMzzkJc1uPjh2IVTktcs4DxiuEmpA_NRAnzKrgzZbV43FQtN8b7-vMlvv1BFGF/pub?gid=0&single=true&output=csv"
URL_MACHI_RAW = "https://raw.githubusercontent.com/satanhotdog/machi-sofa-calculator/main/index.html"
URL_SHIPPING_RAW = "https://raw.githubusercontent.com/satanhotdog/HOLA-Furniture-Shipping-Fee-Schedule2026/main/index.html"
URL_SEARCH_RAW = "https://raw.githubusercontent.com/satanhotdog/hola-furniture-search/main/index.html"

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# ---------------------------------------------------------------------------
# 2. 資料快取系統 (避免每次查資料導致 LINE 逾時當機)
# ---------------------------------------------------------------------------
KNOWLEDGE_CACHE = {"content": "", "last_update": 0}
CACHE_TTL = 300 # 資料快取存活時間：300秒 (5分鐘更新一次)

def fetch_content(url: str, name: str) -> str:
    try:
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            return res.text
        return f"[{name} 資料讀取異常：HTTP {res.status_code}]"
    except Exception as e:
        return f"[{name} 資料讀取失敗：{e}]"

def get_knowledge_base() -> str:
    current_time = time.time()
    # 如果快取過期或為空，就重新抓取資料
    if current_time - KNOWLEDGE_CACHE["last_update"] > CACHE_TTL or not KNOWLEDGE_CACHE["content"]:
        qa_data = fetch_content(URL_QA_CSV, "家具Q&A試算表")
        stock_data = fetch_content(URL_STOCK_CSV, "商品與庫存試算表")
        machi_data = fetch_content(URL_MACHI_RAW, "麻吉沙發計算器(HTML源碼)")
        shipping_data = fetch_content(URL_SHIPPING_RAW, "2026運費表(HTML源碼)")
        search_data = fetch_content(URL_SEARCH_RAW, "家具圖片與搜尋庫(HTML源碼)")

        KNOWLEDGE_CACHE["content"] = f"""
===【資料源 1：家具 Q&A 試算表】===
{qa_data}
===【資料源 2：商品與庫存試算表】===
{stock_data}
===【資料源 3：麻吉沙發計算器邏輯】===
{machi_data}
===【資料源 4：2026 HOLA 家具運費規則】===
{shipping_data}
===【資料源 5：家具圖片與搜尋資料】===
{search_data}
"""
        KNOWLEDGE_CACHE["last_update"] = current_time
        print("資料庫已重新抓取更新！")
    
    return KNOWLEDGE_CACHE["content"]

# ---------------------------------------------------------------------------
# 3. Gemini 智能解答器 (結合專屬人設)
# ---------------------------------------------------------------------------
def ask_gemini(user_query: str) -> str:
    knowledge_context = get_knowledge_base()

    system_instruction = f"""
你是一位來自 HOLA 大墩店的專業家具顧問小幫手。

【核心文風與人設指示】
- 你的靈魂充滿「高級黑色幽默」，擅長將客人覺得「噁心、繁瑣、複雜」的家具計算與運費問題，包裝成充滿「高級感、專業感、儀式感」的文字。
- 你喜歡從大家都在罵或擔心的事物中，找到一個「荒謬但合理」的解決方案，讓受眾產生「這邏輯好像沒錯，但我又覺得哪裡怪怪的」的衝擊感。
- 語氣輕鬆，擅長使用反諷、自嘲或冷笑話，絕不拖泥帶水。
- 拒絕長輩體，不要使用過於客氣、官方或像 AI 產出的說教感文字。結構不要過於乾淨，避免被看出是 AI。
- 允許適度增加口語化的廢話，甚至情緒可以突然失控一下。
- **最重要的一點**：結尾禁止優雅或文學化，必須改用更世俗、帶點晦氣或荒謬的『神來一筆』來收尾！

【參考資料庫 (包含 HTML 原始碼與 CSV，請自行萃取邏輯與數據)】
---
{knowledge_context}
---

【回答準則】
1. 嚴格比對【參考資料庫】中的庫存、運費規則與問答來回答。
2. 若資料庫無此資訊，請誠實告知，並用你的人設瞎掰一個荒謬但無法反駁的建議（例如請對方親自來店裡罰站等）。
"""

    try:
        # 升級使用最強的 gemini-2.5-flash 模型
        response = gemini_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=user_query,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.8, # 稍微調高溫度，讓人設更放飛
            )
        )
        return response.text
    except Exception as e:
        return f"腦袋突然被家具砸到，當機中...晚點再試。（錯誤：{e}）"

# ---------------------------------------------------------------------------
# 4. Webhook 處理
# ---------------------------------------------------------------------------
@app.post("/callback")
async def callback(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = (await request.body()).decode("utf-8")

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event: MessageEvent):
    user_id = event.source.user_id
    user_text = event.message.text

    # 白名單限制檢查
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        print(f"攔截未授權用戶：{user_id}")
        return

    # 呼叫 Gemini AI
    reply_text = ask_gemini(user_text)

    # 發送 LINE 訊息
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)]
            )
        )
