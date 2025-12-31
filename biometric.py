import os
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from datetime import datetime, timedelta
import asyncio
import json
import re
from typing import List, Dict, Any
import csv
import io

app = FastAPI()

# ---------------- DATA STORAGE ----------------

# Store ALL logs from device (persistent across restarts)
LOGS: List[str] = []
# Store ALL attendance records with detailed parsing
ATTENDANCE_DATA: List[Dict[str, Any]] = []
# Raw attendance lines for display
ATTENDANCE_DISPLAY: List[str] = []
# Command queue
COMMAND_QUEUE: List[str] = []
# Device information
DEVICE_SN = "Unknown"
DEVICE_INFO: Dict[str, str] = {}

# File for persistent storage
DATA_FILE = "attendance_data.json"
LOG_FILE = "device_logs.txt"

# Track if we're actively fetching all logs
IS_FETCHING_ALL_LOGS = False
LAST_FETCH_TIME = None

# ---------------- PERSISTENT STORAGE FUNCTIONS ----------------

def load_persistent_data():
    """Load previously saved data from files"""
    global ATTENDANCE_DATA, LOGS, DEVICE_SN, DEVICE_INFO
    
    try:
        # Load attendance data
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r') as f:
                data = json.load(f)
                ATTENDANCE_DATA = data.get('attendance', [])
                DEVICE_SN = data.get('device_sn', "Unknown")
                DEVICE_INFO = data.get('device_info', {})
                print(f"📂 Loaded {len(ATTENDANCE_DATA)} attendance records from file")
    except Exception as e:
        print(f"⚠️ Error loading persistent data: {e}")
    
    try:
        # Load logs
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, 'r') as f:
                LOGS = [line.strip() for line in f.readlines() if line.strip()]
                print(f"📂 Loaded {len(LOGS)} log entries from file")
    except Exception as e:
        print(f"⚠️ Error loading logs: {e}")
    
    # Update display from loaded attendance data
    update_attendance_display()

def save_persistent_data():
    """Save current data to files"""
    try:
        # Save attendance data
        data = {
            'attendance': ATTENDANCE_DATA,
            'device_sn': DEVICE_SN,
            'device_info': DEVICE_INFO,
            'last_updated': datetime.utcnow().isoformat()
        }
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"⚠️ Error saving persistent data: {e}")
    
    try:
        # Save logs (keep last 2000 lines to avoid file getting too large)
        logs_to_save = LOGS[-2000:] if len(LOGS) > 2000 else LOGS
        with open(LOG_FILE, 'w') as f:
            for log_entry in logs_to_save:
                f.write(log_entry + "\n")
    except Exception as e:
        print(f"⚠️ Error saving logs: {e}")

def update_attendance_display():
    """Update the display list from parsed attendance data"""
    global ATTENDANCE_DISPLAY
    ATTENDANCE_DISPLAY.clear()
    
    for record in ATTENDANCE_DATA[-1000:]:  # Show last 1000 records in UI
        line = f"{record.get('user_id', 'N/A')}\t{record.get('timestamp', 'N/A')}\t{record.get('status', 'N/A')}\t{record.get('verification', 'N/A')}\t{record.get('workcode', 'N/A')}"
        ATTENDANCE_DISPLAY.append(line)

# ---------------- UI SETUP ----------------

templates = Jinja2Templates(directory="templates")

ENDPOINTS = [
    "/iclock/cdata.aspx",
    "/iclock/getrequest.aspx",
    "/iclock/registry.aspx",
    "/iclock/devicecmd.aspx"
]

COMMANDS = [
    "INFO",
    "GET ATTLOG",
    "GET ATTLOG ALL",  # Added ALL command
    "SET OPTION RTLOG=1",
    "SET OPTION PUSH=1",
    "CLEAR ATTLOG",
    "GET OPTION",
    "GET USERINFO",
    "GET BIODATA",
    "GET PICTURE",
    "GET BIODATA ALL",
    "GET USERINFO ALL",
    "REBOOT",
    "POWEROFF",
    "FORCE GET ALL LOGS"  # New custom command
]

def log(msg: str):
    """Add a log entry with timestamp"""
    ts = f"{datetime.utcnow().isoformat()}Z - {msg}"
    print(ts)
    LOGS.append(ts)
    # Save logs periodically
    if len(LOGS) % 10 == 0:  # Save every 10 log entries
        save_persistent_data()

