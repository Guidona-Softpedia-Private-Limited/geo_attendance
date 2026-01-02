import os
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime
import asyncio
import json
from typing import List, Dict, Any
import csv
import io

app = FastAPI()

# ---------------- GLOBAL DATA STORAGE ----------------
# Store device logs (persistent)
LOGS: List[str] = []
# Store all parsed attendance records (persistent)
ATTENDANCE_DATA: List[Dict[str, Any]] = []
# Display strings for recent attendance
RECENT_ATTENDANCE_DISPLAY: List[str] = []
# Command queue (limited to attendance-related commands)
COMMAND_QUEUE: List[str] = []
# Device information
DEVICE_SN = "Unknown"
DEVICE_INFO: Dict[str, str] = {}
# Persistent storage files
DATA_FILE = "attendance_data.json"
LOG_FILE = "device_logs.txt"
# Flags and status
IS_FETCHING_ALL_LOGS = False
DEVICE_CONNECTED = False
LAST_DEVICE_CONTACT = None

# ---------------- PERSISTENT STORAGE FUNCTIONS ----------------
def load_persistent_data():
    """Load saved attendance and device data from files."""
    global ATTENDANCE_DATA, LOGS, DEVICE_SN, DEVICE_INFO
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r') as f:
                data = json.load(f)
                ATTENDANCE_DATA = data.get('attendance', [])
                DEVICE_SN = data.get('device_sn', "Unknown")
                DEVICE_INFO = data.get('device_info', {})
                print(f"📂 Loaded {len(ATTENDANCE_DATA)} attendance records")
    except Exception as e:
        print(f"⚠️ Error loading attendance data: {e}")

    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, 'r') as f:
                LOGS = [line.strip() for line in f.readlines() if line.strip()]
                print(f"📂 Loaded {len(LOGS)} log entries")
    except Exception as e:
        print(f"⚠️ Error loading logs: {e}")

    update_recent_attendance_display()

def save_persistent_data():
    """Save attendance and device data to files."""
    try:
        data = {
            'attendance': ATTENDANCE_DATA,
            'device_sn': DEVICE_SN,
            'device_info': DEVICE_INFO,
            'last_updated': datetime.now().isoformat()
        }
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"⚠️ Error saving attendance data: {e}")

    try:
        # Limit logs to last 5000 to prevent file growth
        logs_to_save = LOGS[-5000:]
        with open(LOG_FILE, 'w') as f:
            for log_entry in logs_to_save:
                f.write(log_entry + "\n")
    except Exception as e:
        print(f"⚠️ Error saving logs: {e}")

def update_recent_attendance_display():
    """Update display list for recent attendance (last 100 records)."""
    global RECENT_ATTENDANCE_DISPLAY
    RECENT_ATTENDANCE_DISPLAY = [
        f"{r.get('user_id', 'N/A')}\t{r.get('timestamp', 'N/A')}\t{r.get('status', 'N/A')}\t{r.get('verification', 'N/A')}\t{r.get('workcode', 'N/A')}"
        for r in ATTENDANCE_DATA[-100:]
    ]

# ---------------- LOGGING AND PARSING FUNCTIONS ----------------
def log(msg: str):
    """Add timestamped log entry and save periodically."""
    ts = f"{datetime.now().isoformat()}Z - {msg}"
    print(ts)
    LOGS.append(ts)
    if len(LOGS) % 10 == 0:
        save_persistent_data()

def parse_attendance_line(line: str) -> Dict[str, Any]:
    """Parse tab-separated attendance line into a dictionary."""
    parts = line.split('\t')
    if len(parts) < 3:
        return {}

    record = {
        'user_id': parts[0],
        'timestamp': parts[1],
        'status': parts[2],
        'verification': parts[3] if len(parts) > 3 else '',
        'workcode': parts[4] if len(parts) > 4 else '',
        'received_at': datetime.utcnow().isoformat(),
        'raw': line
    }

    status_map = {
        '0': 'Check-in', '1': 'Check-out', '2': 'Break-out', '3': 'Break-in',
        '4': 'Overtime-in', '5': 'Overtime-out', '255': 'Error'
    }
    record['status_text'] = status_map.get(record['status'], 'Unknown')

    return record

