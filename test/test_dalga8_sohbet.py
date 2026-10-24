"""Dalga 8 (sohbet/eylemler/servis/planlayıcı): komut çıktılarında ham ISO tarih ve fazladan emoji olmaması, düğme
etiketlerinin fiilli olması (callback verileri aynı), güvenilmeyen başlıkların bağlantıya dönüşmemesi.
Telegram'a, Claude'a ve Notion'a çıkmaz."""
import datetime as dt
import json
import re
import types

import pytest

from test_cevrimdisi import SahteBeyin, giden, sohbet_kur, yalitim, yonetici  # noqa: F401  (ortak fikstürler)

from telefon import eylemler, limit, planlayici, servis
from telefon import sohbet as sh
from telefon import telegram as tg

ISO = re.compile(r"\d{4}-\d\d-\d\d|T\d\d:")
# Kullanıcıya giden metinde izinli emojiler: 🚨 (acil), ⚠️ (sorun), 🎤 (ses dökümü). Gerisi yazıyla söylenir.
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿⏩-⏿↩️]")
IZINLI = ("🚨", "⚠️", "🎤")


def emojili(metin):
    for e in IZINLI:
        metin = metin.replace(e, "")
    return EMOJI.findall(metin)


def temiz(metin):
    """Ham ISO tarih ve izinsiz emoji yok."""
    assert not ISO.search(metin), metin
    assert not emojili(metin), metin


def gun(n, saat=20, dakika=0):
    return (dt.datetime.now() + dt.timedelta(days=n)).replace(hour=saat, minute=dakika, second=0, microsecond=0)


KOTU_BASLIK = "[buraya tıklayın](https://kotu.example/x) **acil** `kod`"


def baglantisiz(metin):
    """html_bicim'den geçince bağlantı/biçim çıkmamalı (güvenilmeyen başlık)."""
    h = tg.html_bicim(metin.replace("**Kurulu hatırlatmalar**", "").replace("**Önümüzdeki 7 gün**", "")
                      .replace("**Yaklaşan teslimler ve sınavlar**", ""))
    assert "<a " not in h and "<code>" not in h and "<i>" not in h, h


# ================================================================== /yardim ve /yeni
def test_yardim_komutlari_alt_alta_ve_emojisiz(giden):
    s = sohbet_kur()
    s._komut("/yardim")
    metin = giden[-1][0]
    assert metin == sh.YARDIM
    temiz(metin)
    satirlar = [x for x in metin.splitlines() if x.startswith("– /")]
    assert [x.split(":")[0] for x in satirlar] == ["– /hatirlatmalar", "– /takvim", "– /teslimler", "– /onaylar",
                                                   "– /durum", "– /yeni"]
    assert all(": " in x for x in satirlar)


def test_yeni_komutu_emojisiz(giden):
    sohbet_kur()._komut("/yeni")
    temiz(giden[-1][0])
    assert "Sohbeti sıfırladım" in giden[-1][0]


# ================================================================== /hatirlatmalar
def test_hatirlatmalar_tarih_ve_dugmeler(giden):
    a, b = gun(1, 20), gun(5, 9)
    eylemler.yaz(eylemler.ARAMALAR, [
        {"id": "h1", "zaman": a.isoformat(timespec="minutes"), "konu": "ilaç iç", "durum": "bekliyor",
         "tekrar": "gunluk"},
        {"id": "h2", "zaman": b.isoformat(timespec="minutes"), "konu": KOTU_BASLIK, "durum": "bekliyor"}])
    sohbet_kur().hatirlatmalari_yaz()
    metin, k = giden[-1]
    temiz(metin)
    assert metin.startswith("**Kurulu hatırlatmalar** (2)")
    assert f"• yarın 20:00 · ilaç iç (her gün tekrarlanıyor)" in metin
    assert f"• {eylemler.tarih_metni(b)} · " in metin
    baglantisiz(metin)
    etiketler = [satir[0][0] for satir in k["dugmeler"]]
    assert etiketler[0] == "İptal et: yarın 20:00 · ilaç iç"
    assert etiketler[1].startswith(f"İptal et: {eylemler.tarih_metni(b)} · buraya tıklayın")
    for satir in k["dugmeler"]:
        temiz(satir[0][0])
        assert satir[0][1].startswith("hi:") and len(satir) == 1


