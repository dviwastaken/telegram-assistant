"""US-013: uyku algılama. Uykuya geçerken temiz kapanış, uyanınca hemen nabız, saat sıçramasında yedek nabız.

Worker'a çıkmaz (bulut._iste sahte), gerçek iş parçacığı ya da gerçek bekleme yok, bilgisayar uyutulmaz.
"""
import os
import sys
import tempfile
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
    monkeypatch.setattr(bulut, "_son_nabiz_hatasi", [0.0])
    monkeypatch.setattr(bulut, "_uyku_kaydi", {})
    monkeypatch.setattr(bulut, "_uyanis_kilidi", bulut.threading.Lock())
    satirlar = []
    monkeypatch.setattr(bulut, "log", lambda *a: satirlar.append(" ".join(map(str, a))))
    return satirlar


@pytest.fixture
def worker(monkeypatch):
    gelen = []

    def iste(yol, govde=None, sure=8):
        gelen.append((yol, sure))
        return {"ok": True}
    monkeypatch.setattr(bulut, "_iste", iste)
    return gelen


@pytest.fixture
def bekleme(monkeypatch):
    """_dur.wait gerçekten beklemesin: istenen süreler kaydedilir."""
    sureler = []
    monkeypatch.setattr(bulut, "_dur", types.SimpleNamespace(wait=lambda sn: sureler.append(sn) or False))
    return sureler


@pytest.fixture
def es_zamanli(monkeypatch):
    """threading.Thread hemen, aynı iş parçacığında çalışsın (Lock gerçek kalır)."""
    baslayan = []

    class Thread:
        def __init__(self, target, kwargs=None, daemon=None, name=None):
            self.target, self.kwargs = target, kwargs or {}
            baslayan.append((target, self.kwargs, daemon, name))

        def start(self):
            self.target(**self.kwargs)

        def join(self, sure=None):
            baslayan.append(("join", sure))
    monkeypatch.setattr(bulut, "threading", types.SimpleNamespace(Thread=Thread, Lock=bulut.threading.Lock))
    return baslayan


# ------------------------------------------------------------------ olay eşlemesi
def test_uykuya_geciste_kisa_sureli_temiz_kapanis(worker, es_zamanli):
    assert bulut._uyku_olayi(bulut.PBT_APMSUSPEND) == "uyku"
    assert len(worker) == 1 and worker[0][0] == "/kapaniyor" and 0 < worker[0][1] <= bulut.UYKU_SURE <= 2
    assert es_zamanli[0][0] is bulut.kapaniyor and es_zamanli[0][1] == {"sure": bulut.UYKU_SURE}
    assert ("join", bulut.UYKU_SURE) in es_zamanli          # süre kesin sınırlı, sonsuza dek beklemez
    assert bulut._kapanis_gitti[0] is True


@pytest.mark.parametrize("tur", [bulut.PBT_APMRESUMEAUTOMATIC, bulut.PBT_APMRESUMESUSPEND])
def test_uyanista_hemen_nabiz_ve_bayrak_sifirlanir(tur, worker, es_zamanli, bekleme):
    bulut._kapanis_gitti[0] = True                          # uykuya geçerken kapanış gitmişti
    assert bulut._uyku_olayi(tur) == "uyanis"
    assert worker == [("/nabiz", 8)] and bekleme == []      # ilk deneme beklemeden
    assert bulut._kapanis_gitti[0] is False                 # sonraki kapanış yine bildirilir
    assert es_zamanli[0][0] is bulut._uyanis_nabzi and es_zamanli[0][2] is True


def test_uyku_ve_uyanis_sirasi(worker, es_zamanli, bekleme):
    bulut._uyku_olayi(bulut.PBT_APMSUSPEND)
    bulut._uyku_olayi(bulut.PBT_APMRESUMEAUTOMATIC)
    bulut._uyku_olayi(bulut.PBT_APMSUSPEND)                 # ikinci uyku da bildirilir
    assert [y for y, _ in worker] == ["/kapaniyor", "/nabiz", "/kapaniyor"]