# ---------------- BACKGROUND TASKS ----------------
async def auto_send_commands():
    """Periodically queue 'GET ATTLOG ALL' if device is active and queue is empty."""
    while True:
        try:
            if LAST_DEVICE_CONTACT and (datetime.now() - LAST_DEVICE_CONTACT).total_seconds() < 300:
                if not COMMAND_QUEUE:
                    COMMAND_QUEUE.append("GET ATTLOG ALL")
                    log("🔄 Auto-queued GET ATTLOG ALL")
            await asyncio.sleep(30)
        except Exception as e:
            log(f"⚠️ Error in auto_send_commands: {e}")
            await asyncio.sleep(60)

async def periodic_save():
    """Save data every 60 seconds."""
    while True:
        await asyncio.sleep(60)
        save_persistent_data()

async def check_device_status():
    """Monitor device connection status."""
    global DEVICE_CONNECTED
    while True:
        await asyncio.sleep(30)
        if LAST_DEVICE_CONTACT and (datetime.utcnow() - LAST_DEVICE_CONTACT).total_seconds() > 120:
            if DEVICE_CONNECTED:
                DEVICE_CONNECTED = False
                log("⚠️ Device connection lost")

@app.on_event("startup")
async def startup_event():
    """Load data and start background tasks on startup."""
    load_persistent_data()
    asyncio.create_task(auto_send_commands())
    asyncio.create_task(periodic_save())
    asyncio.create_task(check_device_status())
    log("🚀 Simplified eSSL Probe Started - Focus: All Attendance Logs")

