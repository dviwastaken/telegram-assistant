"""Aşama 1 (23 Ekim): çalışırken günlük döndürme, hava durumu geri çekilmesi, Gmail EOF'unda sessiz yeniden bağlanma
ve limit doluyken aynı mailin her dakika yeniden indirilmemesi. Telegram'a, Claude'a ve ağa çıkmaz."""
import datetime as dt
import imaplib
import os
import ssl
import time
import types

import pytest

from test_cevrimdisi import giden, yalitim  # noqa: F401  (ortak fikstürler: Telegram'a hiçbir şey gitmesin)

from telefon import ayarlar, hava, limit
from telefon.gozculer import okul_posta as op
from test_dalga5 import SahteImap, yokla_gozcusu


# ------------------------------------------------------------ günlük döndürme
def test_gunluk_calisirken_doner(tmp_path, monkeypatch):
    yol = str(tmp_path / "servis.log")
    monkeypatch.setattr(ayarlar, "GUNLUK", yol)
    monkeypatch.setattr(ayarlar, "_gunluk", open(yol, "a", encoding="utf-8", buffering=1))
    monkeypatch.setattr(ayarlar, "GUNLUK_SINIR", 300)
    monkeypatch.setattr(ayarlar, "DENETIM_SATIR", 1)
    try:
        for i in range(20):
            ayarlar.log("satır", i, "x" * 40)
        assert os.path.exists(yol + ".1")                                     # servis kapanmadan döndü
        assert os.path.getsize(yol) <= 300 + 100                              # yeni dosya küçük kaldı
        ikisi = "".join(open(y, encoding="utf-8").read() for y in (yol + ".1", yol))
        assert "satır 18" in ikisi and "satır 19" in ikisi                    # dönüş anında satır kaybolmadı
    finally:
        ayarlar._gunluk.close()


def test_gunluk_sinir_altinda_donmez(tmp_path, monkeypatch):
    yol = str(tmp_path / "servis.log")
    monkeypatch.setattr(ayarlar, "GUNLUK", yol)
    monkeypatch.setattr(ayarlar, "_gunluk", open(yol, "a", encoding="utf-8", buffering=1))
    monkeypatch.setattr(ayarlar, "DENETIM_SATIR", 1)
    try:
        ayarlar.log("kısa")
        assert not os.path.exists(yol + ".1")
    finally:
        ayarlar._gunluk.close()


# ------------------------------------------------------------ hava durumu
VERI = {"current": {"temperature_2m": 20.4, "apparent_temperature": 19.6, "weather_code": 1, "wind_speed_10m": 10},
        "daily": {"time": ["2026-10-23", "2026-10-24", "2026-10-25"], "weather_code": [0, 3, 61],
                  "temperature_2m_min": [12, 13, 14], "temperature_2m_max": [22, 21, 20],
                  "precipitation_probability_max": [0, None, 80], "wind_speed_10m_max": [15, 20, 30]}}


@pytest.fixture
def hava_temiz(monkeypatch):
    kayit, cagri = [], []
    monkeypatch.setattr(hava, "_onbellek", {"zaman": 0.0, "veri": None})
    monkeypatch.setattr(hava, "_hata", {"sayi": 0, "sonraki": 0.0})
    monkeypatch.setattr(hava, "KONUM", "38.42,27.14")
    monkeypatch.setattr(hava, "log", lambda *p: kayit.append(" ".join(map(str, p))))
    return types.SimpleNamespace(kayit=kayit, cagri=cagri)


def test_hava_hatada_tek_satir_ve_artan_bekleme(hava_temiz, monkeypatch):
    def dus(timeout):
        hava_temiz.cagri.append(timeout)
        raise OSError("HTTP Error 503: Service Unavailable")
    monkeypatch.setattr(hava, "_indir", dus)
    beklemeler = []
    for _ in range(8):
        with pytest.raises(OSError):
            hava._getir()
        beklemeler.append(round(hava._bekleme()))
    assert len(hava_temiz.kayit) == 1 and "alınamadı" in hava_temiz.kayit[0]  # 8 hatada günlüğe tek satır
    assert beklemeler[:3] == [60, 120, 300] and beklemeler[-1] == 1800       # üstel, 30 dk'da tavan

    monkeypatch.setattr(hava, "_indir", lambda timeout: VERI)
    hava._getir()
    assert "8 başarısız denemeden sonra" in hava_temiz.kayit[-1]
    assert hava._hata["sayi"] == 0 and hava._bekleme() == hava.ONBELLEK_SN - 300


