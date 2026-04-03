# ============================================================
# RASPBERRY PI CLIENT — เสียง + ภาพ + Ultrasonic Sensor + MongoDB
# กด U = เลือก/เปลี่ยนผู้ใช้ปัจจุบัน (ชื่อ + รหัส)
# กด S = เริ่มอัด | กด S อีกครั้ง = หยุดอัด + ส่ง AI + บันทึกลง DB
# กด Q = ปิดโปรแกรม
# ============================================================

import requests
import subprocess
import sys
import select
import termios
import tty
import os
import wave
import cv2
import time
import threading
import random
import uuid
from datetime import datetime, timezone

import RPi.GPIO as GPIO
from gtts import gTTS
from pymongo import MongoClient

# ============================================================
# API / FILE
# ============================================================
API_URL = "https://unequivocal-ardath-matrilaterally.ngrok-free.dev/analyze"
AUDIO_FILE = "input.wav"
AUDIO_DEVICE = os.getenv("AUDIO_DEVICE", "plughw:3,0")

# ============================================================
# MongoDB setup
# เหลือแค่ bear_users + bear_interactions
# ============================================================
MONGODB_URI = "mongodb://aomsin:aomsin@localhost:27017/aomsin?authSource=aomsin"
MONGO_DB = "aomsin"

mongo_client = None
db = None
users_collection = None
interactions_collection = None

# ============================================================
# Ultrasonic Sensor Pins
# ============================================================
TRIG_PIN = 17
ECHO_PIN = 27
LED_PIN = 4
DISTANCE_THRESHOLD = 100

# ============================================================
# MQ2 Smoke Sensor Pin
# ============================================================
MQ2_PIN = 22

SMOKE_WARNINGS = [
    "ระวังด้วยนะ คาปิตรวจพบควันแล้ว ตรวจสอบด้วยนะ",
    "มีควันนะ อันตรายมาก รีบออกไปข้างนอกได้เลย",
    "คาปิเจอควันแล้ว ระวังไฟไหม้ด้วยนะ",
    "ตรวจพบควัน รีบตรวจสอบแหล่งที่มาด้วยนะ",
]

GREETINGS = [
    "สวัสดีจ้า คาปิยินดีต้อนรับเลย",
    "ว้าว มีคนมาหาคาปิด้วย ยินดีต้อนรับนะ",
    "คาปิสังเกตว่ามีคนเข้ามาใกล้ สวัสดีจ้า",
    "โอ้โห มีแขกมาเยี่ยม ยินดีต้อนรับเลยนะ",
    "สวัสดี คาปิพร้อมคุยแล้ว กด S เพื่อพูดได้เลย",
]

# ============================================================
# Shared State
# ============================================================
quit_flag = threading.Event()
recording_flag = threading.Event()
last_frame = None
frame_lock = threading.Lock()

pressed_key = None
key_lock = threading.Lock()

last_greet_time = 0
GREET_COOLDOWN = 10
person_was_close = False
speaking_flag = threading.Event()
face_detected = False

last_smoke_time = 0
SMOKE_COOLDOWN = 15

# terminal / input state
NORMAL_TERMIOS = None
CBREAK_TERMIOS = None
terminal_mode_lock = threading.Lock()
text_input_mode = threading.Event()

gpio_ready = False

# ระบุตัวผู้ใช้งานปัจจุบัน
current_person_id = None
current_person_name = None
current_person_code = None
current_session_id = None
session_started_at = None
SESSION_TIMEOUT_SECONDS = 30 * 60  # 30 นาที
identity_lock = threading.Lock()

# ============================================================
# Helpers
# ============================================================
def utcnow():
    return datetime.now(timezone.utc)


def safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def clean_with_backspace(text: str) -> str:
    if not text:
        return ""
    result = []
    for ch in text:
        if ch in ("\x08", "\x7f"):
            if result:
                result.pop()
        elif ch.isprintable():
            result.append(ch)
    return "".join(result).strip()