def test_iptal_dugmesi_sonucu_ham_iso_icermez(giden):
    a = gun(1, 20)
    eylemler.yaz(eylemler.ARAMALAR, [{"id": "h1", "zaman": a.isoformat(timespec="minutes"), "konu": "ilaç iç",
                                       "durum": "bekliyor"}])
    s = sohbet_kur()
    s.hatirlatmalari_yaz()
    veri = giden[-1][1]["dugmeler"][0][0][1]
    s.dugme_geldi(veri, "q1", {"message_id": 4})
    metin = giden[-1][0]
    temiz(metin)
    assert metin.startswith("Hatırlatma iptal edildi: yarın 20:00 · ilaç iç")
    assert eylemler.oku(eylemler.ARAMALAR, [])[0]["durum"] == "iptal"


def test_eylem_sonuclari_ham_iso_icermez():
    a = gun(2, 18, 30)
    eylemler.yaz(eylemler.ARAMALAR, [{"id": "h1", "zaman": a.isoformat(timespec="minutes"), "konu": "toplantı",
                                       "durum": "bekliyor"}])
    sonuc = eylemler.uygula({"tip": "hatirlatma_iptal", "konu": "toplantı"})
    temiz(sonuc)
    assert eylemler.tarih_metni(a) in sonuc
    z = gun(9, 10)
    eylemler.yaz(eylemler.TAKVIM, {"tekrarlayan": [], "olaylar": [
        {"id": "o1", "baslik": "Kütüphane", "zaman": z.isoformat(timespec="minutes")}]})
    sonuc = eylemler.uygula({"tip": "takvim_sil", "baslik": "Kütüphane", "zaman": z.isoformat(timespec="minutes")})
    temiz(sonuc)
    assert sonuc.startswith("takvimden kaldırıldı") and eylemler.tarih_metni(z) in sonuc


# ================================================================== /takvim ve /teslimler
def test_takvim_komutu(giden):
    z = gun(3, 23, 59)
    eylemler.yaz(eylemler.TAKVIM, {"tekrarlayan": [], "olaylar": [
        {"id": "t1", "baslik": KOTU_BASLIK, "ders": "BIL251", "tur": "Teslim", "zaman": z.isoformat(timespec="minutes"),
         "durum": "Yapılacak"},
        {"id": "t2", "baslik": "Diş randevusu", "tur": "Randevu", "zaman": gun(1, 14).isoformat(timespec="minutes")}]})
    sohbet_kur().takvimi_yaz()
    metin = giden[-1][0]
    temiz(metin)
    assert metin.startswith("**Önümüzdeki 7 gün**")
    assert "• yarın 14:00 Diş randevusu" in metin
    assert f"• {eylemler.tarih_metni(z)} BIL 251 buraya tıklayın" in metin and "(Teslim), Yapılacak" in metin
    baglantisiz(metin)


def test_takvim_komutu_bos_ve_bozuk(giden):
    eylemler.yaz(eylemler.TAKVIM, {"tekrarlayan": [], "olaylar": []})
    sohbet_kur().takvimi_yaz()
    assert giden[-1][0].endswith("Takvimde bir şey yok.")
    temiz(giden[-1][0])


def test_teslimler_durum_yaziyla_ve_baslik_baglantiya_donusmez(giden):
    simdi = dt.datetime.now()
    eylemler.yaz(eylemler.TAKVIM, {"tekrarlayan": [], "olaylar": [
        {"id": "t1", "baslik": KOTU_BASLIK, "ders": "BIL251", "tur": "Teslim",
         "zaman": gun(5, 23, 59).isoformat(timespec="minutes"), "durum": "Yapılacak"},
        {"id": "t2", "baslik": "Vize", "ders": "BIL207", "tur": "Sınav",
         "zaman": gun(1, 10).isoformat(timespec="minutes"), "durum": "Başlandı"},
        {"id": "t3", "baslik": "Rapor", "ders": "BIL205", "tur": "Teslim",
         "zaman": (simdi - dt.timedelta(minutes=30)).isoformat(timespec="minutes"), "durum": "Gönderildi"}]})
    sohbet_kur()._komut("/teslimler")
    metin = giden[-1][0]
    temiz(metin)
    assert metin.startswith("**Yaklaşan teslimler ve sınavlar** (30 gün)")
    assert "Rapor — Gönderildi, geçti" in metin
    assert "BIL 207 Vize (sınav) — Başlandı, yarın" in metin
    assert "BIL 251 buraya tıklayın (https://kotu.example/x) acil kod — Yapılacak, 5 gün kaldı" in metin
    assert "• yarın 10:00" in metin and all(x.startswith("•") for x in metin.splitlines()[1:])
    baglantisiz(metin)


