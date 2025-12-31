import os
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, PlainTextResponse
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
        # Save logs (keep last 1000 lines to avoid file getting too large)
        logs_to_save = LOGS[-1000:] if len(LOGS) > 1000 else LOGS
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
    "POWEROFF"
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
    while True:
        try:
            if not COMMAND_QUEUE:
                # Add initial commands to get all data
                COMMAND_QUEUE.append("INFO")
                COMMAND_QUEUE.append("GET OPTION")
                COMMAND_QUEUE.append("GET ATTLOG ALL")  # Try to get ALL attendance
                log("🤖 Auto-added initial commands to queue")
            
            # Check if we need to request attendance again
            await asyncio.sleep(30)
            
            # Always keep GET ATTLOG in queue to get continuous data
            if not any("ATTLOG" in cmd for cmd in COMMAND_QUEUE):
                COMMAND_QUEUE.append("GET ATTLOG")
                log("🔄 Auto-added GET ATTLOG to queue for continuous polling")
                
        except Exception as e:
            log(f"⚠️ Error in auto_send_commands: {e}")
            await asyncio.sleep(60)

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
        log("💾 Auto-saved data to disk")

# ---------------- UI ROUTES ----------------

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Main dashboard page"""
    current_time = datetime.utcnow().isoformat() + "Z"
    
    # Get statistics
    today = datetime.utcnow().date()
    today_records = [r for r in ATTENDANCE_DATA 
                    if r.get('timestamp', '').startswith(today.strftime('%Y-%m-%d'))]
    
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
            "device_info": DEVICE_INFO
        }
    )

@app.get("/get_logs")
async def get_logs():
    """AJAX endpoint to get updated logs"""
    today = datetime.utcnow().date()
    today_records = [r for r in ATTENDANCE_DATA 
                    if r.get('timestamp', '').startswith(today.strftime('%Y-%m-%d'))]
    
    return {
        "logs": LOGS[-200:],
        "attendance": ATTENDANCE_DISPLAY[-100:],
        "queue": COMMAND_QUEUE,
        "queue_count": len(COMMAND_QUEUE),
        "attendance_count": len(ATTENDANCE_DATA),
        "today_count": len(today_records),
        "logs_count": len(LOGS),
        "device_sn": DEVICE_SN,
        "device_info": DEVICE_INFO
    }

@app.post("/send_command", response_class=HTMLResponse)
async def send_command(
    request: Request,
    endpoint: str = Form(...),
    command: str = Form(...)
):
    """Send command to device"""
    if endpoint == "/iclock/getrequest.aspx":
        COMMAND_QUEUE.append(command)
        log(f"✅ COMMAND QUEUED: {command}")
    else:
        log(f"⚠️  This endpoint ({endpoint}) doesn't support queued commands")
    
    # Redirect back to home
    from fastapi.responses import RedirectResponse
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
        "data": ATTENDANCE_DATA[-1000:],  # Return last 1000 records
        "device_sn": DEVICE_SN,
        "device_info": DEVICE_INFO
    }

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
        
        for line in lines:
            if not line.strip():
                continue
                
            # Check for device info
            if "SN=" in line.upper():
                global DEVICE_SN
                DEVICE_SN = line.split("SN=")[1].strip() if "SN=" in line else line.split("sn=")[1].strip()
                log(f"📱 Device SN: {DEVICE_SN}")
                save_persistent_data()
            
            # Parse attendance data (tab-separated)
            elif '\t' in line and not line.startswith("GET OPTION") and not line.startswith("INFO"):
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
                        attendance_count += 1
                        
                        # Log details
                        log(f"👤 User: {record['user_id']}, ⏰ Time: {record['timestamp']}, 📊 Status: {record['status_text']}")
                
                else:
                    # If not standard attendance, log it anyway
                    log_attendance_raw(f"RAW: {line}")
        
        if attendance_count > 0:
            log(f"✅ Added {attendance_count} new attendance records (Total: {len(ATTENDANCE_DATA)})")
            save_persistent_data()
        
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
    
    await asyncio.sleep(0.5)
    
    if COMMAND_QUEUE:
        command = COMMAND_QUEUE.pop(0)
        log(f"📤 SENDING COMMAND: {command}")
        
        # Special handling for attendance commands
        if "ATTLOG" in command:
            # Schedule next attendance pull
            async def add_next_attlog():
                await asyncio.sleep(10)  # Shorter interval for continuous polling
                if not any("ATTLOG" in cmd for cmd in COMMAND_QUEUE):
                    COMMAND_QUEUE.append("GET ATTLOG")
                    log("🔄 Auto-queued next GET ATTLOG for continuous data")
            
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
                key, value = line.split('=', 1)
                DEVICE_INFO[key.strip()] = value.strip()
                log(f"⚙️  Device Info: {key.strip()} = {value.strip()}")
    
    save_persistent_data()
    return PlainTextResponse("OK")

@app.get("/favicon.ico")
async def favicon():
    return PlainTextResponse("")

@app.get("/reset_device")
async def reset_device():
    """Reset device connection"""
    global COMMAND_QUEUE
    COMMAND_QUEUE = ["INFO", "GET OPTION", "GET ATTLOG ALL"]
    log("🔄 Device connection reset - queued initial commands")
    return PlainTextResponse("OK")
