import os
import json
import asyncio
import hashlib
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from collections import OrderedDict
import urllib.parse

app = FastAPI(title="eSSL Attendance Fetcher v2.0")

# ------------- CONFIGURATION -------------
DATA_FILE = "attendance_data.json"
DEVICE_INFO_FILE = "device_info.json"
LOG_FILE = "system_logs.txt"
COMM_LOG_FILE = "device_comms.json"

# ------------- DATA STORAGE -------------
ATTENDANCE_RECORDS: List[Dict[str, Any]] = []
LIVE_ATTENDANCE: List[Dict[str, Any]] = []
DEVICE_INFO: Dict[str, str] = OrderedDict()
DEVICE_INFO_TIMESTAMPS: Dict[str, str] = {}
COMMAND_QUEUE: List[str] = []
LOGS: List[str] = []
COMM_LOGS: List[Dict[str, Any]] = []  # Store all device communications
TOTAL_DATA_RECEIVED: int = 0
LAST_DATA_TIME: str = "Never"

# ------------- PERSISTENCE FUNCTIONS -------------
def load_persistent_data():
    """Load previously saved data"""
    global ATTENDANCE_RECORDS, DEVICE_INFO, COMM_LOGS, TOTAL_DATA_RECEIVED
    
    try:
        # Load attendance data
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r') as f:
                data = json.load(f)
                ATTENDANCE_RECORDS = data.get('attendance', [])
                TOTAL_DATA_RECEIVED = data.get('total_data_received', 0)
                print(f"📂 Loaded {len(ATTENDANCE_RECORDS)} attendance records")
                update_live_attendance()
        
        # Load device info
        if os.path.exists(DEVICE_INFO_FILE):
            with open(DEVICE_INFO_FILE, 'r') as f:
                device_data = json.load(f)
                DEVICE_INFO.update(device_data.get('device_info', {}))
                DEVICE_INFO_TIMESTAMPS.update(device_data.get('timestamps', {}))
        
        # Load communication logs
        if os.path.exists(COMM_LOG_FILE):
            with open(COMM_LOG_FILE, 'r') as f:
                comm_data = json.load(f)
                COMM_LOGS = comm_data.get('communications', [])
                LAST_DATA_TIME = comm_data.get('last_data_time', 'Never')
                
    except Exception as e:
        print(f"⚠️ Error loading data: {e}")

def save_persistent_data():
    """Save data to disk"""
    try:
        # Save attendance data
        data = {
            'attendance': ATTENDANCE_RECORDS,
            'device_info': dict(DEVICE_INFO),
            'total_data_received': TOTAL_DATA_RECEIVED,
            'last_updated': datetime.now().isoformat()
        }
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f, indent=2)
        
        # Save device info separately
        device_data = {
            'device_info': dict(DEVICE_INFO),
            'timestamps': DEVICE_INFO_TIMESTAMPS,
            'last_updated': datetime.now().isoformat()
        }
        with open(DEVICE_INFO_FILE, 'w') as f:
            json.dump(device_data, f, indent=2)
        
        # Save communication logs
        comm_data = {
            'communications': COMM_LOGS[-1000:],  # Keep last 1000 communications
            'last_data_time': LAST_DATA_TIME,
            'total_count': len(COMM_LOGS),
            'last_updated': datetime.now().isoformat()
        }
        with open(COMM_LOG_FILE, 'w') as f:
            json.dump(comm_data, f, indent=2)
            
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
            timestamp_str = record.get('timestamp', '')
            if 'T' in timestamp_str:
                record_time = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
            elif len(timestamp_str) >= 19:
                record_time = datetime.strptime(timestamp_str[:19], "%Y-%m-%d %H:%M:%S")
            else:
                continue
                
            if record_time >= live_cutoff:
                LIVE_ATTENDANCE.append(record)
        except Exception as e:
            continue

# ------------- DEVICE DATA PARSING -------------
def parse_device_data(raw_body: str, source: str = "device") -> Dict[str, Any]:
    """
    Parse various device data formats:
    - Attendance records
    - Device information
    - Configuration data
    """
    parsed_data = {
        'raw': raw_body,
        'source': source,
        'timestamp': datetime.now().isoformat(),
        'length': len(raw_body),
        'hash': hashlib.md5(raw_body.encode()).hexdigest()[:8],
        'lines': raw_body.count('\n') + 1,
        'type': 'unknown'
    }
    
    # Check for attendance data format
    if '\t' in raw_body and any(line.strip() for line in raw_body.split('\t') if len(line) > 5):
        parsed_data['type'] = 'attendance'
    
    # Check for device info
    elif 'SN=' in raw_body.upper() or 'DEVICE=' in raw_body.upper():
        parsed_data['type'] = 'device_info'
        parsed_data['parsed'] = parse_device_info(raw_body)
    
    # Check for configuration
    elif 'GET OPTION' in raw_body or 'SET OPTION' in raw_body:
        parsed_data['type'] = 'configuration'
    
    # Check for registration
    elif 'registry.aspx' in raw_body or 'registration' in raw_body.lower():
        parsed_data['type'] = 'registration'
    
    return parsed_data

