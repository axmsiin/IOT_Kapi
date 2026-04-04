import streamlit as st
import pandas as pd
from pymongo import MongoClient
from streamlit_autorefresh import st_autorefresh
import json
from datetime import datetime

# --- 1. การตั้งค่าหน้าจอ ---
st.set_page_config(page_title="Kapiii Dashboard", page_icon="🧸", layout="wide")

# --- 2. ฟังก์ชันเชื่อมต่อ MongoDB ---
@st.cache_resource
def init_connection():o
    return MongoClient("mongodb://172.20.10.5:27017/", serverSelectionTimeoutMS=5000)

client = init_connection()
db = client['aomsin'] 
collection = db['bear_interactions']
# เพิ่มการเชื่อมต่อกับ collection สำหรับเก็บข้อมูล user
users_collection = db['bear_users'] 

# --- 3. ระบบจัดการ Session (Login State) ---
if 'logged_in' not in st.session_state:
    st.session_state.logged_in = False
if 'user_name' not in st.session_state:
    st.session_state.user_name = ""

# ฟังก์ชันสำหรับ Logout
def logout():
    st.session_state.logged_in = False
    st.session_state.user_name = ""
    st.rerun()

# --- หน้า Login (เช็คจาก Database) ---
if not st.session_state.logged_in:
    st.title("🔐 เข้าสู่ระบบเพื่อดู Dashboard")
    with st.container():
        user_input = st.text_input("กรุณาใส่ชื่อผู้ใช้งาน", key="login_input")
        password_input = st.text_input("กรุณาใส่รหัสผ่าน", type="password", key="password_input")
        
        if st.button("เข้าสู่ระบบ"):
            if user_input and password_input:
                # ค้นหา user ในคอลเลกชัน bear_users
                user_data = users_collection.find_one({
                    "person_name": user_input,
                    "person_code": password_input # ในระบบจริงควรใช้การ hash password
                })
                
                if user_data:
                    st.session_state.logged_in = True
                    st.session_state.user_name = user_input
                    st.success(f"ยินดีต้อนรับคุณ {user_input}!")
                    st.rerun()
                else:
                    st.error("ชื่อผู้ใช้งานหรือรหัสผ่านไม่ถูกต้อง ❌")
            else:
                st.error("กรุณากรอกชื่อผู้ใช้งานและรหัสผ่าน")
    st.stop() 

# --- 4. ส่วนของ Dashboard (เมื่อล็อกอินสำเร็จแล้ว) ---

# ระบบ Auto-refresh ทุก 10 วินาที
st_autorefresh(interval=10000, key="auto_refresh_data")

# Sidebar
st.sidebar.title(f"👤 ผู้ใช้: {st.session_state.user_name}")
st.sidebar.divider()

if st.sidebar.button("🔄 ดึงข้อมูลล่าสุด (Refresh)", use_container_width=True):
    st.toast("กำลังดึงข้อมูลล่าสุดจาก MongoDB...", icon="📥")

if st.sidebar.button("🚪 ออกจากระบบ", use_container_width=True):
    logout()

st.title(f"🧸 Dashboard: {st.session_state.user_name}")
st.write(f"อัปเดตข้อมูลเมื่อ: {datetime.now().strftime('%H:%M:%S')}")

# --- 5. ฟังก์ชันดึงข้อมูลตาม User ---
def get_filtered_data(name):
    # ดึงข้อมูลจาก bear_interactions โดยกรองตามชื่อคน
    query = {"person_name": name}
    items = list(collection.find(query).sort("created_at", -1).limit(50))
    return pd.DataFrame(items)

try:
    df = get_filtered_data(st.session_state.user_name)

    if not df.empty:
        # --- ดึงข้อมูลล่าสุดมาหาอารมณ์และคำตอบ ---
        latest = df.iloc[0]
        
        emotion = "ปกติ"
        raw_payload = latest.get('raw_payload', {})
        if isinstance(raw_payload, str):
            try:
                raw_payload = json.loads(raw_payload)
            except: pass
        
        if isinstance(raw_payload, dict):
            emotion = raw_payload.get('face_emotion', 'ปกติ')

        emoji_map = {"มีความสุข": "😊", "เสียใจ": "😢", "โกรธ": "😡", "เหนื่อย": "😫", "ปกติ": "🙂", "ไม่พบใบหน้า": "😶"}
        current_emoji = emoji_map.get(emotion, "🧸")

        col_face, col_info = st.columns([1, 2])
        
        with col_face:
            st.markdown(f"<h1 style='text-align: center; font-size: 150px;'>{current_emoji}</h1>", unsafe_allow_html=True)
            st.markdown(f"<h3 style='text-align: center;'>ความรู้สึก: {emotion}</h3>", unsafe_allow_html=True)
            
        with col_info:
            st.subheader("💬 น้องหมีพูดว่า:")
            bear_reply = latest.get('bear_reply', '...')
            st.info(f"{bear_reply}")
            
            st.divider()
            st.metric("จำนวนครั้งที่คุยกัน", len(df))

        st.subheader("📋 ประวัติการโต้ตอบ 50 รายการล่าสุด")
        display_cols = ['created_at', 'bear_reply', 'source']
        existing = [c for c in display_cols if c in df.columns]
        st.dataframe(df[existing], use_container_width=True)

    else:
        st.warning(f"ยังไม่มีข้อมูลสำหรับคุณ '{st.session_state.user_name}' ในฐานข้อมูล")
        st.info("กรุณาลองส่งข้อมูลจากเครื่อง Raspberry Pi เข้ามาในชื่อนี้")

except Exception as e:
    st.error(f"เกิดข้อผิดพลาดในการดึงข้อมูล: {e}")

st.sidebar.markdown("---")
st.sidebar.caption("Status: 🟢 Connected to MongoDB")