def audio_duration_seconds(path: str):
    try:
        with wave.open(path, "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            if rate:
                return round(frames / float(rate), 2)
    except Exception:
        return None
    return None


def setup_terminal_modes():
    global NORMAL_TERMIOS, CBREAK_TERMIOS
    fd = sys.stdin.fileno()
    NORMAL_TERMIOS = termios.tcgetattr(fd)
    CBREAK_TERMIOS = termios.tcgetattr(fd)
    CBREAK_TERMIOS[3] = CBREAK_TERMIOS[3] & ~termios.ICANON
    CBREAK_TERMIOS[3] = CBREAK_TERMIOS[3] | termios.ECHO
    CBREAK_TERMIOS[6][termios.VMIN] = 1
    CBREAK_TERMIOS[6][termios.VTIME] = 0


def set_terminal_normal():
    if NORMAL_TERMIOS is None:
        return
    with terminal_mode_lock:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, NORMAL_TERMIOS)


def set_terminal_cbreak():
    if CBREAK_TERMIOS is None:
        return
    with terminal_mode_lock:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, CBREAK_TERMIOS)


def connect_mongodb():
    global mongo_client, db
    global users_collection, interactions_collection

    try:
        mongo_client = MongoClient(MONGODB_URI)
        db = mongo_client[MONGO_DB]

        users_collection = db["bear_users"]
        interactions_collection = db["bear_interactions"]

        users_collection.create_index("person_id", unique=True)
        users_collection.create_index("person_code", unique=True)

        interactions_collection.create_index([("person_id", 1), ("created_at", -1)])
        interactions_collection.create_index([("session_id", 1), ("created_at", 1)])
        interactions_collection.create_index([("week_key", 1)])
        interactions_collection.create_index([("month_key", 1)])
        interactions_collection.create_index([("day_key", 1)])

        print(f"✅ Connected to MongoDB | DB={MONGO_DB}")
        print("🗂 collections: bear_users, bear_interactions")
    except Exception as e:
        print(f"❌ MongoDB connection error: {e}")
        sys.exit(1)


def choose_person():
    global current_person_id, current_person_name, current_person_code
    global current_session_id, session_started_at

    if recording_flag.is_set():
        print("⚠ กำลังอัดเสียงอยู่ เปลี่ยนผู้ใช้ตอนนี้ไม่ได้")
        return

    try:
        text_input_mode.set()
        time.sleep(0.1)
        set_terminal_normal()

        print("--- กรอกข้อมูลผู้ใช้ ---", flush=True)
        person_name = clean_with_backspace(input("ชื่อผู้ใช้: "))
        person_code = clean_with_backspace(input("รหัสผู้ใช้: "))

        if not person_name:
            print("⚠ ชื่อห้ามว่าง")
            return
        if not person_code:
            print("⚠ รหัสห้ามว่าง")
            return

        user = users_collection.find_one({"person_code": person_code})

        if user:
            existing_name = clean_with_backspace((user.get("person_name") or "").strip())
            if existing_name and existing_name != person_name:
                print("⚠ รหัสนี้มีอยู่แล้ว แต่ชื่อไม่ตรง ข้อมูลอาจเป็นคนละคน")
                print(f"   ในระบบเป็นชื่อ: {existing_name}")
                return

            person_id = user["person_id"]
            users_collection.update_one(
                {"person_code": person_code},
                {
                    "$set": {
                        "person_name": person_name,
                        "updated_at": utcnow(),
                    }
                },
            )
        else:
            person_id = str(uuid.uuid4())
            users_collection.insert_one(
                {
                    "person_id": person_id,
                    "person_name": person_name,
                    "person_code": person_code,
                    "created_at": utcnow(),
                    "updated_at": utcnow(),
                }
            )

        with identity_lock:
            current_person_id = person_id
            current_person_name = person_name
            current_person_code = person_code
            current_session_id = str(uuid.uuid4())
            session_started_at = utcnow()

        print(f"✅ ผู้ใช้ปัจจุบัน: {current_person_name}")
        print(f"🧾 person_id: {current_person_id}")
        print(f"🧾 session_id: {current_session_id}")

    except Exception as e:
        print(f"❌ เปลี่ยนผู้ใช้ไม่สำเร็จ: {e}")
    finally:
        set_terminal_cbreak()
        text_input_mode.clear()