def test_teslimler_bos(giden):
    eylemler.yaz(eylemler.TAKVIM, {"tekrarlayan": [], "olaylar": []})
    sohbet_kur()._komut("/teslimler")
    assert giden[-1][0] == "Önümüzdeki 30 günde teslim ya da sınav yok."


def test_gun_listesi_ham_iso_icermez():
    g = (dt.date.today() + dt.timedelta(days=7))
    eylemler.yaz(eylemler.TAKVIM, {"olaylar": [], "tekrarlayan": [
        {"gun": g.weekday(), "saat": "10:20", "baslik": "Ders", "ders": "BIL205",
         "not": "10:20-12:00 · ML 201 · Hoca · OBS ders programı (2026-2027 Güz)"}]})
    satirlar = eylemler.gun_listesi(g)
    assert satirlar and satirlar[0].startswith("• 10:20 BIL 205 Ders")
    temiz("\n".join(satirlar))


# ================================================================== /onaylar ve onay istekleri
def test_onaylar_etiketleri_tarih_ve_vazgec(giden, yonetici):
    s = sohbet_kur()
    z = gun(4, 23, 59).isoformat(timespec="minutes")
    k1 = s._onay_iste("gorev", {"proje": "telegram-assistant", "gorev": "README'ye kurulum bölümü ekle"}, "m", "Başlat")
    k2 = s._onay_iste("takvim_sil", {"baslik": "Lab 2", "zaman": z}, "m", "Sil")
    k3 = s._onay_iste("gorev_cevap", {"id": "ab12cd", "cevap": "evet, devam et"}, "m", "Gönder")
    s.mesaj_geldi({"message_id": 1, "text": "/onaylar"})
    metin, k = giden[-1]
    temiz(metin)
    assert metin.startswith("Bekleyen onay istekleri (3)")
    assert f"• Silme: Lab 2 ({eylemler.tarih_metni(z)}) (kalan" in metin
    etiketler = [satir[0][0] for satir in k["dugmeler"]]
    assert etiketler[0].startswith("Vazgeç: Görev (telegram-assistant)")
    assert etiketler[1] == f"Vazgeç: Silme: Lab 2 ({eylemler.tarih_metni(z)})"
    assert etiketler[2].startswith("Vazgeç: Göreve cevap")
    assert [satir[0][1] for satir in k["dugmeler"]] == [f"on:h:{x}" for x in (k1, k2, k3)]
    for e in etiketler:
        temiz(e)
    assert k["bicimli"] is False


def test_silme_onayi_ders_kodu_bosluklu_ve_tarih_okunur(giden):
    z = gun(6, 23, 59).isoformat(timespec="minutes")
    eylemler.yaz(eylemler.TAKVIM, {"tekrarlayan": [], "olaylar": [
        {"id": "o1", "baslik": "Sunum", "ders": "BIL251", "tur": "Teslim", "zaman": z}]})
    s = sohbet_kur('Onay düğmesini gönderdim. ⟦EYLEM {"tip":"takvim_sil","baslik":"sunum"}⟧')
    s._kullanici_turu([("kullanici", "sunumu sil", "")])
    metin, k = giden[-1]
    temiz(metin)
    assert "BIL 251 Sunum" in metin and "BIL251" not in metin and eylemler.tarih_metni(z) in metin
    (evet_yazi, evet), (vazgec_yazi, vazgec) = k["dugmeler"][0]
    assert (evet_yazi, vazgec_yazi) == ("Sil", "Vazgeç")
    assert evet.startswith("on:e:") and vazgec.startswith("on:h:") and evet[5:] == vazgec[5:]
    # onay sonrası mesaj da emojisiz ve ISO'suz
    s.dugme_geldi(evet, "q1", {"message_id": 9})
    temiz(giden[-1][0])
    assert giden[-1][0].startswith("Takvimden kaldırıldı")


