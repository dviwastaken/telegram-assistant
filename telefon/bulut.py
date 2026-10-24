"""Bulut hatırlatıcısıyla (`bulut/`, Cloudflare Worker) eşgüdüm.

Bilgisayar kapalıyken hatırlatmaları Worker gönderir; açıkken servis. İkisi aynı hatırlatmayı göndermesin diye servis,
Notion'daki bir hatırlatmayı göndermeden önce Worker'a "kayıt@zaman" anahtarını sahiplenip sahiplenemediğini sorar.
Worker zaten göndermişse servis göndermez, yalnızca yerelde gönderildi sayar.

`.env`: BULUT_URL (ör. https://claude-hatirlatici.<hesap>.workers.dev) ve BULUT_JETON (Worker'daki SERVIS_JETON).
İkisi de yoksa servis eskisi gibi çalışır (bulut kapalı sayılır).
"""
import atexit
import datetime as dt
import json
import os
import signal
import threading
import time
import urllib.error
import urllib.request

from .ayarlar import KAYITLAR, ayar, log, sessiz_mi

URL = (ayar("BULUT_URL", "") or "").rstrip("/")
JETON = ayar("BULUT_JETON", "") or ""
NABIZ_SN = 300        # bu kadar sn'de bir POST /nabiz (Worker 15 dk nabız alamazsa "Masaüstü yanıt vermiyor" yazar)
KAPANIS_SURE = 3      # sn: kapanırken /kapaniyor bundan uzun bekletmesin
KAPANIS_DENEME = 2    # /kapaniyor süre içinde bu kadar denenir (19 Eki'de tek deneme 2 sn'ye sığmadı)
# Kapanış bildirimi gidemezse (ya da süreç arada öldürülürse) açılışta geriye dönük düzeltme için yerel işaret
KAPANIS_ISARETI = os.path.join(KAYITLAR, "kapanis_bekliyor.json")
UYKU_SURE = 2         # sn: uykuya geçerken Windows birkaç sn tanır; /kapaniyor en çok bu kadar bekletir
UYANIS_ARALIK = (0, 5, 10, 15, 30)   # sn: uyanınca ağ birkaç sn gecikebilir; ~1 dk boyunca bu aralıklarla nabız dene
PBT_APMSUSPEND, PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC = 4, 7, 18
OTURUM_SURE = 2       # sn: WM_ENDSESSION'da /kapaniyor en çok bu kadar bekletir (Windows ~5 sn tanır)
WM_DESTROY, WM_CLOSE, WM_QUERYENDSESSION, WM_ENDSESSION, WM_POWERBROADCAST = 0x2, 0x10, 0x11, 0x16, 0x218
ENDSESSION_LOGOFF = 0x80000000
PENCERE_SINIFI = "ClaudeTelefonBulut"
_son_hata = [0.0]
_uyarildi = [False]
_son_nabiz_hatasi = [0.0]
_kapanis_gitti = [False]
_kapanis_kilidi = threading.Lock()
_dur = threading.Event()   # testte nabız döngüsünü durdurmak için
_uyanis_kilidi = threading.Lock()   # 7 ve 18 çoğu zaman ikisi birden gelir: aynı anda tek deneme dizisi
_uyku_kaydi = {}   # ctypes geri çağırması, parametre yapısı ve tutamaç: çöp toplayıcı silmesin
_pencere_kaydi = {}   # gizli pencerenin WNDPROC geri çağırması, sınıfı, user32 ve tutamacı: çöp toplayıcı silmesin


def etkin():
    return bool(URL and JETON)


def _iste(yol, govde=None, sure=8):
    istek = urllib.request.Request(URL + yol, method="POST" if govde is not None else "GET",
                                   data=json.dumps(govde).encode() if govde is not None else None,
                                   headers={"Authorization": f"Bearer {JETON}", "Content-Type": "application/json",
                                            "User-Agent": "claude-telefon"})
    with urllib.request.urlopen(istek, timeout=sure) as r:
        return json.loads(r.read().decode())


