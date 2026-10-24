"""Dalga 5: sistem gözcüsü durumu kalıcı (US-004), saat dilimi sabit (US-005), okul postası /durum satırı (US-006).
Telegram'a, Claude'a, Notion'a ve IMAP'e çıkmaz."""
import datetime as dt
import json
import os
import time
import types

import pytest

from test_cevrimdisi import giden, yalitim  # noqa: F401  (ortak fikstürler: Telegram'a hiçbir şey gitmesin)

from telefon import eylemler, notion
from telefon.gozculer import okul_posta as op
from telefon.gozculer import sistem


def hhmm(ts):
    return dt.datetime.fromtimestamp(ts, eylemler.TZ).strftime("%H:%M")


# ================================================================== US-004: sistem gözcüsü durumu kalıcı
class SahteUyari:
    def __init__(self):
        self.bildirimler = []

    def bildir(self, onem, metin, kaynak):
        self.bildirimler.append((onem, metin, kaynak))


class Dugum:
    def __init__(self, *a, **k):
        pass

    def start(self):
        pass


@pytest.fixture
def gozcu_kur(tmp_path, monkeypatch, tum_kontroller):
    """SistemGozcusu'nu iş parçacığı başlatmadan, durum dosyası tmp'de kurar. Çağrı = 'yeniden başlatma'."""
    monkeypatch.setattr(sistem.threading, "Thread", Dugum)
    monkeypatch.setattr(sistem, "DURUM", str(tmp_path / "sistem.durum.json"))

    def kur():
        uyari = SahteUyari()
        return sistem.SistemGozcusu(uyari), uyari
    return kur


def test_sistem_durumu_dosyaya_yazilir_ve_yeniden_baslatmada_okunur(gozcu_kur, giden, tmp_path):
    g, uyari = gozcu_kur()
    t0 = 1_800_000_000.0
    kotu, iyi = (lambda: (False, "bağlı değil")), (lambda: (True, ""))
    g._kontrol_et("Tailscale", kotu, 2, "onemli", simdi=t0)            # 1. hata: henüz bildirim yok
    assert uyari.bildirimler == [] and g.ilk_hata["Tailscale"] == t0
    g._kontrol_et("Tailscale", kotu, 2, "onemli", simdi=t0 + 300)      # 2. hata: bildirim
    assert [b[1] for b in uyari.bildirimler] == ["Tailscale: bağlı değil"]
    assert g.ilk_hata["Tailscale"] == t0                               # dizinin ilk zamanı korunur
    g._kaydet()
    d = json.load(open(tmp_path / "sistem.durum.json", encoding="utf-8"))
    assert d["hata"] == {"Tailscale": 2} and d["bildirildi"] == ["Tailscale"] and d["ilk_hata"] == {"Tailscale": t0}
    assert not os.path.exists(str(tmp_path / "sistem.durum.json") + ".tmp")   # atomik: tmp kalmaz

    # --- yeniden başlatma: bildirilmiş sorun tekrar bildirilmez
    g2, uyari2 = gozcu_kur()
    assert g2.hata["Tailscale"] == 2 and g2.bildirildi == {"Tailscale"} and g2.ilk_hata == {"Tailscale": t0}
    g2._kontrol_et("Tailscale", kotu, 2, "onemli", simdi=t0 + 900)
    assert uyari2.bildirimler == [] and g2.hata["Tailscale"] == 3 and g2.ilk_hata["Tailscale"] == t0
    # --- düzelme yine bir kez haber verilir
    g2._kontrol_et("Tailscale", iyi, 2, "onemli", simdi=t0 + 1200)
    assert giden.metinler() == ["Tailscale yeniden çalışıyor."]
    g2._kontrol_et("Tailscale", iyi, 2, "onemli", simdi=t0 + 1500)
    assert len(giden) == 1
    g2._kaydet()
    g3, _ = gozcu_kur()                                                # düzelme de kalıcı: sıfırdan temiz
    assert g3.hata["Tailscale"] == 0 and g3.bildirildi == set() and g3.ilk_hata == {}


def test_sistem_durumu_bildirim_oncesi_kaydedilir(gozcu_kur, tmp_path):
    """Bildirim gönderilirken çökülürse yeniden başlatma aynı sorunu ikinci kez bildirmesin."""
    g, uyari = gozcu_kur()
    uyari.bildir = lambda *a: (_ for _ in ()).throw(RuntimeError("çöktü"))
    with pytest.raises(RuntimeError):
        g._kontrol_et("C diski", lambda: (False, "C: diskinde yalnızca %3 boş yer kaldı (5 GB)"), 1, "acil_kritik")
    g2, _ = gozcu_kur()
    assert g2.bildirildi == {"C diski"}