def test_ilgisiz_guc_olayi_bir_sey_yapmaz(worker, es_zamanli):
    for tur in (10, 32787, 0):                              # pil durumu, ayar değişikliği...
        assert bulut._uyku_olayi(tur) is None
    assert worker == [] and es_zamanli == []


def test_uyanista_ag_gelene_kadar_dener(monkeypatch, bekleme):
    denemeler = []

    def iste(yol, govde=None, sure=8):
        denemeler.append(yol)
        if len(denemeler) < 3:
            raise OSError("ağ henüz yok")
        return {"ok": True}
    monkeypatch.setattr(bulut, "_iste", iste)
    assert bulut._uyanis_nabzi() is True
    assert denemeler == ["/nabiz"] * 3 and bekleme == list(bulut.UYANIS_ARALIK[1:3])
    assert sum(bulut.UYANIS_ARALIK) <= 90                   # ~1 dk, sonsuza dek değil


def test_uyanis_ag_hic_gelmezse_vazgecer(monkeypatch, bekleme, yalitim):
    def kopuk(*a, **k):
        raise OSError("ağ yok")
    monkeypatch.setattr(bulut, "_iste", kopuk)
    assert bulut._uyanis_nabzi() is False
    assert bekleme == [s for s in bulut.UYANIS_ARALIK if s]
    assert sum("nabız gönderilemedi" in s for s in yalitim) == 1   # günlük boğulmaz


def test_ayni_anda_tek_uyanis_dizisi(worker, bekleme):
    assert bulut._uyanis_kilidi.acquire(blocking=False)     # bir dizi zaten sürüyor
    try:
        assert bulut._uyanis_nabzi() is False and worker == []
    finally:
        bulut._uyanis_kilidi.release()


def test_geri_cagirma_hata_firlatmaz(monkeypatch, yalitim):
    def bozuk(_tur):
        raise RuntimeError("beklenmeyen")
    monkeypatch.setattr(bulut, "_uyku_olayi", bozuk)
    assert bulut._uyku_geri_cagirma(None, bulut.PBT_APMSUSPEND, None) == 0
    assert any("güç olayı işlenemedi" in s for s in yalitim)


def test_geri_cagirma_olaya_iletir(monkeypatch):
    gelen = []
    monkeypatch.setattr(bulut, "_uyku_olayi", gelen.append)
    assert bulut._uyku_geri_cagirma(None, 18, None) == 0 and gelen == [18]


# ------------------------------------------------------------------ kayıt
def test_kayit_basarisizsa_sessizce_surer(monkeypatch, yalitim):
    import ctypes

    def dll_yok(ad):
        raise OSError(f"{ad} yok")
    monkeypatch.setattr(ctypes, "WinDLL", dll_yok, raising=False)
    assert bulut._uyku_kaydet() is False
    assert len(yalitim) == 1 and "uyku bildirimi kaydedilemedi" in yalitim[0]
    assert bulut._uyku_kaydi == {}


def test_kayit_hata_kodu_donerse_sessizce_surer(monkeypatch, yalitim):
    import ctypes

    class Islev:
        def __call__(self, *a):
            return 5                                         # ERROR_ACCESS_DENIED
    monkeypatch.setattr(ctypes, "WinDLL",
                        lambda ad: types.SimpleNamespace(PowerRegisterSuspendResumeNotification=Islev()),
                        raising=False)
    if not hasattr(ctypes, "WINFUNCTYPE"):
        monkeypatch.setattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, raising=False)
    assert bulut._uyku_kaydet() is False
    assert yalitim == ["bulut: uyku bildirimi kaydedilemedi (hata 5)"] and bulut._uyku_kaydi == {}