def test_hava_ozet_hata_suresinde_aga_cikmaz(hava_temiz, monkeypatch):
    def dus(timeout):
        hava_temiz.cagri.append(timeout)
        raise TimeoutError("timed out")
    monkeypatch.setattr(hava, "_indir", dus)
    assert hava.ozet() == []                                                  # ilk deneme: kısa zaman aşımıyla
    assert hava_temiz.cagri == [5]
    for _ in range(5):
        assert hava.ozet() == []                                              # geri çekilme sürerken ağ beklenmez
    assert hava_temiz.cagri == [5]
    hava._hata["sonraki"] = time.time() - 1                                   # süre doldu: bir kez daha dener
    hava.ozet()
    assert hava_temiz.cagri == [5, 5]


def test_hava_ozet_gun_etiketleri_tarihe_gore(hava_temiz):
    hava._onbellek.update(zaman=time.time(), veri=VERI)
    sat = hava.ozet(dt.datetime(2026, 10, 23, 9, 0))
    assert sat[0].startswith("Hava durumu (") and "şu an 20°C" in sat[0]
    assert [s.split(" (")[0] for s in sat[1:]] == ["- Bugün", "- Yarın", "- Öbür gün"]
    assert "yağış ihtimali %0" in sat[2]                                      # None → %0

    sat = hava.ozet(dt.datetime(2026, 10, 24, 9, 0))                          # dünün önbelleği: kayma yok
    assert [s.split(" (")[0] for s in sat[1:]] == ["- Bugün", "- Yarın"] and "2026-10-24" in sat[1]


def test_hava_eski_veri_saatini_soyler_ve_bayatsa_yalniz_tahmin(hava_temiz):
    zaman = time.time() - 3 * 3600
    hava._onbellek.update(zaman=zaman, veri=VERI)
    sat = hava.ozet(dt.datetime(2026, 10, 23, 12, 0))
    assert f"{dt.datetime.fromtimestamp(zaman):%H:%M} itibarıyla" in sat[0] and "şu an" not in sat[0]

    hava._onbellek.update(zaman=time.time() - 30 * 3600)
    sat = hava.ozet(dt.datetime(2026, 10, 24, 12, 0))
    assert "güncel veri alınamadı" in sat[0] and "°C (hissedilen" not in sat[0]
    assert len(sat) == 3
    assert hava.ozet(dt.datetime(2026, 10, 30, 12, 0)) == []                 # hiçbir gün kalmadı


# ------------------------------------------------------------ Gmail: kopma ve bekleyen mail
class Dur(BaseException):  # gözcünün "except Exception"ına takılmasın
    pass


def kopan_gozcu(monkeypatch, tmp_path, olaylar):
    """_yokla sırayla olaylar'daki hatayı fırlatır ("ok": başarılı tur); bitince Dur."""
    uykular, kayit, girisler = [], [], []
    monkeypatch.setattr(op.time, "sleep", uykular.append)
    monkeypatch.setattr(op, "log", lambda *p: kayit.append(" ".join(map(str, p))))
    monkeypatch.setattr(op.threading, "Thread", lambda *a, **k: types.SimpleNamespace(start=lambda: None))

    class Sahte:
        def __init__(self, *a, **k):
            girisler.append(1)

        def login(self, k, s):
            pass

    monkeypatch.setattr(op.imaplib, "IMAP4_SSL", Sahte)
    sira = iter(olaylar)

    def yokla(self, imap):
        olay = next(sira, None)
        if olay is None:
            raise Dur()
        if olay != "ok":
            raise olay

    class Gozcu(op.GmailImapGozcusu):
        KULLANICI, SIFRE, DURUM = "kullanici", "x", str(tmp_path / "gmail.durum.json")
        _yokla = yokla

    g = Gozcu(types.SimpleNamespace(bildir=lambda *a: None))
    return g, uykular, kayit, girisler


