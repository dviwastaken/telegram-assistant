"""Dalga 8 (bildirim metinleri): hatırlatma, uyarı, sistem, okul postası, limit ve bulut mesajlarının yeni biçimi.
Telegram'a, Claude'a, Notion'a ve IMAP'e çıkmaz."""
import datetime as dt
import re
import types

import pytest
import urllib.error

from test_cevrimdisi import giden, yalitim  # noqa: F401  (ortak fikstürler: Telegram'a hiçbir şey gitmesin)

from telefon import bulut, eylemler, limit, telegram, zamanlayici as zm
from telefon.gozculer import okul_posta as op
from telefon.gozculer import sistem
from telefon.gozculer import uyari as uy

ISO = re.compile(r"\d{4}-\d\d-\d\d|T\d\d:\d\d|\d\d\.\d\d \d\d:\d\d")
IZINLI = ("🚨", "⚠️", "🎤")
EMOJI = re.compile("[\U0001F000-\U0001FFFF\u2300-\u23FF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]")


def emojisiz(metin):
    """İzinli üç emoji dışında emoji yok."""
    for e in IZINLI:
        metin = metin.replace(e, "")
    assert not EMOJI.search(metin), metin


def kayit(**ek):
    a = {"id": "h1", "zaman": "2026-10-17T20:00", "konu": "kütüphane", "durum": "bekliyor"}
    a.update(ek)
    return a


# ================================================================== zamanlayıcı: hatırlatma metni
def test_hatirlatma_zamaninda_saat_bir_kez():
    metin = zm.hatirlatma_metni(kayit(), dt.datetime(2026, 10, 17, 20, 1))
    assert metin == "**Hatırlatma** · bugün 20:00\nkütüphane"
    emojisiz(metin)


def test_hatirlatma_gec_kalinca_dakika_yazilir():
    metin = zm.hatirlatma_metni(kayit(), dt.datetime(2026, 10, 17, 20, 12))
    assert metin == "**Hatırlatma** · bugün 20:00 içindi, 12 dk geç\nkütüphane"
    assert metin.count("20:00") == 1 and "geç kaldım" not in metin
    uzun = zm.hatirlatma_metni(kayit(), dt.datetime(2026, 10, 17, 21, 5))
    assert "bugün 20:00 içindi, 1 saat 5 dk geç" in uzun


def test_hatirlatma_baska_gun_bilgisayar_kapaliydi():
    metin = zm.hatirlatma_metni(kayit(zaman="2026-10-15T20:00"), dt.datetime(2026, 10, 17, 9, 0))
    assert metin.startswith("**Hatırlatma** · 15 Eki Per 20:00 içindi, bilgisayar kapalıydı\n")
    assert not ISO.search(metin)
    dun = zm.hatirlatma_metni(kayit(zaman="2026-10-16T20:00"), dt.datetime(2026, 10, 17, 9, 0))
    assert "dün 20:00 içindi, bilgisayar kapalıydı" in dun


def test_hatirlatma_acil_baslik_ve_tekrar_yazisi():
    metin = zm.hatirlatma_metni(kayit(tur="acil"), dt.datetime(2026, 10, 17, 20, 0))
    assert metin.startswith("🚨 **Hatırlatma** · bugün 20:00")
    emojisiz(metin)
    for kod, yazi in (("gunluk", "her gün"), ("haftaici", "hafta içi her gün"), ("haftalik", "her hafta"),
                      ("aylik", "her ay")):
        m = zm.hatirlatma_metni(kayit(tekrar=kod), dt.datetime(2026, 10, 17, 20, 0))
        assert m.endswith(f"\n{yazi} tekrarlanıyor") and "🔁" not in m


def test_hatirlatma_not_ve_konu_guvenilmeyen_metin():
    a = kayit(konu="[tıkla](https://kotu.example/x) **acil**", **{"not": "bak `kod`"})
    metin = zm.hatirlatma_metni(a, dt.datetime(2026, 10, 17, 20, 0))
    h = telegram.html_bicim(metin)
    assert "<a" not in h and "<code>" not in h and "https://kotu.example/x" in h
    assert "*bak kod*" in metin                                 # not italik kalır, içindeki işaretler gider