def log_attendance_raw(raw_line: str):
    """Log raw attendance line for display"""
    ts = f"{datetime.utcnow().isoformat()}Z - {raw_line}"
    ATTENDANCE_DISPLAY.append(raw_line)
    # Keep only last 1000 lines in display
    if len(ATTENDANCE_DISPLAY) > 1000:
        ATTENDANCE_DISPLAY.pop(0)

def parse_attendance_line(line: str) -> Dict[str, Any]:
    """
    Parse attendance line in format:
    USER_ID\tTIMESTAMP\tSTATUS\tVERIFICATION\tWORKCODE
    Example: 42\t2025-12-31 17:29:04\t0\t1\t\t0
    """
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
    
    # Map status codes to human readable
    status_map = {
        '0': 'Check-in',
        '1': 'Check-out',
        '2': 'Break-out',
        '3': 'Break-in',
        '4': 'Overtime-in',
        '5': 'Overtime-out'
    }
    record['status_text'] = status_map.get(record['status'], 'Unknown')
    
    return record

async def log_request(request: Request, body: str):
    """Log device request details"""
    log("DEVICE REQUEST")
    log(f"  CLIENT   : {request.client.host if request.client else 'Unknown'}")
    log(f"  ENDPOINT : {request.url.path}")
    log(f"  METHOD   : {request.method}")
    log(f"  QUERY    : {dict(request.query_params)}")
    if body and len(body) > 1000:
        log(f"  BODY     : {body[:1000]}... ({len(body)} chars)")
    else:
        log(f"  BODY     : {body if body else '<empty>'}")
    log("-" * 60)

async def auto_send_commands():
    """Automatically send commands to device periodically"""
    global IS_FETCHING_ALL_LOGS, LAST_FETCH_TIME
    
    first_run = True
    fetch_attempts = 0
    
    while True:
        try:
            if first_run:
                # Initial aggressive sequence to get ALL data
                COMMAND_QUEUE.append("INFO")
                COMMAND_QUEUE.append("GET OPTION")
                COMMAND_QUEUE.append("SET OPTION RTLOG=1")
                COMMAND_QUEUE.append("SET OPTION PUSH=1")
                COMMAND_QUEUE.append("GET ATTLOG ALL")  # Try to get ALL attendance
                COMMAND_QUEUE.append("GET ATTLOG ALL")  # Try again to ensure we get everything
                log("🤖 Auto-added aggressive commands to fetch ALL logs")
                IS_FETCHING_ALL_LOGS = True
                LAST_FETCH_TIME = datetime.utcnow()
                first_run = False
                fetch_attempts += 1
            
            # Wait before next attempt
            await asyncio.sleep(15)
            
            # If we haven't gotten data in a while, try again aggressively
            if IS_FETCHING_ALL_LOGS and fetch_attempts < 5:
                if not any("ATTLOG" in cmd for cmd in COMMAND_QUEUE):
                    COMMAND_QUEUE.append("GET ATTLOG ALL")
                    fetch_attempts += 1
                    log(f"🔄 Attempt {fetch_attempts}: Requesting ALL attendance logs")
                    
                    # After 5 attempts, stop aggressive fetching
                    if fetch_attempts >= 5:
                        IS_FETCHING_ALL_LOGS = False
                        log("⚠️ Stopped aggressive fetch after 5 attempts")
            
            # Continuous polling for new data
            if not IS_FETCHING_ALL_LOGS and not any("ATTLOG" in cmd for cmd in COMMAND_QUEUE):
                COMMAND_QUEUE.append("GET ATTLOG")
                
        except Exception as e:
            log(f"⚠️ Error in auto_send_commands: {e}")
            await asyncio.sleep(30)

@app.on_event("startup")
async def startup_event():
    """Initialize application"""
    # Load previous data
    load_persistent_data()
    
    # Start background tasks
    asyncio.create_task(auto_send_commands())
    
    # Start periodic data saving
    asyncio.create_task(periodic_save())
    
    log("🚀 eSSL Probe Started - Loading ALL attendance data")

async def periodic_save():
    """Periodically save data to disk"""
    while True:
        await asyncio.sleep(60)  # Save every minute
        save_persistent_data()