def ensure_active_person():
    if not current_person_id:
        print("\n⚠ ยังไม่ได้เลือกผู้ใช้")
        print("กด U ก่อน เพื่อกำหนดว่า Bear กำลังคุยกับใคร")
        return False
    return True


def ensure_active_session():
    global current_session_id, session_started_at

    if not current_person_id:
        return False

    now = utcnow()
    need_new_session = (
        current_session_id is None or
        session_started_at is None or
        (now - session_started_at).total_seconds() > SESSION_TIMEOUT_SECONDS
    )

    if need_new_session:
        current_session_id = str(uuid.uuid4())
        session_started_at = now

    return True


def close_current_session():
    return


def parse_analysis_payload(payload):
    face_analysis = payload.get("face_analysis") or payload.get("facial_analysis") or {}
    symptom_scores = payload.get("symptom_scores") or {}

    transcript = payload.get("transcript") or payload.get("text") or payload.get("user_text") or ""
    reply = payload.get("response") or payload.get("reply") or ""

    if not isinstance(face_analysis, dict):
        face_analysis = {}
    if not isinstance(symptom_scores, dict):
        symptom_scores = {}

    return {
        "transcript": transcript,
        "reply": reply,
        "face_analysis": {
            "label": face_analysis.get("label") or face_analysis.get("emotion") or "unknown",
            "score": safe_float(face_analysis.get("score") or face_analysis.get("confidence")),
            "raw": face_analysis,
        },
        "symptom_scores": {
            "stress": safe_float(symptom_scores.get("stress")),
            "depression": safe_float(symptom_scores.get("depression")),
            "anxiety": safe_float(symptom_scores.get("anxiety")),
            "fatigue": safe_float(symptom_scores.get("fatigue")),
        },
        "raw_payload": payload,
    }


def save_interaction_to_db(analysis_result, has_face, audio_file_size, image_file_size):
    if not ensure_active_person() or not ensure_active_session():
        return

    created_at = utcnow()
    day_key = created_at.strftime("%Y-%m-%d")
    week_key = created_at.strftime("%G-W%V")
    month_key = created_at.strftime("%Y-%m")

    doc = {
        "interaction_id": str(uuid.uuid4()),
        "person_id": current_person_id,
        "person_name": current_person_name or current_person_id,
        "person_code": current_person_code,
        "session_id": current_session_id,
        "created_at": created_at,
        "day_key": day_key,
        "week_key": week_key,
        "month_key": month_key,
        "source": "talking_bear",
        "has_face": bool(has_face),
        "audio_file_size": audio_file_size,
        "image_file_size": image_file_size,
        "transcript": analysis_result.get("transcript", ""),
        "bear_reply": analysis_result.get("reply", ""),
        "face_analysis": analysis_result.get("face_analysis", {}),
        "symptom_scores": analysis_result.get("symptom_scores", {}),
        "raw_payload": analysis_result.get("raw_payload", {}),
    }

    interactions_collection.insert_one(doc)
    print(f"✅ บันทึก interaction ลง DB แล้ว ของ {current_person_id}")

# ============================================================
# GPIO Setup
# ============================================================
GPIO.setmode(GPIO.BCM)
GPIO.setup(TRIG_PIN, GPIO.OUT)
GPIO.setup(ECHO_PIN, GPIO.IN)
GPIO.setup(LED_PIN, GPIO.OUT)
GPIO.setup(MQ2_PIN, GPIO.IN)
gpio_ready = True

# ============================================================
# Face Detector — โหลดครั้งเดียว
# ============================================================
face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')

