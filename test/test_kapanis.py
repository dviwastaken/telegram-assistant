"""Oturum kapanışı / bilgisayar kapanışı: gizli pencere WM_ENDSESSION'da temiz kapanışı bildirir.

Gizli servis süreci user32 yüklü olduğundan Windows ona CTRL_SHUTDOWN/LOGOFF_EVENT göndermez; kapanış gizli üst düzey
pencerenin WM_QUERYENDSESSION / WM_ENDSESSION mesajlarıyla yakalanır. Worker'a çıkmaz (bulut._iste ve kapaniyor
sahte), oturum kapatılmaz, bilgisayar kapatılmaz ya da uyutulmaz: gerçek pencereye mesajları test kendisi gönderir.
"""
import json
import os
import sys
import tempfile
import threading
import types

os.environ.setdefault("TAKVIM_KLASORU", tempfile.mkdtemp(prefix="takvim-test-"))
os.environ.setdefault("GUNLUK_DOSYASI", "test.log")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from telefon import bulut  # noqa: E402


@pytest.fixture(autouse=True)
def yalitim(monkeypatch, tmp_path):
    monkeypatch.setattr(bulut, "URL", "https://ornek.workers.dev")
    monkeypatch.setattr(bulut, "JETON", "j")
    monkeypatch.setattr(bulut, "_kapanis_gitti", [False])
    monkeypatch.setattr(bulut, "KAPANIS_ISARETI", str(tmp_path / "kapanis_bekliyor.json"))
    monkeypatch.setattr(bulut, "_pencere_kaydi", {})

    def worker_yok(*a, **k):
        raise AssertionError(f"testte gerçek Worker çağrısı: {a[:1]}")
    monkeypatch.setattr(bulut, "_iste", worker_yok)
    satirlar = []
    monkeypatch.setattr(bulut, "log", lambda *a: satirlar.append(" ".join(map(str, a))))
    return satirlar


@pytest.fixture
def kapanis(monkeypatch):
    """kapaniyor sahte: çağrılar (kwargs) kaydedilir."""
    cagri = []
    monkeypatch.setattr(bulut, "kapaniyor", lambda *a, **k: cagri.append(k) or True)
    return cagri


@pytest.fixture
def uyku(monkeypatch):
    gelen = []
    monkeypatch.setattr(bulut, "_uyku_olayi", gelen.append)
    return gelen


# ------------------------------------------------------------------ mesaj eşlemesi (saf Python)
def test_sorgu_kapanisi_engellemez_ve_bildirmez(kapanis):
    assert bulut._pencere_olayi(bulut.WM_QUERYENDSESSION, 0, 0) == 1
    assert bulut._pencere_olayi(bulut.WM_QUERYENDSESSION, 0, bulut.ENDSESSION_LOGOFF) == 1
    assert kapanis == []                                     # yalnızca sorgu: kapanış iptal edilebilir


def test_oturum_bitince_kisa_sureli_temiz_kapanis(kapanis, yalitim):
    assert bulut._pencere_olayi(bulut.WM_ENDSESSION, 1, 0) == 0
    assert kapanis == [{"sure": bulut.OTURUM_SURE}] and bulut.OTURUM_SURE <= 2
    assert any("bilgisayar kapanıyor (WM_ENDSESSION)" in s for s in yalitim)
    assert bulut._pencere_olayi(bulut.WM_ENDSESSION, 1, bulut.ENDSESSION_LOGOFF) == 0
    assert len(kapanis) == 2 and any("oturum kapanıyor" in s for s in yalitim)


def test_kapanis_iptal_edilirse_bildirilmez(kapanis):
    assert bulut._pencere_olayi(bulut.WM_ENDSESSION, 0, 0) == 0
    assert kapanis == []


def test_guc_yayini_uyku_isleyicisine_gider(uyku, kapanis):
    for tur in (bulut.PBT_APMSUSPEND, bulut.PBT_APMRESUMEAUTOMATIC, bulut.PBT_APMRESUMESUSPEND, 10):
        assert bulut._pencere_olayi(bulut.WM_POWERBROADCAST, tur, 0) == 1
    assert uyku == [4, 18, 7, 10] and kapanis == []