def _anahtar_govdesi(notion_id, zaman):
    """İstek gövdesi: Worker'daki sahiplenme anahtarı "kayıt@YYYY-MM-DDTHH:MM"."""
    return {"anahtar": f"{notion_id}@{zaman[:16]}"}


def _seyrek_log(son, *parcalar):
    """Aynı tür hata günlüğe en çok 5 dk'da bir yazılsın (çağrılar 20 sn'de bir gelebilir). son: [zaman damgası]."""
    if time.time() - son[0] > 300:
        son[0] = time.time()
        log(*parcalar)


def _yapilandirma_hatasi(e):
    """401/400/500 ağ sorunu değil, kurulum hatasıdır (jeton yanlış, sırlar eksik): bir kez kullanıcıya de yaz."""
    _seyrek_log(_son_hata, f"bulut: YAPILANDIRMA HATASI HTTP {e.code} — BULUT_JETON ya da Worker sırları yanlış; "
                           "bulut\\kur.ps1'i yeniden çalıştırın")
    if not _uyarildi[0]:
        _uyarildi[0] = True
        from . import telegram
        telegram.gonder(f"⚠️ Bulut hatırlatıcısı servisi reddediyor (HTTP {e.code}).\n"
                        "Jeton ya da sırlar yanlış olabilir; bilgisayar kapalıyken hatırlatmalar gitmeyebilir.\n"
                        "Bilgisayarda bulut\\kur.ps1 yeniden çalıştırılmalı.", gecmise=False, sessiz=sessiz_mi())


def sahiplen(notion_id, zaman):
    """True: servis göndersin. False: Worker göndermiş, gönderme. None: Worker'a sorulamadı (ağ ya da yapılandırma
    hatası; karar çağırana kalır)."""
    try:
        return bool(_iste("/sahiplen", _anahtar_govdesi(notion_id, zaman))["kazandi"])
    except urllib.error.HTTPError as e:  # URLError'ın alt sınıfı: önce bu
        if e.code in (400, 401, 403, 500):
            _yapilandirma_hatasi(e)
        else:
            _seyrek_log(_son_hata, f"bulut: sahiplenme sorulamadı: HTTP {e.code}")
        return None
    except (urllib.error.URLError, OSError, ValueError, KeyError, TimeoutError) as e:
        _seyrek_log(_son_hata, "bulut: sahiplenme sorulamadı:", repr(e)[:150])
        return None


