import streamlit as st
import pytesseract
from PIL import Image
import re
from google import genai
import json
from gtts import gTTS
import io
import sqlite3
import requests
import threading
import time
from datetime import datetime
import pandas as pd
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication

# Optional PDF support using PyMuPDF (fitz)
try:
    import fitz
    PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False

# --- TESSERACT PATH ---
if os.name == 'nt':
    tesseract_path = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
    if os.path.exists(tesseract_path):
        pytesseract.pytesseract.tesseract_cmd = tesseract_path

# --- PAGE CONFIG & CSS ---
st.set_page_config(page_title="Docu Guide", page_icon="📄", layout="centered")

st.markdown("""
    <style>
    h1, h2, h3 { color: #0F3D3E; }
    .stButton>button { background-color: #F29F05; color: white; font-weight: bold; font-size: 16px; border-radius: 8px; width: 100%; border: none; padding: 10px; }
    .stButton>button:hover { background-color: #0F3D3E; color: white; border: 2px solid #F29F05; }
    .warning-box { padding: 15px; border-radius: 8px; margin-bottom: 15px; border: 1px solid #ccc; font-size: 16px; }
    .warning-green { background-color: #d4edda; border-left: 6px solid #28a745; color: #155724; }
    .warning-red { background-color: #f8d7da; border-left: 6px solid #dc3545; color: #721c24; }
    .info-card { background-color: white; padding: 22px; border-radius: 12px; box-shadow: 0 4px 10px rgba(0,0,0,0.08); border-left: 6px solid #0F3D3E; color: #111111; margin-bottom: 20px; }
    .info-card li { color: #111111; font-size: 16px; margin-bottom: 6px; }
    </style>
""", unsafe_allow_html=True)

# --- DATABASE SETUP ---
DB_FILE = 'reminders.db'

def get_db_connection():
    conn = sqlite3.connect(DB_FILE, timeout=30.0, check_same_thread=False)
    conn.execute('PRAGMA journal_mode=WAL;')
    return conn