EOF = imaplib.IMAP4.abort("command: EXAMINE => socket error: EOF")


def test_gmail_tek_kopmada_sessiz_ve_hizli_doner(monkeypatch, tmp_path):
    g, uykular, kayit, girisler = kopan_gozcu(monkeypatch, tmp_path, ["ok", EOF, "ok", EOF, "ok"])
    with pytest.raises(Dur):
        g._dongu()
    assert kayit == ["gmail: bağlandı"]                                       # iki kopma da günlüğe düşmedi
    assert uykular.count(op.KOPMA_BEKLEME) == 2 and 30 not in uykular         # 30 sn değil 5 sn
    assert len(girisler) == 3 and g.bagli


def test_gmail_ust_uste_kopmada_yazar(monkeypatch, tmp_path):
    ssl_eof = ssl.SSLEOFError(8, "EOF occurred in violation of protocol (_ssl.c:2427)")
    g, uykular, kayit, _ = kopan_gozcu(monkeypatch, tmp_path, ["ok", EOF, ssl_eof, EOF, "ok"])
    with pytest.raises(Dur):
        g._dongu()
    assert any("3 kez üst üste koptu" in s for s in kayit)
    hata_uykulari = [u for u in uykular if u != op.ARALIK]                    # başarılı turların 60 sn'si hariç
    assert hata_uykulari[:3] == [op.KOPMA_BEKLEME, op.KOPMA_BEKLEME, 30]      # üçüncüde olağan bekleme
    assert kayit.count("gmail: bağlandı") == 2                                # gerçek kesintiden dönüş yazılır


def test_gmail_baska_hata_eskisi_gibi(monkeypatch, tmp_path):
    g, uykular, kayit, _ = kopan_gozcu(monkeypatch, tmp_path, [TimeoutError("The read operation timed out")])
    with pytest.raises(Dur):
        g._dongu()
    assert any("bağlantı hatası" in s for s in kayit) and uykular[0] == 30


def test_limit_doluyken_ayni_mail_yeniden_indirilmez(tmp_path, monkeypatch):
    kayit = []
    monkeypatch.setattr(op, "log", lambda *p: kayit.append(" ".join(map(str, p))))
    dolu = {"v": True}
    monkeypatch.setattr(limit, "dolu_mu", lambda *a, **k: dolu["v"])
    getirilen = []

    class Sayan(SahteImap):
        def uid(self, komut, *a):
            if komut == "FETCH":
                getirilen.append(a[0])
            return super().uid(komut, *a)

    g = yokla_gozcusu(tmp_path, basarili=False)
    for _ in range(5):
        g._yokla(Sayan())
    assert getirilen == ["11", "12"] and g.uyari.cagri == 1                  # 5 turda tek indirme
    assert kayit == ["okul postası: 2 yeni mail"]

    class Ucuncu(Sayan):                                                      # yeni bir mail geldi: parti değişti
        def uid(self, komut, *a):
            if komut == "SEARCH":
                return "OK", [b"11 12 13"]
            return super().uid(komut, *a)
    g._yokla(Ucuncu())
    assert g.uyari.cagri == 2 and kayit[-1] == "okul postası: 3 yeni mail"

    dolu["v"], g.uyari.basarili = False, True                                 # limit açıldı: işlenir, ilerler
    g._yokla(Ucuncu())
    assert g.durum["son_uid"] == 13 and g._bekleyen == () and len(kayit) == 2


def test_limit_disi_hatada_her_turda_denenir_ama_yazilmaz(tmp_path, monkeypatch):
    kayit = []
    monkeypatch.setattr(op, "log", lambda *p: kayit.append(" ".join(map(str, p))))
    monkeypatch.setattr(limit, "dolu_mu", lambda *a, **k: False)              # ör. Haiku zaman aşımı
    g = yokla_gozcusu(tmp_path, basarili=False)
    for _ in range(3):
        g._yokla(SahteImap())
    assert g.uyari.cagri == 3 and len(kayit) == 1
