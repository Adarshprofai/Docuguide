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
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.audio import MIMEAudio

# Optional PDF support using PyMuPDF (fitz)
try:
    import fitz
    PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False

# --- TESSERACT PATH (Smart Check for Windows vs Linux Cloud) ---
if os.name == 'nt':
    tesseract_path = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
    if os.path.exists(tesseract_path):
        pytesseract.pytesseract.tesseract_cmd = tesseract_path

# --- PAGE CONFIG & CSS (Teal #0F3D3E & Orange #F29F05) ---
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

# --- DATABASE SETUP (SQLite - Lock-Free with WAL Mode & Timeout) ---
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

def save_reminder_to_db(contact_info, doc_type, deadline, risk_level, next_steps):
    steps_str = ", ".join(next_steps) if isinstance(next_steps, list) else str(next_steps)
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute('''
            INSERT INTO reminders (chat_id, doc_type, deadline, risk_level, next_steps, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (str(contact_info), str(doc_type), str(deadline), str(risk_level), steps_str, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()

def get_all_reminders():
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT id, chat_id, doc_type, deadline, risk_level, created_at FROM reminders ORDER BY id DESC')
        return c.fetchall()

def delete_all_user_data():
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute('DELETE FROM reminders')
        conn.commit()

init_db()

# --- SESSION STATES ---
if "key_index" not in st.session_state:
    st.session_state.key_index = 0
if "analysis_result" not in st.session_state:
    st.session_state.analysis_result = None
if "current_file_name" not in st.session_state:
    st.session_state.current_file_name = None
if "show_date_input" not in st.session_state:
    st.session_state.show_date_input = False
if "chat_id" not in st.session_state:
    st.session_state.chat_id = ""
if "user_email" not in st.session_state:
    st.session_state.user_email = ""
if "bulk_results" not in st.session_state:
    st.session_state.bulk_results = []

# --- SECRETS HELPERS (Auto-Clean Token, URLs & Email) ---
def clean_token_str(raw_token):
    if not raw_token:
        return ""
    t = str(raw_token).strip().strip('"').strip("'")
    if t.startswith("bot"):
        t = t[3:]
    return re.sub(r'[^a-zA-Z0-9:_-]', '', t)

def get_bot_token():
    raw = st.secrets.get("TELEGRAM_BOT_TOKEN", "")
    return clean_token_str(raw)

def get_tg_api_url(token, method):
    clean_t = clean_token_str(token)
    return f"https://api.telegram.org/bot{clean_t}/{method}".strip()

def get_gemini_keys():
    keys_list = list(st.secrets.get("GEMINI_API_KEYS", []))
    if not keys_list and "GEMINI_API_KEY" in st.secrets:
        keys_list = [st.secrets["GEMINI_API_KEY"]]
    return [str(k).strip() for k in keys_list if str(k).strip()]

def get_email_credentials():
    sender = str(st.secrets.get("EMAIL_SENDER", "")).strip().strip('"').strip("'")
    # Clean spaces, quotes or invisible chars from Google 16-digit App Password
    raw_pass = str(st.secrets.get("EMAIL_PASSWORD", "")).strip().strip('"').strip("'")
    app_pass = re.sub(r'\s+', '', raw_pass)
    return sender, app_pass

# --- FULL SPEECH GENERATOR (Supports Hindi 'hi' & Marathi 'mr') ---
def build_full_speech(analysis, lang_code='hi'):
    parts = []
    doc_type = analysis.get('doc_type', 'कागज़')
    warnings = analysis.get('warning_signs', [])
    exp = analysis.get('explanation_local') or analysis.get('explanation_hindi', '')
    facts = analysis.get('key_facts', [])
    deadline = analysis.get('deadline')
    steps = analysis.get('next_steps', [])
    where = analysis.get('where_to_go', '')
    
    has_deadline = deadline and str(deadline).lower() not in ['null', 'none', 'कोई तारीख नहीं मिली', 'कोणतीही तारीख सापडली नाही', 'no date']

    if lang_code == 'mr':
        parts.append(f"हा दस्तऐवज {doc_type} आहे.")
        if warnings:
            parts.append("सावधान! या कागदपत्रात काही संशयास्पद बाबी आढळल्या आहेत: " + ". ".join(warnings) + ".")
        else:
            parts.append("या कागदपत्रात कोणतीही मोठी संशयास्पद बाब आढळली नाही.")
        if exp:
            parts.append(f"सोप्या भाषेत अर्थ: {exp}")
        if facts:
            parts.append("महत्त्वाची माहिती: " + ". ".join(facts) + ".")
        if has_deadline:
            parts.append(f"लक्षात ठेवा, याची अंतिम तारीख {deadline} आहे.")
        else:
            parts.append("यामध्ये कोणतीही अंतिम तारीख दिलेली नाही.")
        if steps:
            parts.append("पुढे तुम्हाला ही पावले उचलावी लागतील: " + ". ".join(steps) + ".")
        if where:
            parts.append(f"संपर्क करण्याचे ठिकाण: {where}")
    else:
        parts.append(f"यह कागज़ {doc_type} है।")
        if warnings:
            parts.append("सावधान! इस कागज़ में कुछ संदिग्ध संकेत मिले हैं: " + "। ".join(warnings) + "।")
        else:
            parts.append("इस कागज़ में कोई बड़ा संदिग्ध संकेत नहीं मिला है।")
        if exp:
            parts.append(f"सरल भाषा में मतलब: {exp}")
        if facts:
            parts.append("मुख्य जानकारी: " + "। ".join(facts) + "।")
        if has_deadline:
            parts.append(f"ध्यान दें, इसकी अंतिम तारीख {deadline} है।")
        else:
            parts.append("इसमें कोई अंतिम तारीख नहीं दी गई है।")
        if steps:
            parts.append("आगे आपको यह कदम उठाने चाहिए: " + "। ".join(steps) + "।")
        if where:
            parts.append(f"संपर्क करने का स्थान: {where}")
            
    return " ".join(parts)

def text_to_speech_audio(text, lang_code='hi'):
    try:
        tts_lang = 'mr' if lang_code == 'mr' else 'hi'
        tts = gTTS(text=text, lang=tts_lang)
        fp = io.BytesIO()
        tts.write_to_fp(fp)
        fp.seek(0)
        return fp
    except Exception:
        return None

# --- ROBUST FREE EMAIL SENDER (SSL Port 465 + Port 587 Fallback) ---
def send_email_alert(sender_email, app_password, to_email, subject, body_text, audio_bytes=None):
    if not sender_email or not app_password:
        return False, "Streamlit Secrets में EMAIL_SENDER या EMAIL_PASSWORD सेट नहीं है।"
    if not to_email or "@" not in to_email:
        return False, "कृपया सही Email ID डालें।"

    msg = MIMEMultipart()
    msg['From'] = sender_email
    msg['To'] = to_email.strip()
    msg['Subject'] = subject
    msg.attach(MIMEText(body_text, 'plain', 'utf-8'))

    if audio_bytes:
        try:
            audio_part = MIMEAudio(audio_bytes, _subtype="mp3")
            audio_part.add_header('Content-Disposition', 'attachment', filename="DocuGuide_Voice_Explanation.mp3")
            msg.attach(audio_part)
        except Exception:
            pass

    # Method 1: Try SMTP_SSL on Port 465 (Most reliable on Streamlit Cloud)
    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20, context=context) as server:
            server.ehlo()
            server.login(sender_email, app_password)
            server.send_message(msg)
        return True, "Email Sent via SSL (465)!"
    except smtplib.SMTPAuthenticationError:
        return False, "Gmail ने पासवर्ड रिजेक्ट कर दिया! कृपया नार्मल पासवर्ड की जगह Google का 16-अक्षरों वाला 'App Password' इस्तेमाल करें।"
    except Exception as e_ssl:
        # Method 2: Fallback to Port 587 with STARTTLS
        try:
            with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as server:
                server.ehlo()
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
                server.login(sender_email, app_password)
                server.send_message(msg)
            return True, "Email Sent via TLS (587)!"
        except smtplib.SMTPAuthenticationError:
            return False, "Gmail ने पासवर्ड रिजेक्ट कर दिया! कृपया नार्मल पासवर्ड की जगह Google का 16-अक्षरों वाला 'App Password' इस्तेमाल करें।"
        except Exception as e_tls:
            return False, f"{e_ssl} | Fallback: {e_tls}"

# --- TELEGRAM API FUNCTIONS ---
def fetch_latest_chat_id():
    token = get_bot_token()
    if not token or token == "Apna_Telegram_Token_Yahan_Dalein":
        return None, "कृपया secrets.toml में अपना असली TELEGRAM_BOT_TOKEN डालें।"
    url = get_tg_api_url(token, "getUpdates")
    try:
        res = requests.get(url, timeout=10).json()
        if res.get("ok") and len(res.get("result", [])) > 0:
            for item in reversed(res["result"]):
                if "message" in item and "chat" in item["message"]:
                    chat_id = str(item["message"]["chat"]["id"])
                    user_name = item["message"]["chat"].get("first_name", "User")
                    return chat_id, f"Connected: {user_name} ({chat_id})"
                elif "callback_query" in item and "message" in item["callback_query"]:
                    chat_id = str(item["callback_query"]["message"]["chat"]["id"])
                    return chat_id, f"Connected ({chat_id})"
        return None, "कोई नया मैसेज नहीं मिला। अपने Telegram Bot को खोलकर एक बार 'Hello' भेजें और फिर यह बटन दबाएं।"
    except Exception as e:
        return None, f"Error: {e}"

def send_telegram_message(token, chat_id, message_text, reply_markup=None):
    clean_t = clean_token_str(token)
    if not clean_t or not chat_id:
        return False, "Telegram Token या Chat ID खाली है।"
    url = get_tg_api_url(clean_t, "sendMessage")
    payload = {"chat_id": str(chat_id).strip(), "text": message_text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        res = requests.post(url, json=payload, timeout=10).json()
        if res.get("ok"):
            return True, "Message sent!"
        return False, res.get("description", "Failed to send")
    except Exception as e:
        return False, str(e)

def send_telegram_audio(token, chat_id, audio_fp, caption="🔊 संपूर्ण माहिती आवाजात ऐका / पूरी जानकारी आवाज़ में सुनें"):
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

def delayed_test_reminder(token, chat_id, sender_email, app_pass, to_email, doc_type, deadline, steps_str, lang_code='hi'):
    time.sleep(60)
    if lang_code == 'mr':
        subj = f"🔔 TEST REMINDER: {doc_type} (अंतिम तारीख: {deadline})"
        msg = (
            f"🔔 TEST REMINDER (1-Min Live Demo)\n\n"
            f"📄 दस्तऐवज: {doc_type}\n"
            f"⏳ अंतिम तारीख: {deadline}\n\n"
            f"👉 काय करावे: {steps_str}\n\n"
            f"(हे Docu Guide चे लाईव्ह डेमो रिमाइंडर आहे)"
        )
    else:
        subj = f"🔔 TEST REMINDER: {doc_type} (अंतिम तारीख: {deadline})"
        msg = (
            f"🔔 TEST REMINDER (1-Min Live Demo)\n\n"
            f"📄 कागज़: {doc_type}\n"
            f"⏳ अंतिम तारीख: {deadline}\n\n"
            f"👉 क्या करें: {steps_str}\n\n"
            f"(यह Docu Guide का लाइव डेमो रिमाइंडर है)"
        )
    if token and chat_id:
        send_telegram_message(token, chat_id, msg)
    if sender_email and app_pass and to_email:
        send_email_alert(sender_email, app_pass, to_email, subj, msg)

# --- OCR & MASKING FUNCTIONS ---
def load_images_from_upload(uploaded_file):
    images = []
    if uploaded_file.name.lower().endswith(".pdf"):
        if not PDF_SUPPORT:
            return None, "PDF पढ़ने के लिए टर्मिनल में 'pip install pymupdf' चलाएं।"
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
            try:
                text = pytesseract.image_to_string(img, lang='hin+mar+eng')
            except Exception:
                text = pytesseract.image_to_string(img, lang='hin+eng')
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

# --- GEMINI API WITH 5-KEY ROTATION & MULTI-LANGUAGE OUTPUT ---
_bg_key_idx = 0

def analyze_with_gemini(text, target_lang="Hindi", keys_list=None):
    global _bg_key_idx
    if keys_list is None:
        keys_list = get_gemini_keys()
    if not keys_list:
        return {"error": "secrets.toml में GEMINI_API_KEYS नहीं मिली।"}
        
    lang_instruction = "simple Marathi (मराठी)" if target_lang == "Marathi" else "simple Hindi (हिंदी)"
    
    prompt = f"""
    You are an expert legal and financial assistant for rural India. Analyze this document text.
    Write all user-facing explanations, key facts, warning signs, next steps, and where to go in {lang_instruction}.
    Return ONLY a valid JSON object with the exact keys below, no markdown formatting:
    {{
        "doc_type": "string (e.g., bank notice, land record, court summons, insurance letter, electricity bill, in {lang_instruction})",
        "key_facts": ["string (amount, names, dates, sender in {lang_instruction})"],
        "deadline": "string (ISO date YYYY-MM-DD) or null if not found",
        "risk_level": "string (URGENT, SOON, INFO)",
        "warning_signs": ["string (in {lang_instruction}: signs paper may be fake/scam) or empty list [] if none"],
        "explanation_local": "string (in {lang_instruction}, 4-6 short simple sentences explaining the paper)",
        "next_steps": ["string (3 short actionable steps in {lang_instruction})"],
        "where_to_go": "string (which office or helpline type in {lang_instruction}, general only)"
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
            if "429" in error_msg or "quota" in error_msg or "exhausted" in error_msg:
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
            
    return {"error": "माफ़ करें, सभी API keys की लिमिट खत्म हो गई है।"}

# ==============================================================================
# TWO-WAY TELEGRAM BOT LISTENER (Supports Hindi & Marathi + Interactive Buttons)
# ==============================================================================
def telegram_bot_polling_worker(token, keys_list):
    clean_t = clean_token_str(token)
    offset = None
    pending_docs = {}
    user_lang_pref = {}
    
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
            if offset:
                params["offset"] = offset
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
                        
                        if data == "set_lang:hi":
                            user_lang_pref[chat_id] = "hi"
                            send_telegram_message(clean_t, chat_id, "✅ आपकी भाषा हिंदी सेट हो गई है! अब किसी भी कागज़ का फोटो भेजें।")
                            continue
                        elif data == "set_lang:mr":
                            user_lang_pref[chat_id] = "mr"
                            send_telegram_message(clean_t, chat_id, "✅ तुमची भाषा मराठी सेट झाली आहे! आता कोणत्याही कागदपत्राचा फोटो पाठवा.")
                            continue
                            
                        u_lang = user_lang_pref.get(chat_id, "hi")
                        if data.startswith("confirm_yes:"):
                            final_date = data.split(":", 1)[1]
                            doc_info = pending_docs.get(chat_id, {"doc_type": "Document", "risk": "URGENT", "steps": ["समय पर शाखा जाएं"]})
                            
                            save_reminder_to_db(chat_id, doc_info["doc_type"], final_date, doc_info["risk"], doc_info["steps"])
                            steps_str = ", ".join(doc_info["steps"])
                            
                            if u_lang == "mr":
                                confirm_msg = (
                                    f"✅ Docu Guide: तुमचा रिमाइंडर सेट झाला आहे!\n\n"
                                    f"📑 दस्तऐवज: {doc_info['doc_type']}\n"
                                    f"⏳ अंतिम तारीख: {final_date}\n"
                                    f"👉 काय करावे: {steps_str}\n\n"
                                    f"🔔 आम्ही तुम्हाला तारखेच्या 7 दिवस, 3 दिवस, 1 दिवस आधी आणि त्या दिवशी सकाळी आठवण करून देऊ.\n"
                                    f"⏱️ (Live Demo: बरोबर 1 मिनिटानंतर तुम्हाला एक टेस्ट रिमाइंडर येईल!)"
                                )
                            else:
                                confirm_msg = (
                                    f"✅ Docu Guide: आपका रिमाइंडर सेट हो गया है!\n\n"
                                    f"📑 कागज़: {doc_info['doc_type']}\n"
                                    f"⏳ अंतिम तारीख: {final_date}\n"
                                    f"👉 क्या करें: {steps_str}\n\n"
                                    f"🔔 हम आपको तारीख से 7 दिन, 3 दिन, 1 दिन पहले और उस सुबह याद दिलाएंगे।\n"
                                    f"⏱️ (Live Demo: ठीक 1 मिनट बाद आपको एक टेस्ट रिमाइंडर भी आएगा!)"
                                )
                            send_telegram_message(clean_t, chat_id, confirm_msg)
                            
                            t = threading.Thread(target=delayed_test_reminder, args=(clean_t, chat_id, "", "", "", doc_info["doc_type"], final_date, steps_str, u_lang), daemon=True)
                            t.start()
                            
                        elif data == "confirm_no":
                            if chat_id not in pending_docs:
                                pending_docs[chat_id] = {"doc_type": "Document", "risk": "INFO", "steps": []}
                            pending_docs[chat_id]["awaiting_date"] = True
                            prompt_msg = "🗓️ कृपया योग्य अंतिम तारीख टाइप करून पाठवा (उदा. 2026-10-28):" if u_lang == "mr" else "🗓️ कृपया सही अंतिम तारीख टाइप करके भेजें (उदाहरण: 2026-10-28):"
                            send_telegram_message(clean_t, chat_id, prompt_msg)
                        continue

                    msg = item.get("message", {})
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    if not chat_id:
                        continue
                        
                    u_lang = user_lang_pref.get(chat_id, "hi")
                    
                    if "text" in msg and "photo" not in msg:
                        txt = msg["text"].strip()
                        
                        if pending_docs.get(chat_id, {}).get("awaiting_date"):
                            pending_docs[chat_id]["awaiting_date"] = False
                            final_date = txt
                            doc_info = pending_docs[chat_id]
                            
                            save_reminder_to_db(chat_id, doc_info["doc_type"], final_date, doc_info["risk"], doc_info["steps"])
                            steps_str = ", ".join(doc_info["steps"])
                            
                            confirm_msg = (
                                f"✅ Docu Guide: नई तारीख ({final_date}) के लिए रिमाइंडर सेट हो गया है!\n\n"
                                f"📑 कागज़: {doc_info['doc_type']}\n"
                                f"⏳ अंतिम तारीख: {final_date}\n\n"
                                f"🔔 हम आपको तारीख से 7 दिन, 3 दिन, 1 दिन पहले और उस सुबह याद दिलाएंगे।\n"
                                f"⏱️ (Live Demo: ठीक 1 मिनट बाद आपको एक टेस्ट रिमाइंडर भी आएगा!)"
                            )
                            send_telegram_message(clean_t, chat_id, confirm_msg)
                            t = threading.Thread(target=delayed_test_reminder, args=(clean_t, chat_id, "", "", "", doc_info["doc_type"], final_date, steps_str, u_lang), daemon=True)
                            t.start()
                            continue
                            
                        if txt.startswith("/start") or txt.lower() in ["hi", "hello", "lang"]:
                            welcome = (
                                f"🙏 नमस्ते / नमस्कार! Docu Guide Bot में आपका स्वागत है।\n\n"
                                f"📌 आपका Chat ID है: {chat_id}\n\n"
                                f"🌐 पहले अपनी भाषा चुनें (Choose your language below) और फिर किसी भी कागज़ का फोटो भेजें:"
                            )
                            lang_kb = {
                                "inline_keyboard": [[
                                    {"text": "🇮🇳 हिंदी में समझें", "callback_data": "set_lang:hi"},
                                    {"text": "🚩 मराठीत समजून घ्या", "callback_data": "set_lang:mr"}
                                ]]
                            }
                            send_telegram_message(clean_t, chat_id, welcome, reply_markup=lang_kb)
                            
                    if "photo" in msg:
                        wait_txt = "⏳ Docu Guide AI तुमचा कागद वाचत आहे... कृपया 10 सेकंद थांबा..." if u_lang == "mr" else "⏳ Docu Guide AI आपका कागज़ पढ़ रहा है... कृपया 10 सेकंड प्रतीक्षा करें..."
                        send_telegram_message(clean_t, chat_id, wait_txt)
                        
                        file_id = msg["photo"][-1]["file_id"]
                        f_info_url = get_tg_api_url(clean_t, "getFile")
                        f_info = requests.get(f_info_url, params={"file_id": file_id}, timeout=10).json()
                        file_path = f_info["result"]["file_path"]
                        
                        dl_url = f"https://api.telegram.org/file/bot{clean_t}/{file_path}".strip()
                        img_bytes = requests.get(dl_url, timeout=15).content
                        
                        img = Image.open(io.BytesIO(img_bytes))
                        raw_text = extract_text_from_images([img])
                        
                        if len(raw_text.strip()) < 20 or raw_text.startswith("OCR_ERROR"):
                            err_txt = "❌ फोटो स्पष्ट नाही. कृपया उजेडात स्पष्ट फोटो काढून पुन्हा पाठवा." if u_lang == "mr" else "❌ फोटो साफ नहीं है या अक्षर बहुत छोटे हैं। कृपया रोशनी में साफ फोटो खींच कर दोबारा भेजें।"
                            send_telegram_message(clean_t, chat_id, err_txt)
                        else:
                            safe_text = mask_sensitive_data(raw_text)
                            target_l = "Marathi" if u_lang == "mr" else "Hindi"
                            analysis = analyze_with_gemini(safe_text, target_lang=target_l, keys_list=keys_list)
                            
                            if "error" in analysis:
                                send_telegram_message(clean_t, chat_id, f"❌ Error: {analysis['error']}")
                            else:
                                d_type = analysis.get('doc_type', 'कागज़')
                                risk = analysis.get('risk_level', 'INFO')
                                exp = analysis.get('explanation_local') or analysis.get('explanation_hindi', '')
                                facts = "\n• ".join(analysis.get('key_facts', []))
                                d_val = analysis.get('deadline')
                                no_date_str = 'कोणतीही तारीख सापडली नाही' if u_lang == 'mr' else 'कोई तारीख नहीं मिली'
                                d_line = d_val if d_val and str(d_val).lower() != 'null' else no_date_str
                                steps_list = analysis.get('next_steps', [])
                                steps = "\n• ".join(steps_list)
                                where = analysis.get('where_to_go', '')
                                warnings = analysis.get('warning_signs', [])
                                
                                if u_lang == "mr":
                                    warn_txt = "⚠️ सावधान (संशयास्पद बाबी): " + ", ".join(warnings) if warnings else "✅ कोणतीही मोठी संशयास्पद बाब आढळली नाही"
                                    reply = (
                                        f"📑 दस्तऐवज: {d_type}\n"
                                        f"⚠️ Risk: {risk}\n"
                                        f"{warn_txt}\n\n"
                                        f"🗣️ सोप्या मराठीत अर्थ:\n{exp}\n\n"
                                        f"📌 महत्त्वाची माहिती:\n• {facts}\n\n"
                                        f"⏳ अंतिम तारीख: {d_line}\n\n"
                                        f"👉 पुढे काय करावे:\n• {steps}\n\n"
                                        f"🏢 कुठे जावे: {where}\n\n"
                                        f"⚠️ (ही केवळ सामान्य माहिती आहे, कायदेशीर सल्ला नाही.)"
                                    )
                                else:
                                    warn_txt = "⚠️ सावधान (संदिग्ध संकेत): " + ", ".join(warnings) if warnings else "✅ कोई बड़ा संदिग्ध संकेत नहीं मिला"
                                    reply = (
                                        f"📑 कागज़: {d_type}\n"
                                        f"⚠️ Risk: {risk}\n"
                                        f"{warn_txt}\n\n"
                                        f"🗣️ सरल भाषा में मतलब:\n{exp}\n\n"
                                        f"📌 मुख्य जानकारी:\n• {facts}\n\n"
                                        f"⏳ अंतिम तारीख: {d_line}\n\n"
                                        f"👉 आगे क्या करें:\n• {steps}\n\n"
                                        f"🏢 कहाँ जाएँ: {where}\n\n"
                                        f"⚠️ (यह केवल सामान्य जानकारी है, कानूनी सलाह नहीं।)"
                                    )
                                send_telegram_message(clean_t, chat_id, reply)
                                
                                full_speech = build_full_speech(analysis, lang_code=u_lang)
                                audio_fp = text_to_speech_audio(full_speech, lang_code=u_lang)
                                if audio_fp:
                                    cap = "🔊 संपूर्ण माहिती मराठी आवाजात ऐका" if u_lang == "mr" else "🔊 पूरी जानकारी हिंदी आवाज़ में सुनें"
                                    send_telegram_audio(clean_t, chat_id, audio_fp, caption=cap)
                                    
                                pending_docs[chat_id] = {
                                    "doc_type": d_type,
                                    "risk": risk,
                                    "steps": steps_list,
                                    "awaiting_date": False
                                }
                                
                                if d_line != no_date_str:
                                    yes_lbl = f"✅ होय, बरोबर आहे ({d_line})" if u_lang == "mr" else f"✅ हाँ, सही है ({d_line})"
                                    no_lbl = "❌ नाही, तारीख बदला" if u_lang == "mr" else "❌ नहीं, तारीख बदलें"
                                    q_txt = f"🗓️ ही अंतिम तारीख ({d_line}) बरोबर आहे का? रिमाइंडर सेट करण्यासाठी खालील बटन दाबा:" if u_lang == "mr" else f"🗓️ क्या यह अंतिम तारीख ({d_line}) सही है? रिमाइंडर सेट करने के लिए नीचे बटन दबाएं:"
                                    keyboard = {
                                        "inline_keyboard": [[
                                            {"text": yes_lbl, "callback_data": f"confirm_yes:{d_line}"},
                                            {"text": no_lbl, "callback_data": "confirm_no"}
                                        ]]
                                    }
                                    send_telegram_message(clean_t, chat_id, q_txt, reply_markup=keyboard)
                                else:
                                    add_lbl = "➕ स्वतः तारीख जोडा" if u_lang == "mr" else "➕ खुद रिमाइंडर तारीख जोड़ें"
                                    q_txt = "🗓️ या कागदपत्रात तारीख सापडली नाही. तुम्हाला स्वतः रिमाइंडर तारीख सेट करायची आहे का?" if u_lang == "mr" else "🗓️ इस कागज़ में कोई तारीख नहीं मिली। क्या आप खुद रिमाइंडर तारीख सेट करना चाहते हैं?"
                                    keyboard = {"inline_keyboard": [[{"text": add_lbl, "callback_data": "confirm_no"}]]}
                                    send_telegram_message(clean_t, chat_id, q_txt, reply_markup=keyboard)
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

# --- SIDEBAR: LANGUAGE, EMAIL, TELEGRAM & PRIVACY ---
with st.sidebar:
    lang_choice = st.radio(
        "🌐 भाषा / Language:",
        ["हिंदी (Hindi)", "मराठी (Marathi)", "English"]
    )
    is_mr = lang_choice == "मराठी (Marathi)"
    is_hi = lang_choice == "हिंदी (Hindi)"
    lang_code = "mr" if is_mr else "hi"
    target_ai_lang = "Marathi" if is_mr else "Hindi"
    
    st.markdown("---")
    st.header("📧 Free Email Reminder")
    st.caption("अपना Email डालें — रिमाइंडर और हिंदी/मराठी ऑडियो (MP3) सीधा आपके Email पर आएगा!")
    email_input = st.text_input("आपका Email ID:", value=st.session_state.user_email, placeholder="example@gmail.com")
    st.session_state.user_email = email_input.strip()

    if st.button("📨 Send Test Email Now"):
        s_mail, a_pass = get_email_credentials()
        if not st.session_state.user_email:
            st.warning("पहले ऊपर बॉक्स में अपना Email ID लिखें।")
        else:
            ok_test, msg_test = send_email_alert(
                s_mail,
                a_pass,
                st.session_state.user_email,
                "✅ Docu Guide Test Email",
                "बधाई हो! आपका Docu Guide Free Email सिस्टम एकदम सही काम कर रहा है।"
            )
            if ok_test:
                st.success(f"ईमेल भेज दिया गया! ({msg_test})")
            else:
                st.error(f"Error: {msg_test}")

    st.markdown("---")
    st.header("📲 Telegram Bot & Setup")
    if bot_running:
        st.success("🤖 24x7 Telegram Bot: ACTIVE\n(Bot पर /start भेजें, हिंदी या मराठी चुनें और सीधा फोटो भेजें!)")
    st.caption("1. Telegram में अपने Bot को खोलकर **/start** भेजें।\n2. नीचे अपना Chat ID डालें या Auto-Detect दबाएं।")
    
    if st.button("🔄 Auto-Detect My Chat ID"):
        cid, status_msg = fetch_latest_chat_id()
        if cid:
            st.session_state.chat_id = cid
            st.success(status_msg)
        else:
            st.warning(status_msg)
            
    chat_id_input = st.text_input("Telegram Chat ID:", value=st.session_state.chat_id, placeholder="e.g. 123456789")
    st.session_state.chat_id = chat_id_input.strip()
    
    st.markdown("---")
    st.header("⚡ Live Demo Mode")
    test_mode = st.toggle(
        "Test Mode (1-Min Reminder)",
        value=True,
        help="ON करने पर रिमाइंडर सेट करते ही तुरंत कन्फर्मेशन आएगा और ठीक 1 मिनट बाद Email और Telegram पर लाइव डेमो रिमाइंडर आएगा।"
    )
    
    st.markdown("---")
    st.header("🛡️ Privacy & Data")
    if st.button("🗑️ Delete My Data"):
        delete_all_user_data()
        st.session_state.analysis_result = None
        st.session_state.bulk_results = []
        st.success("✅ डेटा डिलीट कर दिया गया है! / सर्व डेटा डिलीट केला गेला आहे!")

# --- MAIN HEADER ---
st.title("📄 Docu Guide")
if is_mr:
    st.markdown("**कागद समजून घ्या, कोणतीही तारीख विसरू नका. (Understand the paper. Never miss the date.)**")
    st.info("⚠️ **अस्वीकरण (Disclaimer):** ही केवळ सामान्य माहिती आहे, कायदेशीर सल्ला नाही. कृपया संबंधित कार्यालयाशी संपर्क साधून खात्री करा.")
elif is_hi:
    st.markdown("**कागज़ समझें, कोई तारीख ना भूलें। (Understand the paper. Never miss the date.)**")
    st.info("⚠️ **अस्वीकरण (Disclaimer):** यह केवल सामान्य जानकारी है, कानूनी सलाह नहीं। कृपया संबंधित कार्यालय से पुष्टि करें।")
else:
    st.markdown("**Understand the paper. Never miss the date.**")
    st.info("⚠️ **Disclaimer:** This is an explanation, not legal advice. Please verify with the issuing office.")

# --- TABS: SINGLE PAPER VS BULK MODE ---
tab1_title = "📄 एक कागद वाचा (Single Paper)" if is_mr else ("📄 एक कागज़ पढ़ें (Single Paper)" if is_hi else "📄 Single Paper")
tab2_title = "📚 अनेक कागदपत्रे - CSC/Bank (Bulk Mode)" if is_mr else ("📚 एक साथ कई कागज़ - CSC/Bank (Bulk Mode)" if is_hi else "📚 Bulk Mode (CSC / Banks)")
tab1, tab2 = st.tabs([tab1_title, tab2_title])

# ==========================================
# TAB 1: SINGLE PAPER FLOW
# ==========================================
with tab1:
    up_label = "कागदपत्राचा फोटो किंवा PDF अपलोड करा" if is_mr else ("कागज़ का फोटो या PDF अपलोड करें" if is_hi else "Upload Photo or PDF of a paper")
    uploaded_file = st.file_uploader(up_label, type=["png", "jpg", "jpeg", "pdf"], key="single_uploader")

    if uploaded_file is not None:
        if st.session_state.current_file_name != uploaded_file.name:
            st.session_state.analysis_result = None
            st.session_state.current_file_name = uploaded_file.name
            st.session_state.show_date_input = False
            
        images, err = load_images_from_upload(uploaded_file)
        if err:
            st.error(err)
        elif images:
            st.image(images[0], caption="Uploaded Paper (Page 1)", width="stretch")
            
            btn_label = "🔍 कागद वाचा (Read Document)" if is_mr else ("🔍 कागज़ पढ़ें (Read Document)" if is_hi else "🔍 Read Document")
            if st.button(btn_label):
                spin_txt = "AI कागद वाचत आहे... कृपया प्रतीक्षा करा..." if is_mr else "AI कागज़ पढ़ रहा है... कृपया प्रतीक्षा करें..."
                with st.spinner(spin_txt):
                    raw_text = extract_text_from_images(images)
                    
                    if raw_text.startswith("OCR_ERROR"):
                        st.error("❌ Tesseract OCR Error. Please check installation.")
                        st.code(raw_text)
                    elif len(raw_text.strip()) < 20:
                        st.error("❌ फोटो साफ नहीं है / फोटो स्पष्ट नाही. कृपया साफ फोटो अपलोड करें।")
                    else:
                        safe_text = mask_sensitive_data(raw_text)
                        analysis = analyze_with_gemini(safe_text, target_lang=target_ai_lang)
                        
                        if "error" in analysis:
                            st.error(f"Error: {analysis['error']}")
                        else:
                            st.session_state.analysis_result = analysis

    # --- DISPLAY SINGLE RESULT ---
    if st.session_state.analysis_result is not None:
        analysis = st.session_state.analysis_result
        st.markdown("---")
        
        warnings = analysis.get('warning_signs', [])
        if len(warnings) > 0:
            w_title = "⚠️ Warning signs found (सावधान - संशयास्पद बाबी आढळल्या):" if is_mr else "⚠️ Warning signs found (सावधान - संदिग्ध संकेत मिले):"
            w_sub = "(हा कागद बनावट किंवा फसवणूक करणारा असू शकतो, कृपया कार्यालयातून खात्री करा)" if is_mr else "(यह कागज़ फर्जी या स्कैम हो सकता है, कृपया जारी करने वाले ऑफिस से जांच करें)"
            warning_html = f"""<div class="warning-box warning-red"><b>{w_title}</b><br>• {'<br>• '.join(warnings)}<br><i>{w_sub}</i></div>"""
            st.markdown(warning_html, unsafe_allow_html=True)
        else:
            ok_title = "✅ No major warning signs found (कोणतीही मोठी संशयास्पद बाब आढळली नाही)" if is_mr else "✅ No major warning signs found (कोई बड़ा संदिग्ध संकेत नहीं मिला)"
            warning_html = f"""<div class="warning-box warning-green"><b>{ok_title}</b></div>"""
            st.markdown(warning_html, unsafe_allow_html=True)
        
        key_facts_li = "".join([f"<li>{fact}</li>" for fact in analysis.get('key_facts', [])])
        next_steps_li = "".join([f"<li>{step}</li>" for step in analysis.get('next_steps', [])])
        deadline_val = analysis.get('deadline')
        no_date_label = 'कोणतीही तारीख सापडली नाही' if is_mr else 'कोई तारीख नहीं मिली'
        deadline_text = deadline_val if deadline_val and str(deadline_val).lower() != 'null' else no_date_label
        
        risk = analysis.get('risk_level', 'INFO').upper()
        badge_color = "#dc3545" if risk == "URGENT" else ("#ffc107" if risk == "SOON" else "#17a2b8")
        badge_text_color = "black" if risk == "SOON" else "white"
        exp_text = analysis.get('explanation_local') or analysis.get('explanation_hindi', '')
        
        lbl_meaning = "सोप्या मराठीत अर्थ:" if is_mr else "सरल हिंदी में मतलब:"
        lbl_facts = "📌 महत्त्वाची माहिती (Key Facts):" if is_mr else "📌 मुख्य जानकारी (Key Facts):"
        lbl_deadline = "⏳ अंतिम तारीख (Deadline):"
        lbl_steps = "👉 पुढे काय करावे (Next Steps):" if is_mr else "👉 आगे क्या करें (Next Steps):"
        lbl_where = "🏢 कुठे संपर्क साधावा (Where to go):" if is_mr else "🏢 कहाँ संपर्क करें (Where to go):"
        
        result_html = f"""<div class="info-card">
<h3>📑 {analysis.get('doc_type', 'Document')}</h3>
<span style="background-color:{badge_color}; color:{badge_text_color}; padding: 5px 12px; border-radius: 6px; font-weight:bold; font-size: 14px;">Risk: {risk}</span>
<p style="font-size: 18px; margin-top: 18px; line-height: 1.5;"><b>{lbl_meaning}</b><br>{exp_text}</p>
<b>{lbl_facts}</b><ul>{key_facts_li}</ul>
<b>{lbl_deadline}</b> <span style="color:#dc3545; font-weight:bold; font-size: 18px;">{deadline_text}</span><br><br>
<b>{lbl_steps}</b><ul>{next_steps_li}</ul>
<b>{lbl_where}</b> {analysis.get('where_to_go', '')}
</div>"""
        st.markdown(result_html, unsafe_allow_html=True)
        st.caption("⚠️ *This is an explanation, not legal advice. Please verify with the issuing office.*")

        aud_header = "🔊 **संपूर्ण माहिती मराठी आवाजात ऐका:**" if is_mr else "🔊 **पूरी जानकारी हिंदी आवाज़ में सुनें:**"
        st.markdown(aud_header)
        full_speech_text = build_full_speech(analysis, lang_code=lang_code)
        audio_fp = text_to_speech_audio(full_speech_text, lang_code=lang_code)
        audio_bytes = audio_fp.getvalue() if audio_fp else None
        if audio_fp:
            st.audio(audio_fp, format='audio/mp3')

        # --- QUICK EMAIL / TELEGRAM INPUT RIGHT BEFORE REMINDER BUTTONS ---
        st.markdown("---")
        st.subheader("🔔 रिमाइंडर कहाँ भेजें? (Where to send reminders?)")
        col_e, col_t = st.columns(2)
        with col_e:
            quick_email = st.text_input("📧 अपना Email ID डालें (100% Free):", value=st.session_state.user_email, placeholder="user@gmail.com", key="main_email_box")
            st.session_state.user_email = quick_email.strip()
        with col_t:
            quick_chat = st.text_input("📲 Telegram Chat ID (Optional):", value=st.session_state.chat_id, placeholder="e.g. 123456789", key="main_chat_box")
            st.session_state.chat_id = quick_chat.strip()

        # Helper to trigger & save reminder (Both Email & Telegram!)
        def trigger_reminder(final_date):
            doc_type = analysis.get('doc_type', 'Document')
            steps = analysis.get('next_steps', [])
            steps_str = ", ".join(steps) if isinstance(steps, list) else str(steps)
            
            contact_record = st.session_state.user_email or st.session_state.chat_id or "WebUser"
            save_reminder_to_db(contact_record, doc_type, final_date, risk, steps)
            st.success(f"✅ Reminders set: 7 days, 3 days, 1 day before, and the morning of the date ({final_date}).")
            
            if is_mr:
                subj = f"✅ Docu Guide रिमाइंडर: {doc_type} (अंतिम तारीख: {final_date})"
                confirm_msg = (
                    f"✅ Docu Guide: तुमचा रिमाइंडर सेट झाला आहे!\n\n"
                    f"📑 दस्तऐवज: {doc_type}\n"
                    f"⏳ अंतिम तारीख: {final_date}\n"
                    f"⚠️ Risk: {risk}\n"
                    f"🗣️ सोप्या भाषेत अर्थ: {exp_text}\n\n"
                    f"👉 काय करावे: {steps_str}\n\n"
                    f"आम्ही तुम्हाला तारखेच्या 7 दिवस, 3 दिवस, 1 दिवस आधी आणि त्या दिवशी सकाळी आठवण करून देऊ.\n"
                    f"(सोबत संपूर्ण माहितीची मराठी ऑडिओ क्लिप जोडली आहे)"
                )
            else:
                subj = f"✅ Docu Guide रिमाइंडर: {doc_type} (अंतिम तारीख: {final_date})"
                confirm_msg = (
                    f"✅ Docu Guide: रिमाइंडर सेट हो गया है!\n\n"
                    f"📑 कागज़: {doc_type}\n"
                    f"⏳ अंतिम तारीख: {final_date}\n"
                    f"⚠️ Risk: {risk}\n"
                    f"🗣️ सरल भाषा में मतलब: {exp_text}\n\n"
                    f"👉 क्या करें: {steps_str}\n\n"
                    f"हम आपको तारीख से 7 दिन, 3 दिन, 1 दिन पहले और उस सुबह याद दिलाएंगे।\n"
                    f"(साथ में पूरी जानकारी की ऑडियो क्लिप भी जुड़ी है)"
                )

            token = get_bot_token()
            sender_email, app_pass = get_email_credentials()
            sent_any = False

            # 1. Send Free Email Alert (With MP3 Audio Attachment!)
            if st.session_state.user_email:
                ok_mail, mail_resp = send_email_alert(sender_email, app_pass, st.session_state.user_email, subj, confirm_msg, audio_bytes)
                if ok_mail:
                    sent_any = True
                    st.success(f"📧 **{st.session_state.user_email}** पर रिमाइंडर ईमेल और वॉइस नोट (MP3) भेज दिया गया है!")
                else:
                    st.warning(f"📧 Email नहीं जा सका: {mail_resp}")

            # 2. Send Telegram Alert
            if st.session_state.chat_id and token:
                ok_tg, tg_resp = send_telegram_message(token, st.session_state.chat_id, confirm_msg)
                if ok_tg:
                    sent_any = True
                    if audio_fp:
                        send_telegram_audio(token, st.session_state.chat_id, audio_fp)
                    st.toast("📲 Telegram पर मैसेज और वॉइस नोट भेज दिया गया है!")
                else:
                    st.warning(f"📲 Telegram मैसेज नहीं गया: {tg_resp}")

            # 3. Start 1-Minute Live Demo Thread for Email & Telegram
            if sent_any and test_mode:
                st.info("⏱️ **Test Mode ON:** ठीक 1 मिनट बाद आपके Email और Telegram पर लाइव डेमो रिमाइंडर आएगा!")
                t = threading.Thread(
                    target=delayed_test_reminder,
                    args=(token, st.session_state.chat_id, sender_email, app_pass, st.session_state.user_email, doc_type, final_date, steps_str, lang_code),
                    daemon=True
                )
                t.start()
            elif not st.session_state.user_email and not st.session_state.chat_id:
                st.warning("⚠️ रिमाइंडर डेटाबेस में सेव हो गया है! अपने फोन पर अलर्ट पाने के लिए ऊपर अपना Email ID या Telegram Chat ID डालें।")

        # 4. Yes / No Date Confirmation
        if deadline_text != no_date_label:
            q_head = f"### 🗓️ ही अंतिम तारीख ({deadline_text}) बरोबर आहे का?" if is_mr else f"### 🗓️ क्या यह अंतिम तारीख ({deadline_text}) सही है?"
            st.markdown(q_head)
            col1, col2 = st.columns(2)
            with col1:
                yes_btn = "✅ होय, बरोबर आहे (Yes)" if is_mr else "✅ हाँ, सही है (Yes)"
                if st.button(yes_btn):
                    trigger_reminder(deadline_text)
                    st.session_state.show_date_input = False
            with col2:
                no_btn = "❌ नाही, तारीख बदला (No)" if is_mr else "❌ नहीं, तारीख बदलें (No)"
                if st.button(no_btn):
                    st.session_state.show_date_input = True
        else:
            st.markdown("### 🗓️ कोई तारीख नहीं मिली / तारीख सापडली नाही")
            if st.button("➕ तारीख जोड़ें (Add Date)"):
                st.session_state.show_date_input = True
                
        if st.session_state.show_date_input:
            default_val = deadline_text if deadline_text != no_date_label else "2026-10-28"
            new_date = st.text_input("सही तारीख लिखें / योग्य तारीख लिहा (YYYY-MM-DD):", value=default_val)
            if st.button("💾 रिमाइंडर सेव करें (Save & Set Reminders)"):
                if new_date:
                    trigger_reminder(new_date)
                    st.session_state.show_date_input = False

# ==========================================
# TAB 2: BULK MODE (CSC / BANKS / NGOs)
# ==========================================
with tab2:
    st.subheader("📚 Bulk Document Processor (CSC, बँक आणि NGO साठी)")
    st.markdown("एक साथ कई कागज़ अपलोड करें। Docu Guide सभी को पढ़कर टेबल बनाएगा, CSV डाउनलोड देगा और सबके रिमाइंडर सेट कर देगा।")
    
    bulk_files = st.file_uploader(
        "कई फोटो या PDF एक साथ चुनें (Select multiple files)",
        type=["png", "jpg", "jpeg", "pdf"],
        accept_multiple_files=True,
        key="bulk_uploader"
    )
    
    if bulk_files:
        if st.button(f"⚡ सभी {len(bulk_files)} कागज़ प्रोसेस करें ({target_ai_lang})"):
            results_list = []
            progress_bar = st.progress(0)
            
            for idx, b_file in enumerate(bulk_files):
                with st.spinner(f"Processing {b_file.name} ({idx+1}/{len(bulk_files)})..."):
                    imgs, err = load_images_from_upload(b_file)
                    if err or not imgs:
                        results_list.append({
                            "File": b_file.name,
                            "Doc Type": "Error",
                            "Deadline": "N/A",
                            "Risk": "ERROR",
                            "Summary": err or "Could not load file"
                        })
                    else:
                        r_text = extract_text_from_images(imgs)
                        if len(r_text.strip()) < 20 or r_text.startswith("OCR_ERROR"):
                            results_list.append({
                                "File": b_file.name,
                                "Doc Type": "Unclear Photo",
                                "Deadline": "N/A",
                                "Risk": "INFO",
                                "Summary": "फोटो साफ नहीं है"
                            })
                        else:
                            s_text = mask_sensitive_data(r_text)
                            res = analyze_with_gemini(s_text, target_lang=target_ai_lang)
                            if "error" in res:
                                results_list.append({
                                    "File": b_file.name,
                                    "Doc Type": "API Error",
                                    "Deadline": "N/A",
                                    "Risk": "ERROR",
                                    "Summary": res["error"]
                                })
                            else:
                                d_type = res.get("doc_type", "Document")
                                d_line = res.get("deadline") or "No Date"
                                r_level = res.get("risk_level", "INFO")
                                h_exp = res.get("explanation_local") or res.get("explanation_hindi", "")
                                steps = res.get("next_steps", [])
                                
                                results_list.append({
                                    "File": b_file.name,
                                    "Doc Type": d_type,
                                    "Deadline": d_line,
                                    "Risk": r_level,
                                    "Summary": h_exp
                                })
                                
                                if d_line not in ["No Date", "null", None]:
                                    contact_rec = st.session_state.user_email or st.session_state.chat_id or "BulkUser"
                                    save_reminder_to_db(contact_rec, d_type, d_line, r_level, steps)
                                    
                progress_bar.progress((idx + 1) / len(bulk_files))
                
            st.session_state.bulk_results = results_list
            st.success(f"✅ सभी {len(bulk_files)} कागज़ प्रोसेस हो गए और उनके रिमाइंडर शेड्यूल कर दिए गए हैं!")
            
            token = get_bot_token()
            sender_email, app_pass = get_email_credentials()
            bulk_msg = f"📚 Bulk Update: Docu Guide ने अभी {len(bulk_files)} कागज़ ({target_ai_lang}) प्रोसेस किए हैं और सभी के रिमाइंडर सेट कर दिए हैं।"
            if st.session_state.chat_id and token:
                send_telegram_message(token, st.session_state.chat_id, bulk_msg)
            if st.session_state.user_email and sender_email and app_pass:
                send_email_alert(sender_email, app_pass, st.session_state.user_email, "📚 Docu Guide: Bulk Processing Report", bulk_msg)

    if len(st.session_state.bulk_results) > 0:
        df = pd.DataFrame(st.session_state.bulk_results)
        st.dataframe(df[["File", "Doc Type", "Deadline", "Risk", "Summary"]], width="stretch")
        
        csv_data = df.to_csv(index=False).encode('utf-8-sig')
        st.download_button(
            label="📥 रिपोर्ट डाउनलोड करें (Download CSV)",
            data=csv_data,
            file_name=f"docu_guide_bulk_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
            mime="text/csv"
        )

# --- FOOTER: SAVED REMINDERS TABLE ---
st.markdown("---")
with st.expander("📂 सेव किए गए रिमाइंडर देखें (Saved Reminders in Database)"):
    saved = get_all_reminders()
    if len(saved) > 0:
        saved_df = pd.DataFrame(saved, columns=["ID", "User (Email/ChatID)", "Doc Type", "Deadline", "Risk Level", "Created At"])
        st.dataframe(saved_df, width="stretch")
    else:
        st.write("अभी कोई रिमाइंडर सेव नहीं है।")
