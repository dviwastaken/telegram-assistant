"""22 Ekim günlüğünden çıkan sorunlar: zaman aşımı uyarıları, haftalık limit tarihi, gece planlayıcısının limitte
boşa denemesi, posta okunamazken yanlış "gecikti" uyarısı, aynı konulu maillerin iki ayrı uyarısı.
Telegram'a, Claude'a, Notion'a ve IMAP'e çıkmaz."""
import datetime as dt
import subprocess
import time
import types

import pytest

from test_cevrimdisi import giden, yalitim  # noqa: F401  (ortak fikstürler)

from telefon import eylemler, limit, planlayici
from telefon.gozculer import sistem
from telefon.gozculer import uyari as uy


# ================================================================== sistem: zaman aşımı
class Dugum:
    def __init__(self, *a, **k):
        pass

    def start(self):
        pass


class SahteUyari:
    def __init__(self):
        self.bildirimler = []

    def bildir(self, onem, metin, kaynak):
        self.bildirimler.append((onem, metin, kaynak))


@pytest.fixture
def gozcu(tmp_path, monkeypatch, tum_kontroller):
    monkeypatch.setattr(sistem.threading, "Thread", Dugum)
    monkeypatch.setattr(sistem, "DURUM", str(tmp_path / "sistem.durum.json"))
    return sistem.SistemGozcusu(SahteUyari())


def _takilan():
    raise subprocess.TimeoutExpired(["claude", "auth", "status"], 30)


def test_zaman_asimi_tek_turda_bildirilmez_ham_hata_yazilmaz(gozcu):
    t0 = 1_800_000_000.0
    for i in range(sistem.ZAMAN_ASIMI_ESIK - 1):  # eşiği 1 olan "Claude girişi" bile hemen uyarmaz
        gozcu._kontrol_et("Claude girişi", _takilan, 1, "onemli", simdi=t0 + i * 300)
    assert gozcu.uyari.bildirimler == []
    gozcu.son_tur = t0 + 600
    assert "izleniyor: Claude girişi" in gozcu.durum_satiri(t0 + 660)  # eşik altında: sorun değil, izleniyor
    gozcu._kontrol_et("Claude girişi", _takilan, 1, "onemli", simdi=t0 + 900)
    (onem, metin, _), = gozcu.uyari.bildirimler
    assert onem == "onemli" and metin == "Claude girişi: kontrol zaman aşımına uğradı (bilgisayar çok yoğun olabilir)"
    assert "TimeoutExpired" not in metin and "\\" not in metin


def test_zaman_asimi_sonrasi_gercek_hata_kendi_esigiyle(gozcu):
    gozcu._kontrol_et("Claude girişi", _takilan, 1, "onemli", simdi=1.0)
    gozcu._kontrol_et("Claude girişi", lambda: (False, "giriş düşmüş"), 1, "onemli", simdi=301.0)
    assert [m for _, m, _ in gozcu.uyari.bildirimler] == ["Claude girişi: giriş düşmüş"]


def test_tailscale_zaman_asimini_yutmaz(monkeypatch):
    def takil(*a, **k):
        raise subprocess.TimeoutExpired(a, 15)
    monkeypatch.setattr(sistem, "_komut", takil)
    with pytest.raises(subprocess.TimeoutExpired):
        sistem.tailscale()


def test_beklenmeyen_hata_tur_adiyla(gozcu):
    def bozuk():
        raise OSError(r"C:\yol\gizli ayrıntı")
    gozcu._kontrol_et("Syncthing", bozuk, 1, "onemli", simdi=1.0)
    assert gozcu.uyari.bildirimler[0][1] == "Syncthing: kontrol yapılamadı (OSError)"


def test_claude_girisi_alti_turda_bir_sorunluyken_her_tur(gozcu, monkeypatch):
    sayac, durum = {"n": 0}, {"tamam": True}

    def giris():
        sayac["n"] += 1
        return durum["tamam"], "giriş düşmüş"
    gozcu.kontroller = [("Claude girişi", giris, 1, "onemli")]
    gozcu.hata = {"Claude girişi": 0}
    uykular = []

    class Bitti(Exception):
        pass

    def uyu(s):
        uykular.append(s)
        if len(uykular) == 1 + 12:  # açılış beklemesi + 12 tur
            raise Bitti
        if len(uykular) == 1 + 6:   # 7. turdan sonra giriş düşer
            durum["tamam"] = False
    monkeypatch.setattr(sistem.time, "sleep", uyu)
    with pytest.raises(Bitti):
        gozcu._dongu()
    # turlar 0 ve 6'da yoklandı; 6'da düştü, 7-11 her tur yoklandı
    assert sayac["n"] == 2 + 5


# ================================================================== limit: haftalık sıfırlanma tarihi
def test_haftalik_limit_tarihi_okunur():
    simdi = dt.datetime(2026, 10, 20, 23, 30)
    z, metin = limit._sifirlanma("You've hit your weekly limit · resets Oct 22, 11pm (Europe/Istanbul)", simdi)
    assert z == dt.datetime(2026, 10, 22, 23, 0) and metin == "22 Eki Per 23:00"