def test_kayit_geri_cagirmayi_modulde_tutar(monkeypatch):
    import ctypes
    cagri = []

    class Islev:
        def __call__(self, bayrak, parametre, tutamac):
            cagri.append(bayrak)
            return 0
    monkeypatch.setattr(ctypes, "WinDLL",
                        lambda ad: types.SimpleNamespace(PowerRegisterSuspendResumeNotification=Islev()),
                        raising=False)
    if not hasattr(ctypes, "WINFUNCTYPE"):
        monkeypatch.setattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, raising=False)
    assert bulut._uyku_kaydet() is True and cagri == [2]    # DEVICE_NOTIFY_CALLBACK
    k = bulut._uyku_kaydi
    assert set(k) == {"geri", "parametre", "tutamac"}       # çöp toplayıcı silmesin
    assert isinstance(k["geri"], ctypes._CFuncPtr)
    assert k["parametre"].Callback is not None
    assert bulut._uyku_kaydet() is True and cagri == [2]    # ikinci kez kaydedilmez


def test_nabiz_baslat_windowsta_uykuyu_kaydeder(monkeypatch):
    kayit = []

    class SahteThread:
        def __init__(self, **k):
            pass

        def start(self):
            pass
    monkeypatch.setattr(bulut, "threading", types.SimpleNamespace(Thread=SahteThread))
    monkeypatch.setattr(bulut.atexit, "register", lambda f: None)
    monkeypatch.setattr(bulut.signal, "signal", lambda s, f: None)
    monkeypatch.setattr(bulut, "_uyku_kaydet", lambda: kayit.append(1) or False)
    monkeypatch.setattr(bulut, "os", types.SimpleNamespace(name="nt"))
    import ctypes
    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(
        kernel32=types.SimpleNamespace(SetConsoleCtrlHandler=lambda f, a: True)), raising=False)
    if not hasattr(ctypes, "WINFUNCTYPE"):
        monkeypatch.setattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, raising=False)
    assert bulut.nabiz_baslat() is True                     # kayıt başarısız olsa da servis sürer
    assert kayit == [1]


# ------------------------------------------------------------------ yedek: saat sıçraması
def _saat(*degerler):
    it = iter(degerler)
    return lambda: next(it)


def _durdur_sonra(monkeypatch, kac):
    """_dur.wait: ilk `kac` NABIZ_SN beklemesi geçer (False), sonraki durdurur; kısa (yeniden deneme) beklemeleri
    geçer."""
    sureler = []

    def wait(sn):
        sureler.append(sn)
        return sn == bulut.NABIZ_SN and sureler.count(bulut.NABIZ_SN) > kac
    monkeypatch.setattr(bulut, "_dur", types.SimpleNamespace(wait=wait))
    return sureler


def test_saat_sicramasi_hemen_nabiz(worker, monkeypatch, yalitim):
    sureler = _durdur_sonra(monkeypatch, 1)
    bulut._nabiz_dongusu(saat=_saat(0, 3 * 3600, 3 * 3600, 3 * 3600 + 300))   # 3 sa uyudu
    assert [y for y, _ in worker] == ["/nabiz", "/nabiz"] and sureler == [bulut.NABIZ_SN] * 2
    assert any("180 dk ileri atladı" in s for s in yalitim)


def test_saat_sicramasinda_ag_yoksa_yeniden_dener(monkeypatch, yalitim):
    denemeler = []

    def iste(yol, govde=None, sure=8):
        denemeler.append(yol)
        if len(denemeler) in (2, 3):                       # uyanınca ağ birkaç sn yok
            raise OSError("ağ yok")
        return {"ok": True}
    monkeypatch.setattr(bulut, "_iste", iste)
    sureler = _durdur_sonra(monkeypatch, 1)
    bulut._nabiz_dongusu(saat=_saat(0, 2000, 2000, 2300))
    assert denemeler == ["/nabiz"] * 4
    assert sureler == [bulut.NABIZ_SN, 5, 10, bulut.NABIZ_SN]  # 5 dk değil, saniyeler içinde yeniden


def test_normal_beklemede_sicrama_sayilmaz(worker, monkeypatch, yalitim):
    sureler = _durdur_sonra(monkeypatch, 2)
    bulut._nabiz_dongusu(saat=_saat(0, 301, 301, 605, 605, 900))
    assert [y for y, _ in worker] == ["/nabiz"] * 3 and sureler == [bulut.NABIZ_SN] * 3
    assert not any("atladı" in s for s in yalitim)