def test_sistem_durum_dosyasi_bozuksa_yok_sayilir(gozcu_kur, tmp_path):
    for icerik in ("{bozuk", "[1, 2]", '{"hata": {"Tailscale": "x"}}', ""):
        (tmp_path / "sistem.durum.json").write_text(icerik, encoding="utf-8")
        g, _ = gozcu_kur()
        assert g.bildirildi == set() and all(n == 0 for n in g.hata.values()) and g.ilk_hata == {}


def test_sistem_durum_dosyasinda_bilinmeyen_kontrol_atlanir(gozcu_kur, tmp_path):
    (tmp_path / "sistem.durum.json").write_text(json.dumps(
        {"hata": {"Eski kontrol": 5, "Tailscale": 1}, "bildirildi": ["Eski kontrol"], "ilk_hata": {"Eski kontrol": 1.0}}),
        encoding="utf-8")
    g, _ = gozcu_kur()
    assert g.hata["Tailscale"] == 1 and "Eski kontrol" not in g.hata and g.bildirildi == set()


def test_sistem_durum_satiri_beri(monkeypatch):
    g = sistem.SistemGozcusu.__new__(sistem.SistemGozcusu)
    g.kontroller = [("Tailscale", None, 2, "onemli"), ("C diski", None, 1, "acil_kritik")]
    simdi = dt.datetime(2026, 10, 17, 15, 0, tzinfo=eylemler.TZ).timestamp()
    g.son_tur = simdi - 120
    g.hata, g.neden = {"Tailscale": 1, "C diski": 0}, {"Tailscale": "bağlı değil"}
    g.ilk_hata = {"Tailscale": simdi - 20 * 60}                         # bugün 14:40
    assert g.durum_satiri(simdi) == ("Sistem kontrolleri: 1/2 tamam (2 dk önce); "
                                     "izleniyor: Tailscale (1 kez, 14:40'tan beri).")
    g.hata["Tailscale"] = 2
    g.ilk_hata = {"Tailscale": simdi - 20 * 60}
    assert g.durum_satiri(simdi) == ("⚠️ sorun: sistem kontrollerinde 1 sorun var (son tur 2 dk önce)\n"
                                     "• Tailscale: bağlı değil (14:40'tan beri)")
    g.ilk_hata = {"Tailscale": dt.datetime(2026, 10, 16, 23, 5, tzinfo=eylemler.TZ).timestamp()}   # dün
    assert g.durum_satiri(simdi).endswith("Tailscale: bağlı değil (dün 23:05'ten beri)")
    g.ilk_hata = {}                                                     # eski/eksik veri: ek metin yok
    assert g.durum_satiri(simdi) == ("⚠️ sorun: sistem kontrollerinde 1 sorun var (son tur 2 dk önce)\n"
                                     "• Tailscale: bağlı değil")


def test_beri_metni_istanbul_saatiyle():
    simdi = dt.datetime(2026, 10, 17, 1, 0, tzinfo=eylemler.TZ).timestamp()      # UTC'de hâlâ 16 Ekim
    assert sistem.beri_metni(simdi - 30 * 60, simdi) == "00:30'dan beri"
    assert sistem.beri_metni(simdi - 3 * 3600, simdi) == "dün 22:00'den beri"


# ================================================================== US-005: saat dilimi sabit
class YerelYasak(dt.datetime):
    """Makinenin yerel saat dilimine dokunan (astimezone() argümansız) her çağrıyı yakalar."""

    def astimezone(self, tz=None):
        assert tz is not None, "makinenin yerel saat dilimi kullanıldı"
        return super().astimezone(tz)


def yerel_yasak_dt(monkeypatch):
    sahte = types.SimpleNamespace(datetime=YerelYasak, timedelta=dt.timedelta, timezone=dt.timezone, date=dt.date,
                                  time=dt.time)
    monkeypatch.setattr(notion, "dt", sahte)
    monkeypatch.setattr(eylemler, "dt", sahte)


NOTION_VEKTORLERI = [
    ("2026-10-16T17:40:00.000+03:00", "2026-10-16T17:40"),
    ("2026-10-16T14:40:00.000Z", "2026-10-16T17:40"),
    ("2026-10-16T22:30:00.000Z", "2026-10-17T01:30"),
    ("2026-10-16T17:40:00.000", "2026-10-16T17:40"),     # ofsetsiz: zaten yerel
    ("2026-10-17", "2026-10-17T00:00"),                  # yalnız tarih
    (None, None),
]


