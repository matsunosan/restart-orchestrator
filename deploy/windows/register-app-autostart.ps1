# Option 1. A Scheduled Task with an "At startup" trigger (app_service_type: windows_task).
# Suitable for ordinary .exe/.bat/.py that are not services.
# The task must stay in the Running state while the application runs.
$action    = New-ScheduledTaskAction -Execute "C:\MyApp\MyApp.exe" -WorkingDirectory "C:\MyApp"
$trigger   = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = "PT30S"   # wait for the network
$settings  = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName "StartKioskApp" -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force

# Option 2. A Windows service (app_service_type: windows_service).
# For real services:   New-Service -Name MyAgentService -BinaryPathName "C:\MyAgent\agent.exe" -StartupType Automatic
# To run an ordinary exe as a service, use an NSSM or WinSW wrapper:
#   nssm install MyAgentService C:\MyApp\MyApp.exe
#   nssm set MyAgentService Start SERVICE_AUTO_START
# Auto-restart the service on crash:
#   sc.exe failure MyAgentService reset= 86400 actions= restart/5000/restart/5000/restart/5000