def test_tarihsiz_limit_bugun_ya_da_yarin():
    simdi = dt.datetime(2026, 10, 20, 10, 0)
    assert limit._sifirlanma("hit your session limit · resets 3pm", simdi) == (dt.datetime(2026, 10, 20, 15, 0),
                                                                             "bugün 15:00")
    assert limit._sifirlanma("hit your session limit · resets 9am", simdi)[1] == "yarın 09:00"


def test_yil_donumu_limit_tarihi():
    z, _ = limit._sifirlanma("weekly limit · resets Jan 2, 9am", dt.datetime(2026, 12, 30, 12, 0))
    assert z == dt.datetime(2027, 1, 2, 9, 0)


def test_limit_mesaji_gunu_yazar(giden, monkeypatch):
    monkeypatch.setitem(limit._durum, "bitis", None)
    monkeypatch.setattr(limit, "sessiz_mi", lambda: False)
    assert limit.kontrol("You've hit your weekly limit · resets Oct 22, 11pm", "gece planlayıcısı",
                         dt.datetime(2026, 10, 20, 23, 30))
    assert "22 Eki Per 23:00 civarı sıfırlanacak" in giden[0][0]


# ================================================================== planlayıcı: limitte boşa deneme yok
def _planlayici_dongusu(monkeypatch, tmp_path, bitis, durdur):
    """_dongu'yu `durdur`uncu uykuya kadar çalıştır (1. uyku 23:30'u bekler): planla çağrıları ve uykular."""
    cagri, uykular = [], []
    monkeypatch.setattr(planlayici, "KAYITLAR", str(tmp_path))

    def planla(gun):
        cagri.append(gun)
        if len(cagri) == 1:
            raise planlayici.LimitDolu(bitis)

    class Bitti(Exception):
        pass

    def uyu(s):
        uykular.append(s)
        if len(uykular) >= durdur:
            raise Bitti
    monkeypatch.setattr(planlayici, "planla", planla)
    monkeypatch.setattr(planlayici.time, "sleep", uyu)
    with pytest.raises(Bitti):
        planlayici.GecePlanlayicisi._dongu(None)
    return cagri, uykular


def test_planlayici_limit_sabaha_yetismiyorsa_birakir(monkeypatch, tmp_path):
    # 2. uyku bir sonraki geceyi bekleyen uyku olmalı: 10 dk'lık yeniden deneme yok
    cagri, uykular = _planlayici_dongusu(monkeypatch, tmp_path, dt.datetime.now() + dt.timedelta(days=2), durdur=2)
    assert len(cagri) == 1 and uykular[1] != 600


def test_planlayici_limit_gece_sifirlaninca_yeniden_dener(monkeypatch, tmp_path):
    # sabah 08:30'dan önce sıfırlanan limit: o zamana kadar bekle, sonra bir kez daha dene
    bitis = dt.datetime.now() + dt.timedelta(minutes=20)
    monkeypatch.setattr(planlayici, "EN_ERKEN", dt.time(23, 59, 59))  # sınır yarın 23:59: şimdi + 20 dk hep önce
    cagri, uykular = _planlayici_dongusu(monkeypatch, tmp_path, bitis, durdur=3)
    assert len(cagri) == 2 and 1260 <= uykular[1] <= 1330  # sıfırlanma + 2 dk


def test_planla_limitte_limit_dolu_firlatir(monkeypatch, tmp_path, giden):
    monkeypatch.setattr(planlayici, "KAYITLAR", str(tmp_path))
    monkeypatch.setitem(limit._durum, "bitis", None)
    monkeypatch.setattr(limit, "sessiz_mi", lambda: False)
    cikti = '{"result": "You\'ve hit your weekly limit \\u00b7 resets Oct 22, 11pm"}'
    monkeypatch.setattr(planlayici.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout=cikti))
    with pytest.raises(planlayici.LimitDolu) as e:
        planlayici.planla(dt.date.today() + dt.timedelta(days=1))
    assert e.value.args[0] is not None


# ================================================================== gecikme: posta okunmuyorsa
def _gecikmis_teslim(saat_once=5):
    z = (dt.datetime.now() - dt.timedelta(hours=saat_once)).replace(second=0, microsecond=0)
    eylemler.yaz(eylemler.TAKVIM, {"tekrarlayan": [], "olaylar": [
        {"id": "t1", "baslik": "Sunum", "zaman": z.isoformat(timespec="minutes"), "tur": "Teslim",
         "ders": "BIL251", "durum": "Yapılacak"}]})
    return z


def _posta(son_yoklama, ilk_hata=None):
    return types.SimpleNamespace(kapali=False, son_yoklama=son_yoklama,
                                 erisim=types.SimpleNamespace(durum={"ilk_hata": ilk_hata}))


def test_acilista_posta_yoklanmadan_gecikme_yazilmaz(giden):
    _gecikmis_teslim()
    u = uy.Uyari(baslat=False)
    u.posta = _posta(0.0)
    u.gecikme_kontrol()
    assert giden == []
    assert eylemler.oku(eylemler.TAKVIM, {})["olaylar"][0]["durum"] == "Yapılacak"