@pytest.mark.parametrize("girdi,beklenen", NOTION_VEKTORLERI)
def test_notion_yerel_bulut_vektorleri(girdi, beklenen, monkeypatch):
    assert notion._yerel(girdi) == beklenen
    yerel_yasak_dt(monkeypatch)                          # makinenin dilimine hiç bakılmadan aynı sonuç
    assert notion._yerel(girdi) == beklenen


def test_notion_ofsetli_istanbul(monkeypatch):
    assert notion._ofsetli("2026-10-17T08:00") == "2026-10-17T08:00:00+03:00"
    assert notion._ofsetli("2026-01-05T08:00:30") == "2026-01-05T08:00:30+03:00"
    yerel_yasak_dt(monkeypatch)
    assert notion._ofsetli("2026-10-17T08:00") == "2026-10-17T08:00:00+03:00"
    assert notion._ofsetli("2026-10-17T05:00:00+00:00") == "2026-10-17T08:00:00+03:00"   # ofsetli girdi İstanbul'a çevrilir


def test_notion_gidis_donus_tutarli():
    for yerel in ("2026-10-16T17:40", "2026-10-17T01:30", "2026-12-31T23:59"):
        assert notion._yerel(notion._ofsetli(yerel)) == yerel


def test_eylemler_zaman_istanbul(monkeypatch):
    assert eylemler._zaman("2026-10-16T22:30:00.000Z") == dt.datetime(2026, 10, 17, 1, 30)
    assert eylemler._zaman("2026-10-16T17:40:00+03:00") == dt.datetime(2026, 10, 16, 17, 40)
    assert eylemler._zaman("2026-10-16T17:40") == dt.datetime(2026, 10, 16, 17, 40)    # ofsetsiz: olduğu gibi
    assert eylemler._zaman("2026-10-16T17:40").tzinfo is None
    yerel_yasak_dt(monkeypatch)
    assert eylemler._zaman("2026-10-16T22:30:00.000Z") == dt.datetime(2026, 10, 17, 1, 30)


def test_istanbul_dilimi_yedegi_sabit_arti_uc(monkeypatch):
    import zoneinfo
    asil = eylemler._istanbul_dilimi()

    def yok(anahtar):
        raise zoneinfo.ZoneInfoNotFoundError(anahtar)
    monkeypatch.setattr(zoneinfo, "ZoneInfo", yok)       # tzdata kurulu değilmiş gibi
    yedek = eylemler._istanbul_dilimi()
    assert yedek.utcoffset(None) == dt.timedelta(hours=3)
    # yedek, gerçek İstanbul dilimiyle (2016'dan beri sabit +03:00) aynı sonucu verir
    for iso in ("2026-01-05T12:00:00+00:00", "2026-07-05T12:00:00+00:00", "2026-10-16T22:30:00+00:00"):
        z = dt.datetime.fromisoformat(iso)
        assert z.astimezone(yedek).isoformat() == z.astimezone(asil).isoformat()
        assert z.astimezone(yedek).utcoffset() == dt.timedelta(hours=3)


def test_istanbul_dilimi_gercek_dilim_yaz_saatsiz():
    # tzdata kuruluysa ZoneInfo, değilse sabit ofset: ikisinde de yıl boyu +03:00
    for ay in (1, 4, 7, 10, 12):
        z = dt.datetime(2026, ay, 15, 12, tzinfo=dt.timezone.utc).astimezone(eylemler.TZ)
        assert z.utcoffset() == dt.timedelta(hours=3)


# ================================================================== US-006: okul postası /durum satırı
def test_durum_satiri_kesinti_baslangici():
    g = op.OkulPostaGozcusu.__new__(op.OkulPostaGozcusu)
    g.kapali, g.bagli, g.son_yoklama, g.son_yeni, g.son_hata = False, False, 0.0, 0.0, "AUTHENTICATIONFAILED"
    simdi = dt.datetime(2026, 10, 17, 15, 0, tzinfo=eylemler.TZ).timestamp()
    g.erisim = types.SimpleNamespace(durum={"ilk_hata": simdi - 45 * 60})
    assert g.durum_satiri(simdi) == "⚠️ sorun: Okul postası bağlı değil (AUTHENTICATIONFAILED), 14:15'ten beri."
    g.erisim = types.SimpleNamespace(durum={"ilk_hata": dt.datetime(2026, 10, 16, 9, 30, tzinfo=eylemler.TZ).timestamp()})
    assert g.durum_satiri(simdi) == "⚠️ sorun: Okul postası bağlı değil (AUTHENTICATIONFAILED), dün 09:30'dan beri."
    g.erisim = types.SimpleNamespace(durum={"ilk_hata": None})          # kesinti henüz izlenmiyor
    assert g.durum_satiri(simdi) == "⚠️ sorun: Okul postası bağlı değil (AUTHENTICATIONFAILED)."
    g.erisim = None                                                      # Gmail: erişim takibi yok
    assert g.durum_satiri(simdi) == "⚠️ sorun: Okul postası bağlı değil (AUTHENTICATIONFAILED)."
    g.son_hata = ""
    assert g.durum_satiri(simdi) == "⚠️ sorun: Okul postası bağlı değil (bağlanıyor)."


