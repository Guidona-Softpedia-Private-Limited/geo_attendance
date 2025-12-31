import os
import sqlite3
from fastapi import FastAPI, Request, Form, Query
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from datetime import datetime, timedelta
import asyncio
import json
import re
from typing import List, Dict, Any, Optional
import csv
import io
from contextlib import contextmanager
import aiofiles
import hashlib
import time

app = FastAPI()

# ---------------- DATABASE SETUP ----------------

DATABASE_FILE = "attendance.db"
LOG_FILE = "device_logs.txt"

def init_database():
    """Initialize SQLite database for efficient storage"""
    conn = sqlite3.connect(DATABASE_FILE, check_same_thread=False)
    cursor = conn.cursor()
    
    # Attendance records table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS attendance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        status TEXT,
        status_text TEXT,
        verification TEXT,
        workcode TEXT,
        raw_data TEXT,
        device_sn TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, timestamp, status, verification)
    )
    ''')
    
    # Device logs table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS device_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        log_time TIMESTAMP NOT NULL,
        message TEXT NOT NULL,
        device_sn TEXT,
        endpoint TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    # Command queue table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS command_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        command TEXT NOT NULL,
        endpoint TEXT,
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    # Device info table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS device_info (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    # Create indexes for performance
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_attendance_user ON attendance(user_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_attendance_timestamp ON attendance(timestamp)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_logs_time ON device_logs(log_time)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_logs_device ON device_logs(device_sn)')
    
    conn.commit()
    conn.close()

@contextmanager
def get_db_connection():
    """Context manager for database connections"""
    conn = sqlite3.connect(DATABASE_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

# ---------------- DATA MANAGEMENT ----------------

def save_attendance_record(record: Dict[str, Any], device_sn: str):
    """Save attendance record to database"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute('''
            INSERT OR IGNORE INTO attendance 
            (user_id, timestamp, status, status_text, verification, workcode, raw_data, device_sn)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                record.get('user_id'),
                record.get('timestamp'),
                record.get('status'),
                record.get('status_text'),
                record.get('verification'),
                record.get('workcode'),
                record.get('raw'),
                device_sn
            ))
            conn.commit()
        except Exception as e:
            print(f"Error saving attendance: {e}")

def get_attendance_count() -> int:
    """Get total number of attendance records"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) as count FROM attendance')
        result = cursor.fetchone()
        return result['count'] if result else 0

def get_today_attendance_count() -> int:
    """Get today's attendance count"""
    today = datetime.now().date().strftime('%Y-%m-%d')
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
        SELECT COUNT(*) as count FROM attendance 
        WHERE date(timestamp) = ?
        ''', (today,))
        result = cursor.fetchone()
        return result['count'] if result else 0

def get_attendance_records(limit: int = 100, offset: int = 0, 
                          user_id: Optional[str] = None,
                          date_filter: Optional[str] = None) -> List[Dict]:
    """Get attendance records with filtering and pagination"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        query = '''
        SELECT user_id, timestamp, status, status_text, verification, workcode, raw_data, device_sn
        FROM attendance
        WHERE 1=1
        '''
        params = []
        
        if user_id:
            query += ' AND user_id LIKE ?'
            params.append(f'%{user_id}%')
        
        if date_filter:
            query += ' AND date(timestamp) = ?'
            params.append(date_filter)
        
        query += ' ORDER BY timestamp DESC, id DESC LIMIT ? OFFSET ?'
        params.extend([limit, offset])
        
        cursor.execute(query, params)
        results = cursor.fetchall()
        
        return [dict(row) for row in results]

def get_all_attendance_paginated(page: int = 1, page_size: int = 1000) -> Dict[str, Any]:
    """Get all attendance records with pagination"""
    offset = (page - 1) * page_size
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # Get total count
        cursor.execute('SELECT COUNT(*) as total FROM attendance')
        total = cursor.fetchone()['total']
        
        # Get page data
        cursor.execute('''
        SELECT user_id, timestamp, status, status_text, verification, workcode, raw_data, device_sn
        FROM attendance
        ORDER BY timestamp DESC, id DESC
        LIMIT ? OFFSET ?
        ''', (page_size, offset))
        
        records = [dict(row) for row in cursor.fetchall()]
        
        return {
            'total': total,
            'page': page,
            'page_size': page_size,
            'total_pages': (total + page_size - 1) // page_size,
            'records': records
        }

def save_log_entry(log_time: str, message: str, device_sn: str = None, endpoint: str = None):
    """Save log entry to database"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
        INSERT INTO device_logs (log_time, message, device_sn, endpoint)
        VALUES (?, ?, ?, ?)
        ''', (log_time, message, device_sn, endpoint))
        conn.commit()

