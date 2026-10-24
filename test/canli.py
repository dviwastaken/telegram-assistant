"""Çalışan servise karşı canlı uçtan uca test (yerel test kancası /simule üzerinden). Her adım günlükten doğrulanır.

pytest bunu toplamaz (adı test_*.py değil): servisin cevapları gerçekten kullanıcıya gider, Claude ve Notion kullanılır.
Kullanım: python test\\canli.py komutlar sohbet hatirlatma [is] [odev] [tekrar] [akis] [brifing]
  komutlar   /durum ve /takvim
  sohbet     disk sorusu; cevapta iç ayrıntı (işaret adı, bağlam) olmamalı
  hatirlatma 2 dk sonraya hatırlatma kur → gelişi → 10 dk ertele → /hatirlatmalar → İptal (yaklaşık 3 dk)
  is         dosya modunda arka plan işi (Notion'a sayfa yazar)
  odev       Blokchain Sunumu değerlendirmesi (Değerlendirme.md'yi yeniden yazar)
  tekrar     her gün tekrarlayan hatırlatma: kur → gelişi → ertesi güne ilerleme → durdur (yaklaşık 3 dk)
  akis       uzun cevap aynı mesajda yazıldıkça güncellenir
  brifing    brifing düğmeleri, /teslimler, /durum'daki gözcü satırları
"""
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.request

KOK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(KOK, "kayitlar", "servis.log")
JETON = None  # __main__'de okunur: dosya içe aktarılınca (ör. pytest test\canli.py) yan etki olmasın
ARAMALAR = os.path.join(os.environ.get("TAKVIM_KLASORU", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "veri", "takvim")), "aramalar.json")
DUGMELER = os.path.join(KOK, "kayitlar", "telegram_dugmeler.json")
SOHBET = os.path.join(KOK, "kayitlar", "sohbet.jsonl")
SONUC = []