def test_hatirlatma_gece_yarisi_kurali():
    """Gün değişse bile gecikme 60 dk'yı aşmadıysa 'N dk geç'; aşıp başka güne kaldıysa 'bilgisayar kapalıydı';
    aynı gün kuralı değişmez."""
    a = kayit(zaman="2026-10-17T23:58")
    assert zm.hatirlatma_metni(a, dt.datetime(2026, 10, 18, 0, 4)).startswith(
        "**Hatırlatma** · dün 23:58 içindi, 6 dk geç\n")
    assert "dün 23:50 içindi, 1 saat geç" in zm.hatirlatma_metni(kayit(zaman="2026-10-17T23:50"),
                                                                 dt.datetime(2026, 10, 18, 0, 50))   # tam 60 dk
    assert "dün 23:50 içindi, bilgisayar kapalıydı" in zm.hatirlatma_metni(kayit(zaman="2026-10-17T23:50"),
                                                                           dt.datetime(2026, 10, 18, 0, 51))
    assert "bugün 08:00 içindi, 3 saat geç" in zm.hatirlatma_metni(kayit(zaman="2026-10-18T08:00"),
                                                                    dt.datetime(2026, 10, 18, 11, 0))


def test_hatirlatma_cok_satirli_not_her_satir_italik():
    a = kayit(**{"not": "ilk\n\n  ikinci *x*  \r\nüçüncü"})
    metin = zm.hatirlatma_metni(a, dt.datetime(2026, 10, 17, 20, 0))
    assert metin == "**Hatırlatma** · bugün 20:00\nkütüphane\n*ilk*\n*ikinci x*\n*üçüncü*"
    assert telegram.html_bicim(metin).endswith("<i>ilk</i>\n<i>ikinci x</i>\n<i>üçüncü</i>")
    assert "*" not in telegram.html_bicim(metin)                  # ham yıldız kalmaz
    assert zm.hatirlatma_metni(kayit(**{"not": "  \n \n"}), dt.datetime(2026, 10, 17, 20, 0)).endswith("kütüphane")


def test_hatirlatma_servis_ve_bulut_ortak_ornekler():
    """Aynı örnekler bulut/test/birim.test.mjs'de de koşar: iki taraf aynı html'i üretmeli."""
    import json
    import os
    yol = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hatirlatma_ornekleri.json")
    with open(yol, encoding="utf-8") as f:
        ornekler = json.load(f)
    assert len(ornekler) >= 10
    for o in ornekler:
        metin = zm.hatirlatma_metni(o["kayit"], dt.datetime.fromisoformat(o["simdi"]))
        assert telegram.html_bicim(metin) == o["html"], o["ad"]


def test_dugme_etiketleri_fiilli_callback_ayni():
    d = zm.hatirlatma_dugmeleri("su iç", "", "id1", "gunluk")
    assert [e for e, _ in d[0]] == ["Tamam", "10 dk ertele", "1 saat ertele", "Yarına ertele"]
    assert [v.split(":")[0:3] if v.startswith("h:e") else v.split(":")[0:2] for _, v in d[0]] == [
        ["h", "ok"], ["h", "e", "10"], ["h", "e", "60"], ["h", "e", "y"]]
    assert d[1][0][0] == "Bu tekrarı durdur" and d[1][0][1].startswith("h:dur:")
    assert len(zm.hatirlatma_dugmeleri("su iç")) == 1           # tekrarsız kayıtta durdur düğmesi yok
    for satir in d:
        for etiket, _ in satir:
            emojisiz(etiket)
            assert not EMOJI.search(etiket)


