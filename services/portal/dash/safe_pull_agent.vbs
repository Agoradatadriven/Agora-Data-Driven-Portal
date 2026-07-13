' Hidden launcher for the Watcher Safe-pull agent. Task Scheduler runs this every 5 minutes
' (install with install_safe_pull_task.ps1); wscript //B keeps it windowless, and the script
' WAITS for the scrape so the task's IgnoreNew policy prevents overlapping runs (the python
' side holds a PID lock too, covering manually launched full sweeps).
'
' Runs: <repo>\.venv-portal\Scripts\python.exe -u safe_scrape_local.py --queue
' Log:  %TEMP%\watcher_safe_scrape\agent.log
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

dash = fso.GetParentFolderName(WScript.ScriptFullName)
root = fso.GetParentFolderName(fso.GetParentFolderName(fso.GetParentFolderName(dash)))
py = root & "\.venv-portal\Scripts\python.exe"
script = dash & "\safe_scrape_local.py"

logdir = sh.ExpandEnvironmentStrings("%TEMP%") & "\watcher_safe_scrape"
If Not fso.FolderExists(logdir) Then fso.CreateFolder(logdir)
logfile = logdir & "\agent.log"

cmd = "cmd /c """"" & py & """ -u """ & script & """ --queue >> """ & logfile & """ 2>&1"""
sh.Run cmd, 0, True
