"""Hava durumu (Open-Meteo: ücretsiz, anahtarsız). 30 dk önbellekli; her aramanın bağlamına kısa bir özet girer.
Konum .env'de HAVA_KONUM="enlem,boylam" (varsayılan Ankara merkez), adı HAVA_YER."""
import datetime as dt
import json
import threading
import time
import urllib.request

from .ayarlar import ayar, log

KONUM = ayar("HAVA_KONUM", "39.93,32.86")
YER = ayar("HAVA_YER", "Ankara")
ONBELLEK_SN = 1800
ESKI_SN = 2 * 3600                 # bundan eski veride "şu an" denmez, alındığı saat yazılır
GERI_CEKILME = (60, 120, 300, 600, 1200, 1800)   # ardışık hatalarda sonraki denemeye kadar beklenen saniye
_onbellek = {"zaman": 0.0, "veri": None}
_hata = {"sayi": 0, "sonraki": 0.0}   # sonraki: bu zamandan önce ağa çıkılmaz (503/zaman aşımı fırtınasında)
_kilit = threading.Lock()

# WMO hava kodları -> Türkçe
KODLAR = {0: "açık", 1: "çoğunlukla açık", 2: "parçalı bulutlu", 3: "kapalı", 45: "sisli", 48: "kırağılı sis",
          51: "hafif çisenti", 53: "çisenti", 55: "yoğun çisenti", 56: "dondurucu çisenti", 57: "dondurucu çisenti",
          61: "hafif yağmurlu", 63: "yağmurlu", 65: "kuvvetli yağmurlu", 66: "dondurucu yağmur", 67: "dondurucu yağmur",
          71: "hafif kar yağışlı", 73: "kar yağışlı", 75: "yoğun kar yağışlı", 77: "kar taneli",
          80: "hafif sağanaklı", 81: "sağanaklı", 82: "kuvvetli sağanaklı", 85: "kar sağanaklı", 86: "kar sağanaklı",
          95: "gök gürültülü fırtınalı", 96: "dolulu fırtınalı", 99: "dolulu fırtınalı"}


def _indir(timeout):
    enlem, boylam = (x.strip() for x in KONUM.split(","))
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={enlem}&longitude={boylam}"
           "&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m"
           "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max"
           "&timezone=Europe%2FIstanbul&forecast_days=3")
    return json.load(urllib.request.urlopen(url, timeout=timeout))


def _getir(timeout=10):
    """Ağdan çeker ve önbelleğe koyar. Hata sayacını tutar: ilk hata ve düzelme günlüğe yazılır, aradakiler
    yazılmaz (17 ve 21 Ekim'de 503/zaman aşımı günlüğü dolduruyordu)."""
    try:
        veri = _indir(timeout)
    except Exception as e:
        with _kilit:
            _hata["sayi"] += 1
            n = _hata["sayi"]
            _hata["sonraki"] = time.time() + GERI_CEKILME[min(n, len(GERI_CEKILME)) - 1]
        if n == 1:
            log("hava durumu alınamadı:", repr(e)[:120], "(düzelene kadar aralıklarla denenecek)")
        raise
    with _kilit:
        if _hata["sayi"]:
            log(f"hava durumu yeniden alındı ({_hata['sayi']} başarısız denemeden sonra)")
        _hata.update(sayi=0, sonraki=0.0)
        _onbellek.update(zaman=time.time(), veri=veri)
    return veri


def _bekleme():
    """Bir sonraki tazelemeye kadar kaç saniye: başarılıysa önbellek süresi, hatadaysa geri çekilme."""
    with _kilit:
        if _hata["sayi"]:
            return max(1.0, _hata["sonraki"] - time.time())
    return ONBELLEK_SN - 300


def tazeleyici():
    """Servis açılınca: önbelleği arka planda tazele ki sohbet cevabı ağı beklemesin."""
    def dongu():
        while True:
            try:
                _getir()
            except Exception:
                pass                   # _getir günlüğe yazdı; geri çekilme _bekleme'de
            time.sleep(_bekleme())
    if KONUM:
        threading.Thread(target=dongu, daemon=True, name="hava").start()


def ozet(simdi=None):
    """Bağlam için kısa metin; alınamazsa boş liste (arama bundan etkilenmesin). Önbellek eskiyse de bekletmeden
    onu kullanır (tazeleyici yeniler); hiç veri yoksa geri çekilme süresi dolmuşsa bir kez, kısa süreyle dener."""
    if not KONUM:
        return []
    v = _onbellek["veri"]
    if not v:
        if time.time() < _hata["sonraki"]:
            return []                  # servis hatada: her sohbet turu 10 sn ağ beklemesin
        try:
            v = _getir(timeout=5)
        except Exception:
            return []
    simdi = simdi or dt.datetime.now()
    c, g = v["current"], v["daily"]
    yas = time.time() - _onbellek["zaman"]
    ne_zaman = "şu an" if yas < ESKI_SN else f"{dt.datetime.fromtimestamp(_onbellek['zaman']):%H:%M} itibarıyla"
    sat = []
    adlar = {0: "Bugün", 1: "Yarın", 2: "Öbür gün"}
    for i, gun in enumerate(g["time"]):
        fark = (dt.date.fromisoformat(gun) - simdi.date()).days
        if fark not in adlar:
            continue                   # eski önbellekte dünün tahmini "Bugün" diye yazılmasın
        sat.append(f"- {adlar[fark]} ({gun}): {KODLAR.get(g['weather_code'][i], 'değişken')}, "
                   f"{g['temperature_2m_min'][i]:.0f}-{g['temperature_2m_max'][i]:.0f}°C, "
                   f"yağış ihtimali %{g['precipitation_probability_max'][i] or 0:.0f}, "
                   f"rüzgâr en fazla {g['wind_speed_10m_max'][i]:.0f} km/s")
    if yas >= 86400:                   # bir günden eski anlık ölçüm yanıltır, yalnızca tahmin satırları kalsın
        return ([f"Hava durumu ({YER}, güncel veri alınamadı; eldeki tahmin):"] + sat) if sat else []
    return [f"Hava durumu ({YER}): {ne_zaman} {c['temperature_2m']:.0f}°C (hissedilen "
            f"{c['apparent_temperature']:.0f}°C), {KODLAR.get(c['weather_code'], 'değişken')}, "
            f"rüzgâr {c['wind_speed_10m']:.0f} km/s."] + sat