def test_ertele_yarin_sonra_hatasi_yok(monkeypatch):
    monkeypatch.setattr(eylemler, "uygula", lambda *a, **k: "tamam")
    simdi = dt.datetime(2026, 10, 17, 9, 59, 20)
    on = zm.Zamanlayici.ertele({"konu": "x"}, "10", simdi)
    assert on == "Bugün 10:09'da yeniden hatırlatacağım."
    saat = zm.Zamanlayici.ertele({"konu": "x"}, "60", simdi)
    assert saat == "Bugün 10:59'da yeniden hatırlatacağım."
    yarin = zm.Zamanlayici.ertele({"konu": "x"}, "y", simdi)
    assert yarin == "Yarın 09:59'da yeniden hatırlatacağım." and "sonra" not in yarin
    assert zm.Zamanlayici.ertele({"konu": "x"}, "y", dt.datetime(2026, 10, 17, 10, 0)) == \
        "Yarın 10:00'da yeniden hatırlatacağım."


def test_kacirilanlar_toplu_liste_tarih_metniyle(giden):
    eylemler.yaz(eylemler.ARAMALAR, [
        {"id": "e1", "zaman": "2026-10-14T09:00", "konu": "çok eski [x](https://kotu.example)", "durum": "bekliyor"}])
    zm.Zamanlayici(baslat=False).kontrol(dt.datetime(2026, 10, 17, 20, 0, 30))
    metin = giden.metinler()[0]
    assert metin.startswith("Bilgisayar kapalıyken zamanı geçen hatırlatmalar:\n• 14 Eki Çar 09:00 · çok eski")
    assert not ISO.search(metin) and "](" not in metin
    emojisiz(metin)


# ================================================================== uyarı merkezi
def test_aralik_metni_bulunma_eki(monkeypatch):
    for deger, beklenen in (("20:00", "her gün 20:00'de"), ("10:00", "her gün 10:00'da"),
                            ("13:00", "her gün 13:00'te"), ("09:00,20:00", "her gün 09:00 ve 20:00'de")):
        monkeypatch.setenv("TAKIP_SAATLERI", deger)
        assert uy.aralik_metni() == beklenen


def _yeni_odev(**ek):
    teslim = (dt.datetime.now() + dt.timedelta(days=5)).replace(hour=23, minute=59, second=0, microsecond=0)
    x = {"kod": "BIL 283", "odev": "Oyun Mekanikleri", "teslim": teslim.isoformat(timespec="minutes"), "arama": 1}
    x.update(ek)
    return x, teslim


def test_yeni_odev_mesaji_iso_yok_siz_hitabi(giden, monkeypatch):
    monkeypatch.setenv("TAKIP_SAATLERI", "20:00")
    x, teslim = _yeni_odev()
    uy.Uyari(baslat=False).odev_yaz([x])
    metin, k = giden[0]
    assert metin.startswith("**Yeni ödev**\nBIL 283 — Oyun Mekanikleri\nSon teslim: ")
    assert eylemler.tarih_metni(teslim) in metin and not ISO.search(metin)
    assert "Başladığınızda haber verin" in metin and "her gün 20:00'de hatırlatacağım" in metin
    emojisiz(metin)
    assert [e for satir in k["dugmeler"] for e, _ in satir] == ["Başladım", "Değerlendir", "Yarın hatırlat",
                                                                   "Takibi durdur"]
    assert [v.split(":")[1] for satir in k["dugmeler"] for _, v in satir] == ["bas", "deg", "yar", "dur"]


def test_yeni_odev_adi_baglantiya_donusmez(giden):
    x, _ = _yeni_odev(odev="Rapor [buraya](https://kotu.example/x) **acil**")
    uy.Uyari(baslat=False).odev_yaz([x])
    metin = giden[0][0]
    h = telegram.html_bicim(metin)
    assert "<a" not in h and "https://kotu.example/x" in h and "Rapor buraya (https://kotu.example/x) acil" in metin


def test_odev_takip_sonraki_mesaj_ve_bozuk_tarih(giden):
    x, teslim = _yeni_odev(arama=2)
    uy.Uyari(baslat=False).odev_yaz([x, dict(x, teslim="bozuk")])
    assert giden[0][0] == f"BIL 283 — Oyun Mekanikleri ödevine başladınız mı?\nSon teslim: {eylemler.tarih_metni(teslim)}"
    assert "Son teslim: bozuk" in giden[1][0]