# ---------------- UI ROUTES ----------------

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Main dashboard page"""
    current_time = datetime.utcnow().isoformat() + "Z"
    
    # Get statistics
    today = datetime.utcnow().date()
    today_records = [r for r in ATTENDANCE_DATA 
                    if r.get('timestamp', '').startswith(today.strftime('%Y-%m-%d'))]
    
    # Get unique users
    unique_users = len(set(r.get('user_id', '') for r in ATTENDANCE_DATA))
    
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "logs": LOGS[-200:],  # Show last 200 logs
            "attendance": ATTENDANCE_DISPLAY[-100:],  # Show last 100 attendance
            "endpoints": ENDPOINTS,
            "commands": COMMANDS,
            "queue": COMMAND_QUEUE,
            "device_sn": DEVICE_SN,
            "current_time": current_time,
            "total_records": len(ATTENDANCE_DATA),
            "today_records": len(today_records),
            "unique_users": unique_users,
            "device_info": DEVICE_INFO,
            "fetching_all": IS_FETCHING_ALL_LOGS
        }
    )

@app.get("/get_logs")
async def get_logs():
    """AJAX endpoint to get updated logs"""
    today = datetime.utcnow().date()
    today_records = [r for r in ATTENDANCE_DATA 
                    if r.get('timestamp', '').startswith(today.strftime('%Y-%m-%d'))]
    unique_users = len(set(r.get('user_id', '') for r in ATTENDANCE_DATA))
    
    return {
        "logs": LOGS[-200:],
        "attendance": ATTENDANCE_DISPLAY[-100:],
        "queue": COMMAND_QUEUE,
        "queue_count": len(COMMAND_QUEUE),
        "attendance_count": len(ATTENDANCE_DATA),
        "today_count": len(today_records),
        "unique_users": unique_users,
        "logs_count": len(LOGS),
        "device_sn": DEVICE_SN,
        "device_info": DEVICE_INFO,
        "fetching_all": IS_FETCHING_ALL_LOGS
    }

@app.post("/send_command", response_class=HTMLResponse)
async def send_command(
    request: Request,
    endpoint: str = Form(...),
    command: str = Form(...)
):
    """Send command to device"""
    global IS_FETCHING_ALL_LOGS, LAST_FETCH_TIME
    
    if endpoint == "/iclock/getrequest.aspx":
        # Handle custom command
        if command == "FORCE GET ALL LOGS":
            # Clear queue and add aggressive commands
            COMMAND_QUEUE.clear()
            COMMAND_QUEUE.extend([
                "INFO",
                "GET OPTION",
                "SET OPTION RTLOG=1",
                "SET OPTION PUSH=1",
                "GET ATTLOG ALL",
                "GET ATTLOG ALL",  # Repeat to ensure we get everything
                "GET ATTLOG ALL"   # One more time
            ])
            IS_FETCHING_ALL_LOGS = True
            LAST_FETCH_TIME = datetime.utcnow()
            log("🚨 FORCE FETCH: Aggressive commands queued to get ALL attendance logs")
        else:
            COMMAND_QUEUE.append(command)
            log(f"✅ COMMAND QUEUED: {command}")
    else:
        log(f"⚠️  This endpoint ({endpoint}) doesn't support queued commands")
    
    return RedirectResponse(url="/", status_code=303)

@app.post("/clear_queue")
async def clear_queue(request: Request):
    """Clear command queue"""
    global COMMAND_QUEUE
    COMMAND_QUEUE = []
    log("🗑️ Command queue cleared")
    return PlainTextResponse("OK")

@app.post("/clear_logs")
async def clear_logs(request: Request):
    """Clear logs (but keep attendance data)"""
    global LOGS
    LOGS = []
    log("🧹 All logs cleared (attendance data preserved)")
    return PlainTextResponse("OK")

@app.post("/clear_attendance")
async def clear_attendance(request: Request):
    """Clear attendance data"""
    global ATTENDANCE_DATA, ATTENDANCE_DISPLAY
    ATTENDANCE_DATA = []
    ATTENDANCE_DISPLAY = []
    log("🧹 All attendance data cleared")
    save_persistent_data()
    return PlainTextResponse("OK")

@app.get("/export_attendance")
async def export_attendance(format: str = "csv"):
    """Export attendance data in various formats"""
    if not ATTENDANCE_DATA:
        return PlainTextResponse("No attendance data available")
    
    if format == "csv":
        # Create CSV
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Write header
        writer.writerow(["User ID", "Timestamp", "Status", "Status Text", "Verification", "Workcode", "Received At"])
        
        # Write data
        for record in ATTENDANCE_DATA:
            writer.writerow([
                record.get('user_id', ''),
                record.get('timestamp', ''),
                record.get('status', ''),
                record.get('status_text', ''),
                record.get('verification', ''),
                record.get('workcode', ''),
                record.get('received_at', '')
            ])
        
        content = output.getvalue()
        filename = f"attendance_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        
        return PlainTextResponse(
            content,
            headers={
                "Content-Disposition": f"attachment; filename={filename}",
                "Content-Type": "text/csv"
            }
        )
    
    else:  # JSON format
        content = json.dumps(ATTENDANCE_DATA, indent=2)
        filename = f"attendance_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        
        return PlainTextResponse(
            content,
            headers={
                "Content-Disposition": f"attachment; filename={filename}",
                "Content-Type": "application/json"
            }
        )

@app.get("/get_all_attendance")
async def get_all_attendance(request: Request):
    """API endpoint to get all attendance data"""
    return {
        "count": len(ATTENDANCE_DATA),
        "data": ATTENDANCE_DATA[-2000:],  # Return last 2000 records
        "device_sn": DEVICE_SN,
        "device_info": DEVICE_INFO,
        "fetching_all": IS_FETCHING_ALL_LOGS
    }

@app.get("/force_fetch_all")
async def force_fetch_all():
    """Force fetch all attendance logs from device"""
    global COMMAND_QUEUE, IS_FETCHING_ALL_LOGS, LAST_FETCH_TIME
    
    # Clear queue and add aggressive commands
    COMMAND_QUEUE.clear()
    COMMAND_QUEUE.extend([
        "INFO",
        "GET OPTION",
        "SET OPTION RTLOG=1",
        "SET OPTION PUSH=1",
        "GET ATTLOG ALL",
        "GET ATTLOG ALL",
        "GET ATTLOG ALL"
    ])
    IS_FETCHING_ALL_LOGS = True
    LAST_FETCH_TIME = datetime.utcnow()
    
    log("🚨 MANUAL FORCE FETCH: Aggressive commands queued to get ALL attendance logs")
    return PlainTextResponse("Aggressive fetch initiated. Check logs for progress.")

# ---------------- DEVICE ENDPOINTS ----------------

@app.api_route("/iclock/cdata.aspx", methods=["GET", "POST"])
async def iclock_cdata(request: Request):
    """Handle attendance data push from device"""
    body = (await request.body()).decode(errors="ignore")
    await log_request(request, body)

    if request.method == "GET":
        return PlainTextResponse("OK")

    if request.method == "POST":
        lines = body.splitlines()
        attendance_count = 0
        bulk_data = []
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
                
            # Check for device info
            if "SN=" in line.upper():
                global DEVICE_SN
                DEVICE_SN = line.split("SN=")[1].strip() if "SN=" in line else line.split("sn=")[1].strip()
                log(f"📱 Device SN: {DEVICE_SN}")
                DEVICE_INFO['sn'] = DEVICE_SN
                save_persistent_data()
            
            # Check if this is an attendance log (tab-separated with timestamp)
            elif '\t' in line:
                # Try to detect if this is an attendance line
                parts = line.split('\t')
                if len(parts) >= 2 and len(parts[1]) >= 10:  # Has timestamp
                    log(f"📥 ATTENDANCE LINE: {line}")
                    log_attendance_raw(line)
                    
                    # Parse and store
                    record = parse_attendance_line(line)
                    if record:
                        # Check if this is a duplicate
                        record_key = f"{record['user_id']}_{record['timestamp']}_{record['status']}"
                        existing = False
                        for existing_record in ATTENDANCE_DATA:
                            existing_key = f"{existing_record.get('user_id')}_{existing_record.get('timestamp')}_{existing_record.get('status')}"
                            if record_key == existing_key:
                                existing = True
                                break
                        
                        if not existing:
                            ATTENDANCE_DATA.append(record)
                            bulk_data.append(record)
                            attendance_count += 1
                            
                            # Log details for bulk data
                            if attendance_count % 50 == 0:
                                log(f"📦 Processed {attendance_count} records...")
                    else:
                        # If not standard attendance, log it anyway
                        log_attendance_raw(f"RAW: {line}")
                else:
                    # Might be other data, log it
                    log_attendance_raw(f"OTHER: {line}")
        
        if bulk_data:
            # Bulk save after processing
            save_persistent_data()
            log(f"✅ Added {attendance_count} new attendance records (Total: {len(ATTENDANCE_DATA)})")
            
            # If we got a lot of data at once, schedule another GET ATTLOG ALL
            if attendance_count > 20 and not any("ATTLOG ALL" in cmd for cmd in COMMAND_QUEUE):
                COMMAND_QUEUE.append("GET ATTLOG ALL")
                log(f"📈 Got {attendance_count} records, queuing another GET ATTLOG ALL")
        
        return PlainTextResponse("OK")

@app.get("/iclock/getrequest.aspx")
async def iclock_getrequest(request: Request):
    """Device pulls commands from here"""
    log("📡 DEVICE PULLING COMMAND")
    
    # Get query parameters
    sn = request.query_params.get("SN", "Unknown")
    if sn != "Unknown":
        global DEVICE_SN
        DEVICE_SN = sn
        log(f"📱 Device SN from query: {DEVICE_SN}")
        DEVICE_INFO['sn'] = DEVICE_SN
    
    await asyncio.sleep(0.1)  # Small delay
    
    if COMMAND_QUEUE:
        command = COMMAND_QUEUE.pop(0)
        log(f"📤 SENDING COMMAND: {command}")
        
        # Special handling for attendance commands
        if "ATTLOG" in command:
            # Schedule next attendance pull
            async def add_next_attlog():
                await asyncio.sleep(5)  # Shorter interval for continuous polling
                if "ALL" in command and not any("ATTLOG ALL" in cmd for cmd in COMMAND_QUEUE):
                    # If we just requested ALL logs, wait a bit then ask again
                    await asyncio.sleep(10)
                    COMMAND_QUEUE.append("GET ATTLOG ALL")
                    log("🔄 Auto-queued another GET ATTLOG ALL to ensure complete data")
            
            asyncio.create_task(add_next_attlog())
        
        return PlainTextResponse(command)
    else:
        # Default to getting attendance if queue is empty
        log("📤 SENDING DEFAULT: GET ATTLOG")
        return PlainTextResponse("GET ATTLOG")

@app.get("/iclock/registry.aspx")
async def iclock_registry(request: Request):
    """Device registration endpoint"""
    log("📝 DEVICE REGISTRATION REQUEST")
    
    # Log registration details
    for key, value in request.query_params.items():
        if key.upper() == "SN":
            global DEVICE_SN
            DEVICE_SN = value
            log(f"📱 Registered Device SN: {DEVICE_SN}")
        DEVICE_INFO[key] = value
    
    save_persistent_data()
    return PlainTextResponse("OK")

@app.post("/iclock/devicecmd.aspx")
async def iclock_devicecmd(request: Request):
    """Device command responses"""
    body = (await request.body()).decode(errors="ignore")
    log(f"📋 DEVICE COMMAND RESPONSE: {body[:500]}")
    
    # Parse INFO responses
    if "=" in body and not '\t' in body:
        lines = body.splitlines()
        for line in lines:
            if '=' in line:
                try:
                    key, value = line.split('=', 1)
                    DEVICE_INFO[key.strip()] = value.strip()
                    log(f"⚙️  Device Info: {key.strip()} = {value.strip()}")
                except:
                    pass
    
    # Check if device supports GET ATTLOG ALL
    if "GET ATTLOG ALL" in body.upper():
        log("✅ Device supports GET ATTLOG ALL command!")
    
    save_persistent_data()
    return PlainTextResponse("OK")

@app.get("/favicon.ico")
async def favicon():
    return PlainTextResponse("")

@app.get("/reset_device")
async def reset_device():
    """Reset device connection"""
    global COMMAND_QUEUE, IS_FETCHING_ALL_LOGS
    COMMAND_QUEUE = ["INFO", "GET OPTION", "SET OPTION RTLOG=1", "SET OPTION PUSH=1", "GET ATTLOG ALL", "GET ATTLOG ALL"]
    IS_FETCHING_ALL_LOGS = True
    log("🔄 Device connection reset - queued aggressive fetch commands")
    return PlainTextResponse("OK")