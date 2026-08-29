import base64
from datetime import datetime
import io
import json
import os
from PIL import Image, ImageOps
import pillow_heif
import pytesseract
import re
import sqlite3
import streamlit as st
import tempfile
import time

# Supabase SDK
try:
    from supabase import create_client, Client
except ImportError:
    create_client = None
    Client = None

# AI SDK
try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

# グラフ描画
try:
    import pandas as pd
    import plotly.express as px
    import plotly.graph_objects as go
except ImportError:
    pd = None
    px = None
    go = None

pillow_heif.register_heif_opener()

# ==========================================
# 0. 定数・カテゴリー定義
# ==========================================
CATEGORIES = [
    "食費 (食材・自炊)",
    "外食・カフェ",
    "日用品・消耗品",
    "住居・家具・家電",
    "通信・サブスク",
    "交通費・ガソリン",
    "娯楽・趣味・書籍",
    "衣服・美容",
    "医療・健康",
    "その他"
]

JSON_PROMPT = f"""
Analyze this Japanese receipt or invoice image/PDF and extract structured data in JSON format only.
Do not wrap the output in markdown codeblocks (e.g. ```json).

Output JSON schema:
{{
    "store_name": "店舗名またはECサイト名 (例: Amazon.co.jp, TRIAL, セブン-イレブン など)",
    "date": "YYYY/MM/DD",
    "total_amount": 0 (最終支払合計金額・税込・整数),
    "discount": 0 (値引き・割引合計額、なければ0・整数),
    "points_used": 0 (利用ポイント数、なければ0・整数),
    "category": "以下の候補から最も適切なものを1つ選択",
    "tax_info": {{
        "tax_type": "外税" | "内税",
        "tax_8_amount": 0,
        "tax_8_tax": 0,
        "tax_10_amount": 0,
        "tax_10_tax": 0
    }},
    "items": [
        {{"name": "商品名", "price": 0}}
    ]
}}

Category Selection Guidelines:
Choose EXACTLY ONE from this list:
{json.dumps(CATEGORIES, ensure_ascii=False)}

Rules for Category:
- "食費 (食材・自炊)": Supermarket groceries, food items, drinks, meat, produce.
- "外食・カフェ": Restaurants, cafes, fast food, convenience store snacks/prepared food consumed immediately.
- "日用品・消耗品": Pharmacy supplies, detergents, shampoos, paper goods, pet supplies, 100-yen shop goods.
- "住居・家具・家電": Electronics, PC parts, home accessories, furniture.
- "通信・サブスク": Mobile bills, cloud subscriptions, software fees.
- "交通費・ガソリン": Train, bus, gas stations, parking.
- "娯楽・趣味・書籍": Games, books, leisure, hobby supplies.
- "衣服・美容": Apparel, haircuts, beauty services.
- "医療・健康": Clinic fees, prescription medicines, supplements.
- "その他": Anything not matching above.

Important Extraction Rules:
1. Item Prices: If both (税抜/excl. tax) and (税込/incl. tax) prices are listed per item (e.g. Amazon invoices), ALWAYS extract the 税込 (inclusive of tax) price so that the sum of items aligns with the total amount.
2. Tax Info: If the total amount already includes tax, set "tax_type" to "内税".
3. Do not include summary/tax rows inside items list.
"""

def infer_category_rule(store_name, items, default_category="その他"):
    """商品明細と店舗名からカテゴリーを高精度に推論・補正"""
    item_names = []
    for it in items:
        if isinstance(it, dict):
            item_names.append(it.get("name", ""))
        elif isinstance(it, (list, tuple)) and len(it) > 0:
            item_names.append(str(it[0]))
            
    items_text = " ".join(item_names).lower()
    store_text = str(store_name).lower()

    # ステップ1: 明細キーワードを最優先
    daily_keywords = [
        "洗濯", "洗剤", "柔軟剤", "ソフター", "漂白剤", "アタック", "ボールド", "ナノックス",
        "シャンプー", "トリートメント", "リンス", "石鹸", "ソープ", "ボディ", "ハミガキ", "歯ブラシ",
        "ティッシュ", "トイレット", "ペーパー", "ラップ", "ホイル", "ゴミ袋", "掃除", "消臭",
        "猫", "犬", "ペット", "砂", "シーツ", "デオトイレ", "ニャンとも", "電池", "タオル"
    ]
    if any(k in items_text for k in daily_keywords):
        return "日用品・消耗品"

    appliance_keywords = ["パソコン", "パーツ", "家電", "ryzen", "radeon", "msi", "asus", "usb", "ケーブル", "充電器", "インク", "lc412"]
    if any(k in items_text for k in appliance_keywords):
        return "住居・家具・家電"

    medical_keywords = ["クリニック", "病院", "歯科", "眼科", "処方", "薬", "シップ", "目薬", "ビタミン", "サプリ"]
    if any(k in items_text for k in medical_keywords):
        return "医療・健康"

    hobby_keywords = ["steam", "game", "ゲーム", "本", "書籍", "comic", "コミック", "kindle", "雑誌"]
    if any(k in items_text for k in hobby_keywords):
        return "娯楽・趣味・書籍"

    # ステップ2: AI推論
    if default_category in CATEGORIES and default_category != "その他":
        return default_category

    # ステップ3: 店舗名
    if any(k in store_text for k in ["ドラッグ", "薬局", "サンドラッグ", "コスモス", "マツキヨ", "ダイソー", "セリア", "キャンドゥ"]):
        return "日用品・消耗品"
    if any(k in store_text for k in ["マクドナルド", "すき家", "スタバ", "スターバックス", "カフェ", "居酒屋", "食堂", "ラーメン", "レストラン"]):
        return "外食・カフェ"
    if any(k in store_text for k in ["スーパー", "trial", "トライアル", "マックスバリュ", "業務スーパー", "精肉", "青果", "鮮魚", "イオン", "ライフ"]):
        return "食費 (食材・自炊)"
    if any(k in store_text for k in ["出光", "eneos", "コスモ石油", "ガソリン", "駐車", "jr", "メトロ"]):
        return "交通費・ガソリン"
    if any(k in store_text for k in ["ヨドバシ", "ビックカメラ", "ヤマダ", "edion"]):
        return "住居・家具・家電"

    return "その他"