# ============================================================
# 🔊 ฟังก์ชันพูดออกลำโพง
# ============================================================
def speak(text):
    speaking_flag.set()
    try:
        print(f"🔊 หมีพูด: {text}")
        tts = gTTS(text=text, lang='th', slow=False)
        tts.save("/tmp/bear_reply.mp3")
        subprocess.run(["mpg123", "-q", "/tmp/bear_reply.mp3"])
    except Exception as e:
        print(f"❌ TTS error: {e}")
    finally:
        speaking_flag.clear()

# ============================================================
# Key Listener Thread
# ============================================================
def key_listener():
    global pressed_key
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    try:
        while not quit_flag.is_set():
            if text_input_mode.is_set():
                time.sleep(0.05)
                continue

            if select.select([sys.stdin], [], [], 0.05)[0]:
                ch = sys.stdin.read(1).lower()
                with key_lock:
                    pressed_key = ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def get_key():
    global pressed_key
    with key_lock:
        k = pressed_key
        pressed_key = None
    return k

# ============================================================
# Camera Thread — พร้อมตรวจจับใบหน้า
# ============================================================
def camera_loop(cap):
    global last_frame, face_detected
    while not quit_flag.is_set():
        ret, frame = cap.read()
        if not ret:
            break

        with frame_lock:
            last_frame = frame.copy()

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.equalizeHist(gray)
        faces = face_cascade.detectMultiScale(
            gray,
            scaleFactor=1.05,
            minNeighbors=3,
            minSize=(40, 40)
        )
        face_detected = len(faces) > 0

        for (x, y, w, h) in faces:
            cv2.rectangle(frame, (x, y), (x+w, y+h), (255, 255, 0), 2)

        status = "REC" if recording_flag.is_set() else "READY (กด S เพื่ออัด)"
        color = (0, 0, 255) if recording_flag.is_set() else (0, 255, 0)
        cv2.putText(frame, status, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)

        face_status = f"Face: {'พบ' if face_detected else 'ไม่พบ'}"
        cv2.putText(frame, face_status, (10, 55), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (255, 255, 0) if face_detected else (100, 100, 100), 2)

        user_status = f"User: {current_person_name or '-'}"
        cv2.putText(frame, user_status, (10, 80), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (0, 255, 255), 2)

        try:
            cv2.imshow('Raspberry Pi', frame)
        except Exception:
            break

        cv2.waitKey(1)

    cv2.destroyAllWindows()

# ============================================================
# Ultrasonic — วัดระยะ
# ============================================================
def measure_distance():
    if not gpio_ready or quit_flag.is_set():
        return None

    try:
        GPIO.output(TRIG_PIN, GPIO.LOW)
        time.sleep(0.000002)
        GPIO.output(TRIG_PIN, GPIO.HIGH)
        time.sleep(0.00001)
        GPIO.output(TRIG_PIN, GPIO.LOW)

        timeout = time.time() + 0.04

        pulse_start = time.time()
        while GPIO.input(ECHO_PIN) == GPIO.LOW:
            pulse_start = time.time()
            if time.time() > timeout or quit_flag.is_set():
                return None

        pulse_end = time.time()
        while GPIO.input(ECHO_PIN) == GPIO.HIGH:
            pulse_end = time.time()
            if time.time() > timeout or quit_flag.is_set():
                return None

        duration = pulse_end - pulse_start
        distance = (duration * 34300) / 2
        return round(distance, 2)
    except Exception as e:
        if not quit_flag.is_set():
            print(f"⚠ Ultrasonic error: {e}")
        return None