def test_gorev_ve_cevap_onay_dugmeleri_fiilli(giden, yonetici):
    s = sohbet_kur('Onay düğmesini gönderdim. ⟦GÖREV {"proje":"telegram-assistant","gorev":"README dosyasına bölüm ekle"}⟧')
    s._kullanici_turu([("kullanici", "başlat", "")])
    metin, k = giden[-1]
    temiz(metin)
    assert [y for y, _ in k["dugmeler"][0]] == ["Başlat", "Vazgeç"]
    s.dugme_geldi(k["dugmeler"][0][0][1], "q1", {"message_id": 7})
    temiz(giden[-1][0])
    assert giden[-1][0].startswith("Görev başladı") and "Bitince buraya yazacağım" in giden[-1][0]
    s2 = sohbet_kur('Tamam. ⟦GÖREV_CEVAP {"id":"ab12cd","cevap":"evet devam"}⟧')
    s2._kullanici_turu([("kullanici", "devam etsin", "")])
    metin, k = giden[-1]
    temiz(metin)
    assert [y for y, _ in k["dugmeler"][0]] == ["Gönder", "Vazgeç"]
    s2.dugme_geldi(k["dugmeler"][0][1][1], "q2", {"message_id": 8})
    assert giden[-1][0] == "Vazgeçtim, bir şey yapmadım."


# ================================================================== brifing
def test_brifing_dugmeleri_ve_yedegi():
    assert [(y, v) for y, v in sh.BRIFING_DUGMELERI[0]] == [
        ("Bugünün ayrıntısı", "b:bugun"), ("Yarın", "b:yarin"), ("Teslimler", "b:teslim")]
    s = sohbet_kur()
    s.brifing({"konu": "BIL 281 dersi, [tık](https://kotu.example) *acil*"})
    madde = s.kuyruk.get_nowait()
    istem, yedek = madde[1], madde[2]
    assert "'**Brifing**' başlığıyla" in istem and "☀️" not in istem and "en fazla 10 satır" in istem.lower()
    assert yedek.startswith("**Brifing**\n") and not emojili(yedek)
    baglantisiz(yedek)
    assert madde[4] == sh.BRIFING_DUGMELERI


def test_brifing_yedegi_limit_doluyken_tarih_okunur(giden, monkeypatch):
    monkeypatch.setattr(limit, "dolu_mu", lambda: True)
    eylemler.yaz(eylemler.TAKVIM, {"olaylar": [], "tekrarlayan": []})
    s = sohbet_kur()
    s.dugme_geldi("b:yarin", "q1", {"message_id": 3})
    metin = giden[-1][0]
    temiz(metin)
    yarin = dt.date.today() + dt.timedelta(days=1)
    assert metin.startswith(f"**Yarın** ({yarin.day} {eylemler.KISA_AY[yarin.month - 1]} "
                            f"{eylemler.KISA_GUN[yarin.weekday()]})")
    assert "ayrıntıyı sonra yazarım" in metin and "⏳" not in metin


# ================================================================== düğme bildirimleri ve hitap
def test_dugme_bildirimleri_metinle(monkeypatch, giden):
    cevaplar = []
    monkeypatch.setattr(tg, "dugme_cevapla", lambda sorgu, metin="", *a, **k: cevaplar.append(metin))
    s = sohbet_kur()
    k = tg.dugme_verisi({"konu": "ilaç iç", "id": "x"})
    s.dugme_geldi(f"h:ok:{k}", "q1", {"message_id": 2})
    assert cevaplar == ["Tamam"]


