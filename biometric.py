import os
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
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

# Track user check-in status
USER_CHECKIN_STATUS: Dict[str, bool] = {}  # True = checked in, False = checked out

# File for persistent storage
DATA_FILE = "attendance_data.json"
LOG_FILE = "device_logs.txt"

# Track device connection
IS_FETCHING_ALL_LOGS = False
DEVICE_CONNECTED = False
LAST_DEVICE_CONTACT = None

# ---------------- AUTO TOGGLE LOGIC ----------------

def auto_toggle_status(user_id: str, original_status: str) -> Dict[str, str]:
    """
    Auto toggle logic:
    - CHECK-IN aaye to CHECK-OUT karo
    - CHECK-OUT aaye to CHECK-IN karo
    - Unknown/Other aaye to CHECK-IN karo
    
    Returns: {"new_status": "...", "action": "..."}
    """
    original_status = original_status.upper() if original_status else ""
    
    # Define status mapping
    status_map = {
        '0': 'CHECK-IN',
        '1': 'CHECK-OUT',
        '2': 'BREAK-OUT',
        '3': 'BREAK-IN',
        '4': 'OVERTIME-IN',
        '5': 'OVERTIME-OUT',
        '255': 'ERROR',
        'CHECK-IN': 'CHECK-IN',
        'CHECK-OUT': 'CHECK-OUT',
        'CHECKIN': 'CHECK-IN',
        'CHECKOUT': 'CHECK-OUT'
    }
    
    # Get standardized original status
    original_std = status_map.get(original_status, 'UNKNOWN')
    
    # Apply toggle logic
    if original_std == 'CHECK-IN':
        new_status = 'CHECK-OUT'
        action = f"CHECK-OUT"
    elif original_std == 'CHECK-OUT':
        new_status = 'CHECK-IN'
        action = f"CHECK-IN"
    else:
        # All other cases convert to CHECK-IN
        new_status = 'CHECK-IN'
        action = f"CHECK-IN"
    
    # Update user status tracking
    USER_CHECKIN_STATUS[user_id] = (new_status == 'CHECK-IN')
    
    return {
        "new_status": new_status,
        "original_status": original_std,
        "action": action,
        "status_code": '0' if new_status == 'CHECK-IN' else '1'
    }

# ---------------- PERSISTENT STORAGE FUNCTIONS ----------------

def load_persistent_data():
    """Load previously saved data from files"""
    global ATTENDANCE_DATA, LOGS, DEVICE_SN, DEVICE_INFO, USER_CHECKIN_STATUS
    
    try:
        # Load attendance data
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r') as f:
                data = json.load(f)
                ATTENDANCE_DATA = data.get('attendance', [])
                DEVICE_SN = data.get('device_sn', "Unknown")
                DEVICE_INFO = data.get('device_info', {})
                USER_CHECKIN_STATUS = data.get('user_status', {})
                print(f"📂 Loaded {len(ATTENDANCE_DATA)} attendance records from file")
                print(f"📂 Loaded {len(USER_CHECKIN_STATUS)} user status records")
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
            'user_status': USER_CHECKIN_STATUS,
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
        line = f"{record.get('user_id', 'N/A')}\t{record.get('timestamp', 'N/A')}\t{record.get('status', 'N/A')}"
        if record.get('auto_converted'):
            line += f"\t[✔ AUTO: {record.get('original_status', '')} → {record.get('status', '')}]"
        line += f"\t{record.get('verification', 'N/A')}\t{record.get('workcode', 'N/A')}"
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
    "GET ATTLOG ALL",
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
    "DATA",
    "TRAN DATA",
    "GET FP INFO",
    "GET PHOTO INFO"
]

def log(msg: str):
    """Add a log entry with timestamp"""
    ts = f"{datetime.utcnow().isoformat()}Z - {msg}"
    print(ts)
    LOGS.append(ts)
    # Save logs periodically
    if len(LOGS) % 10 == 0:
        save_persistent_data()

def log_attendance_raw(raw_line: str):
    """Log raw attendance line for display"""
    ts = f"{datetime.utcnow().isoformat()}Z - {raw_line}"
    ATTENDANCE_DISPLAY.append(raw_line)
    if len(ATTENDANCE_DISPLAY) > 1000:
        ATTENDANCE_DISPLAY.pop(0)