def get_logs_count() -> int:
    """Get total number of log entries"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) as count FROM device_logs')
        result = cursor.fetchone()
        return result['count'] if result else 0

def get_logs(limit: int = 200, offset: int = 0) -> List[str]:
    """Get logs with pagination"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
        SELECT log_time, message, device_sn, endpoint
        FROM device_logs
        ORDER BY id DESC
        LIMIT ? OFFSET ?
        ''', (limit, offset))
        
        results = cursor.fetchall()
        logs = []
        for row in results:
            log_line = f"{row['log_time']} - {row['message']}"
            if row['device_sn']:
                log_line += f" [Device: {row['device_sn']}]"
            if row['endpoint']:
                log_line += f" [Endpoint: {row['endpoint']}]"
            logs.append(log_line)
        
        return logs

def get_all_logs_paginated(page: int = 1, page_size: int = 500) -> Dict[str, Any]:
    """Get all logs with pagination"""
    offset = (page - 1) * page_size
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # Get total count
        cursor.execute('SELECT COUNT(*) as total FROM device_logs')
        total = cursor.fetchone()['total']
        
        # Get page data
        cursor.execute('''
        SELECT log_time, message, device_sn, endpoint
        FROM device_logs
        ORDER BY id DESC
        LIMIT ? OFFSET ?
        ''', (page_size, offset))
        
        results = cursor.fetchall()
        logs = []
        for row in results:
            log_line = f"{row['log_time']} - {row['message']}"
            if row['device_sn']:
                log_line += f" [Device: {row['device_sn']}]"
            if row['endpoint']:
                log_line += f" [Endpoint: {row['endpoint']}]"
            logs.append(log_line)
        
        return {
            'total': total,
            'page': page,
            'page_size': page_size,
            'total_pages': (total + page_size - 1) // page_size,
            'logs': logs
        }

def add_command_to_queue(command: str, endpoint: str = "/iclock/getrequest.aspx"):
    """Add command to queue"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
        INSERT INTO command_queue (command, endpoint, status)
        VALUES (?, ?, 'pending')
        ''', (command, endpoint))
        conn.commit()
        return cursor.lastrowid

def get_queued_commands() -> List[str]:
    """Get all pending commands from queue"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
        SELECT command FROM command_queue 
        WHERE status = 'pending'
        ORDER BY id ASC
        ''')
        results = cursor.fetchall()
        return [row['command'] for row in results]

def pop_queued_command() -> Optional[str]:
    """Get and remove the oldest pending command"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # Get the oldest pending command
        cursor.execute('''
        SELECT id, command FROM command_queue 
        WHERE status = 'pending'
        ORDER BY id ASC
        LIMIT 1
        ''')
        result = cursor.fetchone()
        
        if result:
            # Mark as sent
            cursor.execute('''
            UPDATE command_queue 
            SET status = 'sent' 
            WHERE id = ?
            ''', (result['id'],))
            conn.commit()
            return result['command']
        
        return None

def clear_command_queue():
    """Clear all commands from queue"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM command_queue')
        conn.commit()

def get_queue_count() -> int:
    """Get number of pending commands"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) as count FROM command_queue WHERE status = "pending"')
        result = cursor.fetchone()
        return result['count'] if result else 0