def test_bulut_dugmesi_bulunamadi_siz_hitabi(giden, monkeypatch):
    import time
    cevaplar = []
    monkeypatch.setattr(tg, "dugme_cevapla", lambda sorgu, metin="", *a, **k: cevaplar.append(metin))
    eylemler.yaz(eylemler.ARAMALAR, [])
    s = sohbet_kur()
    s.dugme_geldi("hb:60:" + "a" * 32, "q1", {"message_id": 2, "date": int(time.time())})
    assert cevaplar == ["Bu hatırlatmayı bulamadım"]
    assert "isterseniz" in giden[-1][0] and "yazmanız" in giden[-1][0] and " istersen " not in giden[-1][0]
    s.dugme_geldi("hb:ok:" + "a" * 32, "q2", {"message_id": 2, "date": int(time.time())})
    assert cevaplar[-1] == "Bu hatırlatmayı bulamadım"          # "Tamam" yanıltmasın


def test_odev_dugmeleri_emojisiz(giden, tmp_path, monkeypatch):
    from telefon.gozculer import uyari as uy
    monkeypatch.setattr(uy, "TAKIP", str(tmp_path / "takip.json"))
    monkeypatch.setattr(uy, "takip_durdur", lambda kod, odev: ["x"])
    s = sohbet_kur()
    k = tg.dugme_verisi({"kod": "BIL251", "odev": "Sunum *acil* [x](https://kotu.example)"})
    s.dugme_geldi(f"o:dur:{k}", "q1", {"message_id": 2})
    metin = giden[-1][0]
    temiz(metin)
    baglantisiz(metin)
    assert "takip mesajlarını durdurdum" in metin


# ================================================================== güvenilmeyen başlık: duz_metin'i atlayan yollar
def html_temiz(metin):
    """Gerçek gönderim yolu gibi html_bicim'den geçince bağlantı çıkmamalı; adres görünür kalmalı."""
    assert "<a " not in tg.html_bicim(metin), metin
    assert "https://kotu.example/x" in metin                     # gizlenmedi, açıkça yazıldı


def test_brifing_limit_yedegi_baslik_baglantiya_donusmez(giden, monkeypatch):
    monkeypatch.setattr(limit, "dolu_mu", lambda: True)
    eylemler.yaz(eylemler.TAKVIM, {"tekrarlayan": [], "olaylar": [
        {"id": "t1", "baslik": KOTU_BASLIK, "ders": "BIL251", "tur": "Teslim", "durum": "Yapılacak",
         "zaman": gun(1, 14).isoformat(timespec="minutes")}]})
    sohbet_kur().dugme_geldi("b:yarin", "q1", {"message_id": 3})
    metin = giden[-1][0]
    temiz(metin)
    html_temiz(metin)
    assert "• 14:00 BIL 251 buraya tıklayın (https://kotu.example/x) acil kod (Teslim), Yapılacak" in metin
    # gun_listesi'nin kendisi de güvenli (yedek yolundan bağımsız)
    satirlar = eylemler.gun_listesi(dt.date.today() + dt.timedelta(days=1))
    assert len(satirlar) == 1 and "<a " not in tg.html_bicim(satirlar[0]) and "[" not in satirlar[0]


def test_hatirlatma_iptal_sonucu_konu_baglantiya_donusmez(giden):
    a = gun(1, 20)
    eylemler.yaz(eylemler.ARAMALAR, [{"id": "h1", "zaman": a.isoformat(timespec="minutes"), "konu": KOTU_BASLIK,
                                       "durum": "bekliyor"}])
    s = sohbet_kur()
    s.hatirlatmalari_yaz()
    s.dugme_geldi(giden[-1][1]["dugmeler"][0][0][1], "q1", {"message_id": 4})
    metin = giden[-1][0]
    assert metin.startswith("Hatırlatma iptal edildi: yarın 20:00 · ")
    html_temiz(metin)
    # eylem sonucunun kendisi de (beyin işaretinden gelen iptal bu metni kullanıcıya yansıtabilir)
    eylemler.yaz(eylemler.ARAMALAR, [{"id": "h2", "zaman": a.isoformat(timespec="minutes"), "konu": KOTU_BASLIK,
                                       "durum": "bekliyor"}])
    html_temiz(eylemler.uygula({"tip": "hatirlatma_iptal", "konu": KOTU_BASLIK}))