def parse_attendance_line(line: str, apply_auto_toggle: bool = True) -> Dict[str, Any]:
    """
    Parse attendance line in format:
    USER_ID\tTIMESTAMP\tSTATUS\tVERIFICATION\tWORKCODE
    
    If apply_auto_toggle is True, apply the auto-toggle logic
    """
    parts = line.split('\t')
    if len(parts) < 3:
        return {}
    
    user_id = parts[0]
    timestamp = parts[1]
    original_status = parts[2]
    verification = parts[3] if len(parts) > 3 else ''
    workcode = parts[4] if len(parts) > 4 else ''
    
    # Apply auto-toggle logic
    if apply_auto_toggle:
        toggle_result = auto_toggle_status(user_id, original_status)
        new_status = toggle_result["new_status"]
        action = toggle_result["action"]
        
        # Create record with auto-toggle information
        record = {
            'user_id': user_id,
            'timestamp': timestamp,
            'status': new_status,
            'original_status': toggle_result["original_status"],
            'status_code': toggle_result["status_code"],
            'verification': verification,
            'workcode': workcode,
            'received_at': datetime.utcnow().isoformat(),
            'raw': line,
            'auto_converted': True,
            'conversion_action': action
        }
        
        # Map status codes to human readable
        status_map = {
            '0': 'CHECK-IN',
            '1': 'CHECK-OUT',
            '2': 'BREAK-OUT',
            '3': 'BREAK-IN',
            '4': 'OVERTIME-IN',
            '5': 'OVERTIME-OUT',
            '255': 'ERROR'
        }
        record['status_text'] = status_map.get(record['status_code'], record['status'])
        
        # Log the conversion
        log(f"🔄 {action} for User {user_id} at {timestamp}")
        
    else:
        # Without auto-toggle
        record = {
            'user_id': user_id,
            'timestamp': timestamp,
            'status': original_status,
            'verification': verification,
            'workcode': workcode,
            'received_at': datetime.utcnow().isoformat(),
            'raw': line,
            'auto_converted': False
        }
        
        # Map status codes to human readable
        status_map = {
            '0': 'CHECK-IN',
            '1': 'CHECK-OUT',
            '2': 'BREAK-OUT',
            '3': 'BREAK-IN',
            '4': 'OVERTIME-IN',
            '5': 'OVERTIME-OUT',
            '255': 'ERROR'
        }
        record['status_text'] = status_map.get(record['status'], 'Unknown')
    
    return record

async def log_request(request: Request, body: str):
    """Log device request details"""
    global DEVICE_CONNECTED, LAST_DEVICE_CONTACT
    DEVICE_CONNECTED = True
    LAST_DEVICE_CONTACT = datetime.utcnow()
    
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
    global IS_FETCHING_ALL_LOGS, LAST_DEVICE_CONTACT
    
    first_run = True
    fetch_attempts = 0
    
    while True:
        try:
            # Check if device was recently connected
            device_active = LAST_DEVICE_CONTACT and (datetime.utcnow() - LAST_DEVICE_CONTACT).total_seconds() < 300
            
            if first_run and device_active:
                # Initial sequence
                COMMAND_QUEUE.append("INFO")
                COMMAND_QUEUE.append("GET OPTION")
                COMMAND_QUEUE.append("SET OPTION RTLOG=1")
                COMMAND_QUEUE.append("SET OPTION PUSH=1")
                COMMAND_QUEUE.append("DATA")  # Alternative command for attendance
                COMMAND_QUEUE.append("GET ATTLOG ALL")
                log("🤖 Auto-added initial commands")
                IS_FETCHING_ALL_LOGS = True
                first_run = False
            
            await asyncio.sleep(10)
            
            # If device is active but queue is empty, add attendance command
            if device_active and not COMMAND_QUEUE:
                COMMAND_QUEUE.append("GET ATTLOG ALL")
                log("🔄 Added GET ATTLOG ALL to empty queue")
                
        except Exception as e:
            log(f"⚠️ Error in auto_send_commands: {e}")
            await asyncio.sleep(30)

@app.on_event("startup")
async def startup_event():
    """Initialize application"""
    load_persistent_data()
    asyncio.create_task(auto_send_commands())
    asyncio.create_task(periodic_save())
    asyncio.create_task(check_device_status())
    log("🚀 eSSL Probe Started with AUTO-TOGGLE logic")
    log("ℹ️  Auto-toggle: CHECK-OUT, CHECK-IN")

async def periodic_save():
    """Periodically save data to disk"""
    while True:
        await asyncio.sleep(60)
        save_persistent_data()