def save_device_info(key: str, value: str):
    """Save device information"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
        INSERT OR REPLACE INTO device_info (key, value, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ''', (key, value))
        conn.commit()

def get_device_info() -> Dict[str, str]:
    """Get all device information"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT key, value FROM device_info')
        results = cursor.fetchall()
        return {row['key']: row['value'] for row in results}

def get_device_sn() -> str:
    """Get device serial number"""
    info = get_device_info()
    return info.get('SN', 'Unknown')

# ---------------- LOGGING FUNCTIONS ----------------

def log(msg: str, device_sn: str = None, endpoint: str = None):
    """Add a log entry with timestamp"""
    log_time = datetime.utcnow().isoformat() + "Z"
    save_log_entry(log_time, msg, device_sn, endpoint)
    print(f"{log_time} - {msg}")

def log_request(request: Request, body: str = ""):
    """Log device request details"""
    device_sn = request.query_params.get("SN", "Unknown")
    
    log("DEVICE REQUEST", device_sn, request.url.path)
    log(f"  CLIENT   : {request.client.host if request.client else 'Unknown'}", device_sn)
    log(f"  ENDPOINT : {request.url.path}", device_sn)
    log(f"  METHOD   : {request.method}", device_sn)
    log(f"  QUERY    : {dict(request.query_params)}", device_sn)
    
    if body:
        if len(body) > 1000:
            log(f"  BODY     : {body[:1000]}... ({len(body)} chars)", device_sn)
        else:
            log(f"  BODY     : {body}", device_sn)
    
    log("-" * 60, device_sn)

# ---------------- PARSING FUNCTIONS ----------------

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

# ---------------- BACKGROUND TASKS ----------------

async def auto_send_commands():
    """Automatically send commands to device periodically"""
    while True:
        try:
            queued_commands = get_queued_commands()
            if not queued_commands:
                # Add initial commands to get all data
                add_command_to_queue("INFO")
                add_command_to_queue("GET OPTION")
                add_command_to_queue("GET ATTLOG ALL")  # Get ALL attendance
                log("🤖 Auto-added initial commands to queue")
            
            # Check if we need to request attendance again
            await asyncio.sleep(30)
            
            # Always keep GET ATTLOG in queue to get continuous data
            queued_commands = get_queued_commands()
            if not any("ATTLOG" in cmd for cmd in queued_commands):
                add_command_to_queue("GET ATTLOG")
                log("🔄 Auto-added GET ATTLOG to queue for continuous polling")
                
        except Exception as e:
            log(f"⚠️ Error in auto_send_commands: {e}")
            await asyncio.sleep(60)

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
    "POWEROFF"
]

@app.on_event("startup")
async def startup_event():
    """Initialize application"""
    # Initialize database
    init_database()
    
    # Start background tasks
    asyncio.create_task(auto_send_commands())
    
    log("🚀 eSSL Probe Started - ALL Attendance & Logs System Ready")

