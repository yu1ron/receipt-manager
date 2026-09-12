import io
import json
import time
import logging
import base64
from PIL import Image, ImageOps

# ロギングの設定
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==========================================
# カスタム例外クラス
# ==========================================
class APIError(Exception):
    """API呼び出しエラーの基底クラス"""
    pass

class AuthenticationError(APIError):
    """認証エラー"""
    pass

class RateLimitError(APIError):
    """レートリミットエラー"""
    pass

class ServiceUnavailableError(APIError):
    """サービス利用不可エラー"""
    pass

class ParseError(APIError):
    """解析エラー"""
    pass

# ==========================================
# 定数・プロンプト定義
# ==========================================
JSON_PROMPT = """
以下の画像から、以下のJSON形式でデータを抽出してください。
{
  "total_amount": 数値,
  "items": [
    {"name": "文字列", "quantity": 数値, "price": 数値}
  ]
}
"""

# ==========================================
# 共通リトライロジック
# ==========================================
def retry_with_backoff(func, max_retries=3, base_delay=2):
    """指数バックオフによるリトライデコレータ"""
    def wrapper(*args, **kwargs):
        for attempt in range(max_retries):
            try:
                return func(*args, **kwargs)
            except RateLimitError as e:
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(f"レートリミット検出。{delay}秒後にリトライします... (試行 {attempt + 1}/{max_retries})")
                    time.sleep(delay)
                else:
                    raise e
            except ServiceUnavailableError as e:
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(f"サービス利用不可。{delay}秒後にリトライします... (試行 {attempt + 1}/{max_retries})")
                    time.sleep(delay)
                else:
                    raise e
            except Exception as e:
                logger.error(f"予期せぬエラー: {str(e)}")
                raise e
        return None
    return wrapper

# ==========================================
# Gemini用パーサー
# ==========================================
def parse_with_gemini(uploaded_file, api_key, max_retries=4):
    """Gemini APIを使用してファイルを解析"""
    if not api_key:
        raise ValueError("Gemini APIキーが未設定です。サイドバーで設定してください。")
    
    try:
        import google.generativeai as genai
        from google.generativeai import types
    except ImportError:
        raise ImportError("google-genai パッケージが未導入です。")
    
    # APIキーのクリーンアップ
    clean_key = "".join(c for c in api_key.strip() if 32 <= ord(c) <= 126)
    client = genai.Client(api_key=clean_key)
    
    uploaded_file.seek(0)
    file_name = uploaded_file.name.lower()
    
    # ファイル処理
    if file_name.endswith(".pdf"):
        file_bytes = uploaded_file.read()
        mime_type = "application/pdf"
    else:
        try:
            with Image.open(uploaded_file) as raw_img:
                img = ImageOps.exif_transpose(raw_img)
                if img.mode != "RGB":
                    img = img.convert("RGB")
                
                # 画像のリサイズと圧縮
                max_dim = 1600
                if max(img.size) > max_dim:
                    scale = max_dim / max(img.size)
                    new_size = (int(img.width * scale), int(img.height * scale))
                    img = img.resize(new_size, Image.Resampling.LANCZOS)
                
                buffered = io.BytesIO()
                img.save(buffered, format="JPEG", quality=80, optimize=True)
                file_bytes = buffered.getvalue()
                buffered.close()
                del img
                mime_type = "image/jpeg"
        except Exception as e:
            logger.error(f"画像処理エラー: {str(e)}")
            raise ParseError(f"画像処理に失敗しました: {str(e)}")
    
    # API呼び出し（リトライ付き）
    @retry_with_backoff(max_retries=max_retries, base_delay=2)
    def call_gemini_api():
        try:
            response = client.models.generate_content(
                model="gemini-2.0-flash",  # 最新モデルを使用
                contents=[types.Part.from_bytes(data=file_bytes, mime_type=mime_type), JSON_PROMPT],
                config=types.GenerateContentConfig(response_mime_type="application/json")
            )
            text_content = response.text.strip()
            
            # JSONフォーマットのクリーンアップ
            if text_content.startswith("```json"):
                text_content = text_content[7:]
            if text_content.endswith("```"):
                text_content = text_content[:-3]
            
            parsed_data = json.loads(text_content.strip())
            return parsed_data[0] if isinstance(parsed_data, list) and len(parsed_data) > 0 else parsed_data
        except Exception as e:
            error_str = str(e)
            if "503" in error_str or "UNAVAILABLE" in error_str:
                raise ServiceUnavailableError(f"Geminiサービス利用不可: {error_str}")
            elif "429" in error_str:
                raise RateLimitError(f"Geminiレートリミット: {error_str}")
            else:
                raise ParseError(f"Gemini解析エラー: {error_str}")
    
    return call_gemini_api()