def test_ilgisiz_mesaj_varsayilana_kalir(kapanis, uyku):
    for mesaj in (0x1, bulut.WM_DESTROY, bulut.WM_CLOSE, 0x400):
        assert bulut._pencere_olayi(mesaj, 0, 0) is None
    assert kapanis == [] and uyku == []


def test_iki_yol_birden_calissa_kapanis_bir_kez(monkeypatch):
    """WM_ENDSESSION + atexit/konsol kancası ya da iki güç bildirimi: /kapaniyor tek seferlik."""
    gelen = []
    monkeypatch.setattr(bulut, "_iste", lambda yol, govde=None, sure=8: gelen.append((yol, sure)) or {"ok": True})
    bulut._pencere_olayi(bulut.WM_ENDSESSION, 1, 0)
    bulut._pencere_olayi(bulut.WM_ENDSESSION, 1, 0)
    bulut._konsol_olayi(6)
    bulut.kapaniyor()
    assert len(gelen) == 1 and gelen[0][0] == "/kapaniyor" and 0 < gelen[0][1] <= bulut.OTURUM_SURE
    assert not os.path.exists(bulut.KAPANIS_ISARETI)          # bildirim gitti: açılışta düzeltilecek bir şey yok


def test_kapanis_suresi_kesin_sinirli(monkeypatch):
    """Ağ takılsa da WM_ENDSESSION en çok OTURUM_SURE sn bekletir (Windows süreci sonra yine bitirir)."""
    serbest = threading.Event()
    monkeypatch.setattr(bulut, "kapaniyor", lambda **k: serbest.wait(10))
    monkeypatch.setattr(bulut, "OTURUM_SURE", 0.2)
    import time
    t0 = time.monotonic()
    try:
        assert bulut._pencere_olayi(bulut.WM_ENDSESSION, 1, 0) == 0
        assert time.monotonic() - t0 < 2
    finally:
        serbest.set()


# ------------------------------------------------------------------ yeniden deneme ve geriye dönük düzeltme
def test_ilk_deneme_dusse_ikincisi_gider(monkeypatch, yalitim):
    gelen = []

    def bir_kez_dus(yol, govde=None, sure=8):
        gelen.append(sure)
        if len(gelen) == 1:
            raise TimeoutError("ilk deneme")
        return {"ok": True}
    monkeypatch.setattr(bulut, "_iste", bir_kez_dus)
    assert bulut.kapaniyor(sure=1) is True
    # ilk deneme sürenin yarısını alır; hemen düştüğü için ikincisi kalanın tamamını (bitiş anı sabit)
    assert len(gelen) == 2 and gelen[0] <= 0.51 and gelen[1] <= 1.0
    assert not os.path.exists(bulut.KAPANIS_ISARETI)
    assert any("temiz kapanış bildirildi" in s for s in yalitim)


def test_iki_deneme_de_duserse_isaret_kalir(monkeypatch, yalitim):
    gelen = []

    def hep_dus(yol, govde=None, sure=8):
        gelen.append(sure)
        raise OSError("ağ yok")
    monkeypatch.setattr(bulut, "_iste", hep_dus)
    assert bulut.kapaniyor(sure=0.5) is False
    assert len(gelen) == bulut.KAPANIS_DENEME == 2
    assert os.path.exists(bulut.KAPANIS_ISARETI)
    assert any("açılışta geriye dönük bildirilecek" in s for s in yalitim)


def _isaret(ms):
    with open(bulut.KAPANIS_ISARETI, "w", encoding="utf-8") as f:
        json.dump({"ms": ms}, f)


@pytest.fixture
def giden(monkeypatch):
    from telefon import telegram
    mesajlar = []
    monkeypatch.setattr(telegram, "gonder", lambda metin, **k: mesajlar.append((metin, k)) or True)
    return mesajlar


KAPANIS_MS = 1_761_250_000_000


