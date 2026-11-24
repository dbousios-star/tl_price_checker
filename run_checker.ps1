$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir
$env:PYTHONIOENCODING = "utf-8"
$Timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path "checker.log" -Value "[$Timestamp] Starting check..." -Encoding UTF8
# checker.py writes its own log lines to checker.log; stray output/tracebacks go to checker_stdout.log
& "$ProjectDir\.venv\Scripts\python.exe" "$ProjectDir\checker.py" *>> "$ProjectDir\checker_stdout.log"
exit $LASTEXITCODE