# ==========================================
# 1. 秘密情報 (secrets.toml) の書き込み・保存
# ==========================================
def save_api_key_to_secrets(key_name, key_value):
    """画面から入力されたAPIキーを .streamlit/secrets.toml に永続保存"""
    if not key_value:
        return
    secrets_dir = ".streamlit"
    secrets_path = os.path.join(secrets_dir, "secrets.toml")
    os.makedirs(secrets_dir, exist_ok=True)

    current_secrets = {}
    if os.path.exists(secrets_path):
        with open(secrets_path, "r", encoding="utf-8") as f:
            for line in f:
                if "=" in line:
                    k, v = line.split("=", 1)
                    current_secrets[k.strip()] = v.strip().strip('"').strip("'")

    current_secrets[key_name] = key_value
    with open(secrets_path, "w", encoding="utf-8") as f:
        for k, v in current_secrets.items():
            f.write(f'{k} = "{v}"\n')

# ==========================================
# 2. データベース操作（Supabase / SQLite ハイブリッド）
# ==========================================
def get_supabase_client():
    """Supabase クライアントの取得（設定がない場合は None）"""
    url = st.secrets.get("SUPABASE_URL", os.environ.get("SUPABASE_URL", ""))
    key = st.secrets.get("SUPABASE_KEY", os.environ.get("SUPABASE_KEY", ""))
    if url and key and create_client:
        try:
            return create_client(url, key)
        except Exception:
            return None
    return None

def setup_database():
    """SQLite用ローカルテーブル初期化（Supabase未設定時のフォールバック）"""
    conn = sqlite3.connect("receipt_data.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            store_name TEXT DEFAULT '',
            total_amount INTEGER,
            discount INTEGER DEFAULT 0,
            points_used INTEGER DEFAULT 0,
            category TEXT,
            tax_8_amount INTEGER DEFAULT 0,
            tax_8_tax INTEGER DEFAULT 0,
            tax_10_amount INTEGER DEFAULT 0,
            tax_10_tax INTEGER DEFAULT 0,
            tax_type TEXT DEFAULT '外税'
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS receipt_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_id INTEGER,
            item_name TEXT,
            item_price INTEGER,
            FOREIGN KEY(receipt_id) REFERENCES receipts(id)
        )
    """)
    conn.commit()
    conn.close()

def save_receipt_with_items(date, store_name, total_amount, discount, points_used, category, items, tax_data):
    """レシートと明細を保存 (Supabase優先)"""
    sp = get_supabase_client()
    if sp:
        # Supabase へ登録
        res = sp.table("receipts").insert({
            "date": str(date),
            "store_name": str(store_name),
            "total_amount": int(total_amount),
            "discount": int(discount),
            "points_used": int(points_used),
            "category": str(category),
            "tax_8_amount": int(tax_data.get("tax_8_amount", 0)),
            "tax_8_tax": int(tax_data.get("tax_8_tax", 0)),
            "tax_10_amount": int(tax_data.get("tax_10_amount", 0)),
            "tax_10_tax": int(tax_data.get("tax_10_tax", 0)),
            "tax_type": str(tax_data.get("tax_type", "外税"))
        }).execute()
        
        if res.data:
            receipt_id = res.data[0]["id"]
            items_to_insert = []
            for item in items:
                name = item.get("name", "").strip() if isinstance(item, dict) else item[0].strip()
                price = item.get("price", 0) if isinstance(item, dict) else item[1]
                if name:
                    items_to_insert.append({
                        "receipt_id": receipt_id,
                        "item_name": name,
                        "item_price": int(price)
                    })
            if items_to_insert:
                sp.table("receipt_items").insert(items_to_insert).execute()
            return receipt_id

    # SQLite フォールバック
    conn = sqlite3.connect("receipt_data.db")
    cursor = conn.cursor()
    cursor.execute(
        """INSERT INTO receipts 
           (date, store_name, total_amount, discount, points_used, category, 
            tax_8_amount, tax_8_tax, tax_10_amount, tax_10_tax, tax_type) 
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            date, store_name, total_amount, discount, points_used, category,
            tax_data.get("tax_8_amount", 0),
            tax_data.get("tax_8_tax", 0),
            tax_data.get("tax_10_amount", 0),
            tax_data.get("tax_10_tax", 0),
            tax_data.get("tax_type", "外税")
        )
    )
    receipt_id = cursor.lastrowid
    for item in items:
        name = item.get("name", "").strip() if isinstance(item, dict) else item[0].strip()
        price = item.get("price", 0) if isinstance(item, dict) else item[1]
        if name:
            cursor.execute("INSERT INTO receipt_items (receipt_id, item_name, item_price) VALUES (?, ?, ?)", (receipt_id, name, price))
    conn.commit()
    conn.close()
    return receipt_id