def test_yanlis_uyari_acilista_duzeltilir(monkeypatch, giden):
    _isaret(KAPANIS_MS)
    son_nabiz = KAPANIS_MS - 120_000                           # kapanıştan 2 dk önceki nabız
    monkeypatch.setattr(bulut, "durum", lambda: {"nabiz": {"uyarildi": True, "sessiz_ms": son_nabiz}})
    assert bulut._kapanis_isaretini_isle() is True
    assert len(giden) == 1 and "yanlış alarmdı" in giden[0][0] and giden[0][1]["gecmise"] is False
    assert not os.path.exists(bulut.KAPANIS_ISARETI)
    assert bulut._kapanis_isaretini_isle() is False             # bir kez


def test_acildi_bekleyen_uyari_da_duzeltilir(monkeypatch, giden):
    """Nabız durum okumasından önce gittiyse 'uyarildi' sıfırlanır, 'acildi' + sessiz_ms kalır."""
    _isaret(KAPANIS_MS)
    monkeypatch.setattr(bulut, "durum", lambda: {"nabiz": {"acildi": True, "sessiz_ms": KAPANIS_MS - 1000}})
    assert bulut._kapanis_isaretini_isle() is True and len(giden) == 1


@pytest.mark.parametrize("nabiz", [
    {"kapandi": True, "kapanis_ms": KAPANIS_MS},                  # uyarı gitmemiş (ör. bildirim geç de olsa ulaştı)
    {"uyarildi": True, "sessiz_ms": KAPANIS_MS - 3 * 3600_000},   # uyarı kapanıştan önceki gerçek bir kesintiye ait
    {"uyarildi": True, "sessiz_ms": KAPANIS_MS + 60_000},         # sessizlik kapanıştan sonra: bu kapanışla ilgisiz
    {},
])
def test_yanlis_uyari_yoksa_mesaj_gitmez(monkeypatch, giden, nabiz):
    _isaret(KAPANIS_MS)
    monkeypatch.setattr(bulut, "durum", lambda: {"nabiz": nabiz})
    assert bulut._kapanis_isaretini_isle() is False
    assert giden == [] and not os.path.exists(bulut.KAPANIS_ISARETI)


def test_bulut_okunamazsa_isaret_sonraki_nabiza_kalir(monkeypatch, giden):
    _isaret(KAPANIS_MS)
    monkeypatch.setattr(bulut, "durum", lambda: None)
    assert bulut._kapanis_isaretini_isle() is False
    assert giden == [] and os.path.exists(bulut.KAPANIS_ISARETI)


def test_bozuk_isaret_atilir(monkeypatch, giden, yalitim):
    with open(bulut.KAPANIS_ISARETI, "w", encoding="utf-8") as f:
        f.write("{bozuk")
    monkeypatch.setattr(bulut, "durum", lambda: pytest.fail("bozuk işarette bulut sorulmamalı"))
    assert bulut._kapanis_isaretini_isle() is False
    assert not os.path.exists(bulut.KAPANIS_ISARETI) and any("bozuk" in s for s in yalitim)


def test_isaret_yokken_nabiz_bulutu_sormaz(monkeypatch):
    gelen = []
    monkeypatch.setattr(bulut, "durum", lambda: pytest.fail("işaret yokken /durum sorulmamalı"))
    monkeypatch.setattr(bulut, "_iste", lambda yol, govde=None, sure=8: gelen.append(yol) or {"ok": True})
    assert bulut.nabiz() is True and gelen == ["/nabiz"]


def test_nabiz_once_isareti_isler(monkeypatch, giden):
    sira = []
    _isaret(KAPANIS_MS)
    monkeypatch.setattr(bulut, "durum", lambda: sira.append("durum") or
                        {"nabiz": {"uyarildi": True, "sessiz_ms": KAPANIS_MS - 1000}})
    monkeypatch.setattr(bulut, "_iste", lambda yol, govde=None, sure=8: sira.append(yol) or {"ok": True})
    assert bulut.nabiz() is True
    assert sira == ["durum", "/nabiz"] and len(giden) == 1      # nabız 'uyarıldı' bilgisini sıfırlamadan önce