# ============================================================
# Ultrasonic Thread
# ============================================================
def ultrasonic_loop():
    global last_greet_time, person_was_close

    while not quit_flag.is_set():
        if recording_flag.is_set() or speaking_flag.is_set():
            time.sleep(0.5)
            continue

        dist = measure_distance()

        if dist is None:
            time.sleep(0.1)
            continue

        now = time.time()
        is_close = dist <= DISTANCE_THRESHOLD
        try:
            if gpio_ready:
                GPIO.output(LED_PIN, GPIO.HIGH if is_close else GPIO.LOW)
        except Exception:
            pass

        if is_close and not person_was_close and face_detected and (now - last_greet_time) >= GREET_COOLDOWN:
            last_greet_time = now
            greeting = random.choice(GREETINGS)
            print(f"\n👋 [Ultrasonic+กล้อง] คนนั่งอยู่ ({dist} ซม.) เจอใบหน้า → ทักทาย")
            threading.Thread(target=speak, args=(greeting,), daemon=True).start()
        elif is_close and not person_was_close and not face_detected:
            print(f"🚶 [Ultrasonic] มีคนผ่าน ({dist} ซม.) แต่ไม่เจอใบหน้า → ไม่ทัก")

        person_was_close = is_close
        time.sleep(0.5)

# ============================================================
# MQ2 Thread — ตรวจจับควัน
# ============================================================
def smoke_loop():
    global last_smoke_time

    print("⏳ [MQ2] กำลังอุ่นเครื่องเซนเซอร์ 60 วินาที...")
    warmup_end = time.time() + 60
    while time.time() < warmup_end and not quit_flag.is_set():
        remaining = int(warmup_end - time.time())
        if remaining % 10 == 0:
            print(f"⏳ [MQ2] อีก {remaining} วินาที...")
        time.sleep(1)
    print("✅ [MQ2] เซนเซอร์พร้อมแล้ว!")

    STABLE_COUNT = 3
    smoke_count = 0

    while not quit_flag.is_set():
        try:
            smoke = GPIO.input(MQ2_PIN)
        except Exception as e:
            if not quit_flag.is_set():
                print(f"⚠ MQ2 error: {e}")
            return

        if smoke == 1:
            smoke_count += 1
            print(f"⚠️ [MQ2] พบควัน ({smoke_count}/{STABLE_COUNT})")
        else:
            smoke_count = 0

        if smoke_count >= STABLE_COUNT:
            now = time.time()
            if (now - last_smoke_time) >= SMOKE_COOLDOWN and not speaking_flag.is_set():
                last_smoke_time = now
                smoke_count = 0
                warning = random.choice(SMOKE_WARNINGS)
                print(f"🚨 [MQ2] ยืนยันพบควัน → {warning}")
                threading.Thread(target=speak, args=(warning,), daemon=True).start()

        time.sleep(1)

# ============================================================
# Start up
# ============================================================
setup_terminal_modes()
connect_mongodb()

print("--- กำลังเปิดกล้อง... ---")
cap = cv2.VideoCapture(0)
if not cap.isOpened():
    print("❌ เปิดกล้องไม่ได้!")
    GPIO.cleanup()
    sys.exit()

cap.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)
print("✅ เปิดกล้องสำเร็จ!")

key_thread = threading.Thread(target=key_listener, daemon=True)
cam_thread = threading.Thread(target=camera_loop, args=(cap,), daemon=True)
ultra_thread = threading.Thread(target=ultrasonic_loop, daemon=True)
smoke_thread = threading.Thread(target=smoke_loop, daemon=True)

key_thread.start()
cam_thread.start()
ultra_thread.start()
smoke_thread.start()

# ============================================================
# Main Loop
# ============================================================
print("\n🤖 พร้อมแล้ว!")
print("กด U = เลือก/เปลี่ยนผู้ใช้ (ชื่อ + รหัส)")
print("กด S = เริ่มอัด | กด S อีกครั้ง = หยุดอัด + ส่ง AI + บันทึก DB")
print("กด Q = ออกจากโปรแกรม")
print("🔊 Ultrasonic พร้อมแล้ว — คาปิจะทักทายเมื่อคนเข้าใกล้")
print("🚨 MQ2 พร้อมแล้ว — คาปิจะเตือนเมื่อตรวจพบควัน\n")

sox_proc = None