class SahteImap:
    HAM = (b"From: hoca@x.edu\r\nSubject: Duyuru\r\nDate: Sat, 17 Oct 2026 10:00:00 +0300\r\n"
           b"Content-Type: text/plain; charset=utf-8\r\n\r\nMerhaba")

    def select(self, kutu, readonly=True):
        return "OK", [b"2"]

    def response(self, ad):
        return ad, [b"7"]

    def uid(self, komut, *a):
        if komut == "SEARCH":
            return "OK", [b"11 12"]
        return "OK", [(b"1 (BODY[] {5}", self.HAM)]


class MailUyari:
    def __init__(self, basarili):
        self.basarili = basarili
        self.cagri = 0

    def mailleri_isle(self, mailler, kaynak, ilk_tur=False):
        self.cagri += 1
        return self.basarili


def yokla_gozcusu(tmp_path, basarili):
    g = op.OkulPostaGozcusu.__new__(op.OkulPostaGozcusu)
    g.DURUM = str(tmp_path / "okul.durum.json")
    g.uyari = MailUyari(basarili)
    g.durum = {"uidvalidity": "7", "son_uid": 10}                        # ilk tur değil
    g.son_yoklama, g.son_yeni = 0.0, 0.0
    return g


def test_son_yeni_yalniz_islem_basariliysa_guncellenir(tmp_path):
    g = yokla_gozcusu(tmp_path, basarili=False)
    g._yokla(SahteImap())
    assert g.son_yeni == 0.0 and not os.path.exists(g.DURUM)             # değerlendirme başarısız: ne bellek ne dosya
    assert g.durum["son_uid"] == 10
    g.uyari.basarili = True
    t0 = time.time()
    g._yokla(SahteImap())
    assert g.son_yeni >= t0 and g.durum["son_uid"] == 12
    kayitli = json.load(open(g.DURUM, encoding="utf-8"))
    assert kayitli["son_yeni"] == g.son_yeni and kayitli["son_uid"] == 12


def test_son_yeni_yeni_mail_yoksa_degismez(tmp_path):
    g = yokla_gozcusu(tmp_path, basarili=True)
    g.son_yeni = 123.0

    class Bos(SahteImap):
        def uid(self, komut, *a):
            return "OK", [b""]
    g._yokla(Bos())
    assert g.son_yeni == 123.0 and g.uyari.cagri == 0


def test_son_yeni_yeniden_baslatmada_geri_yuklenir(tmp_path, monkeypatch):
    dosya = tmp_path / "okul.durum.json"
    dosya.write_text(json.dumps({"uidvalidity": "7", "son_uid": 12, "son_yeni": 1_800_000_000.5}), encoding="utf-8")
    monkeypatch.setattr(op.OkulPostaGozcusu, "DURUM", str(dosya))
    monkeypatch.setattr(op.OkulPostaGozcusu, "KULLANICI", "")             # kapalı: iş parçacığı/ağ yok
    g = op.OkulPostaGozcusu(MailUyari(True))
    assert g.son_yeni == 1_800_000_000.5
    dosya.write_text(json.dumps({"uidvalidity": "7", "son_uid": 12}), encoding="utf-8")   # eski biçim
    assert op.OkulPostaGozcusu(MailUyari(True)).son_yeni == 0.0
    dosya.write_text(json.dumps({"uidvalidity": "7", "son_yeni": "x"}), encoding="utf-8")  # bozuk değer
    assert op.OkulPostaGozcusu(MailUyari(True)).son_yeni == 0.0


def test_ayrilma_eki_okunusa_gore():
    from telefon.gozculer.sistem import ayrilma_eki
    beklenen = {"14:40": "'tan", "23:05": "'ten", "22:00": "'den", "10:00": "'dan", "00:00": "'dan", "09:30": "'dan",
                "08:01": "'den", "13:03": "'ten", "12:06": "'dan", "07:20": "'den", "18:50": "'den", "16.10 11:09": "'dan"}
    for saat, ek in beklenen.items():
        assert ayrilma_eki(saat) == ek, saat
