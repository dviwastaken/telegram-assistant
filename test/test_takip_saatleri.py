"""TAKIP_SAATLERI: ödev takibinde sabit saatlerde hatırlatma (US-008)."""
import datetime as dt

import pytest

from telefon import eylemler
from telefon.gozculer import uyari as uy


def _tarih(g, s, d=0):
    return dt.datetime(2026, 10, g, s, d)


def test_tek_saat_bugun_ya_da_yarin(monkeypatch):
    monkeypatch.setenv("TAKIP_SAATLERI", "20:00")
    assert uy.sonraki_takip(_tarih(12, 14)) == _tarih(12, 20)
    assert uy.sonraki_takip(_tarih(12, 20, 0)) == _tarih(13, 20)   # tam o an: kesinlikle sonrası
    assert uy.sonraki_takip(_tarih(12, 21)) == _tarih(13, 20)


def test_iki_saat_ve_ertesi_gune_tasma(monkeypatch):
    monkeypatch.setenv("TAKIP_SAATLERI", "20:00, 09:00")             # sırasız ve boşluklu da olur
    assert uy.takip_saatleri() == [dt.time(9, 0), dt.time(20, 0)]
    assert uy.sonraki_takip(_tarih(12, 7)) == _tarih(12, 9)
    assert uy.sonraki_takip(_tarih(12, 9, 0)) == _tarih(12, 20)
    assert uy.sonraki_takip(_tarih(12, 14)) == _tarih(12, 20)
    assert uy.sonraki_takip(_tarih(12, 20, 1)) == _tarih(13, 9)      # yarına taşar


def test_teslim_gecen_saat_eski_araliga_duser(monkeypatch):
    monkeypatch.setenv("TAKIP_SAATLERI", "20:00")
    monkeypatch.setattr(uy, "TAKIP_ARALIGI", dt.timedelta(hours=2))
    simdi = _tarih(12, 14)
    assert uy.sonraki_takip(simdi, "2026-10-12T23:59") == _tarih(12, 20)       # teslimden önce: listedeki saat
    assert uy.sonraki_takip(simdi, "2026-10-12T18:00") == _tarih(12, 16)       # 20:00 teslimi geçer: 2 saat sonra
    assert uy.sonraki_takip(simdi, "2026-10-12T20:00") == _tarih(12, 16)       # teslim anına eşit de geçer
    assert uy.sonraki_takip(simdi, "bozuk") == _tarih(12, 20)                  # bozuk teslim: engel olmaz


@pytest.mark.parametrize("deger", ["", "abc", "25:00", "20:60", "20", "20.00", " , "])
def test_gecersiz_deger_takip_saatine_doner(monkeypatch, deger):
    monkeypatch.setenv("TAKIP_SAATLERI", deger)
    monkeypatch.setattr(uy, "TAKIP_ARALIGI", dt.timedelta(hours=2))
    assert uy.takip_saatleri() == []
    simdi = _tarih(12, 14)
    assert uy.sonraki_takip(simdi) == simdi + dt.timedelta(hours=2)
    assert uy.aralik_metni() == "2 saatte bir"


def test_gecersiz_deger_bir_kez_gunluge_yazilir(monkeypatch):
    kayit = []
    monkeypatch.setattr(uy, "log", lambda *a: kayit.append(" ".join(map(str, a))))
    monkeypatch.setattr(uy, "_saat_uyarilan", set())
    monkeypatch.setenv("TAKIP_SAATLERI", "yarın akşam")
    for _ in range(3):
        uy.takip_saatleri()
    assert len(kayit) == 1 and "yarın akşam" in kayit[0]


def test_kismen_gecersiz_gecerli_saatleri_kullanir(monkeypatch):
    monkeypatch.setattr(uy, "log", lambda *a: None)
    monkeypatch.setenv("TAKIP_SAATLERI", "20:00,xx")
    assert uy.takip_saatleri() == [dt.time(20, 0)]