try:
    while True:
        ch = get_key()

        if ch == "q":
            raise KeyboardInterrupt

        if ch == "u":
            choose_person()

        if ch == "s":
            if not recording_flag.is_set():
                if not ensure_active_person():
                    continue

                print(f"\n🔴 เริ่มอัดแล้ว... ผู้ใช้ปัจจุบัน: {current_person_name}")
                print(f"🎙️ ใช้อุปกรณ์ไมค์: {AUDIO_DEVICE}")
                recording_flag.set()

                sox_proc = subprocess.Popen([
                    "sox",
                    "-t", "alsa", AUDIO_DEVICE,
                    "-r", "16000", "-c", "1", "-b", "16",
                    AUDIO_FILE
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            else:
                with frame_lock:
                    captured = last_frame.copy() if last_frame is not None else None

                recording_flag.clear()

                if sox_proc:
                    sox_proc.terminate()
                    sox_proc.wait()
                    sox_proc = None

                if not os.path.exists(AUDIO_FILE) or os.path.getsize(AUDIO_FILE) < 2000:
                    print("⚠ ไม่มีเสียง ลองใหม่อีกครั้ง")
                    continue

                duration = audio_duration_seconds(AUDIO_FILE)
                print(f"🎧 ไฟล์เสียง: {os.path.getsize(AUDIO_FILE)} bytes | duration: {duration} s")

                if captured is None:
                    print("⚠ ไม่มีภาพจากกล้อง")
                    continue

                ok, buf = cv2.imencode('.jpg', captured)
                if not ok:
                    print("⚠ เข้ารหัสภาพไม่สำเร็จ")
                    continue

                print(f"📤 ส่งเสียง + ภาพไป AI... ของผู้ใช้ {current_person_name}")

                try:
                    with open(AUDIO_FILE, "rb") as af:
                        response = requests.post(
                            API_URL,
                            files={
                                "audio": af,
                                "image": ("frame.jpg", buf.tobytes(), "image/jpeg")
                            },
                            data={
                                "person_id": current_person_id,
                                "person_name": current_person_name or "",
                                "person_code": current_person_code or "",
                                "session_id": current_session_id or "",
                                "source": "talking_bear",
                            },
                            timeout=60
                        )

                    if response.status_code == 200:
                        payload = response.json()
                        analysis_result = parse_analysis_payload(payload)
                        reply = analysis_result.get("reply", "")

                        print(f"🧠 transcript: {analysis_result.get('transcript', '')}")
                        print(f"🙂 face label: {analysis_result.get('face_analysis', {}).get('label')}")
                        print(f"🤖 AI: {reply}")

                        save_interaction_to_db(
                            analysis_result=analysis_result,
                            has_face=face_detected,
                            audio_file_size=os.path.getsize(AUDIO_FILE),
                            image_file_size=len(buf.tobytes()),
                        )

                        if reply:
                            speak(reply)
                    else:
                        print(f"❌ ติดต่อ AI ไม่สำเร็จ: status={response.status_code}")
                        print("ℹ️ รอบนี้ยังไม่บันทึก bear_interactions เพราะยังไม่ได้ผลวิเคราะห์จาก AI")
                        try:
                            print(response.text)
                        except Exception:
                            pass

                except Exception as e:
                    print(f"❌ ส่งข้อมูลไป AI/บันทึก DB ไม่สำเร็จ: {e}")
                    print("ℹ️ ถ้า error ก่อน response 200 ข้อมูล interaction จะยังไม่ถูก insert")

                print("\n(กด S เพื่ออัดรอบใหม่)")

        time.sleep(0.05)

except KeyboardInterrupt:
    print("\n🛑 กำลังปิดระบบ...")
    if sox_proc:
        sox_proc.terminate()
    recording_flag.clear()
    quit_flag.set()
    cam_thread.join(timeout=3)

finally:
    quit_flag.set()
    try:
        cap.release()
    except Exception:
        pass
    try:
        if gpio_ready:
            GPIO.output(LED_PIN, GPIO.LOW)
    except Exception:
        pass
    try:
        if gpio_ready:
            GPIO.cleanup()
    except Exception:
        pass
    if mongo_client:
        mongo_client.close()
    print("✅ ปิดระบบแล้ว")