# ==========================================
# OpenAI用パーサー
# ==========================================
def parse_with_openai(uploaded_file, api_key, max_retries=3):
    """OpenAI APIを使用してファイルを解析"""
    if not api_key:
        raise ValueError("OpenAI APIキーが未設定です。サイドバーで設定してください。")
    
    try:
        from openai import OpenAI
    except ImportError:
        raise ImportError("openai パッケージが未導入です。")
    
    # APIキーのクリーンアップ
    clean_key = "".join(c for c in api_key.strip() if 32 <= ord(c) <= 126)
    client = OpenAI(api_key=clean_key)
    
    uploaded_file.seek(0)
    file_name = uploaded_file.name.lower()
    
    # ファイル処理
    if file_name.endswith(".pdf"):
        try:
            import fitz
            doc = fitz.open(stream=uploaded_file.read(), filetype="pdf")
            page = doc.load_page(0)
            pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
            img_b64 = base64.b64encode(pix.tobytes("jpeg")).decode("utf-8")
            file_data = {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
        except ImportError:
            logger.warning("PyMuPDF (fitz) がインストールされていません。PDFをそのまま送信します。")
            uploaded_file.seek(0)
            img_b64 = base64.b64encode(uploaded_file.read()).decode("utf-8")
            file_data = {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
    else:
        try:
            with Image.open(uploaded_file) as raw_img:
                img = ImageOps.exif_transpose(raw_img)
                if img.mode != "RGB":
                    img = img.convert("RGB")
                
                max_dim = 1600
                if max(img.size) > max_dim:
                    scale = max_dim / max(img.size)
                    new_size = (int(img.width * scale), int(img.height * scale))
                    img = img.resize(new_size, Image.Resampling.LANCZOS)
                
                buffered = io.BytesIO()
                img.save(buffered, format="JPEG", quality=80, optimize=True)
                img_b64 = base64.b64encode(buffered.getvalue()).decode("utf-8")
                buffered.close()
                del img
                file_data = {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
        except Exception as e:
            logger.error(f"画像処理エラー: {str(e)}")
            raise ParseError(f"画像処理に失敗しました: {str(e)}")
    
    # API呼び出し（リトライ付き）
    @retry_with_backoff(max_retries=max_retries, base_delay=2)
    def call_openai_api():
        try:
            response = client.chat.completions.create(
                model="gpt-4o-mini",  # 最新モデルを使用
                messages=[
                    {"role": "system", "content": "あなたは請求書や領収書のデータ抽出専門家です。"},
                    {"role": "user", "content": [
                        {"type": "text", "text": JSON_PROMPT},
                        file_data
                    ]}
                ],
                response_format={"type": "json_object"}
            )
            parsed_data = json.loads(response.choices[0].message.content)
            return parsed_data[0] if isinstance(parsed_data, list) and len(parsed_data) > 0 else parsed_data
        except Exception as e:
            error_str = str(e)
            if "401" in error_str or "authentication" in error_str.lower():
                raise AuthenticationError(f"OpenAI認証エラー: {error_str}")
            elif "429" in error_str:
                raise RateLimitError(f"OpenAIレートリミット: {error_str}")
            elif "503" in error_str or "unavailable" in error_str.lower():
                raise ServiceUnavailableError(f"OpenAIサービス利用不可: {error_str}")
            else:
                raise ParseError(f"OpenAI解析エラー: {error_str}")
    
    return call_openai_api()

# ==========================================
# メールテキスト解析用
# ==========================================
def parse_email_text_with_gemini(email_text, api_key, max_retries=3):
    """Gemini APIを使用してメールテキストを解析"""
    if not api_key:
        raise ValueError("Gemini APIキーが未設定です。サイドバーで設定してください。")
    
    try:
        import google.generativeai as genai
    except ImportError:
        raise ImportError("google-genai パッケージが未導入です。")
    
    clean_key = "".join(c for c in api_key.strip() if 32 <= ord(c) <= 126)
    client = genai.Client(api_key=clean_key)
    
    # API呼び出し（リトライ付き）
    @retry_with_backoff(max_retries=max_retries, base_delay=2)
    def call_gemini_api():
        try:
            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=[email_text, JSON_PROMPT],
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
            error_str = str(e)
            if "503" in error_str or "UNAVAILABLE" in error_str:
                raise ServiceUnavailableError(f"Geminiサービス利用不可: {error_str}")
            elif "429" in error_str:
                raise RateLimitError(f"Geminiレートリミット: {error_str}")
            else:
                raise ParseError(f"Gemini解析エラー: {error_str}")
    
    return call_gemini_api()

# ==========================================
# 解析結果のバリデーション
# ==========================================
def validate_parsed_data(data):
    """解析結果のバリデーション"""
    if not isinstance(data, dict):
        raise ParseError("解析結果が辞書形式ではありません")
    
    # 必須フィールドのチェックとデフォルト値設定
    result = {
        "total_amount": data.get("total_amount", 0),
        "items": data.get("items", [])
    }
    
    # itemsのバリデーション
    if not isinstance(result["items"], list):
        result["items"] = []
    
    validated_items = []
    for item in result["items"]:
        if isinstance(item, dict):
            validated_items.append({
                "name": item.get("name", "不明"),
                "quantity": item.get("quantity", 1),
                "price": item.get("price", 0)
            })
        else:
            logger.warning(f"無効なアイテム形式: {item}")
    
    result["items"] = validated_items
    return result