def test_silme_onayi_sonucu_baslik_baglantiya_donusmez(giden):
    z = gun(6, 23, 59).isoformat(timespec="minutes")
    eylemler.yaz(eylemler.TAKVIM, {"tekrarlayan": [], "olaylar": [
        {"id": "o1", "baslik": KOTU_BASLIK, "ders": "BIL251", "tur": "Teslim", "zaman": z}]})
    s = sohbet_kur()
    k = s._onay_iste("takvim_sil", {"baslik": KOTU_BASLIK, "zaman": z}, "Silme onayı", "Sil")
    s.dugme_geldi(f"on:e:{k}", "q1", {"message_id": 9})
    metin = giden[-1][0]
    assert metin.startswith("Takvimden kaldırıldı")
    html_temiz(metin)
    temiz(metin)


def test_silme_onayi_hatasi_baslik_baglantiya_donusmez(giden):
    z = gun(6, 23, 59).isoformat(timespec="minutes")
    eylemler.yaz(eylemler.TAKVIM, {"tekrarlayan": [], "olaylar": []})        # onaylanan kayıt artık yok
    s = sohbet_kur()
    k = s._onay_iste("takvim_sil", {"baslik": KOTU_BASLIK, "zaman": z}, "Silme onayı", "Sil")
    s.dugme_geldi(f"on:e:{k}", "q1", {"message_id": 9})
    metin = giden[-1][0]
    assert metin.startswith("⚠️ Onayladınız ama yapamadım: takvimde bulunamadı")
    html_temiz(metin)


def test_tekrari_durdur_sonucu_konu_baglantiya_donusmez(giden):
    a = gun(1, 20)
    eylemler.yaz(eylemler.ARAMALAR, [{"id": "h1", "zaman": a.isoformat(timespec="minutes"), "konu": KOTU_BASLIK,
                                       "durum": "bekliyor", "tekrar": "gunluk"}])
    k = tg.dugme_verisi({"tur": "hatirlatma", "konu": KOTU_BASLIK, "not": "", "id": "h1"})
    sohbet_kur().dugme_geldi(f"h:dur:{k}", "q1", {"message_id": 4})
    metin = giden[-1][0]
    assert metin.startswith("Bu hatırlatma artık tekrarlanmayacak: ")
    html_temiz(metin)


def test_odev_degerlendirme_bildirimi_etiket_baglantiya_donusmez(giden):
    s = sohbet_kur()
    etiket = "BIL 251 — Sunum [tıkla](https://kotu.example/x)"          # klasör adı Learn'deki ödev adından gelir
    s.odev_bildir(etiket, "Süre 2 saat.", None, None)
    s.odev_bildir(etiket, "", None, "limit")
    for metin, _ in giden[-2:]:
        html_temiz(metin)


def test_takip_baslat_gecmis_teslim_hatasi_ham_iso_icermez():
    z = (dt.datetime.now() - dt.timedelta(days=2)).replace(hour=23, minute=59, second=0, microsecond=0)
    eylemler.yaz(eylemler.TAKVIM, {"tekrarlayan": [], "olaylar": [
        {"id": "t1", "baslik": "Sunum", "ders": "BIL251", "tur": "Teslim", "zaman": z.isoformat(timespec="minutes")}]})
    with pytest.raises(ValueError) as e:
        eylemler.uygula({"tip": "odev_takip_baslat", "ders": "BIL 251", "odev": "sunum"})
    temiz(str(e.value))
    assert eylemler.tarih_metni(z) in str(e.value)


# ================================================================== sistem bildirimleri
def test_gorev_ders_odev_is_bildirimleri_emojisiz(giden, monkeypatch):
    s = sohbet_kur()
    monkeypatch.setattr(s, "_ders_notu_yolu", lambda d, t: None)
    g = {"proje": "telegram-assistant", "kisa": "ab12cd", "gorev": "README düzelt"}
    for tur, metin in (("soru", "Hangisi?"), ("hata", "bitmedi"), ("ozet", "yapıldı")):
        s.gorev_bildir(g, tur, metin)
    s.ders_bildir("PSYC 114", "2026-10-01", "## Kısa özet\nKonu işlendi.", None)
    s.ders_bildir("PSYC 114", "2026-10-01", None, "kayıt yok")
    s.odev_bildir("BIL 283 — One Pager", "Süre 2 saat.", None, None)
    s.odev_bildir("BIL 283 — One Pager", "", None, "limit")
    s._is_bitti("araştırma")("özet", "https://notion.so/x", None)
    s._is_bitti("araştırma")("", "", "hata oldu")
    for m in giden.metinler():
        temiz(m)
    assert any("1 Eki Per ders notu hazır" in m for m in giden.metinler())
    assert any(m.startswith("⚠️ Görev") for m in giden.metinler())    # sorun mesajlarında ⚠️ kalır