# ---------------- UI ROUTES ----------------
templates = Jinja2Templates(directory="templates")

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Render simplified dashboard with live and all attendance cards."""
    today = datetime.utcnow().date()
    today_records = len([r for r in ATTENDANCE_DATA if r.get('timestamp', '').startswith(today.strftime('%Y-%m-%d'))])
    unique_users = len(set(r.get('user_id', '') for r in ATTENDANCE_DATA))

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "logs": LOGS[-100:],
            "recent_attendance": RECENT_ATTENDANCE_DISPLAY,
            "device_sn": DEVICE_SN,
            "total_records": len(ATTENDANCE_DATA),
            "today_records": today_records,
            "unique_users": unique_users,
            "fetching_all": IS_FETCHING_ALL_LOGS,
            "device_connected": DEVICE_CONNECTED
        }
    )

@app.get("/get_logs")
async def get_logs():
    """AJAX endpoint for updating UI data."""
    today = datetime.utcnow().date()
    today_records = len([r for r in ATTENDANCE_DATA if r.get('timestamp', '').startswith(today.strftime('%Y-%m-%d'))])
    unique_users = len(set(r.get('user_id', '') for r in ATTENDANCE_DATA))

    return {
        "logs": LOGS[-100:],
        "recent_attendance": RECENT_ATTENDANCE_DISPLAY,
        "attendance_count": len(ATTENDANCE_DATA),
        "today_count": today_records,
        "unique_users": unique_users,
        "device_sn": DEVICE_SN,
        "fetching_all": IS_FETCHING_ALL_LOGS,
        "device_connected": DEVICE_CONNECTED
    }

@app.get("/force_fetch_all")
async def force_fetch_all():
    """Queue commands to force fetch all attendance logs."""
    global COMMAND_QUEUE, IS_FETCHING_ALL_LOGS
    COMMAND_QUEUE = ["GET ATTLOG ALL"]
    IS_FETCHING_ALL_LOGS = True
    log("🚨 Force Fetch All Attendance Logs Queued")
    return PlainTextResponse("Force fetch initiated.")

# ---------------- DEVICE ENDPOINTS ----------------
async def log_request(request: Request, body: str):
    """Log incoming device request details."""
    global DEVICE_CONNECTED, LAST_DEVICE_CONTACT
    DEVICE_CONNECTED = True
    LAST_DEVICE_CONTACT = datetime.utcnow()

    log(f"DEVICE REQUEST: {request.url.path} from {request.client.host}")
    log(f"QUERY: {dict(request.query_params)}")
    log(f"BODY: {body[:500] + '...' if len(body) > 500 else body or '<empty>'}")

@app.api_route("/iclock/cdata.aspx", methods=["GET", "POST"])
async def iclock_cdata(request: Request):
    """Handle data pushes from device (attendance logs)."""
    body = (await request.body()).decode(errors="ignore")
    await log_request(request, body)

    if request.method == "GET":
        return PlainTextResponse("OK")

    lines = body.splitlines()
    attendance_count = 0

    for line in lines:
        line = line.strip()
        if not line:
            continue

        if "SN=" in line.upper():
            global DEVICE_SN
            DEVICE_SN = line.split("=")[1].strip()
            DEVICE_INFO['sn'] = DEVICE_SN
            log(f"📱 Device SN: {DEVICE_SN}")

        elif '\t' in line:
            record = parse_attendance_line(line)
            if record:
                record_key = f"{record['user_id']}_{record['timestamp']}_{record['status']}"
                if not any(f"{r.get('user_id')}_{r.get('timestamp')}_{r.get('status')}" == record_key for r in ATTENDANCE_DATA):
                    ATTENDANCE_DATA.append(record)
                    attendance_count += 1
                    log(f"✅ New Attendance: User {record['user_id']} at {record['timestamp']}")
                else:
                    log("⚠️ Duplicate attendance skipped")

    if attendance_count > 0:
        update_recent_attendance_display()
        save_persistent_data()
        log(f"🎉 Added {attendance_count} new records (Total: {len(ATTENDANCE_DATA)})")

    return PlainTextResponse("OK")

@app.get("/iclock/getrequest.aspx")
async def iclock_getrequest(request: Request):
    """Handle command pulls from device."""
    sn = request.query_params.get("SN", "")
    if sn:
        global DEVICE_SN
        DEVICE_SN = sn
        DEVICE_INFO['sn'] = DEVICE_SN
        log(f"📱 Device SN: {DEVICE_SN}")

    await log_request(request, "")

    if COMMAND_QUEUE:
        command = COMMAND_QUEUE.pop(0)
        log(f"📤 Sending Command: {command}")
        return PlainTextResponse(command)

    log("📤 No commands, default: GET ATTLOG ALL")
    return PlainTextResponse("GET ATTLOG ALL")

@app.get("/iclock/registry.aspx")
async def iclock_registry(request: Request):
    """Handle device registration."""
    await log_request(request, "")
    for key, value in request.query_params.items():
        if key.upper() == "SN":
            global DEVICE_SN
            DEVICE_SN = value
            DEVICE_INFO['sn'] = DEVICE_SN
            log(f"📱 Registered Device SN: {DEVICE_SN}")
        DEVICE_INFO[key] = value

    save_persistent_data()
    return PlainTextResponse("OK")

@app.post("/iclock/devicecmd.aspx")
async def iclock_devicecmd(request: Request):
    """Handle command responses from device."""
    body = (await request.body()).decode(errors="ignore")
    await log_request(request, body)

    if "=" in body:
        for line in body.splitlines():
            if '=' in line:
                try:
                    key, value = line.split('=', 1)
                    DEVICE_INFO[key.strip()] = value.strip()
                    log(f"⚙️ Device Info: {key.strip()} = {value.strip()}")
                except:
                    pass

    save_persistent_data()
    return PlainTextResponse("OK")

# ---------------- UTILITY ENDPOINTS ----------------
@app.get("/export_attendance")
async def export_attendance():
    """Export all attendance data as CSV."""
    if not ATTENDANCE_DATA:
        return PlainTextResponse("No attendance data")

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["User ID", "Timestamp", "Status", "Status Text", "Verification", "Workcode", "Received At"])

    for record in ATTENDANCE_DATA:
        writer.writerow([
            record.get('user_id', ''), record.get('timestamp', ''), record.get('status', ''),
            record.get('status_text', ''), record.get('verification', ''), record.get('workcode', ''),
            record.get('received_at', '')
        ])

    content = output.getvalue()
    filename = f"all_attendance_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    return PlainTextResponse(
        content,
        headers={"Content-Disposition": f"attachment; filename={filename}", "Content-Type": "text/csv"}
    )