def test_bildir_acil_kalir_onemli_yazi_olur(giden, monkeypatch):
    monkeypatch.setattr(uy, "sessiz_mi", lambda: True)
    monkeypatch.setattr(eylemler, "uygula", lambda *a, **k: "not")
    u = uy.Uyari(baslat=False)
    u.bildir("acil", "Bugünkü ders iptal", "okul postası")
    u.bildir("onemli", "Yeni duyuru", "okul postası")
    assert giden[0] == ("🚨 Bugünkü ders iptal", {})
    assert giden[1] == ("**Önemli:** Yeni duyuru", {"sessiz": True})
    for m, _ in giden:
        emojisiz(m)
        assert "📌" not in m


def test_bildir_mail_ozeti_baglantiya_donusmez(giden, monkeypatch):
    monkeypatch.setattr(uy, "sessiz_mi", lambda: False)
    monkeypatch.setattr(eylemler, "uygula", lambda *a, **k: "not")
    u = uy.Uyari(baslat=False)
    kotu = "Notunuz için [giriş yapın](https://kotu.example/giris) **hemen**"
    u.bildir("onemli", kotu, "gmail")
    u.bildir("acil", kotu, "gmail")
    for m, _ in giden:
        h = telegram.html_bicim(m)
        assert "<a" not in h and "https://kotu.example/giris" in h and "giriş yapın" in h


def test_tarih_degisti_ve_gecikti_tek_biciminde(giden, monkeypatch):
    monkeypatch.setattr(uy, "sessiz_mi", lambda: False)
    monkeypatch.setattr(eylemler, "uygula", lambda *a, **k: "not")
    monkeypatch.setattr(eylemler, "takvim_guncelle", lambda *a, **k: None)
    u = uy.Uyari(baslat=False)
    monkeypatch.setattr(u, "_hatirlatmalari_kur", lambda *a, **k: None)
    monkeypatch.setattr(uy.odevler, "klasor", lambda *a, **k: None)
    yeni = (dt.datetime.now() + dt.timedelta(days=10)).replace(hour=23, minute=59, second=0, microsecond=0)
    eski = yeni - dt.timedelta(days=3)
    eylemler.yaz(eylemler.TAKVIM, {"tekrarlayan": [], "olaylar": [
        {"id": "t1", "baslik": "Rapor", "zaman": eski.isoformat(timespec="minutes"), "tur": "Teslim",
         "ders": "BIL207", "durum": "Yapılacak"}]})
    u.teslim_isle({"ders": "BIL207", "baslik": "Rapor", "zaman": yeni.isoformat(timespec="minutes"),
                   "tur": "Teslim"}, "gmail")
    metin = giden[0][0]
    assert f"tarihi değişti, {eylemler.tarih_metni(eski)} yerine {eylemler.tarih_metni(yeni)}" in metin
    assert not ISO.search(metin)

    gecen = (dt.datetime.now() - dt.timedelta(hours=5)).replace(second=0, microsecond=0)
    eylemler.yaz(eylemler.TAKVIM, {"tekrarlayan": [], "olaylar": [
        {"id": "t2", "baslik": "Lab", "zaman": gecen.isoformat(timespec="minutes"), "tur": "Teslim",
         "ders": "BIL207", "durum": "Yapılacak"}]})
    giden.clear()
    u.gecikme_kontrol()
    metin = giden[0][0]
    assert f"son teslim ({eylemler.tarih_metni(gecen)}) geçti" in metin and not ISO.search(metin)
    assert "söyleyin" in metin


# ================================================================== sistem gözcüsü
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


@pytest.mark.parametrize("gece", [True, False])
def test_yeniden_calisiyor_yazi_ve_gece_sessiz(gozcu, giden, monkeypatch, gece):
    monkeypatch.setattr(sistem, "sessiz_mi", lambda *a: gece)
    kotu, iyi = (lambda: (False, "bağlı değil")), (lambda: (True, ""))
    gozcu._kontrol_et("Tailscale", kotu, 1, "onemli", simdi=1_800_000_000.0)
    gozcu._kontrol_et("Tailscale", iyi, 1, "onemli", simdi=1_800_000_300.0)
    assert giden == [("Tailscale yeniden çalışıyor.", {"sessiz": gece})]
    emojisiz(giden[0][0])