def test_ders_notu_ve_odev_baslangic_mesajlari(giden):
    from telefon import dersler, odevler
    s = sohbet_kur()
    y = types.SimpleNamespace(notu_oku=lambda d, t: None, hazirla=lambda d, t: "hazırlık başladı")
    dersler.YONETICI, eski = y, dersler.YONETICI
    try:
        s._ders_notu({"ders": "PSYC 114", "tarih": "2026-10-01"})
    finally:
        dersler.YONETICI = eski
    temiz(giden[-1][0])
    assert giden[-1][0].startswith("PSYC 114 1 Eki Per notu henüz yok")
    odevler.YONETICI, eski = types.SimpleNamespace(degerlendir=lambda *a: "BIL 283 — Sunum: değerlendirme başladı"), \
        odevler.YONETICI
    try:
        s._odev({"ders": "BIL283", "odev": "Sunum"})
    finally:
        odevler.YONETICI = eski
    temiz(giden[-1][0])


# ================================================================== /durum
def _servis(monkeypatch, saglikli=True, beklenen=0):
    s = servis.Servis.__new__(servis.Servis)
    s.baslangic = 0
    s.dinleyici = types.SimpleNamespace(saglikli=lambda: saglikli, bot_adi="b", son_basari=0, son_hata="zaman aşımı")
    s.esitleyici = types.SimpleNamespace(son_basari=0)
    s.sohbet = types.SimpleNamespace(beyin=None)
    s.gorevler = types.SimpleNamespace(kayitlar=[])
    s.gozculer = []
    monkeypatch.setattr(tg, "cift_yonlu", lambda: True)
    monkeypatch.setattr(tg, "_kuyruk_oku", lambda: ["x"] * beklenen)
    # gerçek kayitlar\plan-*.json okunmasın (canlı servisin gece planı durumu testi bozuyordu)
    monkeypatch.setattr(planlayici, "durum_satiri", lambda simdi=None: "Gece planı: yarın için yapıldı.")
    return s


def test_durum_metni_yaziyla_ve_sorun_basliklari(monkeypatch):
    a = gun(1, 20)
    eylemler.yaz(eylemler.ARAMALAR, [{"id": "h1", "zaman": a.isoformat(timespec="minutes"), "konu": "x",
                                       "durum": "bekliyor"}])
    monkeypatch.setattr(limit, "dolu_mu", lambda: False)
    iyi = _servis(monkeypatch)
    metin = iyi.durum_metni()
    temiz(metin)
    assert "🟢" not in metin and "⏳" not in metin and "sorun" not in metin
    assert metin.splitlines()[0].startswith("Servis çalışıyor, ")
    assert "Kurulu hatırlatma: 1 (sıradaki yarın 20:00)" in metin
    kotu = _servis(monkeypatch, saglikli=False, beklenen=2)
    monkeypatch.setattr(limit, "dolu_mu", lambda: True)
    satirlar = kotu.durum_metni().splitlines()
    temiz("\n".join(satirlar))
    sorunlu = [x for x in satirlar if x.startswith("⚠️")]
    assert sorunlu and all(x.startswith("⚠️ sorun: ") for x in sorunlu)
    assert any("Telegram yoklaması başarısız" in x for x in sorunlu)
    assert any("gönderilmeyi bekleyen 2 mesaj" in x for x in sorunlu)
    assert any(x.startswith("Claude kullanım limiti dolu") for x in satirlar)


def test_durum_metni_bozuk_gozcu_ve_dosya(monkeypatch):
    s = _servis(monkeypatch)
    monkeypatch.setattr(limit, "dolu_mu", lambda: False)

    class Bozuk:
        def durum_satiri(self):
            raise RuntimeError("x")
    s.gozculer = [Bozuk()]
    metin = s.durum_metni()
    assert "⚠️ sorun: Bozuk: durum okunamadı (RuntimeError)" in metin
    with open(eylemler.ARAMALAR, "w", encoding="utf-8") as f:
        f.write("{bozuk")
    assert "⚠️ sorun: hatırlatma dosyası okunamadı." in s.durum_metni()