async def check_device_status():
    """Check if device is still connected"""
    global DEVICE_CONNECTED, LAST_DEVICE_CONTACT
    while True:
        await asyncio.sleep(30)
        if LAST_DEVICE_CONTACT and (datetime.utcnow() - LAST_DEVICE_CONTACT).total_seconds() > 120:
            if DEVICE_CONNECTED:
                DEVICE_CONNECTED = False
                log("⚠️ Device connection lost - no contact for 2 minutes")

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
    
    # Count auto-converted records
    auto_converted = sum(1 for r in ATTENDANCE_DATA if r.get('auto_converted', False))
    
    # Device status
    device_status = "Connected" if DEVICE_CONNECTED else "Disconnected"
    last_contact = LAST_DEVICE_CONTACT.isoformat() if LAST_DEVICE_CONTACT else "Never"
    
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "logs": LOGS[-200:],
            "attendance": ATTENDANCE_DISPLAY[-100:],
            "endpoints": ENDPOINTS,
            "commands": COMMANDS,
            "queue": COMMAND_QUEUE,
            "device_sn": DEVICE_SN,
            "current_time": current_time,
            "total_records": len(ATTENDANCE_DATA),
            "today_records": len(today_records),
            "unique_users": unique_users,
            "device_info": DEVICE_INFO,
            "fetching_all": IS_FETCHING_ALL_LOGS,
            "device_connected": DEVICE_CONNECTED,
            "last_contact": last_contact,
            "auto_converted": auto_converted
        }
    )

@app.get("/get_logs")
async def get_logs():
    """AJAX endpoint to get updated logs"""
    today = datetime.utcnow().date()
    today_records = [r for r in ATTENDANCE_DATA 
                    if r.get('timestamp', '').startswith(today.strftime('%Y-%m-%d'))]
    unique_users = len(set(r.get('user_id', '') for r in ATTENDANCE_DATA))
    auto_converted = sum(1 for r in ATTENDANCE_DATA if r.get('auto_converted', False))
    
    # Device status
    device_status = "Connected" if DEVICE_CONNECTED else "Disconnected"
    last_contact = LAST_DEVICE_CONTACT.isoformat() if LAST_DEVICE_CONTACT else "Never"
    
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
        "fetching_all": IS_FETCHING_ALL_LOGS,
        "device_connected": DEVICE_CONNECTED,
        "last_contact": last_contact,
        "auto_converted": auto_converted
    }

@app.post("/send_command", response_class=HTMLResponse)
async def send_command(
    request: Request,
    endpoint: str = Form(...),
    command: str = Form(...)
):
    """Send command to device"""
    global IS_FETCHING_ALL_LOGS
    
    if endpoint == "/iclock/getrequest.aspx":
        # Handle special commands
        if command == "FORCE FETCH ALL":
            COMMAND_QUEUE.clear()
            COMMAND_QUEUE.extend([
                "INFO",
                "GET OPTION",
                "SET OPTION RTLOG=1",
                "SET OPTION PUSH=1",
                "DATA",
                "GET ATTLOG ALL",
                "TRAN DATA",
                "GET ATTLOG ALL"
            ])
            IS_FETCHING_ALL_LOGS = True
            log("🚨 FORCE FETCH: Aggressive commands queued")
        else:
            COMMAND_QUEUE.append(command)
            log(f"✅ COMMAND QUEUED: {command}")
    else:
        log(f"⚠️  This endpoint ({endpoint}) doesn't support queued commands")
    
    return RedirectResponse(url="/", status_code=303)

@app.get("/force_fetch_all")
async def force_fetch_all():
    """Force fetch all attendance logs from device"""
    global COMMAND_QUEUE, IS_FETCHING_ALL_LOGS
    
    COMMAND_QUEUE.clear()
    COMMAND_QUEUE.extend([
        "INFO",
        "GET OPTION",
        "SET OPTION RTLOG=1",
        "SET OPTION PUSH=1",
        "DATA",
        "GET ATTLOG ALL",
        "TRAN DATA",
        "GET ATTLOG ALL"
    ])
    IS_FETCHING_ALL_LOGS = True
    
    log("🚨 MANUAL FORCE FETCH: Aggressive commands queued to get ALL attendance logs")
    return PlainTextResponse("Aggressive fetch initiated. Check logs for progress.")

# ---------------- DEVICE ENDPOINTS ----------------

