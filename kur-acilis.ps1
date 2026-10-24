# Telegram asistanı açılış görevi — kullanıcı kendisi çalıştırır (kalıcılık kurduğu için).
# Oturum açılınca bekçiyi pencere açmadan başlatır. Kaldırmak için:
#   Unregister-ScheduledTask -TaskName "ClaudeTelefon" -Confirm:$false
$kok = Split-Path -Parent $MyInvocation.MyCommand.Path
$pyw = Join-Path $kok ".venv\Scripts\pythonw.exe"
$bekci = Join-Path $kok "bekci.py"
if (-not (Test-Path $pyw)) { Write-Error "pythonw bulunamadı: $pyw"; exit 1 }

$eylem = New-ScheduledTaskAction -Execute $pyw -Argument "`"$bekci`"" -WorkingDirectory $kok
# Oturum açılışından 30 sn sonra (ağ bağlantısı otursun)
$tetik = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$tetik.Delay = "PT30S"
$ayar = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -MultipleInstances IgnoreNew
try {
    Register-ScheduledTask -TaskName "ClaudeTelefon" -Action $eylem -Trigger $tetik -Settings $ayar `
        -Description "Claude Telegram asistanı (bekci.py -> python -m telefon)" -Force -ErrorAction Stop | Out-Null
} catch {
    Write-Host "Kurulamadı: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
Write-Host "Kuruldu. Bir sonraki oturum açılışında servis kendiliğinden başlayacak." -ForegroundColor Green
Write-Host "(Şu an bekçi zaten çalışıyor, ayrıca başlatmana gerek yok.)"