def test_bulut_satiri_sorunlu_satir_sorunla_baslar():
    simdi = dt.datetime(2026, 10, 17, 15, 0, tzinfo=dt.timezone.utc)
    ms = int(simdi.timestamp() * 1000)
    iyi = servis.bulut_satiri({"son_tarama": {"zaman": "2026-10-17T14:59:00Z", "hatalar": []},
                               "nabiz": {"ms": ms - 3 * 60000}}, simdi)
    assert iyi == "Bulut hatırlatıcısı: son tarama 1 dk önce. Son nabız 3 dk önce."
    gecikmis = servis.bulut_satiri({"son_tarama": {"zaman": "2026-10-17T14:40:00Z", "hatalar": ["Notion 401"]},
                                    "nabiz": {"ms": ms - 20 * 60000}}, simdi)
    assert gecikmis.startswith("⚠️ sorun: Bulut hatırlatıcısı: son tarama 20 dk önce (her dakika olmalıydı).")
    assert "Son hata: Notion 401" in gecikmis and "(5 dk'da bir gelmeli)" in gecikmis
    assert servis.bulut_satiri(None).startswith("⚠️ sorun: Bulut hatırlatıcısına ulaşılamadı")
    for m in (iyi, gecikmis):
        temiz(m)


def test_gece_plani_durum_satiri_okunur(monkeypatch, tmp_path):
    monkeypatch.setattr(planlayici, "KAYITLAR", str(tmp_path))
    simdi = dt.datetime(2026, 10, 17, 20, 0)
    (tmp_path / "plan-2026-10-18.json").write_text(json.dumps(
        {"gun": "2026-10-18", "olusturuldu": "2026-10-17T10:00:00", "kabul": [1, 2]}), encoding="utf-8")
    sat = planlayici.durum_satiri(simdi)
    assert sat == "Gece planı: yarın için yapıldı (bugün 10:00), 2 mesaj kuruldu."
    temiz(sat)
    eksik = planlayici.durum_satiri(dt.datetime(2026, 10, 19, 0, 45))
    assert eksik.startswith("⚠️ sorun: Gece planı:") and eksik.endswith("kuruldu. Bugün için plan yok.")
    temiz(eksik)
    (tmp_path / "plan-2026-10-18.json").write_text("{bozuk", encoding="utf-8")
    assert planlayici.durum_satiri(simdi).startswith("⚠️ sorun: gece planı okunamadı")


# ================================================================== kaynak taraması
KAYNAKLAR = ["sohbet.py", "eylemler.py", "servis.py", "planlayici.py", "dersler.py", "odevler.py", "gorevler.py"]
# kullanıcıya gitmeyen satırlar: günlük, beyne giden bağlam/istem ve işaretler, iç işlev işaretleri
ATLA = ("log(", "🔘", "⟦", "_dugme_kaydi", "hatalar.append", "startswith(", "->", "→")


def test_kaynakta_izinsiz_emoji_yok():
    import os
    kok = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "telefon")
    bulunan = []
    for ad in KAYNAKLAR:
        with open(os.path.join(kok, ad), encoding="utf-8") as f:
            for no, satir in enumerate(f, 1):
                if any(a in satir for a in ATLA):
                    continue
                if emojili(satir):
                    bulunan.append(f"{ad}:{no}: {satir.strip()[:90]}")
    assert not bulunan, "\n".join(bulunan)


def test_istem_emoji_ve_tarih_kurallari():
    import os
    yol = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "istemler", "sohbet.md")
    istem = open(yol, encoding="utf-8").read()
    assert "Emoji kullanma; yalnızca acil bir durumda ⚠️." in istem and "Emoji kullanmayın" not in istem
    assert "21 Eki Çar 23:59" in istem and "bugün 20:00" in istem and "yarın 23:59" in istem
    assert "21 Ekim Salı" not in istem and "**\"siz\"**" in istem
    for eski in ("🗑 Sil", "✅ Başlat", "✅ Gönder", "❌ Vazgeç"):
        assert eski not in istem
