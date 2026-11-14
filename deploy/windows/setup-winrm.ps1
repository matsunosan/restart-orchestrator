# Run on the Windows host as administrator.
# Enables WinRM. For production, use an HTTPS listener (5986) with a certificate.
Enable-PSRemoting -Force -SkipNetworkProfileCheck
Set-Item WSMan:\localhost\Service\AllowUnencrypted -Value $false
# Allow connections only from the orchestrator server (replace the IP):
Set-NetFirewallRule -Name "WINRM-HTTP-In-TCP" -RemoteAddress 10.0.0.5

# If the account is a LOCAL administrator (not a domain one), disable UAC token filtering for remote sessions:
# New-ItemProperty -Path HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System -Name LocalAccountTokenFilterPolicy -Value 1 -PropertyType DWord -Force

# HTTPS option (recommended):
# $cert = New-SelfSignedCertificate -DnsName $env:COMPUTERNAME -CertStoreLocation Cert:\LocalMachine\My
# New-Item -Path WSMan:\localhost\Listener -Transport HTTPS -Address * -CertificateThumbPrint $cert.Thumbprint -Force
# New-NetFirewallRule -Name WINRM-HTTPS -DisplayName "WinRM HTTPS" -Protocol TCP -LocalPort 5986 -RemoteAddress 10.0.0.5