def get_all_receipts(search_keyword=""):
    """全レシートと明細の取得（検索対応）"""
    sp = get_supabase_client()
    if sp:
        # Supabase から取得
        query = sp.table("receipts").select("*, receipt_items(*)").order("date", desc=True)
        res = query.execute()
        rows = res.data or []
        
        data = []
        kw = search_keyword.strip().lower()
        for r in rows:
            r_items = [(it["item_name"], it["item_price"]) for it in r.get("receipt_items", [])]
            # 検索フィルタ
            if kw:
                match_text = f"{r.get('store_name','')} {r.get('category','')} {r.get('date','')} " + " ".join([it[0] for it in r_items])
                if kw not in match_text.lower():
                    continue
                    
            data.append({
                "id": r["id"],
                "date": r.get("date", ""),
                "store_name": r.get("store_name", ""),
                "amount": r.get("total_amount", 0),
                "discount": r.get("discount", 0),
                "points_used": r.get("points_used", 0),
                "category": r.get("category", "その他"),
                "tax_8_amount": r.get("tax_8_amount", 0),
                "tax_8_tax": r.get("tax_8_tax", 0),
                "tax_10_amount": r.get("tax_10_amount", 0),
                "tax_10_tax": r.get("tax_10_tax", 0),
                "tax_type": r.get("tax_type", "外税"),
                "items": r_items
            })
        return data

    # SQLite フォールバック
    conn = sqlite3.connect("receipt_data.db")
    cursor = conn.cursor()
    if search_keyword.strip():
        kw = f"%{search_keyword.strip()}%"
        cursor.execute("""
            SELECT DISTINCT r.id, r.date, r.store_name, r.total_amount, r.discount, r.points_used, r.category, 
                            r.tax_8_amount, r.tax_8_tax, r.tax_10_amount, r.tax_10_tax, r.tax_type 
            FROM receipts r
            LEFT JOIN receipt_items i ON r.id = i.receipt_id
            WHERE r.store_name LIKE ? OR r.category LIKE ? OR r.date LIKE ? OR i.item_name LIKE ?
            ORDER BY r.date DESC
        """, (kw, kw, kw, kw))
    else:
        cursor.execute("""
            SELECT id, date, store_name, total_amount, discount, points_used, category, 
                   tax_8_amount, tax_8_tax, tax_10_amount, tax_10_tax, tax_type 
            FROM receipts ORDER BY date DESC
        """)
    receipts = cursor.fetchall()
    data = []
    for r in receipts:
        r_id, r_date, r_store, r_amount, r_disc, r_pts, r_cat, t8_a, t8_t, t10_a, t10_t, t_type = r
        cursor.execute("SELECT item_name, item_price FROM receipt_items WHERE receipt_id = ?", (r_id,))
        items = cursor.fetchall()
        data.append({
            "id": r_id, "date": r_date, "store_name": r_store, "amount": r_amount, "discount": r_disc,
            "points_used": r_pts, "category": r_cat, "tax_8_amount": t8_a, "tax_8_tax": t8_t,
            "tax_10_amount": t10_a, "tax_10_tax": t10_t, "tax_type": t_type, "items": items
        })
    conn.close()
    return data

def get_monthly_summary():
    """月別集計"""
    all_data = get_all_receipts()
    if not all_data:
        return []
    df = pd.DataFrame(all_data)
    df["month"] = df["date"].str.slice(0, 7)
    grouped = df.groupby("month").agg({
        "amount": "sum",
        "tax_8_tax": "sum",
        "tax_10_tax": "sum"
    }).reset_index().sort_values("month", ascending=False)
    
    return [(row["month"], row["amount"], row["tax_8_tax"], row["tax_10_tax"]) for _, row in grouped.iterrows()]

def get_category_summary(month=None):
    """カテゴリー別集計"""
    all_data = get_all_receipts()
    if not all_data:
        return []
    df = pd.DataFrame(all_data)
    if month:
        df = df[df["date"].str.slice(0, 7) == month]
    if df.empty:
        return []
    grouped = df.groupby("category")["amount"].sum().reset_index().sort_values("amount", ascending=False)
    return [(row["category"], row["amount"]) for _, row in grouped.iterrows()]