def test_aralik_metni(monkeypatch):
    monkeypatch.setenv("TAKIP_SAATLERI", "20:00")
    assert uy.aralik_metni() == "her gün 20:00'de"
    monkeypatch.setenv("TAKIP_SAATLERI", "09:00,20:00")
    assert uy.aralik_metni() == "her gün 09:00 ve 20:00'de"
    monkeypatch.setenv("TAKIP_SAATLERI", "9:00,14:30,20:00")
    assert uy.aralik_metni() == "her gün 09:00, 14:30 ve 20:00'de"
    monkeypatch.delenv("TAKIP_SAATLERI")
    monkeypatch.setattr(uy, "TAKIP_ARALIGI", dt.timedelta(hours=2))
    assert uy.aralik_metni() == "2 saatte bir"


def _takip_kur(tmp_path, monkeypatch, teslim):
    monkeypatch.setattr(uy, "TAKIP", str(tmp_path / "takip.json"))
    u = uy.Uyari(baslat=False)
    eylemler.yaz(eylemler.TAKVIM, {"tekrarlayan": [], "olaylar": [
        {"id": "t1", "baslik": "Ödev 3", "ders": "BIL207", "tur": "Teslim", "zaman": teslim, "durum": "Yapılacak"}]})
    return u


def test_odev_takibi_listelenen_saatte_yazar(tmp_path, monkeypatch):
    monkeypatch.setenv("TAKIP_SAATLERI", "09:00,20:00")
    u = _takip_kur(tmp_path, monkeypatch, "2026-10-16T23:59")
    simdi = _tarih(12, 14)
    u.odev_takibe_al("BIL 207", "Ödev 3", "2026-10-16T23:59", simdi=simdi)
    assert u.odev_takip(simdi)[0]["arama"] == 1                                   # Yeni ödev mesajı hemen
    assert dt.datetime.fromisoformat(u._takip_oku()[0]["sonraki"]) == _tarih(12, 20)
    assert u.odev_takip(_tarih(12, 19, 59)) == []                                 # 20:00'den önce yazılmaz
    assert len(u.odev_takip(_tarih(12, 20, 0))) == 1
    assert dt.datetime.fromisoformat(u._takip_oku()[0]["sonraki"]) == _tarih(13, 9)
    assert len(u.odev_takip(_tarih(13, 9, 0))) == 1                              # sabah 09:00 (sessiz saat dışı)


def test_sessiz_saate_dusen_saat_sabaha_kalir(tmp_path, monkeypatch):
    monkeypatch.setenv("TAKIP_SAATLERI", "23:45")
    u = _takip_kur(tmp_path, monkeypatch, "2026-10-16T23:59")
    simdi = _tarih(12, 14)
    u.odev_takibe_al("BIL 207", "Ödev 3", "2026-10-16T23:59", simdi=simdi)
    u.odev_takip(simdi)
    assert u.odev_takip(_tarih(12, 23, 45)) == []                                 # 23:30-08:00 sessiz saat
    assert len(u.odev_takip(_tarih(13, 8, 1))) == 1                               # sabah ilk fırsatta
    assert dt.datetime.fromisoformat(u._takip_oku()[0]["sonraki"]) == _tarih(13, 23, 45)


def test_ilk_mesajdan_sonra_saat_teslimi_gecerse_aralik(tmp_path, monkeypatch):
    monkeypatch.setenv("TAKIP_SAATLERI", "20:00")
    monkeypatch.setattr(uy, "TAKIP_ARALIGI", dt.timedelta(hours=2))
    u = _takip_kur(tmp_path, monkeypatch, "2026-10-12T18:00")
    simdi = _tarih(12, 14)
    u.odev_takibe_al("BIL 207", "Ödev 3", "2026-10-12T18:00", simdi=simdi)
    u.odev_takip(simdi)
    assert dt.datetime.fromisoformat(u._takip_oku()[0]["sonraki"]) == _tarih(12, 16)