@app.api_route("/iclock/cdata.aspx", methods=["GET", "POST"])
async def iclock_cdata(request: Request):
    """Handle ALL device data - this is the MAIN endpoint"""
    body = (await request.body()).decode(errors="ignore")
    await log_request(request, body)

    if request.method == "GET":
        # Device is checking if server is alive
        log("📡 Device ping received")
        return PlainTextResponse("OK")

    if request.method == "POST":
        lines = body.splitlines()
        attendance_count = 0
        other_data = []
        
        log(f"📦 Received {len(lines)} lines of data")
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
                
            # Log raw line for debugging
            log(f"📥 RAW LINE: {line}")
            
            # Check for device info
            if "SN=" in line.upper():
                global DEVICE_SN
                DEVICE_SN = line.split("SN=")[1].strip() if "SN=" in line else line.split("sn=")[1].strip()
                log(f"📱 Device SN: {DEVICE_SN}")
                DEVICE_INFO['sn'] = DEVICE_SN
                save_persistent_data()
            
            # Try to parse as attendance (tab-separated)
            elif '\t' in line:
                parts = line.split('\t')
                log(f"📊 Parsing attendance: {len(parts)} parts")
                
                # Check if this looks like attendance data
                if len(parts) >= 2:
                    # Parse with auto-toggle logic (default: True)
                    record = parse_attendance_line(line, apply_auto_toggle=True)
                    if record:
                        # Check for duplicate
                        record_key = f"{record['user_id']}_{record['timestamp']}_{record['status']}"
                        existing = any(
                            f"{r.get('user_id')}_{r.get('timestamp')}_{r.get('status')}" == record_key 
                            for r in ATTENDANCE_DATA
                        )
                        
                        if not existing:
                            ATTENDANCE_DATA.append(record)
                            attendance_count += 1
                            
                            # Create display line with auto-toggle indicator
                            display_line = f"{record['user_id']}\t{record['timestamp']}\t{record['status']}"
                            if record.get('auto_converted'):
                                display_line += f"\t[✔ AUTO: {record.get('original_status', '')} → {record['status']}]"
                            display_line += f"\t{record.get('verification', '')}\t{record.get('workcode', '')}"
                            
                            log_attendance_raw(display_line)
                            log(f"✅ Attendance: User {record['user_id']} at {record['timestamp']} ({record['status']})")
                            
                            # If auto-converted, log the action
                            if record.get('auto_converted'):
                                log(f"   ↳ {record.get('conversion_action', 'Auto-converted')}")
                        else:
                            log(f"⚠️ Duplicate attendance skipped")
                    else:
                        # Store as other data
                        other_data.append(line)
                        log(f"📝 Other data: {line[:100]}")
                else:
                    other_data.append(line)
            else:
                # Non-tab data, might be command response or other info
                other_data.append(line)
                log(f"📝 Non-tab data: {line[:100]}")
        
        if attendance_count > 0:
            save_persistent_data()
            log(f"🎉 Added {attendance_count} attendance records (Total: {len(ATTENDANCE_DATA)})")
        
        if other_data:
            log(f"📄 Also received {len(other_data)} lines of other data")
        
        return PlainTextResponse("OK")

@app.get("/iclock/getrequest.aspx")
async def iclock_getrequest(request: Request):
    """Device pulls commands from here"""
    global DEVICE_SN
    
    # Get query parameters
    sn = request.query_params.get("SN", "")
    if sn:
        DEVICE_SN = sn
        log(f"📱 Device SN from query: {DEVICE_SN}")
        DEVICE_INFO['sn'] = DEVICE_SN
    
    # Log the pull request
    log(f"📡 Device pulling command (SN: {sn})")
    
    # Send next command if available
    if COMMAND_QUEUE:
        command = COMMAND_QUEUE.pop(0)
        log(f"📤 SENDING: {command}")
        
        # Special handling for attendance commands
        if "ATTLOG" in command:
            async def add_next_command():
                await asyncio.sleep(5)
                if not COMMAND_QUEUE:
                    COMMAND_QUEUE.append("GET ATTLOG")
                    log("🔄 Auto-added next GET ATTLOG")
            
            asyncio.create_task(add_next_command())
        
        return PlainTextResponse(command)
    else:
        # Default response
        log("📤 No commands in queue, sending GET ATTLOG")
        return PlainTextResponse("GET ATTLOG")

@app.get("/iclock/registry.aspx")
async def iclock_registry(request: Request):
    """Device registration endpoint"""
    log("📝 DEVICE REGISTRATION")
    
    for key, value in request.query_params.items():
        if key.upper() == "SN":
            global DEVICE_SN
            DEVICE_SN = value
            log(f"📱 Registered Device SN: {DEVICE_SN}")
        DEVICE_INFO[key] = value
    
    log(f"📋 Registration params: {dict(request.query_params)}")
    save_persistent_data()
    return PlainTextResponse("OK")

