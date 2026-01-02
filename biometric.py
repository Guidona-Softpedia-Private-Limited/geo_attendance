import os
import json
import asyncio
from datetime import datetime
from typing import List, Dict, Any
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="eSSL Attendance Fetcher")

# ------------- CONFIGURATION -------------
DATA_FILE = "attendance_data.json"
LOG_FILE = "device_logs.txt"

# ------------- DATA STORAGE -------------
# All historical attendance records
ATTENDANCE_RECORDS: List[Dict[str, Any]] = []
# Live/real-time attendance records (last 24 hours)
LIVE_ATTENDANCE: List[Dict[str, Any]] = []
# Device information
DEVICE_INFO: Dict[str, str] = {"sn": "Unknown"}
# Command queue for device communication
COMMAND_QUEUE: List[str] = []
# Logs for debugging
LOGS: List[str] = []

# ------------- PERSISTENCE FUNCTIONS -------------
def load_persistent_data():
    """Load previously saved attendance data"""
    global ATTENDANCE_RECORDS, DEVICE_INFO
    
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r') as f:
                data = json.load(f)
                ATTENDANCE_RECORDS = data.get('attendance', [])
                DEVICE_INFO = data.get('device_info', {"sn": "Unknown"})
                print(f"📂 Loaded {len(ATTENDANCE_RECORDS)} attendance records")
                
                # Update live attendance (last 24 hours)
                update_live_attendance()
                
    except Exception as e:
        print(f"⚠️ Error loading data: {e}")

def save_persistent_data():
    """Save attendance data to disk"""
    try:
        data = {
            'attendance': ATTENDANCE_RECORDS,
            'device_info': DEVICE_INFO,
            'last_updated': datetime.now().isoformat()
        }
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"⚠️ Error saving data: {e}")

def update_live_attendance():
    """Update live attendance with records from last 24 hours"""
    global LIVE_ATTENDANCE
    
    now = datetime.now()
    live_cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
    
    LIVE_ATTENDANCE.clear()
    for record in ATTENDANCE_RECORDS:
        try:
            record_time = datetime.fromisoformat(record['timestamp'].replace('Z', '+00:00'))
            if record_time >= live_cutoff:
                LIVE_ATTENDANCE.append(record)
        except:
            continue

# ------------- ATTENDANCE PARSING -------------
def parse_attendance_line(line: str) -> Dict[str, Any]:
    """
    Parse attendance line format:
    USER_ID\tTIMESTAMP\tSTATUS\tVERIFICATION\tWORKCODE
    """
    parts = line.split('\t')
    if len(parts) < 3:
        return None
    
    record = {
        'user_id': parts[0].strip(),
        'timestamp': parts[1].strip(),
        'status': parts[2].strip(),
        'verification': parts[3].strip() if len(parts) > 3 else '',
        'workcode': parts[4].strip() if len(parts) > 4 else '',
        'received_at': datetime.now().isoformat()
    }
    
    # Status mapping
    status_map = {
        '0': 'Check-in',
        '1': 'Check-out',
        '2': 'Break-out',
        '3': 'Break-in',
        '4': 'Overtime-in',
        '5': 'Overtime-out',
        '255': 'Error'
    }
    record['status_text'] = status_map.get(record['status'], 'Unknown')
    
    return record

def is_duplicate_record(record: Dict[str, Any]) -> bool:
    """Check if attendance record already exists"""
    for existing in ATTENDANCE_RECORDS:
        if (existing['user_id'] == record['user_id'] and 
            existing['timestamp'] == record['timestamp'] and 
            existing['status'] == record['status']):
            return True
    return False