def test_sistem_durum_satiri_yazili_durum(gozcu):
    t0 = 1_800_000_000.0
    gozcu.son_tur = t0
    assert gozcu.durum_satiri(t0 + 120) == "Sistem kontrolleri: 9/9 tamam (2 dk önce)."
    gozcu.hata["Tailscale"], gozcu.neden["Tailscale"] = 2, "bağlı değil"
    sat = gozcu.durum_satiri(t0 + 120)
    assert sat.startswith("⚠️ sorun: ") and "• Tailscale: bağlı değil" in sat
    assert "🟢" not in sat and "⏳" not in sat
    gozcu.hata["Tailscale"] = 0
    gozcu.hata["Ağ cihazı"] = 1                      # eşiğin altı: yalnızca izleniyor, ⚠️ yok
    sat = gozcu.durum_satiri(t0 + 120)
    assert "izleniyor: Ağ cihazı" in sat and "⚠️" not in sat
    gozcu.hata["Ağ cihazı"] = 0
    durmus = gozcu.durum_satiri(t0 + 20 * 60)
    assert durmus.startswith("⚠️ sorun: sistem kontrolleri durmuş görünüyor")


def test_beri_metni_ek_mantigi_korunur():
    simdi = 1_800_000_000.0
    z = dt.datetime.fromtimestamp(simdi, eylemler.TZ)
    bugun = (z - dt.timedelta(hours=1)).timestamp()
    assert re.fullmatch(r"\d\d:\d\d'(dan|den|tan|ten) beri", sistem.beri_metni(bugun, simdi))
    dun = (z - dt.timedelta(days=1, hours=1)).timestamp()
    metin = sistem.beri_metni(dun, simdi)
    assert metin.endswith(" beri") and not ISO.search(metin) and metin.startswith(("dün ", "16 ", "15 ", "14 "))
    assert sistem.bulunma_eki("20:10") == "'da" and sistem.bulunma_eki("21:35") == "'te"
    assert sistem.bulunma_eki("yarın 10:00") == "'da" and sistem.bulunma_eki("13:00") == "'te"


# ================================================================== okul postası
def test_erisim_kesildi_maddeler_ayri_satirda_siz_hitabi(tmp_path):
    t = op.ErisimTakibi(str(tmp_path / "e.json"), lambda m: None, once_basarili=True, internet=lambda: True)
    t.durum["ilk_hata"] = 1_800_000_000.0
    for tur in ("red", "ag", "sunucu"):
        m = t.mesaj(tur)
        satirlar = m.split("\n")
        assert any(s.startswith("1) Webmail") for s in satirlar) and any(s.startswith("2) MFA") for s in satirlar)
        assert "Yapmanız gerekenler:" in satirlar and "O zamana kadar okul maillerini göremiyorum." in satirlar
        assert "girebildiğinize bakın" in m and "yazın" in m and "isteyin" in m
        assert "Yapman " not in m and "giremediğine" not in m and "bak." not in m
        assert not ISO.search(m)
        emojisiz(m)


def test_okul_posta_sabit_mesajlari_siz_ve_satirli():
    assert "fakültenize" in op.OkulPostaGozcusu.RED_MESAJI and "\n" in op.OkulPostaGozcusu.RED_MESAJI
    assert "yazmanız gerekiyor" in op.GmailImapGozcusu.RED_MESAJI and "\n" in op.GmailImapGozcusu.RED_MESAJI
    emojisiz(op.OkulPostaGozcusu.RED_MESAJI + op.GmailImapGozcusu.RED_MESAJI)


def test_okul_posta_yeniden_okunuyor_emojisiz(tmp_path):
    gelen = []
    t = op.ErisimTakibi(str(tmp_path / "e.json"), gelen.append, once_basarili=True, internet=lambda: True)
    t0 = 1_800_000_000.0
    t.hata("red", t0)
    t.hata("red", t0 + 31 * 60)
    t.basari()
    assert gelen[-1] == "Okul postası yeniden okunuyor, giriş tekrar başarılı."