# ---------------- UI ROUTES ----------------

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Main dashboard page"""
    current_time = datetime.utcnow().isoformat() + "Z"
    device_sn = get_device_sn()
    
    # Get statistics
    total_records = get_attendance_count()
    today_records = get_today_attendance_count()
    logs_count = get_logs_count()
    queue_count = get_queue_count()
    
    # Get recent logs and attendance
    recent_logs = get_logs(limit=200)
    recent_attendance = get_attendance_records(limit=100)
    
    # Format attendance for display
    attendance_display = []
    for record in recent_attendance:
        line = f"{record.get('user_id', 'N/A')}\t{record.get('timestamp', 'N/A')}\t{record.get('status', 'N/A')}\t{record.get('verification', 'N/A')}\t{record.get('workcode', 'N/A')}"
        attendance_display.append(line)
    
    # Get queued commands
    queued_commands = get_queued_commands()
    
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "logs": recent_logs,
            "attendance": attendance_display,
            "endpoints": ENDPOINTS,
            "commands": COMMANDS,
            "queue": queued_commands,
            "device_sn": device_sn,
            "current_time": current_time,
            "total_records": total_records,
            "today_records": today_records,
            "logs_count": logs_count,
            "queue_count": queue_count,
            "device_info": get_device_info()
        }
    )

@app.get("/get_logs")
async def get_logs_api():
    """AJAX endpoint to get updated logs"""
    total_records = get_attendance_count()
    today_records = get_today_attendance_count()
    logs_count = get_logs_count()
    queue_count = get_queue_count()
    
    recent_logs = get_logs(limit=200)
    recent_attendance = get_attendance_records(limit=100)
    
    # Format attendance for display
    attendance_display = []
    for record in recent_attendance:
        line = f"{record.get('user_id', 'N/A')}\t{record.get('timestamp', 'N/A')}\t{record.get('status', 'N/A')}\t{record.get('verification', 'N/A')}\t{record.get('workcode', 'N/A')}"
        attendance_display.append(line)
    
    return {
        "logs": recent_logs,
        "attendance": attendance_display,
        "queue": get_queued_commands(),
        "queue_count": queue_count,
        "attendance_count": total_records,
        "today_count": today_records,
        "logs_count": logs_count,
        "device_sn": get_device_sn(),
        "device_info": get_device_info()
    }

@app.post("/send_command", response_class=HTMLResponse)
async def send_command(
    request: Request,
    endpoint: str = Form(...),
    command: str = Form(...)
):
    """Send command to device"""
    if endpoint == "/iclock/getrequest.aspx":
        add_command_to_queue(command, endpoint)
        log(f"✅ COMMAND QUEUED: {command}")
    else:
        log(f"⚠️  This endpoint ({endpoint}) doesn't support queued commands")
    
    # Redirect back to home
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/", status_code=303)

@app.post("/clear_queue")
async def clear_queue(request: Request):
    """Clear command queue"""
    clear_command_queue()
    log("🗑️ Command queue cleared")
    return PlainTextResponse("OK")

@app.post("/clear_logs")
async def clear_logs(request: Request):
    """Clear logs from database"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM device_logs')
        conn.commit()
    log("🧹 All logs cleared")
    return PlainTextResponse("OK")