# ------------- LOGGING -------------
def log(message: str):
    """Add timestamped log entry"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    LOGS.append(log_entry)
    print(log_entry)
    
    # Keep logs manageable
    if len(LOGS) > 1000:
        LOGS.pop(0)

# ------------- DEVICE COMMUNICATION -------------
async def handle_device_data(body: str):
    """
    Process data received from biometric device
    This is the main function for parsing attendance data
    """
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    new_records_count = 0
    
    for line in lines:
        # Parse attendance line
        record = parse_attendance_line(line)
        
        if record and not is_duplicate_record(record):
            ATTENDANCE_RECORDS.append(record)
            new_records_count += 1
            
            # Add to live attendance if today
            record_time = datetime.fromisoformat(record['timestamp'].replace('Z', '+00:00'))
            if record_time.date() == datetime.now().date():
                LIVE_ATTENDANCE.append(record)
                
            log(f"✓ Attendance: User {record['user_id']} at {record['timestamp']}")
    
    if new_records_count > 0:
        save_persistent_data()
        update_live_attendance()
        log(f"📊 Added {new_records_count} new records (Total: {len(ATTENDANCE_RECORDS)})")
    
    return new_records_count

async def auto_fetch_attendance():
    """
    Automatically send commands to fetch all attendance
    Runs in background to continuously get data
    """
    while True:
        try:
            # If device is connected and queue is empty, fetch attendance
            if len(COMMAND_QUEUE) == 0:
                COMMAND_QUEUE.append("GET ATTLOG ALL")
                log("🔄 Auto-queued: GET ATTLOG ALL")
            
            await asyncio.sleep(30)  # Check every 30 seconds
            
        except Exception as e:
            log(f"⚠️ Auto-fetch error: {e}")
            await asyncio.sleep(60)

# ------------- FASTAPI ROUTES -------------

# Device Endpoints (biometric device calls these)
@app.post("/iclock/cdata.aspx")
async def device_data_endpoint(request: Request):
    """Main endpoint for device to send attendance data"""
    body = (await request.body()).decode('utf-8', errors='ignore')
    
    log(f"📥 Received {len(body)} chars from device")
    
    # Process the data
    new_records = await handle_device_data(body)
    
    return PlainTextResponse("OK")

@app.get("/iclock/getrequest.aspx")
async def device_command_endpoint(request: Request):
    """Device pulls commands from here"""
    # Extract device info
    sn = request.query_params.get("SN", "")
    if sn:
        DEVICE_INFO['sn'] = sn
        log(f"📱 Device SN: {sn}")
    
    # Send next command if available
    if COMMAND_QUEUE:
        command = COMMAND_QUEUE.pop(0)
        log(f"📤 Sending to device: {command}")
        return PlainTextResponse(command)
    
    # Default: ask for attendance
    return PlainTextResponse("GET ATTLOG")

@app.get("/iclock/registry.aspx")
async def device_registration(request: Request):
    """Device registration endpoint"""
    # Store all registration parameters
    for key, value in request.query_params.items():
        DEVICE_INFO[key] = value
        log(f"📝 Registration: {key}={value}")
    
    save_persistent_data()
    return PlainTextResponse("OK")

# Web UI Endpoints
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard with two cards"""
    return templates.TemplateResponse("index.html", {
        "request": request,
        "total_records": len(ATTENDANCE_RECORDS),
        "live_records": len(LIVE_ATTENDANCE),
        "device_sn": DEVICE_INFO.get('sn', 'Unknown'),
        "attendance_records": ATTENDANCE_RECORDS[-100:],  # Last 100 for preview
        "live_attendance": LIVE_ATTENDANCE[-50:],  # Last 50 live records
        "command_queue": COMMAND_QUEUE,
        "logs": LOGS[-50:]
    })

@app.get("/api/attendance/all")
async def get_all_attendance():
    """API endpoint for all attendance records"""
    return {
        "count": len(ATTENDANCE_RECORDS),
        "data": ATTENDANCE_RECORDS[-1000:],  # Last 1000 records
        "device": DEVICE_INFO
    }

@app.get("/api/attendance/live")
async def get_live_attendance():
    """API endpoint for live attendance"""
    return {
        "count": len(LIVE_ATTENDANCE),
        "data": LIVE_ATTENDANCE,
        "last_updated": datetime.now().isoformat()
    }

@app.get("/api/command/queue")
async def get_command_queue():
    """Get current command queue"""
    return {
        "queue": COMMAND_QUEUE,
        "count": len(COMMAND_QUEUE)
    }

@app.post("/api/command/send")
async def send_command(command: str):
    """Send a command to the device"""
    if command:
        COMMAND_QUEUE.append(command)
        log(f"✅ Queued command: {command}")
        return {"status": "queued", "command": command}
    return {"status": "error", "message": "No command provided"}

@app.post("/api/command/clear")
async def clear_queue():
    """Clear command queue"""
    COMMAND_QUEUE.clear()
    log("🗑️ Cleared command queue")
    return {"status": "cleared"}

@app.post("/api/fetch/all")
async def fetch_all_attendance():
    """
    Force fetch all attendance records from device
    This is the main feature - gets ALL historical data
    """
    # Clear queue and add aggressive fetch commands
    COMMAND_QUEUE.clear()
    COMMAND_QUEUE.extend([
        "GET ATTLOG ALL",
        "GET ATTLOG ALL",  # Send twice to ensure complete fetch
        "DATA",  # Alternative command
        "TRAN DATA"  # Another alternative
    ])
    
    log("🚀 FORCE FETCH: Queued commands to get ALL attendance")
    return {
        "status": "started",
        "message": "Fetching all attendance records from device",
        "commands_queued": len(COMMAND_QUEUE)
    }

@app.get("/api/export/csv")
async def export_attendance_csv():
    """Export attendance data as CSV"""
    import csv
    from io import StringIO
    
    output = StringIO()
    writer = csv.writer(output)
    
    # Write header
    writer.writerow(["User ID", "Timestamp", "Status", "Status Text", "Verification", "Workcode"])
    
    # Write data
    for record in ATTENDANCE_RECORDS:
        writer.writerow([
            record['user_id'],
            record['timestamp'],
            record['status'],
            record['status_text'],
            record['verification'],
            record['workcode']
        ])
    
    content = output.getvalue()
    filename = f"attendance_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    
    return PlainTextResponse(
        content,
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "Content-Type": "text/csv"
        }
    )

# ------------- APPLICATION STARTUP -------------
@app.on_event("startup")
async def startup():
    """Initialize application"""
    # Load existing data
    load_persistent_data()
    
    # Start background tasks
    asyncio.create_task(auto_fetch_attendance())
    
    log("🚀 eSSL Attendance Fetcher Started")
    log(f"📊 Loaded {len(ATTENDANCE_RECORDS)} existing records")
    log(f"📱 Device SN: {DEVICE_INFO.get('sn', 'Unknown')}")

# Setup templates
templates = Jinja2Templates(directory="templates")