def teslim(notion_id, zaman):
    """Sahiplenilen hatırlatma Telegram'a gitti: bulut devralmasın (yalnızca gerekirse Notion'u kapatsın)."""
    try:
        return bool(_iste("/teslim", _anahtar_govdesi(notion_id, zaman)).get("teslim"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
        log("bulut: teslim bildirilemedi:", repr(e)[:150])
        return False


def birak(notion_id, zaman):
    """Sahiplenilen hatırlatma Telegram'a gidemedi: bırak ki bilgisayar kapanırsa bulut göndersin."""
    try:
        return bool(_iste("/birak", _anahtar_govdesi(notion_id, zaman)).get("birakildi"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
        log("bulut: sahiplenme bırakılamadı:", repr(e)[:150])
        return False


def durum():
    """Worker'ın son taraması (/durum için). Ulaşılamazsa None."""
    try:
        return _iste("/durum", sure=5)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


# ---------------------------------------------------------------- nabız: masaüstü sustu mu
def nabiz():
    """Masaüstü açık: Worker'a nabız. Hiçbir zaman hata fırlatmaz; hata günlüğe en çok 5 dk'da bir yazılır.
    Önce bekleyen kapanış işareti işlenir: ilk nabız Worker'daki "uyarıldı" bilgisini sıfırlar."""
    try:
        _kapanis_isaretini_isle()
    except Exception as e:
        log("bulut: kapanış işareti işlenemedi:", repr(e)[:150])
    try:
        _iste("/nabiz", {})
        _kapanis_gitti[0] = False   # yeniden açıldık: sonraki kapanış yine bildirilsin
        return True
    except Exception as e:
        _seyrek_log(_son_nabiz_hatasi, "bulut: nabız gönderilemedi:", repr(e)[:150])
        return False


def kapaniyor(*_, sure=KAPANIS_SURE):
    """Temiz kapanış: Worker bir sonraki nabıza kadar "sustu" uyarısı yazmasın. Tek seferlik; toplam `sure` sn
    içinde KAPANIS_DENEME kez denenir. Önce yerel işaret yazılır: bildirim gidemezse (ya da Windows süreci arada
    bitirirse) açılışta _kapanis_isaretini_isle Worker'ın yanlış "sustu" uyarısını düzeltir."""
    with _kapanis_kilidi:  # uyku bildirimi ve WM_POWERBROADCAST aynı anda gelebilir: tek /kapaniyor
        if not etkin() or _kapanis_gitti[0]:
            return False
        _kapanis_gitti[0] = True
    _isaret_yaz()
    bitis = time.monotonic() + sure
    hata = None
    for kalan_deneme in range(KAPANIS_DENEME, 0, -1):
        kalan = bitis - time.monotonic()
        if kalan <= 0.05:
            break
        try:
            _iste("/kapaniyor", {}, sure=kalan / kalan_deneme)   # kalan süre denemelere bölünür
            _isaret_sil()
            log("bulut: temiz kapanış bildirildi")
            return True
        except Exception as e:
            hata = e
    log("bulut: kapanış bildirilemedi (açılışta geriye dönük bildirilecek):", repr(hata)[:150])
    return False


def _isaret_yaz():
    try:
        with open(KAPANIS_ISARETI, "w", encoding="utf-8") as f:
            json.dump({"ms": int(time.time() * 1000)}, f)
    except OSError as e:
        log("bulut: kapanış işareti yazılamadı:", repr(e)[:100])


def _isaret_sil():
    try:
        os.remove(KAPANIS_ISARETI)
    except FileNotFoundError:
        pass
    except OSError as e:
        log("bulut: kapanış işareti kaldırılamadı:", repr(e)[:100])


def _kapanis_isaretini_isle():
    """Nabızdan önce: son kapanış ya da uyku temizdi ama Worker'a bildirilemediyse (işaret duruyor) ve Worker bu
    yüzden "Masaüstü yanıt vermiyor" yazdıysa, kullanıcıya o uyarının yanlış alarm olduğu yazılır. Uyarının sessizliği
    kapanıştan önce başladıysa (gerçek kesinti) bir şey yazılmaz. Worker'a ulaşılamazsa işaret sonraki nabıza kalır.
    True: düzeltme gönderildi."""
    try:
        with open(KAPANIS_ISARETI, encoding="utf-8") as f:
            ms = int(json.load(f)["ms"])
    except FileNotFoundError:
        return False
    except (OSError, ValueError, KeyError, TypeError) as e:
        log("bulut: kapanış işareti bozuk, atıldı:", repr(e)[:100])
        _isaret_sil()
        return False
    d = durum()
    if d is None:
        return False
    _isaret_sil()
    n = d.get("nabiz") or {}
    sessiz_ms = n.get("sessiz_ms")
    uyari_kapanistan = (isinstance(sessiz_ms, (int, float))
                        and 0 <= ms - sessiz_ms <= (NABIZ_SN + 60) * 1000)   # son nabız kapanıştan hemen önce
    if not ((n.get("uyarildi") or n.get("acildi")) and uyari_kapanistan):
        log("bulut: önceki kapanış Worker'a bildirilememişti; yanlış uyarı gitmemiş")
        return False
    from . import eylemler, telegram
    zaman = eylemler.tarih_metni(dt.datetime.fromtimestamp(ms / 1000, eylemler.TZ))
    log("bulut: yanlış 'yanıt vermiyor' uyarısı düzeltiliyor (kapanış", zaman + ")")
    telegram.gonder("Önceki \"Masaüstü yanıt vermiyor\" uyarısı yanlış alarmdı: bilgisayar "
                    f"{zaman} temiz kapandı ya da uykuya geçti, ama bu bildirim o an buluta ulaşamadı.",
                    gecmise=False, sessiz=sessiz_mi())
    return True


def _uyanis_nabzi():
    """Uykudan sonra: ağ gelene kadar birkaç kez nabız (en çok ~1 dk). Aynı anda iki dizi çalışmaz."""
    if not _uyanis_kilidi.acquire(blocking=False):
        return False
    try:
        for bekle in UYANIS_ARALIK:
            if bekle and _dur.wait(bekle):
                return False
            if nabiz():
                return True
        return False
    finally:
        _uyanis_kilidi.release()


def _nabiz_dongusu(saat=time.time):
    """Uyku bildirimi kaçarsa yedek: duvar saati beklemenin çok ötesine atladıysa (uyku, donma) bilgisayar yeni
    uyanmıştır; tek nabız yerine ağ gelene kadar birkaç kez denenir."""
    uyandi = False
    while True:
        if etkin():
            if uyandi:
                _uyanis_nabzi()
            else:
                nabiz()
        onceki = saat()
        if _dur.wait(NABIZ_SN):
            return
        gecen = saat() - onceki
        uyandi = gecen > 2 * NABIZ_SN
        if uyandi:
            log(f"bulut: saat {gecen / 60:.0f} dk ileri atladı (uyku ya da donma): hemen nabız")


def _uyku_olayi(tur):
    """Windows güç olayı. Uykuya geçerken temiz kapanış bildirilir (en çok UYKU_SURE sn; Windows uzun beklemez),
    uyanınca arka planda hemen nabız (nabız kapanış bayrağını da sıfırlar)."""
    if tur == PBT_APMSUSPEND:
        log("bulut: bilgisayar uykuya geçiyor")
        is_ = threading.Thread(target=kapaniyor, kwargs={"sure": UYKU_SURE}, daemon=True, name="bulut-uyku")
        is_.start()
        is_.join(UYKU_SURE)   # DNS zaman aşımına uymaz: süreyi iş parçacığıyla kesin sınırla
        return "uyku"
    if tur in (PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC):
        log(f"bulut: bilgisayar uykudan uyandı ({tur})")
        threading.Thread(target=_uyanis_nabzi, daemon=True, name="bulut-uyanis").start()
        return "uyanis"
    return None


def _uyku_geri_cagirma(_baglam, tur, _ayar):
    """DeviceNotifyCallbackRoutine: hata asla Windows'a taşmasın."""
    try:
        _uyku_olayi(tur)
    except Exception as e:
        log("bulut: güç olayı işlenemedi:", repr(e)[:150])
    return 0


def _uyku_kaydet():
    """Uyku/uyanma bildirimleri. Servisin penceresi ve konsolu yok: PowerRegisterSuspendResumeNotification ile
    geri çağırma (DEVICE_NOTIFY_CALLBACK). Kurulamazsa bir kez günlüğe yazar, servis eskisi gibi sürer."""
    if _uyku_kaydi:
        return True
    try:
        import ctypes
        from ctypes import wintypes
        tur = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.c_void_p)

        class Parametre(ctypes.Structure):   # DEVICE_NOTIFY_SUBSCRIBE_PARAMETERS
            _fields_ = [("Callback", tur), ("Context", ctypes.c_void_p)]
        geri = tur(_uyku_geri_cagirma)
        parametre = Parametre(geri, None)
        tutamac = ctypes.c_void_p()
        kaydet = ctypes.WinDLL("powrprof").PowerRegisterSuspendResumeNotification
        kaydet.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        kaydet.restype = wintypes.DWORD
        sonuc = kaydet(2, ctypes.byref(parametre), ctypes.byref(tutamac))  # 2: DEVICE_NOTIFY_CALLBACK
        if sonuc != 0:
            log(f"bulut: uyku bildirimi kaydedilemedi (hata {sonuc})")
            return False
        _uyku_kaydi.update(geri=geri, parametre=parametre, tutamac=tutamac)
        return True
    except (OSError, AttributeError, ImportError, TypeError, ValueError) as e:
        log("bulut: uyku bildirimi kaydedilemedi:", repr(e)[:100])
        return False


# ---------------------------------------------------------------- oturum kapanışı: gizli pencere
def _pencere_olayi(mesaj, wparam, lparam=0):
    """Gizli pencerenin mesajları (saf Python, testte doğrudan çağrılır). Sayı: pencere yordamının sonucu; None:
    DefWindowProcW'ye bırakılır. Servis user32 yüklü, konsolsuz bir süreç: Windows ona CTRL_SHUTDOWN/LOGOFF_EVENT
    göndermez, WM_ENDSESSION'dan sonra süreci bitirir (atexit çalışmaz). Temiz kapanış bu yüzden burada bildirilir."""
    if mesaj == WM_QUERYENDSESSION:
        return 1   # TRUE: kapanışı engelleme
    if mesaj == WM_ENDSESSION:
        if wparam:   # FALSE: kapanış iptal edildi, bir şey yapma
            log(f"bulut: {'oturum' if lparam & ENDSESSION_LOGOFF else 'bilgisayar'} kapanıyor (WM_ENDSESSION)")
            is_ = threading.Thread(target=kapaniyor, kwargs={"sure": OTURUM_SURE}, daemon=True, name="bulut-oturum")
            is_.start()
            is_.join(OTURUM_SURE)   # dönünce Windows süreci bitirebilir: süre iş parçacığıyla kesin sınırlı
        return 0
    if mesaj == WM_POWERBROADCAST:   # uyku bildirimine ikinci yol; kapanış tek seferlik, uyanış dizisi kilitli
        _uyku_olayi(wparam)
        return 1
    return None


def _pencere_yordami(hwnd, mesaj, wparam, lparam):
    """WNDPROC: hata asla Windows'a taşmasın."""
    try:
        sonuc = _pencere_olayi(mesaj, wparam, lparam)
    except Exception as e:
        log("bulut: pencere mesajı işlenemedi:", repr(e)[:150])
        sonuc = None
    if sonuc is not None:
        return sonuc
    user32 = _pencere_kaydi["user32"]
    if mesaj == WM_DESTROY:
        user32.PostQuitMessage(0)
        return 0
    return user32.DefWindowProcW(hwnd, mesaj, wparam, lparam)


def _pencere_dongusu(hazir=None):
    """Kendi iş parçacığında gizli bir üst düzey pencere ve mesaj döngüsü. HWND_MESSAGE değil: yalnızca-mesaj
    pencereleri yayın mesajlarını (WM_QUERYENDSESSION, WM_ENDSESSION, WM_POWERBROADCAST) almaz. Kurulamazsa bir kez
    günlüğe yazar, servis eskisi gibi sürer. `hazir` (threading.Event) kurulum bitince (başarılı ya da değil) kurulur."""
    try:
        import ctypes
        from ctypes import wintypes
        lresult = ctypes.c_ssize_t
        wndproc = ctypes.WINFUNCTYPE(lresult, wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", wndproc), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]
        user32 = ctypes.WinDLL("user32", use_last_error=True)   # ayrı nesne: windll.user32'nin imzaları bozulmasın
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        msgp = ctypes.POINTER(wintypes.MSG)
        for ad, argumanlar, donus in (
                ("DefWindowProcW", [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t], lresult),
                ("RegisterClassW", [ctypes.POINTER(WNDCLASSW)], wintypes.ATOM),
                ("UnregisterClassW", [wintypes.LPCWSTR, wintypes.HINSTANCE], wintypes.BOOL),
                ("CreateWindowExW", [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                     wintypes.HINSTANCE, wintypes.LPVOID], wintypes.HWND),
                ("GetMessageW", [msgp, wintypes.HWND, wintypes.UINT, wintypes.UINT], wintypes.BOOL),
                ("TranslateMessage", [msgp], wintypes.BOOL),
                ("DispatchMessageW", [msgp], lresult),
                ("PostQuitMessage", [ctypes.c_int], None)):
            islev = getattr(user32, ad)
            islev.argtypes, islev.restype = argumanlar, donus
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        ornek = kernel32.GetModuleHandleW(None)
        yordam = wndproc(_pencere_yordami)
        sinif = WNDCLASSW(lpfnWndProc=yordam, hInstance=ornek, lpszClassName=PENCERE_SINIFI)
        if not user32.RegisterClassW(ctypes.byref(sinif)):
            log(f"bulut: kapanış penceresi kaydedilemedi (hata {ctypes.get_last_error()})")
            return False
        _pencere_kaydi.update(yordam=yordam, sinif=sinif, user32=user32)
        # WS_EX_TOOLWINDOW, WS_VISIBLE yok: görünmez, görev çubuğunda ve Alt+Tab'de yok
        hwnd = user32.CreateWindowExW(0x80, PENCERE_SINIFI, "claude-telefon bulut", 0, 0, 0, 0, 0, None, None,
                                      ornek, None)
        if not hwnd:
            log(f"bulut: kapanış penceresi açılamadı (hata {ctypes.get_last_error()})")
            user32.UnregisterClassW(PENCERE_SINIFI, ornek)
            return False
        _pencere_kaydi["hwnd"] = hwnd
    except (OSError, AttributeError, ImportError, TypeError, ValueError) as e:
        log("bulut: kapanış penceresi kurulamadı:", repr(e)[:100])
        return False
    finally:
        if hazir is not None:
            hazir.set()
    mesaj = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(mesaj), None, 0, 0) > 0:   # 0: WM_QUIT, -1: hata
        user32.TranslateMessage(ctypes.byref(mesaj))
        user32.DispatchMessageW(ctypes.byref(mesaj))
    _pencere_kaydi.pop("hwnd", None)
    user32.UnregisterClassW(PENCERE_SINIFI, ornek)
    return True


def _sinyal_geldi(signum, _cerceve):
    kapaniyor()
    raise SystemExit(0)


def _konsol_olayi(olay):
    """Windows konsol olayı: pencere kapandı (2), oturum kapandı (5), bilgisayar kapanıyor (6). Python bunları
    sinyale çevirmediği için ayrıca dinlenir. False: sıradaki işleyici (Python'un Ctrl+C'si) de çalışsın. Gizli
    servis süreci user32 yüklü olduğundan 5 ve 6'yı almaz (onlar için _pencere_olayi); bu kanca konsolla açılınca
    işe yarayan ek yol."""
    if olay in (2, 5, 6):
        kapaniyor()
    return False


def nabiz_baslat():
    """Servis açılışında (ana iş parçacığından): nabız iş parçacığı ve temiz kapanış kancaları. Bekçinin yeniden
    başlatması (Stop-Process -Force) kancaları çalıştırmaz; aradaki ~5 sn eşiğin (15 dk) çok altında."""
    if not etkin():
        return False
    threading.Thread(target=_nabiz_dongusu, daemon=True, name="bulut-nabiz").start()
    atexit.register(kapaniyor)
    for ad in ("SIGTERM", "SIGBREAK"):
        sinyal = getattr(signal, ad, None)
        if sinyal is not None:
            try:
                signal.signal(sinyal, _sinyal_geldi)
            except (ValueError, OSError) as e:  # ana iş parçacığı değil
                log(f"bulut: {ad} kancası kurulamadı:", repr(e)[:100])
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            tur = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
            _konsol_olayi.isleyici = tur(_konsol_olayi)  # çöp toplayıcı silmesin
            if not ctypes.windll.kernel32.SetConsoleCtrlHandler(_konsol_olayi.isleyici, True):
                log("bulut: konsol kapanış kancası kurulamadı")
        except (OSError, AttributeError) as e:
            log("bulut: konsol kapanış kancası kurulamadı:", repr(e)[:100])
        # oturum kapanışı / bilgisayar kapanışı: gizli pencere WM_ENDSESSION alır (asıl yol)
        threading.Thread(target=_pencere_dongusu, daemon=True, name="bulut-pencere").start()
        _uyku_kaydet()   # uyku: süreç donar, nabız kesilir; sahte "yanıt vermiyor" uyarısı olmasın
    return True
