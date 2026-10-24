"""Ortak ayarlar: .env okuma, yollar, günlük."""
import os
import threading
import time

KOK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KAYITLAR = os.path.join(KOK, "kayitlar")
ISTEMLER = os.path.join(KOK, "istemler")
os.makedirs(KAYITLAR, exist_ok=True)


def _env_oku():
    d = {}
    yol = os.path.join(KOK, ".env")
    if os.path.exists(yol):
        with open(yol, encoding="utf-8-sig") as f:  # BOM'lu yazılmış olsa da oku
            for satir in f:
                satir = satir.strip()
                if satir and not satir.startswith("#") and "=" in satir:
                    k, v = satir.split("=", 1)
                    d[k.strip()] = v.strip()
    return d


ENV = _env_oku()


def ayar(ad, varsayilan=None):
    return os.environ.get(ad, ENV.get(ad, varsayilan))


TAKVIM_KLASORU = ayar("TAKVIM_KLASORU", os.path.join(KOK, "veri", "takvim"))
# Gece sessiz saati: bu aralıkta acil olmayan bildirimler Telegram'a sessiz (bildirim sesi çalmadan) gider.
SESSIZ_SAAT = ayar("SESSIZ_SAAT", "23:30-08:00")


def sessiz_mi(simdi=None):
    import datetime as dt
    try:
        bas, bit = (dt.time.fromisoformat(x.strip()) for x in SESSIZ_SAAT.split("-"))
    except ValueError:
        return False
    t = (simdi or dt.datetime.now()).time()
    return (bas <= t or t < bit) if bas > bit else (bas <= t < bit)


KONTROL_PORT = int(ayar("KONTROL_PORT", "7341"))


def dondur(yol, sinir=5 * 2**20):
    """Günlük dosyası 5 MB'ı geçtiyse .1'e çevir (bir önceki .1 silinir): kayitlar/ sınırsız büyümesin."""
    try:
        if os.path.getsize(yol) > sinir:
            os.replace(yol, yol + ".1")
    except OSError:
        pass


GUNLUK = os.path.join(KAYITLAR, ayar("GUNLUK_DOSYASI", "servis.log"))
GUNLUK_SINIR = 5 * 2**20
DENETIM_SATIR = 200               # çalışırken her bu kadar satırda bir boyuta bakılır
_kilit = threading.Lock()
_yazilan = 0
dondur(GUNLUK, GUNLUK_SINIR)
_gunluk = open(GUNLUK, "a", encoding="utf-8", buffering=1)


def _calisirken_dondur():
    """Servis günlerce açık kalabiliyor: açılıştaki dondur yetmez. Windows açık dosyayı yeniden adlandırmaz, önce
    kapatılır. Başka bir süreç dosyayı tutuyorsa (Get-Content -Wait gibi) döndürme bir sonraki denetime kalır."""
    global _gunluk
    try:
        if _gunluk.tell() <= GUNLUK_SINIR:
            return
        _gunluk.close()
        dondur(GUNLUK, GUNLUK_SINIR)
    except (OSError, ValueError):
        pass
    if _gunluk.closed:
        _gunluk = open(GUNLUK, "a", encoding="utf-8", buffering=1)


def log(*parcalar):
    global _yazilan
    satir = time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(p) for p in parcalar)
    with _kilit:
        print(satir, flush=True)
        _gunluk.write(satir + "\n")
        _yazilan += 1
        if _yazilan % DENETIM_SATIR == 0:
            _calisirken_dondur()
