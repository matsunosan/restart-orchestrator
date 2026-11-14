# Windows autologon for applications that need a desktop

Windows services start without a user login; they do not need one.
If the application is windowed (kiosk, dashboard, client), a user must log in after reboot.

1. Create a separate local or domain account without administrator rights, e.g. `kiosk`.
2. Download Sysinternals Autologon (https://learn.microsoft.com/sysinternals/downloads/autologon)
   and run it as administrator:
   `Autologon64.exe kiosk EXAMPLE <password> /accepteula`
   The password is stored as an LSA secret, not in plaintext in the registry.
3. Create a Scheduled Task with an "At log on" trigger for that user:

```powershell
$action    = New-ScheduledTaskAction -Execute "C:\Kiosk\Kiosk.exe"
$trigger   = New-ScheduledTaskTrigger -AtLogOn -User "EXAMPLE\kiosk"
$principal = New-ScheduledTaskPrincipal -UserId "EXAMPLE\kiosk" -LogonType Interactive
$settings  = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "StartKioskApp" -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force
```

4. In config.json for the host: `"app_service_type": "windows_task"`, `"requires_user_session": true`,
   `"session_user": "EXAMPLE\\kiosk"`.

The WinRM account (the orchestrator connects through it) and the autologon account are different.

Risks: anyone physically at the machine gets the kiosk session. So this account must not have
excessive rights. If the kiosk password is changed, autologon stops working and the orchestrator reports a `login` error.