@app.post("/iclock/devicecmd.aspx")
async def iclock_devicecmd(request: Request):
    """Device command responses"""
    body = (await request.body()).decode(errors="ignore")
    
    # Log first 500 chars
    if len(body) > 500:
        log(f"📋 DEVICE CMD RESPONSE: {body[:500]}... ({len(body)} chars)")
    else:
        log(f"📋 DEVICE CMD RESPONSE: {body}")
    
    # Parse INFO responses
    if "=" in body:
        lines = body.splitlines()
        for line in lines:
            if '=' in line:
                try:
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip()
                    DEVICE_INFO[key] = value
                    log(f"⚙️  Device Info: {key} = {value}")
                except:
                    pass
    
    save_persistent_data()
    return PlainTextResponse("OK")

# ---------------- UTILITY ENDPOINTS ----------------

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
    log("🧹 All logs cleared")
    return PlainTextResponse("OK")

@app.post("/clear_attendance")
async def clear_attendance(request: Request):
    """Clear attendance data"""
    global ATTENDANCE_DATA, ATTENDANCE_DISPLAY, USER_CHECKIN_STATUS
    ATTENDANCE_DATA = []
    ATTENDANCE_DISPLAY = []
    USER_CHECKIN_STATUS = {}
    log("🧹 All attendance data and user status cleared")
    save_persistent_data()
    return PlainTextResponse("OK")

@app.get("/export_attendance")
async def export_attendance(format: str = "csv"):
    """Export attendance data"""
    if not ATTENDANCE_DATA:
        return PlainTextResponse("No attendance data available")
    
    if format == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["User ID", "Timestamp", "Status", "Status Text", "Original Status", "Verification", "Workcode", "Received At", "Auto-Converted"])
        
        for record in ATTENDANCE_DATA:
            writer.writerow([
                record.get('user_id', ''),
                record.get('timestamp', ''),
                record.get('status', ''),
                record.get('status_text', ''),
                record.get('original_status', ''),
                record.get('verification', ''),
                record.get('workcode', ''),
                record.get('received_at', ''),
                "Yes" if record.get('auto_converted') else "No"
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
    
    else:
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
        "data": ATTENDANCE_DATA[-1000:],
        "device_sn": DEVICE_SN,
        "device_info": DEVICE_INFO,
        "fetching_all": IS_FETCHING_ALL_LOGS,
        "device_connected": DEVICE_CONNECTED,
        "auto_converted_count": sum(1 for r in ATTENDANCE_DATA if r.get('auto_converted', False)),
        "user_status_count": len(USER_CHECKIN_STATUS)
    }

@app.get("/reset_device")
async def reset_device():
    """Reset device connection"""
    global COMMAND_QUEUE, IS_FETCHING_ALL_LOGS
    COMMAND_QUEUE = ["INFO", "GET OPTION", "SET OPTION RTLOG=1", "SET OPTION PUSH=1", "DATA", "GET ATTLOG ALL"]
    IS_FETCHING_ALL_LOGS = True
    log("🔄 Device connection reset")
    return PlainTextResponse("OK")

@app.get("/toggle_mode")
async def toggle_auto_toggle():
    """Toggle auto-toggle mode on/off"""
    # This endpoint can be used to enable/disable auto-toggle if needed
    # Currently always enabled based on requirement
    return PlainTextResponse("Auto-toggle mode is ALWAYS ENABLED: CHECK-OUT, CHECK-IN")

@app.get("/favicon.ico")
async def favicon():
    return PlainTextResponse("")

@app.get("/stats")
async def get_stats():
    """Get statistics"""
    today = datetime.utcnow().date()
    today_records = [r for r in ATTENDANCE_DATA 
                    if r.get('timestamp', '').startswith(today.strftime('%Y-%m-%d'))]
    
    auto_converted = sum(1 for r in ATTENDANCE_DATA if r.get('auto_converted', False))
    
    # Check-in vs Check-out count
    check_in_count = sum(1 for r in ATTENDANCE_DATA if r.get('status', '').upper() in ['CHECK-IN', '0'])
    check_out_count = sum(1 for r in ATTENDANCE_DATA if r.get('status', '').upper() in ['CHECK-OUT', '1'])
    
    return {
        "total_records": len(ATTENDANCE_DATA),
        "today_records": len(today_records),
        "unique_users": len(set(r.get('user_id', '') for r in ATTENDANCE_DATA)),
        "auto_converted": auto_converted,
        "check_in_count": check_in_count,
        "check_out_count": check_out_count,
        "user_status_tracking": len(USER_CHECKIN_STATUS),
        "last_update": max((r.get('received_at', '') for r in ATTENDANCE_DATA), default='Never')
    }
