# Bulut hatırlatıcısını Cloudflare'a kurar ve servisi ona bağlar. Kullanıcı kendi terminalinde çalıştırır:
#   powershell -ExecutionPolicy Bypass -File .\bulut\kur.ps1
# Yaptıkları:
#   1. Cloudflare girişi yoksa tarayıcıda giriş açar (wrangler login).
#   2. Worker'ı yayınlar (wrangler deploy) ve adresini alır.
#   3. .env'e BULUT_URL ve (yoksa rastgele üretilmiş) BULUT_JETON yazar.
#   4. Sırları Worker'a yükler (wrangler secret bulk, stdin'den; ekrana ve diske yazılmaz):
#      NOTION_KEY, TELEGRAM_JETON (sohbet botu), TELEGRAM_SOHBET_ID, SERVIS_JETON.
#   5. Worker'ın /durum ucunu dener ve servisi yeniden başlatır (bekçi 5 sn içinde yeniden açar).
# Yeniden çalıştırmak güvenli: var olan BULUT_JETON korunur, yalnızca yeniden yayınlanır.
# "Stop" değil: PowerShell 5.1'de yerel programın stderr'e yazdığı her uyarı hata sayılıp betiği durdururdu
$ErrorActionPreference = "Continue"
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$bulut = $PSScriptRoot
$kok = Split-Path $bulut -Parent
$envDosya = Join-Path $kok ".env"
$wrangler = Join-Path $bulut "node_modules\wrangler\bin\wrangler.js"
$node = (Get-Command node.exe -ErrorAction SilentlyContinue).Source
if (-not $node) { throw "Node.js bulunamadı: https://nodejs.org adresinden kurun." }
if (-not (Test-Path $node)) { throw "Node bulunamadı: $node" }
if (-not (Test-Path $wrangler)) { throw "wrangler bulunamadı: $wrangler (bulut klasöründe `npm install` çalıştırın)" }
function wr { & $node $wrangler @args }

function EnvOku([string]$ad) {
    foreach ($s in Get-Content $envDosya -Encoding UTF8) {
        if ($s -match "^\s*$ad\s*=\s*(.*)$") { return $Matches[1].Trim().Trim('"') }
    }
    return $null
}
function EnvYaz([string]$ad, [string]$deger) {
    $satirlar = @(Get-Content $envDosya -Encoding UTF8)
    $var = $false
    for ($i = 0; $i -lt $satirlar.Count; $i++) {
        if ($satirlar[$i] -match "^\s*$ad\s*=") { $satirlar[$i] = "$ad=$deger"; $var = $true }
    }
    if (-not $var) { $satirlar += "$ad=$deger" }
    [System.IO.File]::WriteAllLines($envDosya, $satirlar, (New-Object System.Text.UTF8Encoding($false)))
}

Push-Location $bulut
try {
    Write-Host "1/5 Cloudflare girişi denetleniyor..." -ForegroundColor Cyan
    $kim = (wr whoami 2>&1 | ForEach-Object { "$_" }) -join "`n"
    if ($kim -match "not authenticated|You are not logged in") {
        Write-Host "    Giriş yok. Aşağıda bir adres ve kod çıkacak: adresi tarayıcıda aç, kodu onayla, Allow'a bas." -ForegroundColor Yellow
        Write-Host "    (Kodlu giriş: bilgisayarda localhost:8976 geri dönüş sunucusu gerekmez.)" -ForegroundColor Yellow
        wr login --device
        if ($LASTEXITCODE -ne 0) { throw "Cloudflare girişi olmadı." }
    }

    Write-Host "2/5 Worker yayınlanıyor..." -ForegroundColor Cyan
    $cikti = (wr deploy 2>&1 | ForEach-Object { "$_" }) -join "`n"
    Write-Host $cikti
    if ($cikti -match "workers\.dev subdomain") {
        throw "Hesapta workers.dev alt alanı yok. dash.cloudflare.com > Workers & Pages sayfasını bir kez açıp alt alan seç, sonra betiği yeniden çalıştır."
    }
    if ($cikti -match "More than one account") {
        throw "Birden çok Cloudflare hesabı var: `$env:CLOUDFLARE_ACCOUNT_ID = '<hesap kimliği>'` yazıp yeniden çalıştır."
    }
    if ($LASTEXITCODE -ne 0) { throw "Yayınlama başarısız." }
    $url = [regex]::Match($cikti, "https://claude-hatirlatici\.[A-Za-z0-9-]+\.workers\.dev").Value
    if (-not $url) { throw "Yayın adresi çıktıda bulunamadı." }

    Write-Host "3/5 Servis jetonu hazırlanıyor (BULUT_JETON)..." -ForegroundColor Cyan
    $jeton = EnvOku "BULUT_JETON"
    if (-not $jeton) {
        $b = New-Object byte[] 32
        [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
        $jeton = [Convert]::ToBase64String($b).TrimEnd("=").Replace("+", "-").Replace("/", "_")
        EnvYaz "BULUT_JETON" $jeton
    }

    Write-Host "4/5 Sırlar Worker'a yükleniyor..." -ForegroundColor Cyan
    $notion = EnvOku "NOTION_KEY"; if (-not $notion) { $notion = EnvOku "NOTION_JETON" }
    $tg = EnvOku "TELEGRAM_SOHBET_JETONU"
    $sohbet = EnvOku "TELEGRAM_SOHBET_ID"
    if (-not ($notion -and $tg -and $sohbet)) { throw ".env'de NOTION_KEY, TELEGRAM_SOHBET_JETONU ya da TELEGRAM_SOHBET_ID eksik." }
    $sirlar = @{ NOTION_KEY = $notion; TELEGRAM_JETON = $tg; TELEGRAM_SOHBET_ID = $sohbet; SERVIS_JETON = $jeton } |
        ConvertTo-Json -Compress
    $sirlar | & $node $wrangler secret bulk   # boru bir işleve değil doğrudan wrangler'ın stdin'ine gitsin
    if ($LASTEXITCODE -ne 0) { throw "Sırlar yüklenemedi." }
    # adres ancak sırlar yerindeyken yazılır: yarım kurulumda servis buluta sormaya başlamasın
    EnvYaz "BULUT_URL" $url

    Write-Host "5/5 Deneniyor..." -ForegroundColor Cyan
    Start-Sleep 70   # ilk cron taraması
    try { $d = Invoke-RestMethod "$url/durum" -Headers @{ Authorization = "Bearer $jeton" } }
    catch { Write-Host "    /durum denenemedi: $($_.Exception.Message)" -ForegroundColor Yellow; $d = $null }
    if ($d -and $d.son_tarama) {
        Write-Host "    Son tarama: $($d.son_tarama.zaman), bekleyen $($d.son_tarama.bekleyen), hata: $($d.son_tarama.hatalar -join '; ')"
    } else {
        Write-Host "    Henüz tarama yok (cron birkaç dakika içinde başlar)." -ForegroundColor Yellow
    }
    Get-CimInstance Win32_Process | Where-Object { $_.Name -eq "python.exe" -and $_.CommandLine -like "*-m telefon*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Write-Host "Tamam: servis yeniden başlıyor, Telegram'da /durum yazınca 'Bulut hatırlatıcısı' satırı görünmeli." -ForegroundColor Green
} finally {
    Pop-Location
}
