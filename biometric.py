import os
import json
import asyncio
from datetime import datetime, timedelta
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
ATTENDANCE_RECORDS: List[Dict[str, Any]] = []
LIVE_ATTENDANCE: List[Dict[str, Any]] = []
DEVICE_INFO: Dict[str, str] = {"sn": "Unknown"}
COMMAND_QUEUE: List[str] = []
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
    live_cutoff = now - timedelta(hours=24)
    
    LIVE_ATTENDANCE.clear()
    for record in ATTENDANCE_RECORDS:
        try:
            # Try to parse timestamp - handle different formats
            timestamp_str = record.get('timestamp', '')
            if 'T' in timestamp_str:
                # ISO format: 2024-01-01T12:34:56
                record_time = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
            elif len(timestamp_str) >= 19:
                # Common format: 2024-01-01 12:34:56
                record_time = datetime.strptime(timestamp_str[:19], "%Y-%m-%d %H:%M:%S")
            else:
                continue
                
            if record_time >= live_cutoff:
                LIVE_ATTENDANCE.append(record)
        except Exception as e:
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
    
    # Clean timestamp - ensure consistent format
    timestamp = record['timestamp']
    if ' ' in timestamp and 'T' not in timestamp:
        # Convert "2024-01-01 12:34:56" to "2024-01-01T12:34:56"
        record['timestamp'] = timestamp.replace(' ', 'T')
    
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
    
    # Format time for display
    try:
        if 'T' in record['timestamp']:
            time_parts = record['timestamp'].split('T')
            record['display_date'] = time_parts[0]
            record['display_time'] = time_parts[1][:8] if len(time_parts) > 1 else ''
        else:
            record['display_date'] = record['timestamp'][:10]
            record['display_time'] = record['timestamp'][11:19] if len(record['timestamp']) > 19 else ''
    except:
        record['display_date'] = record['timestamp'][:10]
        record['display_time'] = ''
    
    return record

def is_duplicate_record(record: Dict[str, Any]) -> bool:
    """Check if attendance record already exists"""
    record_key = f"{record['user_id']}_{record['timestamp']}_{record['status']}"
    
    for existing in ATTENDANCE_RECORDS:
        existing_key = f"{existing['user_id']}_{existing['timestamp']}_{existing['status']}"
        if existing_key == record_key:
            return True
    return False