def test_posta_okunamazken_gecikme_mesaji_bunu_soyler(giden):
    _gecikmis_teslim()
    u = uy.Uyari(baslat=False)
    u.baslangic = time.time() - 3600
    u.posta = _posta(time.time() - 2 * 86400, ilk_hata=time.time() - 2 * 86400)
    u.gecikme_kontrol()
    metin = giden[0][0]
    assert "okunamadığı için gönderim onayını göremiyorum" in metin and "onayı gelmedi" not in metin
    assert "beri okunamadığı" in metin


def test_posta_bagli_ve_az_once_yokladiysa_sonraki_yoklama_beklenir(giden):
    z = _gecikmis_teslim(saat_once=15.5 / 60)  # sınır (teslim + 15 dk) 30-90 sn önce geçti
    sinir = z.replace(tzinfo=eylemler.TZ).timestamp() + 15 * 60
    u = uy.Uyari(baslat=False)
    u.baslangic = time.time() - 3600
    u.posta = _posta(sinir - 20)  # son yoklama sınırdan hemen önce, 2 dk'dan yeni
    u.posta.bagli = True
    u.gecikme_kontrol()
    assert giden == []


def test_limit_gecmis_ya_da_taninmayan_tarih_yarim_saat():
    simdi = dt.datetime(2026, 10, 23, 10, 0)
    assert limit._sifirlanma("weekly limit · resets Oct 22, 11pm", simdi) == (simdi + dt.timedelta(minutes=30), None)
    assert limit._sifirlanma("weekly limit · resets Thu 22, 11pm", simdi) == (simdi + dt.timedelta(minutes=30), None)


def test_mail_islerken_hata_cikса_toplanan_uyari_gider(giden, monkeypatch):
    from telefon.gozculer import degerlendir as dg
    mailler = [{"id": "o-1", "konu": "Sınav yeri", "kimden": "x", "metin": "a"},
               {"id": "o-2", "konu": "Bozuk", "kimden": "y", "metin": "b"}]
    monkeypatch.setattr(dg, "degerlendir", lambda m: {
        "o-1": {"onem": "onemli", "ozet": "Sınav C 502'de.", "teslimler": []},
        "o-2": {"onem": "onemli", "ozet": "x", "teslimler": [{"bozuk": True}]}})
    u = uy.Uyari(baslat=False)
    monkeypatch.setattr(u, "teslim_isle", lambda t, k: (_ for _ in ()).throw(RuntimeError("bozuk teslim")))
    with pytest.raises(RuntimeError):
        u.mailleri_isle(mailler, "okul postası")
    assert [m for m, _ in giden] == ["**Önemli:** Sınav C 502'de."]


def test_posta_okunuyorsa_eski_mesaj(giden):
    _gecikmis_teslim()
    u = uy.Uyari(baslat=False)
    u.posta = _posta(time.time())
    u.gecikme_kontrol()
    assert "gönderim onayı gelmedi" in giden[0][0]


# ================================================================== aynı konulu mailler tek uyarı
def test_konu_anahtari_ve_ayni_konu():
    assert uy._konu_anahtari("Re: FWD:  28 Ekim  Telafisi hk.") == "28 ekim telafisi hk."
    assert uy._ayni_konu(uy._konu_anahtari("Ayse Yilmaz: 28 Ekim Telafisi hk."),
                         uy._konu_anahtari("28 Ekim Telafisi hk."))
    assert not uy._ayni_konu("ödev", "ödev 2 duyurusu")  # kısa konu içerme sayılmaz
    assert not uy._ayni_konu("", "")


def test_ayni_turdaki_ayni_konulu_mailler_tek_mesaj(giden, monkeypatch):
    from telefon.gozculer import degerlendir as dg
    mailler = [{"id": "o-1", "konu": "Ayse Yilmaz: 28 Ekim Telafisi hk.", "kimden": "OBS", "metin": "a"},
               {"id": "o-2", "konu": "Erasmus+ başvuru", "kimden": "UIO", "metin": "b"},
               {"id": "o-3", "konu": "28 Ekim Telafisi hk.", "kimden": "Ayse Yilmaz", "metin": "c"}]
    sonuc = {"o-1": {"onem": "onemli", "ozet": "MATH 113 telafisi 24 Eki Cmt'de.", "teslimler": []},
             "o-2": {"onem": "onemli", "ozet": "Erasmus+ son gün 2 Kas.", "teslimler": []},
             "o-3": {"onem": "onemli", "ozet": "MATH 113 telafisi 24 Eki Cmt 08:30, C 502.", "teslimler": []}}
    monkeypatch.setattr(dg, "degerlendir", lambda m: sonuc)
    assert uy.Uyari(baslat=False).mailleri_isle(mailler, "okul postası")
    metinler = [m for m, _ in giden]
    assert len(metinler) == 2
    assert "24 Eki Cmt'de." in metinler[0] and "C 502" in metinler[0] and "Erasmus" in metinler[1]