# ------------------------------------------------------------------ WNDPROC sarmalayıcısı
def _sahte_user32():
    cagri = []
    return cagri, types.SimpleNamespace(DefWindowProcW=lambda *a: cagri.append(("def", a)) or 77,
                                        PostQuitMessage=lambda kod: cagri.append(("quit", kod)))


def test_yordam_hata_firlatmaz_varsayilana_duser(monkeypatch, yalitim):
    cagri, user32 = _sahte_user32()
    bulut._pencere_kaydi["user32"] = user32

    def bozuk(*a):
        raise RuntimeError("beklenmeyen")
    monkeypatch.setattr(bulut, "_pencere_olayi", bozuk)
    assert bulut._pencere_yordami(5, bulut.WM_ENDSESSION, 1, 0) == 77
    assert cagri == [("def", (5, bulut.WM_ENDSESSION, 1, 0))]
    assert any("pencere mesajı işlenemedi" in s for s in yalitim)


def test_yordam_sonuclari_ve_yikim(kapanis):
    cagri, user32 = _sahte_user32()
    bulut._pencere_kaydi["user32"] = user32
    assert bulut._pencere_yordami(5, bulut.WM_QUERYENDSESSION, 0, 0) == 1
    assert bulut._pencere_yordami(5, bulut.WM_ENDSESSION, 1, 0) == 0 and len(kapanis) == 1
    assert bulut._pencere_yordami(5, bulut.WM_DESTROY, 0, 0) == 0 and cagri == [("quit", 0)]
    assert bulut._pencere_yordami(5, 0x400, 3, 4) == 77 and cagri[-1] == ("def", (5, 0x400, 3, 4))


# ------------------------------------------------------------------ kurulum hatası: servis sürer
def test_kurulamazsa_sessizce_surer(monkeypatch, yalitim):
    import ctypes

    def dll_yok(ad, **k):
        raise OSError(f"{ad} yok")
    monkeypatch.setattr(ctypes, "WinDLL", dll_yok, raising=False)
    if not hasattr(ctypes, "WINFUNCTYPE"):
        monkeypatch.setattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, raising=False)
    hazir = threading.Event()
    assert bulut._pencere_dongusu(hazir) is False
    assert hazir.is_set()                                    # bekleyen takılmaz
    assert len(yalitim) == 1 and "kapanış penceresi kurulamadı" in yalitim[0]
    assert bulut._pencere_kaydi == {}


@pytest.mark.skipif(os.name != "nt", reason="Windows'a özgü")
def test_sinif_kaydi_reddedilirse_sessizce_surer(monkeypatch, yalitim):
    import ctypes
    gercek = ctypes.WinDLL

    class User32:
        def __init__(self):
            self._u = gercek("user32", use_last_error=True)
            self.RegisterClassW = Kayit()

        def __getattr__(self, ad):
            return getattr(self._u, ad)

    class Kayit:
        argtypes = restype = None

        def __call__(self, *a):
            ctypes.set_last_error(5)                         # ERROR_ACCESS_DENIED
            return 0

    def sahte(ad, **k):
        return User32() if ad == "user32" else gercek(ad, **k)
    monkeypatch.setattr(ctypes, "WinDLL", sahte)
    assert bulut._pencere_dongusu() is False
    assert yalitim == ["bulut: kapanış penceresi kaydedilemedi (hata 5)"] and bulut._pencere_kaydi == {}


def test_nabiz_baslat_windowsta_pencereyi_baslatir(monkeypatch):
    baslayan = []

    class SahteThread:
        def __init__(self, target=None, daemon=None, name=None, **k):
            baslayan.append((target, daemon, name))

        def start(self):
            pass
    monkeypatch.setattr(bulut, "threading", types.SimpleNamespace(Thread=SahteThread))
    monkeypatch.setattr(bulut.atexit, "register", lambda f: None)
    monkeypatch.setattr(bulut.signal, "signal", lambda s, f: None)
    monkeypatch.setattr(bulut, "_uyku_kaydet", lambda: False)
    monkeypatch.setattr(bulut, "os", types.SimpleNamespace(name="nt"))
    import ctypes
    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(
        kernel32=types.SimpleNamespace(SetConsoleCtrlHandler=lambda f, a: True)), raising=False)
    if not hasattr(ctypes, "WINFUNCTYPE"):
        monkeypatch.setattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, raising=False)
    assert bulut.nabiz_baslat() is True
    assert (bulut._pencere_dongusu, True, "bulut-pencere") in baslayan
    assert (bulut._nabiz_dongusu, True, "bulut-nabiz") in baslayan