# ------------- LOGGING -------------
def log(message: str):
    """Add timestamped log entry"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    LOGS.append(log_entry)
    print(log_entry)
    
    if len(LOGS) > 1000:
        LOGS.pop(0)

# ------------- DEVICE COMMUNICATION -------------
async def handle_device_data(body: str):
    """
    Process data received from biometric device
    """
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    new_records_count = 0
    
    for line in lines:
        # Check for device info
        if 'SN=' in line.upper():
            sn_part = line.upper().split('SN=')[-1].split()[0] if 'SN=' in line.upper() else line
            DEVICE_INFO['sn'] = sn_part
            log(f"📱 Device SN: {sn_part}")
            save_persistent_data()
            continue
            
        # Parse attendance line
        record = parse_attendance_line(line)
        
        if record and not is_duplicate_record(record):
            ATTENDANCE_RECORDS.append(record)
            new_records_count += 1
            
            # Add to live attendance if recent
            try:
                timestamp = record['timestamp']
                if 'T' in timestamp:
                    record_time = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                else:
                    record_time = datetime.strptime(timestamp[:19], "%Y-%m-%d %H:%M:%S")
                    
                if record_time >= datetime.now() - timedelta(hours=24):
                    LIVE_ATTENDANCE.append(record)
            except:
                pass
                
            log(f"✓ {record['status_text']}: User {record['user_id']} at {record['display_time']}")
    
    if new_records_count > 0:
        save_persistent_data()
        log(f"📊 Added {new_records_count} new records (Total: {len(ATTENDANCE_RECORDS)})")
    
    return new_records_count

async def auto_fetch_attendance():
    """Automatically send commands to fetch all attendance"""
    while True:
        try:
            if len(COMMAND_QUEUE) == 0:
                COMMAND_QUEUE.append("GET ATTLOG ALL")
                log("🔄 Auto-queued: GET ATTLOG ALL")
            
            await asyncio.sleep(30)
            
        except Exception as e:
            log(f"⚠️ Auto-fetch error: {e}")
            await asyncio.sleep(60)

# ------------- FASTAPI ROUTES -------------

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
    sn = request.query_params.get("SN", "")
    if sn:
        DEVICE_INFO['sn'] = sn
        log(f"📱 Device SN: {sn}")
    
    if COMMAND_QUEUE:
        command = COMMAND_QUEUE.pop(0)
        log(f"📤 Sending to device: {command}")
        
        # Auto-add next fetch command
        if "ATTLOG" in command:
            async def add_next_command():
                await asyncio.sleep(2)
                if len(COMMAND_QUEUE) == 0:
                    COMMAND_QUEUE.append("GET ATTLOG ALL")
                    log("🔄 Auto-added next GET ATTLOG ALL")
            
            asyncio.create_task(add_next_command())
        
        return PlainTextResponse(command)
    
    return PlainTextResponse("GET ATTLOG")

@app.get("/iclock/registry.aspx")
async def device_registration(request: Request):
    """Device registration endpoint"""
    for key, value in request.query_params.items():
        DEVICE_INFO[key] = value
        log(f"📝 Registration: {key}={value}")
    
    save_persistent_data()
    return PlainTextResponse("OK")

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard"""
    # Prepare data for template
    all_records = ATTENDANCE_RECORDS[-100:]  # Last 100 records for preview
    live_records = LIVE_ATTENDANCE[-50:]  # Last 50 live records
    
    # Format timestamps for display
    for record in all_records + live_records:
        if 'display_time' not in record:
            timestamp = record.get('timestamp', '')
            if 'T' in timestamp:
                parts = timestamp.split('T')
                record['display_date'] = parts[0]
                record['display_time'] = parts[1][:8] if len(parts) > 1 else timestamp
            else:
                record['display_date'] = timestamp[:10]
                record['display_time'] = timestamp[11:19] if len(timestamp) > 19 else timestamp
    
    return templates.TemplateResponse("index.html", {
        "request": request,
        "total_records": len(ATTENDANCE_RECORDS),
        "live_records": len(LIVE_ATTENDANCE),
        "device_sn": DEVICE_INFO.get('sn', 'Unknown'),
        "attendance_records": all_records,
        "live_attendance": live_records,
        "command_queue": COMMAND_QUEUE,
        "logs": LOGS[-50:],
        "now": datetime.now()
    })

@app.get("/api/attendance/all")
async def get_all_attendance():
    """API endpoint for all attendance records"""
    return {
        "count": len(ATTENDANCE_RECORDS),
        "data": ATTENDANCE_RECORDS[-1000:],
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
async def send_command(command: str = "GET ATTLOG ALL"):
    """Send a command to the device"""
    COMMAND_QUEUE.append(command)
    log(f"✅ Queued command: {command}")
    return {"status": "queued", "command": command}

@app.post("/api/fetch/all")
async def fetch_all_attendance():
    """Force fetch all attendance records from device"""
    COMMAND_QUEUE.clear()
    COMMAND_QUEUE.extend([
        "GET ATTLOG ALL",
        "GET ATTLOG ALL",  # Send twice to ensure complete fetch
        "DATA",
        "TRAN DATA"
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
    
    writer.writerow(["User ID", "Date", "Time", "Status", "Status Text", "Verification", "Workcode"])
    
    for record in ATTENDANCE_RECORDS:
        timestamp = record.get('timestamp', '')
        date = timestamp[:10] if timestamp else ''
        time = timestamp[11:19] if len(timestamp) > 19 else ''
        
        writer.writerow([
            record['user_id'],
            date,
            time,
            record['status'],
            record.get('status_text', ''),
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

@app.on_event("startup")
async def startup():
    """Initialize application"""
    load_persistent_data()
    asyncio.create_task(auto_fetch_attendance())
    
    log("🚀 eSSL Attendance Fetcher Started")
    log(f"📊 Loaded {len(ATTENDANCE_RECORDS)} existing records")
    log(f"📱 Device SN: {DEVICE_INFO.get('sn', 'Unknown')}")

# Setup templates
templates = Jinja2Templates(directory="templates")