@app.post("/clear_attendance")
async def clear_attendance(request: Request):
    """Clear attendance data"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM attendance')
        conn.commit()
    log("🧹 All attendance data cleared")
    return PlainTextResponse("OK")

@app.get("/export_attendance")
async def export_attendance(format: str = "csv"):
    """Export attendance data in various formats"""
    if get_attendance_count() == 0:
        return PlainTextResponse("No attendance data available")
    
    if format == "csv":
        # Create CSV
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Write header
        writer.writerow(["User ID", "Timestamp", "Status", "Status Text", "Verification", "Workcode", "Device SN", "Raw Data"])
        
        # Get all records
        all_records = get_attendance_records(limit=1000000)  # Large limit to get all
        
        # Write data
        for record in all_records:
            writer.writerow([
                record.get('user_id', ''),
                record.get('timestamp', ''),
                record.get('status', ''),
                record.get('status_text', ''),
                record.get('verification', ''),
                record.get('workcode', ''),
                record.get('device_sn', ''),
                record.get('raw_data', '')
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
        all_records = get_attendance_records(limit=1000000)
        content = json.dumps(all_records, indent=2)
        filename = f"attendance_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        
        return PlainTextResponse(
            content,
            headers={
                "Content-Disposition": f"attachment; filename={filename}",
                "Content-Type": "application/json"
            }
        )

@app.get("/export_logs")
async def export_logs(format: str = "csv"):
    """Export logs in various formats"""
    if get_logs_count() == 0:
        return PlainTextResponse("No logs available")
    
    if format == "csv":
        # Create CSV
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Write header
        writer.writerow(["Timestamp", "Message", "Device SN", "Endpoint"])
        
        # Get all logs
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM device_logs ORDER BY id')
            logs = [dict(row) for row in cursor.fetchall()]
        
        # Write data
        for log_entry in logs:
            writer.writerow([
                log_entry.get('log_time', ''),
                log_entry.get('message', ''),
                log_entry.get('device_sn', ''),
                log_entry.get('endpoint', '')
            ])
        
        content = output.getvalue()
        filename = f"logs_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        
        return PlainTextResponse(
            content,
            headers={
                "Content-Disposition": f"attachment; filename={filename}",
                "Content-Type": "text/csv"
            }
        )
    
    else:  # JSON format
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM device_logs ORDER BY id')
            logs = [dict(row) for row in cursor.fetchall()]
        
        content = json.dumps(logs, indent=2)
        filename = f"logs_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        
        return PlainTextResponse(
            content,
            headers={
                "Content-Disposition": f"attachment; filename={filename}",
                "Content-Type": "application/json"
            }
        )

@app.get("/get_all_attendance")
async def get_all_attendance_api(
    page: int = Query(1, ge=1),
    page_size: int = Query(1000, ge=1, le=10000),
    user_id: Optional[str] = None,
    date_filter: Optional[str] = None
):
    """API endpoint to get all attendance data with pagination"""
    paginated_data = get_attendance_records_paginated(page, page_size, user_id, date_filter)
    return paginated_data

@app.get("/get_all_logs")
async def get_all_logs_api(
    page: int = Query(1, ge=1),
    page_size: int = Query(500, ge=1, le=1000)
):
    """API endpoint to get all logs with pagination"""
    paginated_data = get_all_logs_paginated(page, page_size)
    return paginated_data

def get_attendance_records_paginated(page: int = 1, page_size: int = 1000, 
                                    user_id: Optional[str] = None,
                                    date_filter: Optional[str] = None) -> Dict[str, Any]:
    """Get attendance records with pagination and filtering"""
    offset = (page - 1) * page_size
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # Build query for total count
        count_query = 'SELECT COUNT(*) as total FROM attendance WHERE 1=1'
        count_params = []
        
        # Build query for data
        data_query = '''
        SELECT user_id, timestamp, status, status_text, verification, workcode, raw_data, device_sn
        FROM attendance
        WHERE 1=1
        '''
        data_params = []
        
        if user_id:
            count_query += ' AND user_id LIKE ?'
            data_query += ' AND user_id LIKE ?'
            count_params.append(f'%{user_id}%')
            data_params.append(f'%{user_id}%')
        
        if date_filter:
            count_query += ' AND date(timestamp) = ?'
            data_query += ' AND date(timestamp) = ?'
            count_params.append(date_filter)
            data_params.append(date_filter)
        
        # Get total count
        cursor.execute(count_query, count_params)
        total = cursor.fetchone()['total']
        
        # Get paginated data
        data_query += ' ORDER BY timestamp DESC, id DESC LIMIT ? OFFSET ?'
        data_params.extend([page_size, offset])
        
        cursor.execute(data_query, data_params)
        records = [dict(row) for row in cursor.fetchall()]
        
        return {
            'total': total,
            'page': page,
            'page_size': page_size,
            'total_pages': (total + page_size - 1) // page_size,
            'records': records
        }

# ---------------- DEVICE ENDPOINTS ----------------

@app.api_route("/iclock/cdata.aspx", methods=["GET", "POST"])
async def iclock_cdata(request: Request):
    """Handle attendance data push from device"""
    body = (await request.body()).decode(errors="ignore")
    log_request(request, body)
    
    if request.method == "GET":
        return PlainTextResponse("OK")
    
    if request.method == "POST":
        lines = body.splitlines()
        attendance_count = 0
        device_sn = get_device_sn()
        
        for line in lines:
            if not line.strip():
                continue
                
            # Check for device info
            if "SN=" in line.upper():
                sn_match = re.search(r'SN=(\S+)', line, re.IGNORECASE)
                if sn_match:
                    device_sn = sn_match.group(1)
                    save_device_info('SN', device_sn)
                    log(f"📱 Device SN: {device_sn}", device_sn)
            
            # Parse attendance data (tab-separated)
            elif '\t' in line and not line.startswith("GET OPTION") and not line.startswith("INFO"):
                log(f"📥 ATTENDANCE LINE: {line}", device_sn)
                
                # Parse and store
                record = parse_attendance_line(line)
                if record:
                    save_attendance_record(record, device_sn)
                    attendance_count += 1
                    
                    # Log details
                    log(f"👤 User: {record['user_id']}, ⏰ Time: {record['timestamp']}, 📊 Status: {record['status_text']}", device_sn)
        
        if attendance_count > 0:
            log(f"✅ Added {attendance_count} new attendance records (Total: {get_attendance_count()})", device_sn)
        
        return PlainTextResponse("OK")

@app.get("/iclock/getrequest.aspx")
async def iclock_getrequest(request: Request):
    """Device pulls commands from here"""
    device_sn = request.query_params.get("SN", "Unknown")
    log("📡 DEVICE PULLING COMMAND", device_sn)
    
    if device_sn != "Unknown":
        save_device_info('SN', device_sn)
        log(f"📱 Device SN from query: {device_sn}", device_sn)
    
    await asyncio.sleep(0.5)
    
    command = pop_queued_command()
    
    if command:
        log(f"📤 SENDING COMMAND: {command}", device_sn)
        
        # Special handling for attendance commands
        if "ATTLOG" in command:
            # Schedule next attendance pull
            async def add_next_attlog():
                await asyncio.sleep(10)
                if not any("ATTLOG" in cmd for cmd in get_queued_commands()):
                    add_command_to_queue("GET ATTLOG")
                    log("🔄 Auto-queued next GET ATTLOG for continuous data", device_sn)
            
            asyncio.create_task(add_next_attlog())
        
        return PlainTextResponse(command)
    else:
        # Default to getting attendance if queue is empty
        log("📤 SENDING DEFAULT: GET ATTLOG", device_sn)
        return PlainTextResponse("GET ATTLOG")

@app.get("/iclock/registry.aspx")
async def iclock_registry(request: Request):
    """Device registration endpoint"""
    device_sn = request.query_params.get("SN", "Unknown")
    log("📝 DEVICE REGISTRATION REQUEST", device_sn)
    
    # Log registration details
    for key, value in request.query_params.items():
        if key.upper() == "SN":
            save_device_info('SN', value)
            log(f"📱 Registered Device SN: {value}", value)
        save_device_info(key, value)
    
    return PlainTextResponse("OK")

@app.post("/iclock/devicecmd.aspx")
async def iclock_devicecmd(request: Request):
    """Device command responses"""
    device_sn = request.query_params.get("SN", "Unknown")
    body = (await request.body()).decode(errors="ignore")
    log(f"📋 DEVICE COMMAND RESPONSE: {body[:500]}", device_sn)
    
    # Parse INFO responses
    if "=" in body and not '\t' in body:
        lines = body.splitlines()
        for line in lines:
            if '=' in line:
                key, value = line.split('=', 1)
                save_device_info(key.strip(), value.strip())
                log(f"⚙️  Device Info: {key.strip()} = {value.strip()}", device_sn)
    
    return PlainTextResponse("OK")

@app.get("/reset_device")
async def reset_device():
    """Reset device connection"""
    clear_command_queue()
    add_command_to_queue("INFO")
    add_command_to_queue("GET OPTION")
    add_command_to_queue("GET ATTLOG ALL")
    log("🔄 Device connection reset - queued initial commands")
    return PlainTextResponse("OK")

@app.get("/search_attendance")
async def search_attendance(
    user_id: Optional[str] = None,
    date: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=1000)
):
    """Search attendance records"""
    result = get_attendance_records_paginated(page, page_size, user_id, date)
    return JSONResponse(result)

@app.get("/favicon.ico")
async def favicon():
    return PlainTextResponse("")