def parse_device_info(info_string: str) -> Dict[str, str]:
    """Parse device information from various formats"""
    info = {}
    
    # Try to parse key=value pairs
    lines = info_string.strip().split('\n')
    for line in lines:
        line = line.strip()
        
        # Check for SN= format
        if 'SN=' in line.upper():
            parts = line.split('=')
            if len(parts) >= 2:
                info['sn'] = parts[1].strip()
        
        # Check for simple key=value
        elif '=' in line and len(line) > 3:
            key, value = line.split('=', 1)
            info[key.strip().lower()] = value.strip()
        
        # Check for tab-separated
        elif '\t' in line:
            parts = line.split('\t')
            if len(parts) >= 2:
                info[parts[0].strip().lower()] = parts[1].strip()
    
    return info

def parse_attendance_line(line: str) -> Optional[Dict[str, Any]]:
    """Parse attendance line format"""
    parts = line.split('\t')
    if len(parts) < 3:
        return None
    
    record = {
        'user_id': parts[0].strip(),
        'timestamp': parts[1].strip(),
        'status': parts[2].strip(),
        'verification': parts[3].strip() if len(parts) > 3 else '',
        'workcode': parts[4].strip() if len(parts) > 4 else '',
        'received_at': datetime.now().isoformat(),
        'source': 'device'
    }
    
    # Clean timestamp
    timestamp = record['timestamp']
    if ' ' in timestamp and 'T' not in timestamp:
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
    
    # Format for display
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
def log(message: str, level: str = "INFO"):
    """Add timestamped log entry"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] [{level}] {message}"
    LOGS.append(log_entry)
    print(log_entry)
    
    # Keep logs manageable
    if len(LOGS) > 1000:
        LOGS.pop(0)

def log_device_communication(raw_data: str, direction: str = "INCOMING"):
    """Log device communication with raw data"""
    global TOTAL_DATA_RECEIVED, LAST_DATA_TIME
    
    timestamp = datetime.now().isoformat()
    comm_id = hashlib.md5(f"{timestamp}{raw_data}".encode()).hexdigest()[:12]
    
    comm_log = {
        'id': comm_id,
        'timestamp': timestamp,
        'direction': direction,
        'raw_data': raw_data,
        'length': len(raw_data),
        'parsed': parse_device_data(raw_data)
    }
    
    COMM_LOGS.append(comm_log)
    TOTAL_DATA_RECEIVED += len(raw_data)
    LAST_DATA_TIME = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Keep communication logs manageable
    if len(COMM_LOGS) > 2000:
        COMM_LOGS.pop(0)
    
    # Log with raw data preview
    raw_preview = raw_data[:200] + ("..." if len(raw_data) > 200 else "")
    log(f"DEVICE DATA {direction}: {len(raw_data)} chars | RAW: {raw_preview}", "DEVICE")
    
    # Save periodically
    if len(COMM_LOGS) % 10 == 0:
        save_persistent_data()

# ------------- DEVICE COMMUNICATION -------------
async def handle_device_data(body: str, request_url: str = ""):
    """
    Process data received from biometric device
    """
    # Log the raw communication
    log_device_communication(body, "INCOMING")
    
    new_records_count = 0
    device_info_updated = False
    
    # Check if this is a registration/configuration request
    if request_url and 'registry' in request_url:
        # Parse query parameters
        query_params = request_url.split('?')[-1] if '?' in request_url else ''
        params = urllib.parse.parse_qs(query_params)
        
        for key, values in params.items():
            if values:
                DEVICE_INFO[key] = values[0]
                DEVICE_INFO_TIMESTAMPS[key] = datetime.now().isoformat()
                device_info_updated = True
        
        if device_info_updated:
            log(f"📝 Device registration/configuration updated: {len(params)} parameters", "DEVICE")
            save_persistent_data()
    
    # Process body content
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    
    for line in lines:
        # Check for device info in body
        if 'SN=' in line.upper() or 'DEVICE=' in line.upper():
            parsed_info = parse_device_info(line)
            for key, value in parsed_info.items():
                if value:
                    DEVICE_INFO[key] = value
                    DEVICE_INFO_TIMESTAMPS[key] = datetime.now().isoformat()
                    device_info_updated = True
        
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
                
            log(f"✓ {record['status_text']}: User {record['user_id']} at {record['display_time']}", "ATTENDANCE")
    
    if new_records_count > 0 or device_info_updated:
        save_persistent_data()
        
        if new_records_count > 0:
            log(f"📊 Added {new_records_count} new records (Total: {len(ATTENDANCE_RECORDS)})", "ATTENDANCE")
        
        if device_info_updated:
            log(f"📱 Device info updated (Total params: {len(DEVICE_INFO)})", "DEVICE")
    
    return new_records_count, device_info_updated

async def auto_fetch_attendance():
    """Automatically send commands to fetch all attendance"""
    while True:
        try:
            if len(COMMAND_QUEUE) == 0:
                COMMAND_QUEUE.append("GET ATTLOG ALL")
                log("🔄 Auto-queued: GET ATTLOG ALL", "SYSTEM")
            
            await asyncio.sleep(30)
            
        except Exception as e:
            log(f"⚠️ Auto-fetch error: {e}", "ERROR")
            await asyncio.sleep(60)

# ------------- FASTAPI ROUTES -------------

@app.post("/iclock/cdata.aspx")
async def device_data_endpoint(request: Request):
    """Main endpoint for device to send attendance data"""
    body = (await request.body()).decode('utf-8', errors='ignore')
    
    # Get the full URL for context
    url = str(request.url)
    
    # Process the data
    new_records, info_updated = await handle_device_data(body, url)
    
    return PlainTextResponse("OK")

@app.get("/iclock/getrequest.aspx")
async def device_command_endpoint(request: Request):
    """Device pulls commands from here"""
    # Log this request
    query_params = dict(request.query_params)
    if query_params:
        log(f"📥 Device request with params: {query_params}", "DEVICE")
    
    # Update device info from query params
    for key, value in query_params.items():
        if value:
            DEVICE_INFO[key] = value
            DEVICE_INFO_TIMESTAMPS[key] = datetime.now().isoformat()
    
    if COMMAND_QUEUE:
        command = COMMAND_QUEUE.pop(0)
        
        # Log outgoing command
        log_device_communication(command, "OUTGOING")
        log(f"📤 Sending to device: {command}", "COMMAND")
        
        # Auto-add next fetch command
        if "ATTLOG" in command and "ALL" in command:
            async def add_next_command():
                await asyncio.sleep(2)
                if len(COMMAND_QUEUE) == 0:
                    COMMAND_QUEUE.append("GET ATTLOG")
                    log("🔄 Auto-added GET ATTLOG command", "SYSTEM")
            
            asyncio.create_task(add_next_command())
        
        return PlainTextResponse(command)
    
    # Default response if no commands queued
    return PlainTextResponse("GET OPTION")

@app.get("/iclock/registry.aspx")
async def device_registration(request: Request):
    """Device registration endpoint"""
    # Get all query parameters
    params = dict(request.query_params)
    
    # Update device info
    for key, value in params.items():
        if value:
            DEVICE_INFO[key] = value
            DEVICE_INFO_TIMESTAMPS[key] = datetime.now().isoformat()
    
    # Log registration
    log(f"📝 Device registration: {len(params)} parameters received", "DEVICE")
    for key, value in params.items():
        log(f"   {key}: {value}", "DEVICE")
    
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
    
    # Get recent communication logs (for raw data display)
    recent_comms = COMM_LOGS[-20:]  # Last 20 communications
    
    # Convert comm logs to display format
    display_logs = []
    for log_entry in LOGS[-100:]:  # Last 100 system logs
        display_logs.append(log_entry)
    
    # Add raw data from recent communications
    for comm in recent_comms:
        timestamp = datetime.fromisoformat(comm['timestamp']).strftime("%Y-%m-%d %H:%M:%S")
        raw_preview = comm['raw_data'][:100] + ("..." if len(comm['raw_data']) > 100 else "")
        display_logs.append(f"[{timestamp}] [DEVICE DATA {comm['direction']}] RAW: {raw_preview}")
    
    return templates.TemplateResponse("index.html", {
        "request": request,
        "total_records": len(ATTENDANCE_RECORDS),
        "live_records": len(LIVE_ATTENDANCE),
        "device_sn": DEVICE_INFO.get('sn', 'Unknown'),
        "attendance_records": all_records,
        "live_attendance": live_records,
        "command_queue": COMMAND_QUEUE,
        "logs": display_logs,
        "comm_logs": recent_comms,
        "device_info": dict(DEVICE_INFO),
        "device_info_timestamps": DEVICE_INFO_TIMESTAMPS,
        "total_data_received": TOTAL_DATA_RECEIVED,
        "last_data_time": LAST_DATA_TIME,
        "now": datetime.now()
    })

# ------------- API ENDPOINTS -------------

@app.get("/api/attendance/all")
async def get_all_attendance():
    """API endpoint for all attendance records"""
    return JSONResponse({
        "count": len(ATTENDANCE_RECORDS),
        "data": ATTENDANCE_RECORDS[-1000:],
        "device": dict(DEVICE_INFO)
    })

@app.get("/api/attendance/live")
async def get_live_attendance():
    """API endpoint for live attendance"""
    return JSONResponse({
        "count": len(LIVE_ATTENDANCE),
        "data": LIVE_ATTENDANCE,
        "last_updated": datetime.now().isoformat()
    })

@app.get("/api/device/info")
async def get_device_info():
    """Get device information"""
    return JSONResponse({
        "info": dict(DEVICE_INFO),
        "timestamps": DEVICE_INFO_TIMESTAMPS,
        "last_updated": datetime.now().isoformat()
    })

@app.get("/api/command/queue")
async def get_command_queue():
    """Get current command queue"""
    return JSONResponse({
        "queue": COMMAND_QUEUE,
        "count": len(COMMAND_QUEUE)
    })

@app.post("/api/command/send")
async def send_command(command: str = Form("GET ATTLOG ALL")):
    """Send a command to the device"""
    COMMAND_QUEUE.append(command)
    log(f"✅ Queued command: {command}", "COMMAND")
    return JSONResponse({"status": "queued", "command": command})

@app.delete("/api/command/remove/{index}")
async def remove_command(index: int):
    """Remove command from queue"""
    if 0 <= index < len(COMMAND_QUEUE):
        removed = COMMAND_QUEUE.pop(index)
        log(f"🗑️ Removed command: {removed}", "COMMAND")
        return JSONResponse({"status": "removed", "command": removed})
    return JSONResponse({"status": "error", "message": "Invalid index"}, status_code=400)

@app.post("/api/fetch/all")
async def fetch_all_attendance():
    """Force fetch all attendance records from device"""
    COMMAND_QUEUE.clear()
    COMMAND_QUEUE.extend([
        "GET OPTION",
        "GET ATTLOG ALL",
        "GET ATTLOG ALL",  # Send twice to ensure complete fetch
        "DATA",
        "TRAN DATA"
    ])
    
    log("🚀 FORCE FETCH: Queued commands to get ALL attendance", "SYSTEM")
    return JSONResponse({
        "status": "started",
        "message": "Fetching all attendance records from device",
        "commands_queued": len(COMMAND_QUEUE)
    })

@app.get("/api/device/comms")
async def get_device_communications(limit: int = 50):
    """Get device communication logs"""
    return JSONResponse({
        "communications": COMM_LOGS[-limit:],
        "total_count": len(COMM_LOGS),
        "last_data_time": LAST_DATA_TIME,
        "total_data_received": TOTAL_DATA_RECEIVED
    })

@app.get("/api/logs")
async def get_logs(limit: int = 100):
    """Get system logs"""
    return JSONResponse({
        "logs": LOGS[-limit:],
        "total_count": len(LOGS),
        "device_comms_count": len(COMM_LOGS)
    })

@app.delete("/api/logs/clear")
async def clear_logs():
    """Clear system logs (not communication logs)"""
    LOGS.clear()
    log("🗑️ System logs cleared", "SYSTEM")
    return JSONResponse({"message": "System logs cleared"})

@app.get("/api/export/csv")
async def export_attendance_csv():
    """Export attendance data as CSV"""
    import csv
    from io import StringIO
    
    output = StringIO()
    writer = csv.writer(output)
    
    writer.writerow(["User ID", "Date", "Time", "Status", "Status Text", "Verification", "Workcode", "Received At"])
    
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
            record['workcode'],
            record.get('received_at', '')
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

@app.get("/api/export/device-info")
async def export_device_info():
    """Export device information as JSON"""
    device_data = {
        "device_info": dict(DEVICE_INFO),
        "timestamps": DEVICE_INFO_TIMESTAMPS,
        "attendance_count": len(ATTENDANCE_RECORDS),
        "communication_count": len(COMM_LOGS),
        "exported_at": datetime.now().isoformat()
    }
    
    content = json.dumps(device_data, indent=2)
    filename = f"device_info_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    
    return PlainTextResponse(
        content,
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "Content-Type": "application/json"
        }
    )

@app.on_event("startup")
async def startup():
    """Initialize application"""
    load_persistent_data()
    asyncio.create_task(auto_fetch_attendance())
    
    log("🚀 eSSL Attendance Fetcher v2.0 Started", "SYSTEM")
    log(f"📊 Loaded {len(ATTENDANCE_RECORDS)} existing records", "SYSTEM")
    log(f"📱 Device SN: {DEVICE_INFO.get('sn', 'Unknown')}", "SYSTEM")
    log(f"📡 Total communications: {len(COMM_LOGS)}", "SYSTEM")

# Setup templates
templates = Jinja2Templates(directory="templates")

# Add custom Jinja2 filters
def reverse_filter(seq):
    return list(reversed(seq))

templates.env.filters["reverse"] = reverse_filter