def test_nabiz_baslat_windows_disinda_pencere_yok(monkeypatch):
    baslayan = []

    class SahteThread:
        def __init__(self, target=None, daemon=None, name=None, **k):
            baslayan.append(name)

        def start(self):
            pass
    monkeypatch.setattr(bulut, "threading", types.SimpleNamespace(Thread=SahteThread))
    monkeypatch.setattr(bulut.atexit, "register", lambda f: None)
    monkeypatch.setattr(bulut.signal, "signal", lambda s, f: None)
    monkeypatch.setattr(bulut, "os", types.SimpleNamespace(name="posix"))
    assert bulut.nabiz_baslat() is True and baslayan == ["bulut-nabiz"]


# ------------------------------------------------------------------ gerçek pencere (bu Windows makinesinde)
@pytest.mark.skipif(os.name != "nt", reason="Windows'a özgü")
def test_gercek_gizli_pencere_kapanis_mesajlarini_alir(kapanis, uyku):
    import ctypes
    from ctypes import wintypes
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
    u.SendMessageW.restype = ctypes.c_ssize_t
    u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
    u.PostMessageW.restype = wintypes.BOOL
    u.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    u.FindWindowW.restype = wintypes.HWND
    for ad in ("IsWindow", "IsWindowVisible"):
        getattr(u, ad).argtypes = [wintypes.HWND]
        getattr(u, ad).restype = wintypes.BOOL

    hazir, sonuc = threading.Event(), []
    is_ = threading.Thread(target=lambda: sonuc.append(bulut._pencere_dongusu(hazir)), daemon=True,
                           name="test-pencere")
    is_.start()
    assert hazir.wait(5)
    hwnd = bulut._pencere_kaydi.get("hwnd")
    try:
        assert hwnd and u.IsWindow(hwnd) and not u.IsWindowVisible(hwnd)
        # üst düzey (yalnızca-mesaj değil): yayın mesajları yalnızca üst düzey pencerelere gider. FindWindow burada
        # kullanılamaz: çalışan servisin aynı sınıftaki penceresini bulabiliyor.
        u.GetAncestor.argtypes, u.GetAncestor.restype = [wintypes.HWND, wintypes.UINT], wintypes.HWND
        u.GetDesktopWindow.restype = wintypes.HWND
        assert u.GetAncestor(hwnd, 1) == u.GetDesktopWindow()  # GA_PARENT
        assert u.SendMessageW(hwnd, bulut.WM_QUERYENDSESSION, 0, 0) == 1
        assert kapanis == []
        assert u.SendMessageW(hwnd, bulut.WM_ENDSESSION, 0, 0) == 0     # iptal edilen kapanış
        assert kapanis == []
        assert u.SendMessageW(hwnd, bulut.WM_ENDSESSION, 1, 0) == 0
        assert kapanis == [{"sure": bulut.OTURUM_SURE}]
        assert u.SendMessageW(hwnd, bulut.WM_POWERBROADCAST, 10, 0) == 1  # PBT_APMPOWERSTATUSCHANGE
        assert uyku == [10]
        assert is_.is_alive()                                           # kapanış mesajları döngüyü bitirmez
    finally:
        if hwnd:
            u.PostMessageW(hwnd, bulut.WM_CLOSE, 0, 0)                   # DestroyWindow → WM_DESTROY → WM_QUIT
        is_.join(5)
    assert not is_.is_alive() and sonuc == [True]
    assert "hwnd" not in bulut._pencere_kaydi and not u.IsWindow(hwnd)