def test_okul_posta_durum_satiri_sorun_ve_tarih(tmp_path, monkeypatch):
    monkeypatch.setattr(op.threading, "Thread", Dugum)

    class Gozcu(op.OkulPostaGozcusu):
        KULLANICI, SIFRE = "kullanici", "x"
        DURUM, ERISIM = str(tmp_path / "d.json"), str(tmp_path / "e.json")

    g = Gozcu(types.SimpleNamespace())
    g.bagli, g.son_hata = False, "AUTHENTICATIONFAILED"
    assert g.durum_satiri(1_800_000_000.0).startswith("⚠️ sorun: Okul postası bağlı değil (AUTHENTICATIONFAILED)")
    g.bagli, g.son_yoklama = True, 1_800_000_000.0 - 20 * 60
    g.son_yeni = 1_800_000_000.0 - 3 * 86400
    sat = g.durum_satiri(1_800_000_000.0)
    assert sat.startswith("⚠️ sorun: Okul postası: bağlı, son yoklama 20 dk önce") and not ISO.search(sat)
    assert re.search(r"son yeni mail \d{1,2} \w{3} \w{3} \d\d:\d\d\.$", sat), sat


# ================================================================== limit ve bulut
def test_limit_mesaji_satirlara_bolunur_emojisiz_gece_sessiz(giden, monkeypatch):
    for gece in (True, False):
        giden.clear()
        monkeypatch.setitem(limit._durum, "bitis", None)
        monkeypatch.setattr(limit, "sessiz_mi", lambda *a, g=gece: g)
        assert limit.kontrol("You've hit your session limit · resets 5pm", "sohbet", dt.datetime(2026, 10, 17, 10, 0))
        metin, k = giden[0]
        satirlar = metin.split("\n")
        assert satirlar[0] == "Claude kullanım limiti doldu (sohbet)." and satirlar[1] == "Bugün 17:00 civarı sıfırlanacak."
        assert len(satirlar) == 4 and k == {"sessiz": gece}
        emojisiz(metin)
        assert "⏳" not in metin
    monkeypatch.setitem(limit._durum, "bitis", None)


def test_bulut_yapilandirma_uyarisi_gece_sessiz(giden, monkeypatch):
    monkeypatch.setattr(bulut, "sessiz_mi", lambda *a: True)
    monkeypatch.setattr(bulut, "_uyarildi", [False])
    e = urllib.error.HTTPError("https://x", 401, "yetkisiz", {}, None)
    bulut._yapilandirma_hatasi(e)
    metin, k = giden[0]
    assert metin.startswith("⚠️ Bulut hatırlatıcısı servisi reddediyor (HTTP 401).\n") and k["sessiz"] is True
    assert k["gecmise"] is False and len(metin.split("\n")) == 3
    bulut._yapilandirma_hatasi(e)
    assert len(giden) == 1                                      # tek sefer


# ================================================================== emoji taraması (şablonlar)
def test_sablonlarda_izinli_uc_emoji_disinda_emoji_yok(giden, monkeypatch):
    monkeypatch.setattr(uy, "sessiz_mi", lambda: False)
    monkeypatch.setattr(eylemler, "uygula", lambda *a, **k: "not")
    simdi = dt.datetime(2026, 10, 17, 20, 12)
    metinler = [zm.hatirlatma_metni(kayit(tur=t, tekrar=r), simdi)
                for t in ("acil", None) for r in (None, "gunluk")]
    metinler += [zm.hatirlatma_metni(kayit(zaman="2026-10-15T20:00"), simdi),
                 zm.Zamanlayici.ertele({"konu": "x"}, "y", simdi)]
    x, _ = _yeni_odev()
    uy.Uyari(baslat=False).odev_yaz([x, dict(x, arama=2)])
    u = uy.Uyari(baslat=False)
    u.bildir("onemli", "duyuru", "okul")
    u.bildir("acil", "acil duyuru", "okul")
    metinler += giden.metinler()
    for m in metinler:
        emojisiz(m)
    for d in (zm.hatirlatma_dugmeleri("x", "", "i", "gunluk"), uy.Uyari.odev_dugmeleri("BIL 283", "x")):
        for satir in d:
            for etiket, _ in satir:
                assert not EMOJI.search(etiket), etiket
