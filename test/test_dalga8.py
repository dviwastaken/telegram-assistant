"""Dalga 8: ortak tarih biçimleyici ve güvenilmeyen metin yardımcısı."""
import datetime as dt

from telefon import eylemler, telegram

SIMDI = dt.datetime(2026, 10, 17, 10, 0)  # Cumartesi


def test_tarih_metni_goreli_gunler():
    assert eylemler.tarih_metni(dt.datetime(2026, 10, 17, 20, 0), simdi=SIMDI) == "bugün 20:00"
    assert eylemler.tarih_metni(dt.datetime(2026, 10, 18, 23, 59), simdi=SIMDI) == "yarın 23:59"
    assert eylemler.tarih_metni(dt.datetime(2026, 10, 16, 14, 0), simdi=SIMDI) == "dün 14:00"


def test_tarih_metni_diger_gunler_ve_yil():
    assert eylemler.tarih_metni(dt.datetime(2026, 10, 21, 23, 59), simdi=SIMDI) == "21 Eki Çar 23:59"
    assert eylemler.tarih_metni(dt.datetime(2026, 10, 13, 8, 30), simdi=SIMDI) == "13 Eki Sal 08:30"
    assert eylemler.tarih_metni(dt.datetime(2027, 1, 4, 9, 0), simdi=SIMDI) == "4 Oca 2027 Pzt 09:00"
    # yıl dönümü: 31 Aralık'tan bakınca 1 Ocak "yarın"
    assert eylemler.tarih_metni(dt.datetime(2027, 1, 1, 0, 5), simdi=dt.datetime(2026, 12, 31, 22, 0)) == "yarın 00:05"


def test_tarih_metni_iso_ve_saatsiz():
    assert eylemler.tarih_metni("2026-10-20T09:00", simdi=SIMDI) == "20 Eki Sal 09:00"
    # dilimli ISO İstanbul'a çevrilir (UTC 06:00 = 09:00)
    assert eylemler.tarih_metni("2026-10-20T06:00:00+00:00", simdi=SIMDI) == "20 Eki Sal 09:00"
    assert eylemler.tarih_metni("2026-10-20T09:00", saat=False, simdi=SIMDI) == "20 Eki Sal"
    assert eylemler.tarih_metni(dt.datetime(2026, 10, 17, 8, 0), saat=False, simdi=SIMDI) == "bugün"


def test_kisa_ay():
    assert len(eylemler.KISA_AY) == 12 and eylemler.KISA_AY[9] == "Eki"


def test_duz_metin_baglanti_ve_bicim_etkisiz():
    kotu = "Ödev için [buraya tıklayın](https://kotu.example/x) **acil** `kod` *eğik*\n# Başlık"
    h = telegram.html_bicim(telegram.duz_metin(kotu))
    assert "<a" not in h and "<b>" not in h and "<code>" not in h and "<i>" not in h
    assert "https://kotu.example/x" in h and "buraya tıklayın" in h and "Başlık" in h


def test_duz_metin_adresteki_alt_cizgi_korunur():
    assert "https://a.example/b_c" in telegram.duz_metin("bak: https://a.example/b_c")