def update_full_receipt(receipt_id, date, store_name, total_amount, discount, points_used, category, tax_type, t8_tax, t10_tax, items):
    """レシート更新"""
    sp = get_supabase_client()
    if sp:
        sp.table("receipts").update({
            "date": str(date), "store_name": str(store_name), "total_amount": int(total_amount),
            "discount": int(discount), "points_used": int(points_used), "category": str(category),
            "tax_type": str(tax_type), "tax_8_tax": int(t8_tax), "tax_10_tax": int(t10_tax)
        }).eq("id", receipt_id).execute()
        
        sp.table("receipt_items").delete().eq("receipt_id", receipt_id).execute()
        items_to_insert = []
        for item in items:
            name = item.get("name", "").strip() if isinstance(item, dict) else item[0].strip()
            price = item.get("price", 0) if isinstance(item, dict) else item[1]
            if name:
                items_to_insert.append({"receipt_id": receipt_id, "item_name": name, "item_price": int(price)})
        if items_to_insert:
            sp.table("receipt_items").insert(items_to_insert).execute()
        return

    conn = sqlite3.connect("receipt_data.db")
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE receipts 
        SET date = ?, store_name = ?, total_amount = ?, discount = ?, points_used = ?,
            category = ?, tax_type = ?, tax_8_tax = ?, tax_10_tax = ?
        WHERE id = ?
    """, (date, store_name, total_amount, discount, points_used, category, tax_type, t8_tax, t10_tax, receipt_id))
    cursor.execute("DELETE FROM receipt_items WHERE receipt_id = ?", (receipt_id,))
    for item in items:
        name = item.get("name", "").strip() if isinstance(item, dict) else item[0].strip()
        price = item.get("price", 0) if isinstance(item, dict) else item[1]
        if name:
            cursor.execute("INSERT INTO receipt_items (receipt_id, item_name, item_price) VALUES (?, ?, ?)", (receipt_id, name, price))
    conn.commit()
    conn.close()

def delete_receipt(receipt_id):
    """レシートおよび紐づく明細を完全削除 (Supabase / SQLite 両対応)"""
    r_id = int(receipt_id)
    sp = get_supabase_client()
    if sp:
        try:
            # 紐づく明細を削除
            sp.table("receipt_items").delete().eq("receipt_id", r_id).execute()
            # レシート本体を削除
            sp.table("receipts").delete().eq("id", r_id).execute()
            return True
        except Exception as e:
            st.error(f"Supabase削除エラー: {e}")
            return False

    # SQLite フォールバック
    conn = sqlite3.connect("receipt_data.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM receipt_items WHERE receipt_id = ?", (r_id,))
    cursor.execute("DELETE FROM receipts WHERE id = ?", (r_id,))
    conn.commit()
    conn.close()
    return True

# ==========================================
# 3. 解析エンジン処理
# ==========================================
def parse_with_gemini(uploaded_file, api_key, max_retries=4):
    if not api_key:
        raise ValueError("Gemini APIキーが未設定です。サイドバーで設定してください。")
    if genai is None:
        raise ImportError("google-genai パッケージが未導入です。")

    clean_key = "".join(c for c in api_key.strip() if 32 <= ord(c) <= 126)
    client = genai.Client(api_key=clean_key)
    uploaded_file.seek(0)
    file_name = uploaded_file.name.lower()

    if file_name.endswith(".pdf"):
        file_bytes = uploaded_file.read()
        mime_type = "application/pdf"
    else:
        raw_img = Image.open(uploaded_file)
        img = ImageOps.exif_transpose(raw_img)
        if img.mode != "RGB":
            img = img.convert("RGB")
            
        # スマホ写真の自動リサイズ＆圧縮（高速化）
        max_dim = 1600
        if max(img.size) > max_dim:
            scale = max_dim / max(img.size)
            new_size = (int(img.width * scale), int(img.height * scale))
            img = img.resize(new_size, Image.Resampling.LANCZOS)

        buffered = io.BytesIO()
        img.save(buffered, format="JPEG", quality=80, optimize=True)
        file_bytes = buffered.getvalue()
        mime_type = "image/jpeg"

    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model="gemini-3.6-flash",
                contents=[types.Part.from_bytes(data=file_bytes, mime_type=mime_type), JSON_PROMPT],
                config=types.GenerateContentConfig(response_mime_type="application/json")
            )
            text_content = response.text.strip()
            if text_content.startswith("```json"):
                text_content = text_content[7:]
            if text_content.endswith("```"):
                text_content = text_content[:-3]
            parsed_data = json.loads(text_content.strip())
            return parsed_data[0] if isinstance(parsed_data, list) and len(parsed_data) > 0 else parsed_data
        except Exception as e:
            if ("503" in str(e) or "UNAVAILABLE" in str(e) or "429" in str(e)) and attempt < max_retries - 1:
                time.sleep((attempt + 1) * 2)
                continue
            raise e

def parse_with_openai(uploaded_file, api_key, max_retries=3):
    if not api_key:
        raise ValueError("OpenAI APIキーが未設定です。サイドバーで設定してください。")
    if OpenAI is None:
        raise ImportError("openai パッケージが未導入です。")

    clean_key = "".join(c for c in api_key.strip() if 32 <= ord(c) <= 126)
    client = OpenAI(api_key=clean_key)
    uploaded_file.seek(0)
    file_name = uploaded_file.name.lower()

    if file_name.endswith(".pdf"):
        try:
            import fitz
            doc = fitz.open(stream=uploaded_file.read(), filetype="pdf")
            page = doc.load_page(0)
            pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
            img_b64 = base64.b64encode(pix.tobytes("jpeg")).decode("utf-8")
        except ImportError:
            uploaded_file.seek(0)
            img_b64 = base64.b64encode(uploaded_file.read()).decode("utf-8")
    else:
        raw_img = Image.open(uploaded_file)
        img = ImageOps.exif_transpose(raw_img)
        if img.mode != "RGB":
            img = img.convert("RGB")
        buffered = io.BytesIO()
        img.save(buffered, format="JPEG", quality=95)
        img_b64 = base64.b64encode(buffered.getvalue()).decode("utf-8")

    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": JSON_PROMPT},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
                    ]
                }],
                response_format={"type": "json_object"}
            )
            parsed_data = json.loads(response.choices[0].message.content.strip())
            return parsed_data[0] if isinstance(parsed_data, list) and len(parsed_data) > 0 else parsed_data
        except Exception as e:
            if ("429" in str(e) or "500" in str(e) or "503" in str(e)) and attempt < max_retries - 1:
                time.sleep((attempt + 1) * 3)
                continue
            raise e

def parse_with_tesseract(uploaded_file):
    uploaded_file.seek(0)
    raw_img = Image.open(uploaded_file)
    img = ImageOps.exif_transpose(raw_img)
    if img.mode != "RGB":
        img = img.convert("RGB")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as temp_file:
        temp_path = temp_file.name
        img.save(temp_path, format="PNG")

    try:
        text = pytesseract.image_to_string(temp_path, lang="jpn", config=r'--oem 3 --psm 6')
    except Exception:
        text = pytesseract.image_to_string(temp_path, lang="jpn")
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

    date_match = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", text)
    date = f"{date_match.group(1)}/{int(date_match.group(2)):02d}/{int(date_match.group(3)):02d}" if date_match else datetime.now().strftime("%Y/%m/%d")

    total_amount = 0
    total_matches = re.findall(r"(?:^|\n)[^\n]*?合計[\s\\/¥]*([0-9,\s]+)", text)
    for m in total_matches:
        digits = re.sub(r"[^\d]", "", m)
        if digits.isdigit() and int(digits) > 100:
            total_amount = int(digits)

    items = []
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    started = False
    for line in lines:
        if re.search(r"領収|スナック|助六|★|\*", line):
            started = True
        if not started:
            continue
        if re.search(r"小計|点\s*\d|内税|外税|税合計|プリカ|残高|支払|ポイント|軽減税率", line):
            break
        if re.search(r"電話|受付|相談|番号|※|＊|===|---|___|店|TEL|消費税", line):
            continue
        price_match = re.search(r"[¥\\/\s]*(\d{1,5})\s*円?$", line)
        if price_match:
            price = int(price_match.group(1))
            name = re.sub(r"^[|!Il王*★・\s\d>]+|[|!Il*★・\s\d>]+$", "", line[:price_match.start()]).strip()
            if len(name) >= 2 and 1 <= price <= 50000:
                items.append({"name": name, "price": price})

    return {
        "store_name": "店舗", "date": date, "total_amount": total_amount, "discount": 0, "points_used": 0,
        "category": "食費 (食材・自炊)", "tax_info": {"tax_type": "外税", "tax_8_amount": 0, "tax_8_tax": 0, "tax_10_amount": 0, "tax_10_tax": 0},
        "items": items
    }

# ==========================================
# 4. Streamlit UI
# ==========================================
@st.dialog("⚠️ 削除の確認")
def confirm_delete_dialog(receipt_id, store_name, total_amount):
    st.warning(f"ID {receipt_id}：【{store_name}】 ¥{total_amount:,} のデータを本当に削除しますか？")
    st.caption("※削除するとクラウドDBから完全に消去され、元に戻すことはできません。")
    
    col_yes, col_no = st.columns(2)
    with col_yes:
        if st.button("🗑️ 完全に削除する", type="primary", use_container_width=True):
            if delete_receipt(receipt_id):
                st.success("削除が完了しました。")
                time.sleep(0.5)
                st.rerun()
    with col_no:
        if st.button("キャンセル", type="secondary", use_container_width=True):
            st.rerun()

def main():
    setup_database()
    st.set_page_config(page_title="家計簿レシート管理アプリ", layout="wide")
    st.title("🧾 家計簿レシート管理アプリ")

    sp_client = get_supabase_client()

    # --- サイドバー ---
    with st.sidebar:
        st.header("⚙️ 設定 / 解析エンジン")
        
        # データベース接続状況のステータス表示
        if sp_client:
            st.success("☁️ Supabase クラウドDB 接続中")
        else:
            st.info("💾 ローカル SQLite (receipt_data.db) 動作中")

        engine_choice = st.radio(
            "解析エンジンを選択",
            ["Gemini API (推奨)", "ChatGPT (OpenAI)", "Tesseract OCR (ローカル)"],
            index=0
        )
        
        default_gemini_key = st.secrets.get("GEMINI_API_KEY", os.environ.get("GEMINI_API_KEY", ""))
        default_openai_key = st.secrets.get("OPENAI_API_KEY", os.environ.get("OPENAI_API_KEY", ""))

        gemini_api_key = ""
        openai_api_key = ""

        if engine_choice == "Gemini API (推奨)":
            with st.form("gemini_key_form"):
                inp_gemini_key = st.text_input(
                    "Gemini APIキー",
                    value=st.session_state.get("gemini_key", default_gemini_key),
                    type="password",
                    autocomplete="off"
                )
                if st.form_submit_button("💾 このキーを保存する"):
                    save_api_key_to_secrets("GEMINI_API_KEY", inp_gemini_key)
                    st.session_state["gemini_key"] = inp_gemini_key
                    st.success("secrets.toml に保存しました！")
                    st.rerun()
            gemini_api_key = st.session_state.get("gemini_key", default_gemini_key)

        elif engine_choice == "ChatGPT (OpenAI)":
            with st.form("openai_key_form"):
                inp_openai_key = st.text_input(
                    "OpenAI APIキー",
                    value=st.session_state.get("openai_key", default_openai_key),
                    type="password",
                    autocomplete="off"
                )
                if st.form_submit_button("💾 このキーを保存する"):
                    save_api_key_to_secrets("OPENAI_API_KEY", inp_openai_key)
                    st.session_state["openai_key"] = inp_openai_key
                    st.success("secrets.toml に保存しました！")
                    st.rerun()
            openai_api_key = st.session_state.get("openai_key", default_openai_key)

        # Supabaseキー設定アコーディオン
        with st.expander("☁️ Supabase 接続設定"):
            default_sp_url = st.secrets.get("SUPABASE_URL", os.environ.get("SUPABASE_URL", ""))
            default_sp_key = st.secrets.get("SUPABASE_KEY", os.environ.get("SUPABASE_KEY", ""))
            
            with st.form("supabase_setting_form"):
                inp_sp_url = st.text_input("Project URL", value=default_sp_url, placeholder="[https://xxxx.supabase.co](https://xxxx.supabase.co)")
                inp_sp_key = st.text_input("Publishable Key / anon key", value=default_sp_key, type="password")
                if st.form_submit_button("💾 Supabase設定を保存"):
                    save_api_key_to_secrets("SUPABASE_URL", inp_sp_url)
                    save_api_key_to_secrets("SUPABASE_KEY", inp_sp_key)
                    st.success("Supabase設定を secrets.toml に保存しました！")
                    st.rerun()

    # --- メインタブ ---
    tab1, tab2, tab3 = st.tabs(["📸 レシート・領収書登録 (一括対応)", "📊 支出ダッシュボード", "🔍 履歴検索・編集・削除"])

    # --- タブ1: レシート登録 ---
    with tab1:
        st.subheader(f"レシート・領収書登録 (使用エンジン: {engine_choice.split()[0]})")

        if "uploader_key" not in st.session_state:
            st.session_state["uploader_key"] = 0
        if "batch_parsed_data" not in st.session_state:
            st.session_state["batch_parsed_data"] = {}

        uploaded_files = st.file_uploader(
            "レシートまたは領収書ファイルを選択 (複数選択・ドラッグ＆ドロップ可能)", 
            type=["jpg", "jpeg", "png", "heic", "HEIC", "pdf"],
            accept_multiple_files=True,
            key=f"receipt_uploader_{st.session_state['uploader_key']}"
        )

        if uploaded_files:
            for up_file in uploaded_files:
                file_sig = f"{up_file.name}_{up_file.size}"
                if file_sig not in st.session_state["batch_parsed_data"]:
                    try:
                        with st.spinner(f"「{up_file.name}」を解析中..."):
                            if engine_choice == "Gemini API (推奨)":
                                res = parse_with_gemini(up_file, gemini_api_key)
                            elif engine_choice == "ChatGPT (OpenAI)":
                                res = parse_with_openai(up_file, openai_api_key)
                            else:
                                res = parse_with_tesseract(up_file)
                            
                            st_name = res.get("store_name", "")
                            raw_cat = res.get("category", "その他")
                            items_data = res.get("items", [{"name": "", "price": 0}])
                            final_cat = infer_category_rule(st_name, items_data, raw_cat)

                            st.session_state["batch_parsed_data"][file_sig] = {
                                "file_name": up_file.name,
                                "store_name": st_name,
                                "date": res.get("date", datetime.now().strftime("%Y/%m/%d")),
                                "total_amount": res.get("total_amount", 0),
                                "discount": res.get("discount", 0),
                                "points_used": res.get("points_used", 0),
                                "category": final_cat,
                                "tax_info": res.get("tax_info", {}),
                                "items": items_data
                            }
                    except Exception as e:
                        st.error(f"「{up_file.name}」の解析エラー: {e}")

            if st.session_state["batch_parsed_data"]:
                st.write(f"### 📋 解析結果一覧 ({len(st.session_state['batch_parsed_data'])} 件)")
                all_forms_data = []

                for idx, (sig, pdata) in enumerate(list(st.session_state["batch_parsed_data"].items())):
                    with st.expander(f"📄 [{idx+1}] {pdata['file_name']} - 【{pdata['store_name'] or '店舗'}】 ¥{pdata['total_amount']:,}", expanded=True):
                        c1, c2, c3 = st.columns([3, 2, 2])
                        with c1:
                            val_store = st.text_input("店舗名", value=pdata["store_name"], key=f"b_store_{sig}")
                        with c2:
                            val_date = st.text_input("利用日付", value=pdata["date"], key=f"b_date_{sig}")
                        with c3:
                            val_amt = st.number_input("合計金額 (円)", value=int(pdata["total_amount"]), step=1, key=f"b_amt_{sig}")

                        c4, c5, c6 = st.columns([2, 2, 3])
                        with c4:
                            val_disc = st.number_input("値引き (円)", value=int(pdata["discount"]), step=1, key=f"b_disc_{sig}")
                        with c5:
                            val_pts = st.number_input("利用ポイント (pt)", value=int(pdata["points_used"]), step=1, key=f"b_pts_{sig}")
                        with c6:
                            cur_c = pdata["category"]
                            c_idx = CATEGORIES.index(cur_c) if cur_c in CATEGORIES else 0
                            val_cat = st.selectbox("カテゴリー", CATEGORIES, index=c_idx, key=f"b_cat_{sig}")

                        tax_i = pdata.get("tax_info", {})
                        t_col1, t_col2, t_col3 = st.columns(3)
                        with t_col1:
                            t_type = st.selectbox("税区分", ["外税", "内税"], index=0 if tax_i.get("tax_type") == "外税" else 1, key=f"b_ttype_{sig}")
                        with t_col2:
                            t8_tax = st.number_input("8% 税額", value=int(tax_i.get("tax_8_tax", 0)), step=1, key=f"b_t8_{sig}")
                        with t_col3:
                            t10_tax = st.number_input("10% 税額", value=int(tax_i.get("tax_10_tax", 0)), step=1, key=f"b_t10_{sig}")

                        st.caption("商品明細:")
                        items_cur = []
                        for i_idx, item in enumerate(pdata.get("items", [])):
                            ic1, ic2 = st.columns([4, 2])
                            with ic1:
                                it_n = st.text_input(f"品名 {i_idx+1}", value=item.get("name", ""), key=f"b_itn_{sig}_{i_idx}")
                            with ic2:
                                it_p = st.number_input(f"価格 {i_idx+1}", value=int(item.get("price", 0)), step=1, key=f"b_itp_{sig}_{i_idx}")
                            items_cur.append({"name": it_n, "price": it_p})

                        all_forms_data.append({
                            "sig": sig, "date": val_date, "store_name": val_store, "total_amount": val_amt,
                            "discount": val_disc, "points_used": val_pts, "category": val_cat,
                            "tax_data": {"tax_type": t_type, "tax_8_amount": tax_i.get("tax_8_amount", 0), "tax_8_tax": t8_tax, "tax_10_amount": tax_i.get("tax_10_amount", 0), "tax_10_tax": t10_tax},
                            "items": items_cur
                        })

                st.write("---")
                col_save_all, col_clear = st.columns([1, 1])
                with col_save_all:
                    if st.button("💾 全てのレシートを一括保存する", type="primary"):
                        for r_item in all_forms_data:
                            save_receipt_with_items(
                                r_item["date"], r_item["store_name"], r_item["total_amount"],
                                r_item["discount"], r_item["points_used"], r_item["category"],
                                r_item["items"], r_item["tax_data"]
                            )
                        st.success(f"{len(all_forms_data)} 件のレシートを保存しました！")
                        st.session_state["uploader_key"] += 1
                        st.session_state["batch_parsed_data"] = {}
                        st.rerun()

                with col_clear:
                    if st.button("❌ キャンセル（クリア）"):
                        st.session_state["uploader_key"] += 1
                        st.session_state["batch_parsed_data"] = {}
                        st.rerun()

    # --- タブ2: 支出ダッシュボード (Plotly) ---
    with tab2:
        st.subheader("📊 支出ダッシュボード")
        summary_data = get_monthly_summary()

        if summary_data:
            cols = st.columns(min(len(summary_data), 4))
            for idx, (month, total, sum_t8, sum_t10) in enumerate(summary_data[:4]):
                with cols[idx]:
                    st.metric(
                        label=f"📅 {month} 総支出",
                        value=f"¥{total:,}",
                        help=f"内訳: 8%税 ¥{sum_t8 or 0:,} / 10%税 ¥{sum_t10 or 0:,}"
                    )

            st.write("---")
            col_chart_left, col_chart_right = st.columns([1.1, 0.9])

            with col_chart_left:
                st.markdown("#### 📈 月別支出推移")
                df_monthly = pd.DataFrame(summary_data, columns=["月", "合計金額", "8%消費税", "10%消費税"])
                df_monthly_sorted = df_monthly.sort_values("月")

                fig_bar = px.bar(
                    df_monthly_sorted,
                    x="月",
                    y="合計金額",
                    text="合計金額",
                    color="合計金額",
                    color_continuous_scale="Tealgrn",
                )
                fig_bar.update_traces(
                    texttemplate='¥%{text:,.0f}',
                    textposition='outside',
                    marker_line_width=0,
                    opacity=0.85
                )
                fig_bar.update_layout(
                    margin=dict(l=10, r=10, t=20, b=10),
                    height=360,
                    xaxis_title=None,
                    yaxis_title=None,
                    coloraxis_showscale=False,
                    plot_bgcolor="rgba(0,0,0,0)",
                    paper_bgcolor="rgba(0,0,0,0)",
                    font=dict(family="sans-serif", size=12)
                )
                st.plotly_chart(fig_bar, use_container_width=True)

            with col_chart_right:
                st.markdown("#### 🍩 カテゴリー別内訳")
                available_months = ["全期間"] + [row[0] for row in summary_data]
                selected_month = st.selectbox("表示月を選択", available_months, index=0, key="donut_month_select")

                target_m = None if selected_month == "全期間" else selected_month
                cat_data = get_category_summary(target_m)

                if cat_data:
                    df_cat = pd.DataFrame(cat_data, columns=["カテゴリー", "金額"])
                    total_cat_amt = df_cat["金額"].sum()

                    fig_donut = go.Figure(data=[go.Pie(
                        labels=df_cat["カテゴリー"],
                        values=df_cat["金額"],
                        hole=0.62,
                        textinfo="label+percent",
                        hoverinfo="label+value+percent",
                        hovertemplate="<b>%{label}</b><br>金額: ¥%{value:,.0f}<br>割合: %{percent}<extra></extra>",
                        marker=dict(colors=px.colors.qualitative.Pastel)
                    )])

                    fig_donut.update_layout(
                        annotations=[dict(
                            text=f"合計<br><b>¥{total_cat_amt:,}</b>",
                            x=0.5, y=0.5,
                            font_size=15,
                            showarrow=False
                        )],
                        showlegend=False,
                        margin=dict(l=10, r=10, t=10, b=10),
                        height=360,
                        plot_bgcolor="rgba(0,0,0,0)",
                        paper_bgcolor="rgba(0,0,0,0)",
                        font=dict(family="sans-serif", size=12)
                    )
                    st.plotly_chart(fig_donut, use_container_width=True)
                else:
                    st.info("データがありません。")

            st.write("---")
            if cat_data:
                st.markdown(f"##### 📑 {selected_month} カテゴリー別詳細")
                df_cat_display = df_cat.copy()
                df_cat_display["構成比"] = (df_cat_display["金額"] / df_cat_display["金額"].sum() * 100).map("{:.1f}%".format)
                df_cat_display["金額 (税込)"] = df_cat_display["金額"].map("¥{:,}".format)
                st.dataframe(
                    df_cat_display[["カテゴリー", "金額 (税込)", "構成比"]],
                    use_container_width=True,
                    hide_index=True
                )
        else:
            st.info("集計対象のデータがまだ登録されていません。")

    # --- タブ3: 履歴検索・編集・削除 ---
    with tab3:
        st.subheader("🔍 データ履歴の検索・編集・削除")
        # 検索条件の入力欄（例: 3列または4列のカラム内）
        sort_order = st.selectbox(
            "並び順",
            options=["登録が新しい順", "登録が古い順", "レシート日付が新しい順", "レシート日付が古い順", "金額が高い順", "金額が安い順"],
            index=0
        )
        search_kw = st.text_input("🔍 キーワード検索 (店舗名、品名、カテゴリー、日付)", placeholder="例: Amazon, シャンプー, 食費, 2026/08")
        records = get_all_receipts(search_kw, sort_order=sort_order)

        if search_kw.strip():
            hit_sum = sum(r["amount"] for r in records)
            st.caption(f"検索結果: **{len(records)} 件** 見つかりました（合計支出: **¥{hit_sum:,}**）")

        if records:# --- 並び替え（ソート）処理 ---
            if sort_order == "登録が新しい順":
                records = sorted(records, key=lambda x: x["id"], reverse=True)
            elif sort_order == "登録が古い順":
                records = sorted(records, key=lambda x: x["id"], reverse=False)
            elif sort_order == "レシート日付が新しい順":
                records = sorted(records, key=lambda x: (str(x["date"]), x["id"]), reverse=True)
            elif sort_order == "レシート日付が古い順":
                records = sorted(records, key=lambda x: (str(x["date"]), x["id"]), reverse=False)
            elif sort_order == "金額が高い順":
                records = sorted(records, key=lambda x: x["amount"], reverse=True)
            elif sort_order == "金額が安い順":
                records = sorted(records, key=lambda x: x["amount"], reverse=False)
            for rec in records:
                r_id = rec["id"]
                store_display = f"【{rec['store_name']}】" if rec['store_name'] else ""
                disc_display = f" | 値引: -¥{rec['discount']:,}" if rec['discount'] > 0 else ""
                pts_display = f" | Pt: {rec['points_used']:,}pt" if rec['points_used'] > 0 else ""

                header_title = f"ID {r_id} | {rec['date']} {store_display} | ¥{rec['amount']:,} ({rec['category']}){disc_display}{pts_display}"
                
                with st.expander(header_title):
                    with st.form(key=f"edit_form_{r_id}"):
                        st.write("##### 📌 基本情報の修正")
                        c1, c2, c3 = st.columns([3, 2, 2])
                        with c1:
                            edit_store = st.text_input("店舗名", value=rec["store_name"], key=f"e_store_{r_id}")
                        with c2:
                            edit_date = st.text_input("利用日付", value=rec["date"], key=f"e_date_{r_id}")
                        with c3:
                            edit_amt = st.number_input("合計金額 (円)", value=int(rec["amount"]), step=1, key=f"e_amt_{r_id}")

                        c4, c5, c6 = st.columns([2, 2, 3])
                        with c4:
                            edit_disc = st.number_input("値引き額 (円)", value=int(rec["discount"]), step=1, key=f"e_disc_{r_id}")
                        with c5:
                            edit_pts = st.number_input("利用ポイント (pt)", value=int(rec["points_used"]), step=1, key=f"e_pts_{r_id}")
                        with c6:
                            matched_idx = 0
                            cur_rec_cat = str(rec.get("category", "")).strip()
                            if cur_rec_cat in CATEGORIES:
                                matched_idx = CATEGORIES.index(cur_rec_cat)
                            else:
                                for i, c in enumerate(CATEGORIES):
                                    if cur_rec_cat and (cur_rec_cat in c or c.startswith(cur_rec_cat[:2])):
                                        matched_idx = i
                                        break
                            edit_cat = st.selectbox("カテゴリー", CATEGORIES, index=matched_idx, key=f"e_cat_{r_id}")

                        st.write("##### 🧾 消費税内訳の修正")
                        t1, t2, t3 = st.columns([2, 2, 2])
                        with t1:
                            tax_idx = 0 if rec["tax_type"] == "外税" else 1
                            edit_tax_type = st.selectbox("税区分", ["外税", "内税"], index=tax_idx, key=f"e_ttype_{r_id}")
                        with t2:
                            edit_t8_tax = st.number_input("8% 消費税額", value=int(rec["tax_8_tax"]), step=1, key=f"e_t8_{r_id}")
                        with t3:
                            edit_t10_tax = st.number_input("10% 消費税額", value=int(rec["tax_10_tax"]), step=1, key=f"e_t10_{r_id}")

                        st.write("##### 🛒 商品明細の修正")
                        edited_items = []
                        for idx, (it_name, it_price) in enumerate(rec["items"]):
                            ic1, ic2 = st.columns([4, 2])
                            with ic1:
                                iname = st.text_input(f"商品名 {idx+1}", value=it_name, key=f"e_iname_{r_id}_{idx}")
                            with ic2:
                                iprice = st.number_input(f"金額 {idx+1}", value=int(it_price), step=1, key=f"e_iprice_{r_id}_{idx}")
                            edited_items.append((iname, iprice))

                        st.write("---")
                        col_btn1, col_btn2 = st.columns([1, 1])
                        with col_btn1:
                            submit_edit = st.form_submit_button("💾 変更を保存する", type="primary", use_container_width=True)
                        with col_btn2:
                            # フォーム内からは「削除確認へ」のトリガーボタンとして動作
                            submit_delete_trigger = st.form_submit_button("🗑️ このレシートを削除...", type="secondary", use_container_width=True)

                        # 保存ボタンが押されたとき
                        if submit_edit:
                            update_full_receipt(
                                r_id, edit_date, edit_store, edit_amt, edit_disc, edit_pts,
                                edit_cat, edit_tax_type, edit_t8_tax, edit_t10_tax, edited_items
                            )
                            st.success(f"ID {r_id} のデータを更新しました！")
                            time.sleep(0.5)
                            st.rerun()

                    # フォームの外（インデントを1段戻す）で削除トリガーを検知し、ポップアップを開く
                    if submit_delete_trigger:
                        confirm_delete_dialog(r_id, rec.get("store_name", "店舗"), rec.get("amount", 0))

                    if st.button(f"🗑️ ID {r_id} のレシートを完全削除", key=f"btn_del_rec_{r_id}"):
                        delete_receipt(r_id)
                        st.warning(f"ID {r_id} のデータを削除しました。")
                        st.rerun()
        else:
            st.info("該当するデータが見つかりませんでした。")

if __name__ == "__main__":
    main()