def init_db():
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT,
                doc_type TEXT,
                deadline TEXT,
                risk_level TEXT,
                next_steps TEXT,
                created_at TEXT
            )
        ''')
        conn.commit()

def save_reminder_to_db(chat_id, doc_type, deadline, risk_level, next_steps):
    steps_str = ", ".join(next_steps) if isinstance(next_steps, list) else str(next_steps)
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute('''
            INSERT INTO reminders (chat_id, doc_type, deadline, risk_level, next_steps, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (str(chat_id), str(doc_type), str(deadline), str(risk_level), steps_str, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()

def get_all_reminders():
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT id, doc_type, deadline, risk_level, created_at FROM reminders ORDER BY id DESC')
        return c.fetchall()

def delete_all_user_data():
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute('DELETE FROM reminders')
        conn.commit()

init_db()

# --- SECRETS HELPERS ---
def clean_token_str(raw_token):
    if not raw_token:
        return ""
    t = str(raw_token).strip().strip('"').strip("'")
    if t.startswith("bot"):
        t = t[3:]
    return re.sub(r'[^a-zA-Z0-9:_-]', '', t)

def get_bot_token():
    return clean_token_str(st.secrets.get("TELEGRAM_BOT_TOKEN", ""))

def get_tg_api_url(token, method):
    return f"https://api.telegram.org/bot{clean_token_str(token)}/{method}".strip()

def get_gemini_keys():
    keys_list = list(st.secrets.get("GEMINI_API_KEYS", []))
    if not keys_list and "GEMINI_API_KEY" in st.secrets:
        keys_list = [st.secrets["GEMINI_API_KEY"]]
    return [str(k).strip() for k in keys_list if str(k).strip()]

def get_email_creds():
    return st.secrets.get("EMAIL_SENDER", ""), st.secrets.get("EMAIL_PASSWORD", "")

# --- EMAIL SENDER (WITH AUDIO ATTACHMENT) ---
def send_email_with_audio(to_email, doc_type, deadline, risk, steps_str, audio_fp=None):
    sender, pwd = get_email_creds()
    if not sender or not pwd:
        return False, "Email credentials secrets.toml में नहीं मिले।"
        
    msg = MIMEMultipart()
    msg['From'] = sender
    msg['To'] = to_email
    msg['Subject'] = f"Docu Guide Reminder: {doc_type} (Deadline: {deadline})"
    
    body = f"""
    नमस्ते / नमस्कार,
    
    Docu Guide की ओर से आपका रिमाइंडर सेट हो गया है!
    
    📄 कागज़: {doc_type}
    ⏳ अंतिम तारीख: {deadline}
    ⚠️ Risk Level: {risk}
    
    👉 आगे क्या करें: 
    {steps_str}
    
    हम आपको तारीख से 7 दिन, 3 दिन, 1 दिन पहले और उस सुबह याद दिलाएंगे।
    (यह एक ऑटोमैटिक सिस्टम ईमेल है। पूरी जानकारी सुनने के लिए कृपया ईमेल में अटैच किया गया ऑडियो MP3 प्ले करें।)
    """
    msg.attach(MIMEText(body, 'plain'))
    
    if audio_fp:
        audio_fp.seek(0)
        part = MIMEApplication(audio_fp.read(), Name="DocuGuide_Audio.mp3")
        part['Content-Disposition'] = 'attachment; filename="DocuGuide_Audio.mp3"'
        msg.attach(part)
        
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, pwd)
            server.send_message(msg)
        return True, "Email Sent successfully!"
    except Exception as e:
        try:
            with smtplib.SMTP("smtp.gmail.com", 587) as server:
                server.starttls()
                server.login(sender, pwd)
                server.send_message(msg)
            return True, "Email Sent successfully via Port 587!"
        except Exception as e2:
            return False, f"Failed: {e2}"

# --- FULL REGIONAL VOICE GENERATOR ---
def build_full_speech(analysis, lang="hi"):
    parts = []
    doc_type = analysis.get('doc_type', 'कागज़')
    
    if lang == "mr":
        parts.append(f"हा कागद {doc_type} आहे.")
        warnings = analysis.get('warning_signs', [])
        if warnings:
            parts.append("सावधान! यात काही संशयास्पद बाबी आहेत: " + ". ".join(warnings) + ".")
        else:
            parts.append("यात कोणतीही मोठी संशयास्पद बाब आढळली नाही.")
            
        exp = analysis.get('explanation_regional', '')
        if exp:
            parts.append(f"सोप्या भाषेत अर्थ: {exp}")
            
        facts = analysis.get('key_facts', [])
        if facts:
            parts.append("मुख्य माहिती: " + ". ".join(facts) + ".")
            
        deadline = analysis.get('deadline')
        if deadline and str(deadline).lower() not in ['null', 'none', 'no date', 'कोई तारीख नहीं मिली', 'तारीख नाही']:
            parts.append(f"लक्षात ठेवा, याची अंतिम तारीख {deadline} आहे.")
        else:
            parts.append("यात कोणतीही अंतिम तारीख दिलेली नाही.")
            
        steps = analysis.get('next_steps', [])
        if steps:
            parts.append("पुढे तुम्ही ही पावले उचलावीत: " + ". ".join(steps) + ".")
            
        where = analysis.get('where_to_go', '')
        if where:
            parts.append(f"संपर्क करण्याचे ठिकाण: {where}")
    else:
        parts.append(f"यह कागज़ {doc_type} है।")
        warnings = analysis.get('warning_signs', [])
        if warnings:
            parts.append("सावधान! इस कागज़ में कुछ संदिग्ध संकेत मिले हैं: " + "। ".join(warnings) + "।")
        else:
            parts.append("इस कागज़ में कोई बड़ा संदिग्ध संकेत नहीं मिला है।")
            
        exp = analysis.get('explanation_regional', '')
        if exp:
            parts.append(f"सरल भाषा में मतलब: {exp}")
            
        facts = analysis.get('key_facts', [])
        if facts:
            parts.append("मुख्य जानकारी: " + "। ".join(facts) + "।")
            
        deadline = analysis.get('deadline')
        if deadline and str(deadline).lower() not in ['null', 'none', 'no date', 'कोई तारीख नहीं मिली', 'तारीख नाही']:
            parts.append(f"ध्यान दें, इसकी अंतिम तारीख {deadline} है।")
        else:
            parts.append("इसमें कोई अंतिम तारीख नहीं दी गई है।")
            
        steps = analysis.get('next_steps', [])
        if steps:
            parts.append("आगे आपको यह कदम उठाने चाहिए: " + "। ".join(steps) + "।")
            
        where = analysis.get('where_to_go', '')
        if where:
            parts.append(f"संपर्क करने का स्थान: {where}")
            
    return " ".join(parts)

def text_to_speech(text, lang_code='hi'):
    try:
        tts = gTTS(text=text, lang=lang_code)
        fp = io.BytesIO()
        tts.write_to_fp(fp)
        fp.seek(0)
        return fp
    except Exception:
        return None

# --- TELEGRAM API FUNCTIONS ---
def send_telegram_message(token, chat_id, message_text, reply_markup=None):
    clean_t = clean_token_str(token)
    if not clean_t or not chat_id:
        return False, "Missing Token or Chat ID"
    url = get_tg_api_url(clean_t, "sendMessage")
    payload = {"chat_id": str(chat_id).strip(), "text": message_text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        res = requests.post(url, json=payload, timeout=10).json()
        if res.get("ok"):
            return True, "Message sent!"
        return False, res.get("description", "Failed")
    except Exception as e:
        return False, str(e)

def send_telegram_audio(token, chat_id, audio_fp, caption="🔊 Voice Note"):
    clean_t = clean_token_str(token)
    if not clean_t or not chat_id or not audio_fp:
        return False
    url = get_tg_api_url(clean_t, "sendAudio")
    try:
        audio_fp.seek(0)
        files = {"audio": ("DocuGuide_Audio.mp3", audio_fp, "audio/mpeg")}
        data = {"chat_id": str(chat_id).strip(), "caption": caption}
        requests.post(url, data=data, files=files, timeout=20)
        return True
    except Exception:
        return False

def delayed_test_reminder(token, chat_id, to_email, doc_type, deadline, steps_str):
    time.sleep(60)
    msg = (
        f"🔔 TEST REMINDER (1-Min Live Demo)\n\n"
        f"📄 कागज़: {doc_type}\n"
        f"⏳ अंतिम तारीख: {deadline}\n\n"
        f"👉 क्या करें: {steps_str}\n\n"
        f"(यह Docu Guide का लाइव डेमो रिमाइंडर है)"
    )
    if token and chat_id:
        send_telegram_message(token, chat_id, msg)
    if to_email:
        send_email_with_audio(to_email, doc_type, deadline, "TEST ALERT", steps_str, audio_fp=None)

# --- OCR & MASKING FUNCTIONS ---
def load_images_from_upload(uploaded_file):
    images = []
    if uploaded_file.name.lower().endswith(".pdf"):
        if not PDF_SUPPORT:
            return None, "PDF support error"
        try:
            pdf_bytes = uploaded_file.read()
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            for page_num in range(len(doc)):
                page = doc.load_page(page_num)
                pix = page.get_pixmap(dpi=150)
                img = Image.open(io.BytesIO(pix.tobytes("png")))
                images.append(img)
            return images, None
        except Exception as e:
            return None, f"PDF Error: {e}"
    else:
        try:
            img = Image.open(uploaded_file)
            return [img], None
        except Exception as e:
            return None, f"Image Error: {e}"

def extract_text_from_images(images):
    full_text = ""
    try:
        for img in images:
            text = pytesseract.image_to_string(img, lang='hin+eng+mar')
            full_text += text + "\n"
        return full_text
    except Exception as e:
        return f"OCR_ERROR: {e}"

def mask_sensitive_data(text):
    text = re.sub(r'\b\d{4}[ -]?\d{4}[ -]?\d{4}\b', '[ID_REDACTED]', text)
    text = re.sub(r'\b[A-Z]{5}[0-9]{4}[A-Z]{1}\b', '[PAN_REDACTED]', text)
    text = re.sub(r'\b\d{10}\b', '[PHONE_REDACTED]', text)
    text = re.sub(r'\b\d{9,18}\b', '[ACCOUNT_REDACTED]', text)
    return text

def clean_json_response(raw_str):
    cleaned = re.sub(r'^`{3}(?:json)?\s*', '', raw_str.strip())
    cleaned = re.sub(r'\s*`{3}$', '', cleaned)
    return cleaned.strip()

# --- GEMINI API (WITH 503 ERROR HANDLING & KEYS ROTATION) ---
_bg_key_idx = 0

def analyze_with_gemini(text, keys_list=None, target_lang="Hindi"):
    global _bg_key_idx
    if keys_list is None:
        keys_list = get_gemini_keys()
    if not keys_list:
        return {"error": "secrets.toml में GEMINI_API_KEYS नहीं मिली।"}
        
    prompt = f"""
    You are an expert legal and financial assistant. Analyze this document text.
    Return ONLY a valid JSON object with the exact keys below, no markdown formatting.
    Ensure all text in 'explanation_regional', 'next_steps', and 'where_to_go' is in {target_lang}.
    {{
        "doc_type": "string (bank notice, land record, court summons, etc.)",
        "key_facts": ["string (amount, names, dates in {target_lang})"],
        "deadline": "string (ISO date YYYY-MM-DD) or null if not found",
        "risk_level": "string (URGENT, SOON, INFO)",
        "warning_signs": ["string (signs of scam/fake in {target_lang}) or empty list []"],
        "explanation_regional": "string (simple 4-6 short sentences explaining paper in {target_lang})",
        "next_steps": ["string (3 short steps in {target_lang})"],
        "where_to_go": "string (which office/helpline in {target_lang})"
    }}
    Document Text:
    {text}
    """
    
    total_keys = len(keys_list)
    for _ in range(total_keys):
        current_key = keys_list[_bg_key_idx % total_keys]
        client = genai.Client(api_key=current_key)
        
        try:
            response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
            result = clean_json_response(response.text)
            return json.loads(result)
            
        except Exception as e:
            error_msg = str(e).lower()
            # Yahan 503 / High Demand ka error pakda aur theek kiya jayega
            if "429" in error_msg or "quota" in error_msg or "exhausted" in error_msg or "503" in error_msg or "unavailable" in error_msg:
                time.sleep(2)  # 2 second break
                _bg_key_idx = (_bg_key_idx + 1) % total_keys
                continue
            if "404" in error_msg or "not found" in error_msg:
                try:
                    response = client.models.generate_content(model='gemini-1.5-flash', contents=prompt)
                    result = clean_json_response(response.text)
                    return json.loads(result)
                except Exception as fallback_e:
                    return {"error": f"Fallback Model Error: {str(fallback_e)}"}
            return {"error": str(e)}
            
    return {"error": "माफ़ करें, API सर्वर पर बहुत लोड है (Error 503)। कृपया कुछ सेकंड बाद फिर कोशिश करें।"}

# ==============================================================================
# TWO-WAY TELEGRAM BOT LISTENER
# ==============================================================================
def telegram_bot_polling_worker(token, keys_list):
    clean_t = clean_token_str(token)
    offset = None
    pending_docs = {}
    
    try:
        init_url = get_tg_api_url(clean_t, "getUpdates")
        init_res = requests.get(init_url, timeout=10).json()
        if init_res.get("ok") and init_res.get("result"):
            offset = init_res["result"][-1]["update_id"] + 1
    except Exception:
        pass

    while True:
        try:
            base_url = get_tg_api_url(clean_t, "getUpdates")
            params = {"timeout": 20}
            if offset: params["offset"] = offset
            res = requests.get(base_url, params=params, timeout=25).json()
            
            if res.get("ok"):
                for item in res.get("result", []):
                    offset = item["update_id"] + 1
                    
                    if "callback_query" in item:
                        cb = item["callback_query"]
                        cb_id = cb["id"]
                        chat_id = str(cb["message"]["chat"]["id"])
                        data = cb.get("data", "")
                        
                        ack_url = get_tg_api_url(clean_t, "answerCallbackQuery")
                        requests.post(ack_url, json={"callback_query_id": cb_id}, timeout=5)
                        
                        if data.startswith("lang:"):
                            lang_pref = data.split(":")[1]
                            if chat_id not in pending_docs: pending_docs[chat_id] = {}
                            pending_docs[chat_id]["lang"] = lang_pref
                            msg = "✅ भाषा सेट हो गई! अब मुझे अपने कागज़ का फोटो भेजें।" if lang_pref == "hindi" else "✅ भाषा सेट झाली! आता मला तुमच्या कागदपत्राचा फोटो पाठवा."
                            send_telegram_message(clean_t, chat_id, msg)
                            continue
                        
                        if data.startswith("confirm_yes:"):
                            final_date = data.split(":", 1)[1]
                            doc_info = pending_docs.get(chat_id, {"doc_type": "Document", "risk": "URGENT", "steps": []})
                            save_reminder_to_db(chat_id, doc_info["doc_type"], final_date, doc_info["risk"], doc_info["steps"])
                            steps_str = ", ".join(doc_info["steps"])
                            confirm_msg = f"✅ रिमाइंडर सेट हो गया है!\nतारीख: {final_date}\n\n(1 मिनट बाद टेस्ट रिमाइंडर आएगा)"
                            send_telegram_message(clean_t, chat_id, confirm_msg)
                            t = threading.Thread(target=delayed_test_reminder, args=(clean_t, chat_id, None, doc_info["doc_type"], final_date, steps_str), daemon=True)
                            t.start()
                            
                        elif data == "confirm_no":
                            if chat_id not in pending_docs: pending_docs[chat_id] = {"doc_type": "Document", "risk": "INFO", "steps": []}
                            pending_docs[chat_id]["awaiting_date"] = True
                            send_telegram_message(clean_t, chat_id, "🗓️ कृपया सही अंतिम तारीख टाइप करें (YYYY-MM-DD):")
                        continue

                    msg = item.get("message", {})
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    if not chat_id: continue
                        
                    if "text" in msg and "photo" not in msg:
                        txt = msg["text"].strip()
                        
                        if pending_docs.get(chat_id, {}).get("awaiting_date"):
                            pending_docs[chat_id]["awaiting_date"] = False
                            final_date = txt
                            doc_info = pending_docs[chat_id]
                            save_reminder_to_db(chat_id, doc_info["doc_type"], final_date, doc_info["risk"], doc_info["steps"])
                            steps_str = ", ".join(doc_info["steps"])
                            confirm_msg = f"✅ नई तारीख ({final_date}) के लिए रिमाइंडर सेट हो गया है!\n\n(1 मिनट बाद टेस्ट रिमाइंडर आएगा)"
                            send_telegram_message(clean_t, chat_id, confirm_msg)
                            t = threading.Thread(target=delayed_test_reminder, args=(clean_t, chat_id, None, doc_info["doc_type"], final_date, steps_str), daemon=True)
                            t.start()
                            continue
                            
                        if txt.startswith("/start") or txt.lower() in ["hi", "hello"]:
                            welcome = "🙏 नमस्ते! Docu Guide Bot में आपका स्वागत है।\n\nChoose language / भाषा चुनें / भाषा निवडा:"
                            keyboard = {"inline_keyboard": [[
                                {"text": "🇮🇳 हिंदी", "callback_data": "lang:hindi"},
                                {"text": "🚩 मराठी", "callback_data": "lang:marathi"}
                            ]]}
                            send_telegram_message(clean_t, chat_id, welcome, reply_markup=keyboard)
                            
                    if "photo" in msg:
                        send_telegram_message(clean_t, chat_id, "⏳ Docu Guide AI आपका कागज़ पढ़ रहा है...")
                        file_id = msg["photo"][-1]["file_id"]
                        f_info_url = get_tg_api_url(clean_t, "getFile")
                        f_info = requests.get(f_info_url, params={"file_id": file_id}, timeout=10).json()
                        dl_url = f"https://api.telegram.org/file/bot{clean_t}/{f_info['result']['file_path']}".strip()
                        img = Image.open(io.BytesIO(requests.get(dl_url, timeout=15).content))
                        raw_text = extract_text_from_images([img])
                        
                        if len(raw_text.strip()) < 20 or raw_text.startswith("OCR_ERROR"):
                            send_telegram_message(clean_t, chat_id, "❌ फोटो साफ नहीं है। दोबारा भेजें।")
                        else:
                            target_lang = pending_docs.get(chat_id, {}).get("lang", "hindi").capitalize()
                            safe_text = mask_sensitive_data(raw_text)
                            analysis = analyze_with_gemini(safe_text, keys_list, target_lang)
                            
                            if "error" in analysis:
                                send_telegram_message(clean_t, chat_id, f"❌ त्रुटि: {analysis['error']}")
                            else:
                                d_type = analysis.get('doc_type', 'Document')
                                risk = analysis.get('risk_level', 'INFO')
                                exp = analysis.get('explanation_regional', '')
                                facts = "\n• ".join(analysis.get('key_facts', []))
                                d_val = analysis.get('deadline')
                                d_line = d_val if d_val and str(d_val).lower() != 'null' else 'No Date'
                                steps_list = analysis.get('next_steps', [])
                                steps = "\n• ".join(steps_list)
                                where = analysis.get('where_to_go', '')
                                
                                reply = (
                                    f"📑 Type: {d_type}\n⚠️ Risk: {risk}\n\n"
                                    f"🗣️ Summary:\n{exp}\n\n"
                                    f"📌 Facts:\n• {facts}\n\n"
                                    f"⏳ Deadline: {d_line}\n\n"
                                    f"👉 Steps:\n• {steps}\n\n🏢 Go to: {where}"
                                )
                                send_telegram_message(clean_t, chat_id, reply)
                                
                                lang_code = 'mr' if target_lang.lower() == 'marathi' else 'hi'
                                full_speech = build_full_speech(analysis, lang_code)
                                audio_fp = text_to_speech(full_speech, lang_code)
                                if audio_fp:
                                    send_telegram_audio(clean_t, chat_id, audio_fp)
                                    
                                pending_docs[chat_id] = {"doc_type": d_type, "risk": risk, "steps": steps_list, "lang": target_lang, "awaiting_date": False}
                                
                                if d_line != 'No Date':
                                    keyboard = {"inline_keyboard": [[
                                        {"text": f"✅ Yes ({d_line})", "callback_data": f"confirm_yes:{d_line}"},
                                        {"text": "❌ No", "callback_data": "confirm_no"}
                                    ]]}
                                    send_telegram_message(clean_t, chat_id, f"🗓️ Is this deadline ({d_line}) correct?", reply_markup=keyboard)
        except Exception:
            time.sleep(3)
        time.sleep(1)

@st.cache_resource
def start_background_telegram_bot():
    token = get_bot_token()
    keys_list = get_gemini_keys()
    if token and token != "Apna_Telegram_Token_Yahan_Dalein" and keys_list:
        t = threading.Thread(target=telegram_bot_polling_worker, args=(token, keys_list), daemon=True)
        t.start()
        return True
    return False

bot_running = start_background_telegram_bot()

# --- UI SESSION STATES ---
if "analysis_result" not in st.session_state: st.session_state.analysis_result = None
if "current_file_name" not in st.session_state: st.session_state.current_file_name = None
if "show_date_input" not in st.session_state: st.session_state.show_date_input = False
if "chat_id" not in st.session_state: st.session_state.chat_id = ""
if "user_email" not in st.session_state: st.session_state.user_email = ""
if "bulk_results" not in st.session_state: st.session_state.bulk_results = []

# --- SIDEBAR ---
with st.sidebar:
    lang_choice = st.radio("🌐 भाषा / Language:", ["हिंदी (Hindi)", "मराठी (Marathi)", "English"], horizontal=True)
    target_lang = "Hindi" if "Hindi" in lang_choice else ("Marathi" if "Marathi" in lang_choice else "English")
    is_hi = target_lang == "Hindi"
    
    st.markdown("---")
    st.header("📧 Free Email Reminder")
    st.caption("अपना Email डालें — रिमाइंडर और ऑडियो सीधा आपके Email पर आएगा!")
    st.session_state.user_email = st.text_input("आपका Email ID:", value=st.session_state.user_email, placeholder="example@gmail.com").strip()
    
    if st.button("📨 Send Test Email Now"):
        if st.session_state.user_email:
            ok, msg = send_email_with_audio(st.session_state.user_email, "Test Document", "2026-10-28", "INFO", "Testing 123", None)
            if ok: st.success("✅ Email Sent!")
            else: st.error(msg)
        else:
            st.warning("Please enter Email ID.")
            
    st.markdown("---")
    st.header("📲 Telegram Chat ID")
    st.session_state.chat_id = st.text_input("Chat ID (Optional):", value=st.session_state.chat_id, placeholder="123456789").strip()
    
    st.markdown("---")
    test_mode = st.toggle("Test Mode (1-Min Reminder)", value=True)
    
    st.markdown("---")
    if st.button("🗑️ Delete My Data"):
        delete_all_user_data()
        st.session_state.analysis_result = None
        st.session_state.bulk_results = []
        st.success("✅ सारा डेटा डिलीट कर दिया गया है!")

# --- MAIN HEADER ---
st.title("📄 Docu Guide")
st.markdown("**कागज़ समझें, कोई तारीख ना भूलें। (Understand the paper. Never miss the date.)**")
st.info("⚠️ This is an explanation, not legal advice. Please verify with the issuing office.")

tab1, tab2 = st.tabs(["📄 एक कागज़ पढ़ें (Single Paper)", "📚 एक साथ कई कागज़ - CSC/Bank (Bulk Mode)"])

# ==========================================
# TAB 1: SINGLE PAPER FLOW
# ==========================================
with tab1:
    uploaded_file = st.file_uploader("कागज़ का फोटो या PDF अपलोड करें", type=["png", "jpg", "jpeg", "pdf"], key="single_uploader")

    if uploaded_file is not None:
        if st.session_state.current_file_name != uploaded_file.name:
            st.session_state.analysis_result = None
            st.session_state.current_file_name = uploaded_file.name
            st.session_state.show_date_input = False
            
        images, err = load_images_from_upload(uploaded_file)
        if err:
            st.error(err)
        elif images:
            if st.button("🔍 कागज़ पढ़ें (Read Document)"):
                with st.spinner("AI कागज़ पढ़ रहा है... कृपया प्रतीक्षा करें..."):
                    raw_text = extract_text_from_images(images)
                    if len(raw_text.strip()) < 20 or raw_text.startswith("OCR_ERROR"):
                        st.error("❌ फोटो साफ नहीं है। कृपया साफ फोटो अपलोड करें।")
                    else:
                        safe_text = mask_sensitive_data(raw_text)
                        analysis = analyze_with_gemini(safe_text, target_lang=target_lang)
                        if "error" in analysis: st.error(f"Error: {analysis['error']}")
                        else: st.session_state.analysis_result = analysis

    # --- DISPLAY SINGLE RESULT ---
    if st.session_state.analysis_result is not None:
        analysis = st.session_state.analysis_result
        st.markdown("---")
        
        warnings = analysis.get('warning_signs', [])
        if len(warnings) > 0:
            st.markdown(f"""<div class="warning-box warning-red"><b>⚠️ Warning signs found (सावधान):</b><br>• {'<br>• '.join(warnings)}</div>""", unsafe_allow_html=True)
        else:
            st.markdown("""<div class="warning-box warning-green"><b>✅ No major warning signs found (कोई बड़ा संदिग्ध संकेत नहीं मिला)</b></div>""", unsafe_allow_html=True)
        
        key_facts_li = "".join([f"<li>{fact}</li>" for fact in analysis.get('key_facts', [])])
        next_steps_li = "".join([f"<li>{step}</li>" for step in analysis.get('next_steps', [])])
        deadline_val = analysis.get('deadline')
        deadline_text = deadline_val if deadline_val and str(deadline_val).lower() != 'null' else 'कोई तारीख नहीं मिली'
        
        risk = analysis.get('risk_level', 'INFO').upper()
        badge_color = "#dc3545" if risk == "URGENT" else ("#ffc107" if risk == "SOON" else "#17a2b8")
        badge_text_color = "black" if risk == "SOON" else "white"
        
        result_html = f"""<div class="info-card">
<h3>📑 {analysis.get('doc_type', 'Document')}</h3>
<span style="background-color:{badge_color}; color:{badge_text_color}; padding: 5px 12px; border-radius: 6px; font-weight:bold; font-size: 14px;">Risk: {risk}</span>
<p style="font-size: 18px; margin-top: 18px; line-height: 1.5;"><b>सरल भाषा में मतलब:</b><br>{analysis.get('explanation_regional', '')}</p>
<b>📌 मुख्य जानकारी (Key Facts):</b><ul>{key_facts_li}</ul>
<b>⏳ अंतिम तारीख (Deadline):</b> <span style="color:#dc3545; font-weight:bold; font-size: 18px;">{deadline_text}</span><br><br>
<b>👉 आगे क्या करें (Next Steps):</b><ul>{next_steps_li}</ul>
<b>🏢 कहाँ संपर्क करें (Where to go):</b> {analysis.get('where_to_go', '')}
</div>"""
        st.markdown(result_html, unsafe_allow_html=True)
        st.caption("⚠️ This is an explanation, not legal advice.")

        lang_code = 'mr' if target_lang == "Marathi" else 'hi'
        st.markdown("🔊 **पूरी जानकारी आवाज़ में सुनें:**")
        full_speech_text = build_full_speech(analysis, lang_code)
        audio_fp = text_to_speech(full_speech_text, lang_code)
        if audio_fp:
            st.audio(audio_fp, format='audio/mp3')

        def trigger_reminder(final_date):
            doc_type = analysis.get('doc_type', 'Document')
            steps = analysis.get('next_steps', [])
            steps_str = ", ".join(steps) if isinstance(steps, list) else str(steps)
            
            save_reminder_to_db(st.session_state.chat_id, doc_type, final_date, risk, steps)
            st.success(f"✅ Reminders set for ({final_date}).")
            
            tg_token = get_bot_token()
            if st.session_state.user_email:
                ok, msg = send_email_with_audio(st.session_state.user_email, doc_type, final_date, risk, steps_str, audio_fp)
                if ok: st.success(f"📧 {st.session_state.user_email} पर रिमाइंडर ईमेल और वॉइस नोट (MP3) भेज दिया गया है!")
                else: st.error(f"Email Error: {msg}")
                
            if st.session_state.chat_id and tg_token:
                confirm_msg = f"✅ रिमाइंडर सेट हो गया है!\n\n📑 कागज़: {doc_type}\n⏳ तारीख: {final_date}\n👉 क्या करें: {steps_str}"
                send_telegram_message(tg_token, st.session_state.chat_id, confirm_msg)
                if audio_fp: send_telegram_audio(tg_token, st.session_state.chat_id, audio_fp)
                st.success("📲 Telegram पर मैसेज और ऑडियो भेज दिया गया है!")
                
            if test_mode and (st.session_state.user_email or st.session_state.chat_id):
                st.info("⏱️ Test Mode ON: ठीक 1 मिनट बाद आपके Email/Telegram पर लाइव डेमो रिमाइंडर आएगा!")
                t = threading.Thread(target=delayed_test_reminder, args=(tg_token, st.session_state.chat_id, st.session_state.user_email, doc_type, final_date, steps_str), daemon=True)
                t.start()

        st.markdown("---")
        if deadline_text != 'कोई तारीख नहीं मिली':
            st.markdown(f"### 🗓️ क्या यह अंतिम तारीख ({deadline_text}) सही है?")
            col1, col2 = st.columns(2)
            with col1:
                if st.button("✅ हाँ, सही है (Yes)"):
                    trigger_reminder(deadline_text)
                    st.session_state.show_date_input = False
            with col2:
                if st.button("❌ नहीं, तारीख बदलें (No)"):
                    st.session_state.show_date_input = True
        else:
            st.markdown("### 🗓️ कोई तारीख नहीं मिली। क्या आप खुद रिमाइंडर जोड़ना चाहते हैं?")
            if st.button("➕ तारीख जोड़ें (Add Date)"):
                st.session_state.show_date_input = True
                
        if st.session_state.show_date_input:
            default_val = deadline_text if deadline_text != 'कोई तारीख नहीं मिली' else "2026-10-28"
            new_date = st.text_input("सही तारीख लिखें (YYYY-MM-DD):", value=default_val)
            if st.button("💾 रिमाइंडर सेव करें (Save)"):
                if new_date:
                    trigger_reminder(new_date)
                    st.session_state.show_date_input = False

# ==========================================
# TAB 2: BULK MODE (WITH TIME.SLEEP FIX)
# ==========================================
with tab2:
    st.subheader("📚 Bulk Document Processor (CSC, बैंक आणि NGO साठी)")
    bulk_files = st.file_uploader("कई फोटो या PDF एक साथ चुनें", type=["png", "jpg", "jpeg", "pdf"], accept_multiple_files=True, key="bulk_uploader")
    
    if bulk_files:
        if st.button(f"⚡ सभी {len(bulk_files)} कागज़ प्रोसेस करें ({target_lang})"):
            results_list = []
            progress_bar = st.progress(0)
            
            for idx, b_file in enumerate(bulk_files):
                with st.spinner(f"Processing {b_file.name} ({idx+1}/{len(bulk_files)})..."):
                    imgs, err = load_images_from_upload(b_file)
                    if err or not imgs:
                        results_list.append({"File": b_file.name, "Doc Type": "Error", "Deadline": "N/A", "Risk": "ERROR", "Summary": err})
                    else:
                        r_text = extract_text_from_images(imgs)
                        if len(r_text.strip()) < 20 or r_text.startswith("OCR_ERROR"):
                            results_list.append({"File": b_file.name, "Doc Type": "Unclear", "Deadline": "N/A", "Risk": "INFO", "Summary": "फोटो साफ नहीं है"})
                        else:
                            s_text = mask_sensitive_data(r_text)
                            res = analyze_with_gemini(s_text, target_lang=target_lang)
                            if "error" in res:
                                results_list.append({"File": b_file.name, "Doc Type": "API Error", "Deadline": "N/A", "Risk": "ERROR", "Summary": res["error"]})
                            else:
                                d_type, d_line, r_level, h_exp, steps = res.get("doc_type"), res.get("deadline", "No Date"), res.get("risk_level", "INFO"), res.get("explanation_regional", ""), res.get("next_steps", [])
                                results_list.append({"File": b_file.name, "Doc Type": d_type, "Deadline": d_line, "Risk": r_level, "Summary": h_exp})
                                if d_line not in ["No Date", "null", None]:
                                    save_reminder_to_db(st.session_state.chat_id, d_type, d_line, r_level, steps)
                                    
                progress_bar.progress((idx + 1) / len(bulk_files))
                
                # ---> YAHAN 4 SECOND KA BREAK HAI (503 ERROR FIX) <---
                time.sleep(4)
                
            st.session_state.bulk_results = results_list
            st.success(f"✅ सभी {len(bulk_files)} कागज़ प्रोसेस हो गए और उनके रिमाइंडर शेड्यूल कर दिए गए हैं!")

    if len(st.session_state.bulk_results) > 0:
        df = pd.DataFrame(st.session_state.bulk_results)
        st.dataframe(df[["File", "Doc Type", "Deadline", "Risk", "Summary"]], width="stretch")
        csv_data = df.to_csv(index=False).encode('utf-8-sig')
        st.download_button("📥 रिपोर्ट डाउनलोड करें (Download CSV)", data=csv_data, file_name=f"docu_guide_bulk_{datetime.now().strftime('%Y%m%d_%H%M')}.csv", mime="text/csv")

# --- FOOTER ---
st.markdown("---")
with st.expander("📂 सेव किए गए रिमाइंडर देखें (Saved Reminders in Database)"):
    saved = get_all_reminders()
    if len(saved) > 0:
        st.dataframe(pd.DataFrame(saved, columns=["ID", "Doc Type", "Deadline", "Risk Level", "Created At"]), width="stretch")
    else:
        st.write("अभी कोई रिमाइंडर सेव नहीं है।")