def post(yol, govde):
    r = urllib.request.Request(f"http://127.0.0.1:7341{yol}", data=json.dumps(govde).encode(),
                               headers={"X-Jeton": JETON, "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=10).read())


def satirlar():
    return open(LOG, encoding="utf-8", errors="replace").read().splitlines()


def bekle(desen, baslangic, sure=120):
    """baslangic satır sayısından sonra desene uyan satırı bekle."""
    son = time.time() + sure
    while time.time() < son:
        for s in satirlar()[baslangic:]:
            if re.search(desen, s):
                return s
        time.sleep(1)
    return None


def kontrol(ad, kosul, ayrinti=""):
    SONUC.append((ad, bool(kosul)))
    print(f"[{'TAMAM' if kosul else 'HATA '}] {ad}" + (f" — {ayrinti}" if ayrinti else ""), flush=True)


def aramalar():
    return json.load(open(ARAMALAR, encoding="utf-8"))


def son_dugme(tur, konu_parcasi):
    d = json.load(open(DUGMELER, encoding="utf-8"))
    adaylar = [(v["_zaman"], k) for k, v in d.items() if v.get("tur") == tur and konu_parcasi in v.get("konu", "")]
    return max(adaylar)[1] if adaylar else None


def adim_komutlar():
    n = len(satirlar())
    post("/simule", {"mesaj": "/durum"})
    s = bekle(r"telegram ➜ #\d+: [\"']Servis çalışıyor", n, 20)
    kontrol("/durum cevabı gitti", s, s)
    kontrol("/durum yanlış alarm vermiyor", s and "⚠️ sorun" not in s, s)
    n = len(satirlar())
    post("/simule", {"mesaj": "/takvim"})
    s = bekle(r"telegram ➜ #\d+: [\"']\*\*Önümüzdeki 7 gün", n, 20)
    kontrol("/takvim cevabı gitti", s, s)


def adim_hatirlatma():
    konu = "Canlı test hatırlatması (otomatik test, yok sayabilirsiniz)"
    n = len(satirlar())
    z = (dt.datetime.now() + dt.timedelta(minutes=2)).replace(second=0, microsecond=0)
    post("/simule", {"mesaj": f"Saat {z:%H:%M}'de bana tam olarak şu konuyla hatırlatma kur: {konu}"})
    s = bekle(r"sohbet eylemi: hatırlatma kuruldu", n, 90)
    kontrol("sohbetten hatırlatma kuruldu", s, s)
    s = bekle(r"notion: [1-9]\d*/\d+ kayıt gönderildi", n, 90)
    kontrol("hatırlatma Notion'a gitti", s, s)
    s = bekle(r"hatırlatma gönderildi: .*Canlı test", n, 200)
    kontrol("zamanı gelince Telegram'a düğmeli gitti", s, s)
    gonderim = next((x for x in satirlar()[n:] if "telegram ➜" in x and "Hatırlatma" in x and "Canlı test" in x), "")
    mid = int(re.search(r"#(\d+)", gonderim).group(1)) if gonderim else None
    a = [x for x in aramalar() if "Canlı test" in x["konu"]]
    kontrol("durum 'arandi' (Notion: Gönderildi)", a and a[-1]["durum"] == "arandi", a[-1]["durum"] if a else "kayıt yok")
    k = son_dugme("hatirlatma", "Canlı test")
    kontrol("düğme anahtarı kaydedildi", k, k)
    n = len(satirlar())
    once = dt.datetime.now()
    post("/simule", {"dugme": f"h:e:10:{k}", "mesaj_id": mid})
    s = bekle(r"telegram ➜ #\d+: [\"'].*yeniden hatırlatacağım", n, 20)
    kontrol("10 dk erteleme onayı gitti", s, s)
    bekleyen = [x for x in aramalar() if "Canlı test" in x["konu"] and x["durum"] == "bekliyor"]
    fark = (dt.datetime.fromisoformat(bekleyen[-1]["zaman"]) - once) if bekleyen else None
    kontrol("yeni hatırlatma ~10 dk sonraya kuruldu", fark and dt.timedelta(minutes=8) < fark <= dt.timedelta(minutes=11),
            str(fark))
    s = bekle(r"notion: [1-9]\d*/\d+ kayıt gönderildi", n, 90)
    kontrol("ertelenen hatırlatma Notion'a gitti", s, s)
    n = len(satirlar())
    post("/simule", {"mesaj": "/hatirlatmalar"})
    s = bekle(r"telegram ➜ #\d+: [\"']\*\*Kurulu hatırlatmalar", n, 20)
    kontrol("/hatirlatmalar listesi gitti", s, s)
    k = son_dugme("hiptal", "Canlı test")
    n = len(satirlar())
    post("/simule", {"dugme": f"hi:{k}"})
    s = bekle(r"telegram ➜ #\d+: [\"']Hatırlatma iptal edildi", n, 20)
    kontrol("İptal düğmesi çalıştı", s, s)
    kalan = [x for x in aramalar() if "Canlı test" in x["konu"] and x["durum"] == "bekliyor"]
    kontrol("bekleyen test hatırlatması kalmadı", not kalan, str(kalan))
    s = bekle(r"notion: \d+/\d+ kayıt gönderildi, [1-9]\d*/\d+ durum", n, 90)
    kontrol("iptal durumu Notion'a gitti", s, s)


def adim_sohbet():
    n = len(satirlar())
    post("/simule", {"mesaj": "disk durumu ne? kısaca"})
    s = bekle(r"telegram (➜ #\d+: |✎ #\d+ \(akış bitti\))", n, 90)
    kontrol("sohbet cevabı geldi", s, s)
    yazisma = [json.loads(x) for x in open(SOHBET, encoding="utf-8")][-1]
    ic = re.search(r"bağlam|BAĞLAM|işaret|\[SİSTEM\]|odev_takip|⟦", yazisma["metin"])
    kontrol("cevapta iç ayrıntı yok", yazisma["kim"] == "Claude" and not ic, yazisma["metin"][:200])


def adim_is():
    n = len(satirlar())
    baslangic = dt.datetime.now().isoformat(timespec="seconds")
    post("/simule", {"mesaj": "Önceki taslak dosyaları okuyamamıştı. Şimdi ödev klasöründeki Değerlendirme.md'ye ve "
                              "BIL 251 Learn içeriği ile ders notlarına dayanarak, grubumuzun 'Askeri İstihbarat ve blok "
                              "zinciri' konusu için benim bölümüme yönelik 4-5 maddelik konuşma notu hazırla, arka planda."})
    s = bekle(r"sohbet eylemi: arka plan işi başladı", n, 90)
    kontrol("arka plan işi başladı", s, s)
    kontrol("dosya modunda başladı", s and "(dosya)" in s, s)
    s = bekle(r"iş bitti \(\d+ sn\)|iş başarısız", n, 900)
    kontrol("iş bitti ve Notion sayfası oluştu", s and "iş bitti" in s and "notion." in s, s)
    s = bekle(r"telegram ➜ #\d+: [\"']\*\*İş bitti", n, 30)
    kontrol("sonuç sohbete gönderildi", s, s)
    son = [json.loads(x) for x in open(SOHBET, encoding="utf-8")]
    sonuc = next((k["metin"] for k in reversed(son) if k["metin"].startswith("**İş bitti")
                  and k["zaman"] >= baslangic), "")
    kontrol("başlık kısa", len(sonuc.split("\n")[0]) < 130, sonuc.split("\n")[0])
    kontrol("dosyaları okuyabildi", sonuc and "okuyamadım" not in sonuc and "erişemedim" not in sonuc, sonuc[:300])


def adim_odev():
    n = len(satirlar())
    post("/simule", {"mesaj": "Blokchain Sunumu ödevini değerlendirir misin?"})
    s = bekle(r"sohbet eylemi: .*değerlendirme başladı", n, 90)
    kontrol("ödev değerlendirmesi başladı", s, s)
    s = bekle(r"ödev değerlendirildi|ödev değerlendirilemedi", n, 900)
    kontrol("değerlendirme bitti", s and "değerlendirildi:" in s, s)
    s = bekle(r"telegram ➜ #\d+ dosya: Değerlendirme.md", n, 60)
    kontrol("Değerlendirme.md dosyası gönderildi", s, s)


def _png_kare(boyut=64, renk=(220, 30, 30)):
    import struct
    import zlib
    satir = b"\x00" + bytes(renk) * boyut
    veri = zlib.compress(satir * boyut)

    def parca(tur, icerik):
        return struct.pack(">I", len(icerik)) + tur + icerik + struct.pack(">I", zlib.crc32(tur + icerik))
    return (b"\x89PNG\r\n\x1a\n" + parca(b"IHDR", struct.pack(">IIBBBBB", boyut, boyut, 8, 2, 0, 0, 0))
            + parca(b"IDAT", veri) + parca(b"IEND", b""))


def adim_medya():
    """Bot sohbete ses, PDF ve görsel yükler; dönen dosya kimlikleriyle kullanıcı göndermiş gibi işletir.
    venv'in python'uyla çalıştır (PyAV gerekiyor): .venv\\Scripts\\python.exe test\\canli.py medya"""
    os.environ.setdefault("GUNLUK_DOSYASI", "canli-test.log")  # servisin günlüğüne karışmasın
    sys.path.insert(0, KOK)
    import tempfile
    import av
    import numpy as np
    from telefon import ses_tanima, telegram
    sys.path.insert(0, os.path.join(KOK, "test"))
    from pdf_yardimci import kucuk_pdf as _kucuk_pdf
    gecici = tempfile.mkdtemp()

    # 1) sesli mesaj: arşivdeki Türkçe örnek → OGG/Opus
    ses = ses_tanima.oku_16k(os.path.join(KOK, "arsiv", "telefon-arama", "ses-ornekleri", "03.mp3"))
    oga = os.path.join(gecici, "ses.oga")
    with av.open(oga, "w", format="ogg") as cikti:
        akis = cikti.add_stream("libopus", rate=48000, layout="mono")
        kare = av.AudioFrame.from_ndarray((ses * 32767).astype(np.int16).reshape(1, -1), format="s16", layout="mono")
        kare.sample_rate = 16000
        yeniden = av.AudioResampler(format="s16", layout="mono", rate=48000)
        for k in yeniden.resample(kare) + yeniden.resample(None):
            for p in akis.encode(k):
                cikti.mux(p)
        for p in akis.encode(None):
            cikti.mux(p)
    m = telegram.api("sendVoice", {"chat_id": telegram.sohbet_id(), "caption": "🔧 canlı test: sesli mesaj"},
                     sure=60, dosya=("voice", "ses.oga", open(oga, "rb").read()))
    n = len(satirlar())
    post("/simule", {"medya": {"voice": {k: m["voice"][k] for k in ("file_id", "file_unique_id")}}})
    s = bekle(r"telegram ⬅ kullanıcı \(ses\): ", n, 180)
    kontrol("sesli mesaj yazıya çevrildi", s and "ders" in s.lower(), s)
    s = bekle(r"telegram ➜ #\d+: [\"']🎤 “", n, 30)
    kontrol("çevrilen metin kullanıcıya gösterildi", s, s)
    s = bekle(r"sohbet: cevap ", n, 120)
    kontrol("sesli mesaja beyin cevap verdi", s, s)

    # 2) PDF belge
    pdf = os.path.join(gecici, "canli-test-yonerge.pdf")
    _kucuk_pdf(pdf, "Canli test: sunum 7 dakika, en az 6 slayt, teslim PDF.")
    m = telegram.api("sendDocument", {"chat_id": telegram.sohbet_id(), "caption": "🔧 canlı test: PDF"},
                     sure=60, dosya=("document", "canli-test-yonerge.pdf", open(pdf, "rb").read()))
    n = n_pdf = len(satirlar())
    post("/simule", {"medya": {"document": {"file_id": m["document"]["file_id"],
                                            "file_name": "canli-test-yonerge.pdf"},
                               "caption": "Bu yönergede sunum kaç dakika? Sadece sayıyı yaz, bir şey taşıma."}})
    s = bekle(r"telegram ⬇ dosya indirildi: .*canli-test-yonerge\.pdf", n, 60)
    kontrol("PDF indirildi", s, s)
    s = bekle(r"sohbet: cevap ", n, 120)
    son = [json.loads(x) for x in open(SOHBET, encoding="utf-8")][-1]
    kontrol("PDF içeriği okundu (cevapta 7)", s and son["kim"] == "Claude" and "7" in son["metin"], son["metin"][:150])

    # 3) fotoğraf
    m = telegram.api("sendPhoto", {"chat_id": telegram.sohbet_id(), "caption": "🔧 canlı test: görsel"},
                     sure=60, dosya=("photo", "kare.png", _png_kare()))
    pdf_satiri = bekle(r"telegram ⬇ dosya indirildi: .*canli-test-yonerge\.pdf", n_pdf, 1)  # bu çalıştırmanınki
    n = len(satirlar())
    post("/simule", {"medya": {"photo": [{k: p.get(k) for k in ("file_id", "file_size", "width")}
                                         for p in m["photo"]],
                               "caption": "Bu görsel ne renk? Tek kelimeyle söyle."}})
    s = bekle(r"sohbet: cevap ", n, 120)
    son = [json.loads(x) for x in open(SOHBET, encoding="utf-8")][-1]
    kontrol("görsel beyne ulaştı (cevapta kırmızı)", s and "kırmızı" in son["metin"].lower(), son["metin"][:150])

    foto_satiri = bekle(r"telegram ⬇ dosya indirildi: .*foto-", n, 1)

    # test dosyalarını gelenlerden kaldır: yalnızca bu adımın indirdiği, günlükte adı geçen dosyalar
    gelen = os.path.join(os.path.dirname(os.path.dirname(ARAMALAR)), "odevler", "_gelen")
    for satir in (pdf_satiri, foto_satiri):
        ad = re.search(r"dosya indirildi: (.+) \(\d+ KB\)", satir or "")
        for yol in ([os.path.join(gelen, ad.group(1)), os.path.join(gelen, ad.group(1) + ".metin.txt")] if ad else []):
            if os.path.isfile(yol):
                os.remove(yol)


def adim_tekrar():
    """Her gün tekrarlayan hatırlatma: kurulur, zamanı gelince gider, kayıt ertesi güne ilerler (Notion'da da),
    "Bu tekrarı durdur" düğmesi seriyi kapatır."""
    sys.path.insert(0, KOK)
    from telefon import notion
    konu = "Canlı test tekrarı (otomatik test, yok sayabilirsiniz)"
    n = len(satirlar())
    z = (dt.datetime.now() + dt.timedelta(minutes=2)).replace(second=0, microsecond=0)
    post("/simule", {"mesaj": f"Her gün saat {z:%H:%M}'de bana tam olarak şu konuyla tekrarlayan hatırlatma kur: {konu}"})
    s = bekle(r"sohbet eylemi: hatırlatma kuruldu: .*her gün tekrarlanıyor", n, 90)
    kontrol("sohbetten günlük tekrar kuruldu", s, s)
    s = bekle(r"notion: [1-9]\d*/\d+ kayıt gönderildi", n, 90)
    kontrol("tekrarlayan hatırlatma Notion'a gitti", s, s)
    s = bekle(r"hatırlatma gönderildi: .*Canlı test tekrarı", n, 200)
    kontrol("zamanı gelince gitti", s, s)
    s = bekle(r"tekrarlayan hatırlatma ilerledi: .*Canlı test tekrarı", n, 20)
    ertesi = (z + dt.timedelta(days=1)).isoformat(timespec="minutes")
    a = [x for x in aramalar() if "Canlı test tekrarı" in x["konu"] and x["zaman"] == ertesi]  # bu çalıştırmanınki
    kontrol("kayıt ertesi güne ilerledi, bekliyor", a and a[-1]["zaman"] == ertesi and a[-1]["durum"] == "bekliyor",
            f"{s} | {a[-1] if a else 'kayıt yok'}")
    s = bekle(r"notion: .*[1-9]\d*/\d+ zaman güncellendi", n, 90)
    kontrol("ilerleyen zaman Notion'a gitti", s, s)
    sayfa = notion._rest("GET", f"/pages/{a[-1]['notion']}") if a and a[-1].get("notion") else {}
    p = sayfa.get("properties", {})
    nz = notion._yerel((p.get("Zaman", {}).get("date") or {}).get("start"))
    ntekrar = (p.get("Tekrar", {}).get("select") or {}).get("name")
    kontrol("Notion'da Zaman ertesi gün, Tekrar 'Günlük'", nz == ertesi and ntekrar == "Günlük", f"{nz} {ntekrar}")
    k = son_dugme("hatirlatma", "Canlı test tekrarı")
    n = len(satirlar())
    post("/simule", {"dugme": f"h:dur:{k}"})
    s = bekle(r"telegram ➜ #\d+: [\"']Bu hatırlatma artık tekrarlanmayacak", n, 20)
    kontrol("'Bu tekrarı durdur' düğmesi çalıştı", s, s)
    a = [x for x in aramalar() if "Canlı test tekrarı" in x["konu"] and x["zaman"] == ertesi]
    kontrol("seri iptal", a and a[-1]["durum"] == "iptal", a[-1]["durum"] if a else "kayıt yok")
    s = bekle(r"notion: \d+/\d+ kayıt gönderildi, [1-9]\d*/\d+ durum", n, 90)
    kontrol("iptal Notion'a gitti", s, s)


def adim_akis():
    """Uzun cevap yazıldıkça aynı mesajda güncellenir, son hâli o mesaja yazılır."""
    n = len(satirlar())
    post("/simule", {"mesaj": "İzmir'de bir öğrencinin hafta sonu gezebileceği üç yeri ikişer cümleyle anlat."})
    s = bekle(r"telegram ➜ #\d+ \(akış taslağı\)", n, 90)
    kontrol("taslak mesaj açıldı", s, s)
    mid = re.search(r"#(\d+)", s).group(1) if s else "yok"
    s = bekle(rf"telegram ✎ #{mid} \(akış bitti\)", n, 120)
    kontrol("son hâl aynı mesaja yazıldı", s, s)
    son = [json.loads(x) for x in open(SOHBET, encoding="utf-8")][-1]
    kontrol("yazışmada imleç ya da işaret yok", son["kim"] == "Claude" and not son["metin"].endswith(" …")
            and "⟦" not in son["metin"], son["metin"][:120])


def adim_brifing():
    """Brifing düğmeleri (Teslimler beyinsiz, Yarın beyne soru olarak), /teslimler ve /durum'daki gözcü satırları."""
    n = len(satirlar())
    post("/simule", {"dugme": "b:teslim", "mesaj_id": None})
    s = bekle(r"telegram ➜ #\d+: [\"'](\*\*Yaklaşan teslimler|Önümüzdeki \d+ günde teslim)", n, 20)
    kontrol("Teslimler düğmesi cevap verdi", s, s)
    n = len(satirlar())
    post("/simule", {"dugme": "b:yarin", "mesaj_id": None})
    s = bekle(r"sohbet: cevap ", n, 120)
    son = [json.loads(x) for x in open(SOHBET, encoding="utf-8")][-1]
    kontrol("Yarın düğmesi beyne soru olarak gitti ve cevaplandı", s and son["kim"] == "Claude", son["metin"][:120])
    n = len(satirlar())
    post("/simule", {"mesaj": "/teslimler"})
    s = bekle(r"telegram ➜ #\d+: [\"'](\*\*Yaklaşan teslimler|Önümüzdeki \d+ günde teslim)", n, 20)
    kontrol("/teslimler komutu", s, s)
    r = urllib.request.Request("http://127.0.0.1:7341/durum", headers={"X-Jeton": JETON})
    durum = json.loads(urllib.request.urlopen(r, timeout=10).read())
    metin = durum.get("durum", "")
    for parca in ("Okul postası", "Gmail", "Sistem kontrolleri", "Gece planı"):
        kontrol(f"/durum'da '{parca}' satırı", parca in metin, next((x for x in metin.split("\n") if parca in x), ""))
    kontrol("/durum'da uyarı yok", "⚠️" not in metin, "\n".join(x for x in metin.split("\n") if "⚠️" in x))


if __name__ == "__main__":
    JETON = open(os.path.join(KOK, "kontrol.jeton"), encoding="utf-8").read().strip()
    for ad in sys.argv[1:]:
        globals()[f"adim_{ad}"]()
    print(f"\n{sum(t for _, t in SONUC)}/{len(SONUC)} kontrol geçti")
