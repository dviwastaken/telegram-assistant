"""Dalga 8.4: gönderim sağlamlığı (bölme HTML sınırına göre, 'too long' 400'de düz metne bölüp yeniden deneme,
akış taslağında ham işaret yok, link_preview_options). Telegram'a çıkmaz: telegram.api sahte."""
import datetime as dt
import os
import sys
import tempfile

os.environ.setdefault("TAKVIM_KLASORU", tempfile.mkdtemp(prefix="takvim-test-"))
os.environ.setdefault("GUNLUK_DOSYASI", "test.log")
os.environ.setdefault("HAVA_KONUM", "")
os.environ.setdefault("TELEGRAM_SOHBET_JETONU", "")    # .env'deki gerçek jeton testlerde kullanılmasın
os.environ.setdefault("TELEGRAM_SOHBET_ID", "4242")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from telefon import eylemler, telegram as tg  # noqa: E402


@pytest.fixture(autouse=True)
def yalitim(tmp_path, monkeypatch):
    """Kuyruk/düğme dosyaları geçici klasörde; gerçek Telegram çağrısı olursa test patlar."""
    monkeypatch.setattr(tg, "KUYRUK", str(tmp_path / "kuyruk.json"))
    monkeypatch.setattr(tg, "DUGMELER", str(tmp_path / "dugmeler.json"))
    monkeypatch.setattr(tg, "OFSET", str(tmp_path / "ofset.txt"))
    monkeypatch.setattr(tg.time, "sleep", lambda s: None)
    monkeypatch.setattr(tg, "gecmis_dinleyicisi", None)
    monkeypatch.setattr(tg, "_yedek_bot", lambda: ("yedek-jeton", "4242"))

    def api_yok(*a, **k):
        raise AssertionError(f"testte gerçek Telegram çağrısı: {a[:1]}")
    monkeypatch.setattr(tg, "api", api_yok)


def _kaydet(monkeypatch, hata=None):
    """telegram.api yerine: çağrıları (yöntem, veri) olarak topla; hata(yontem, veri) bir istisna döndürürse fırlat."""
    cagri = []

    def api(yontem, veri=None, jeton=None, **k):
        cagri.append((yontem, dict(veri or {})))
        h = hata(yontem, veri or {}) if hata else None
        if h:
            raise h
        return {"message_id": len(cagri)} if yontem == "sendMessage" else True
    monkeypatch.setattr(tg, "api", api)
    return cagri


# ------------------------------------------------------------------ bölme
def test_kacisla_uzayan_metnin_her_parcasi_html_sinirinda():
    metin = "\n".join(f"{i}. satır: a & b < c > d && <<etiket>> & **kalın** `kod`" for i in range(400))
    assert len(metin) > 3900 and len(tg.html_bicim(metin)) > 2 * tg.TELEGRAM_SINIR
    parcalar = tg.bol(metin)
    assert len(parcalar) > 1
    assert all(len(tg.html_bicim(p)) <= tg.TELEGRAM_SINIR and len(p) <= tg.SINIR for p in parcalar)
    assert "".join(parcalar).count("satır:") == 400                     # hiçbir satır kaybolmadı


def test_tek_paragraf_ampersand_dolu_metin_bolunur():
    metin = "&" * 5000                                                  # boşluksuz: tam sınırdan kesilir
    parcalar = tg.bol(metin)
    assert all(len(tg.html_bicim(p)) <= tg.TELEGRAM_SINIR for p in parcalar)
    assert sum(len(p) for p in parcalar) == 5000


def test_kod_blogu_bolununce_kapatilip_yeniden_acilir():
    kod = "\n".join(f"    x{i} = {i} < {i + 1}" for i in range(400))
    metin = f"Açıklama:\n```python\n{kod}\n```\nSon söz."
    parcalar = tg.bol(metin)
    assert len(parcalar) > 1
    for p in parcalar:
        assert p.count("```") % 2 == 0, p[:80]                          # her parçada blok kapalı
        h = tg.html_bicim(p)
        assert len(h) <= tg.TELEGRAM_SINIR and "```" not in h          # html_bicim bloğu <pre>'ye çevirebildi
    assert parcalar[1].startswith("```python\n")                        # aynı dille yeniden açıldı
    assert "<pre>    x" in tg.html_bicim(parcalar[1])                   # kod satırının girintisi korundu
    assert "Son söz." in parcalar[-1] and "<pre>" not in tg.html_bicim(parcalar[-1]).split("Son söz.")[1]


def test_kalin_cift_satir_ortasinda_yarim_kalmaz():
    metin = "**" + "uzun kalın cümle " * 400 + "**"                     # tek satır: boşluktan kesilir
    parcalar = tg.bol(metin)
    assert len(parcalar) > 1
    for p in parcalar:
        assert p.count("**") % 2 == 0, p[-40:]
        assert tg.html_bicim(p).startswith("<b>") and "**" not in tg.html_bicim(p)


def test_satir_sonundan_kesilen_tek_yildiz_cifti_dokunulmaz():
    # tam satırda eşlenmemiş '**' (ör. '2 ** 3') kalına çevrilmesin
    metin = ("2 ** 3 = 8\n" + "x" * 80 + "\n") * 60
    for p in tg.bol(metin, sinir=500):
        assert not p.endswith("**")


def test_duz_metin_bolmesi_isaret_eklemez():
    metin = "```\n" + "satır\n" * 1500 + "```"
    parcalar = tg.bol(metin, bicimli=False)
    assert len(parcalar) > 1 and all(len(p) <= tg.SINIR for p in parcalar)
    assert "".join(parcalar).count("```") == 2


# ------------------------------------------------------------------ "message is too long"
def test_too_long_400_kuyruga_girmez_kucuk_duz_parcalarla_gider(monkeypatch):
    def hata(yontem, veri):
        if len(veri.get("text", "")) > 1800:                            # Telegram bizden farklı saydı
            return tg.TelegramHatasi(400, "Bad Request: message is too long")
        return None
    cagri = _kaydet(monkeypatch, hata)
    monkeypatch.setattr(tg, "SOHBET_JETONU", "test")                   # düğmeler yalnızca sohbet botunda
    metin = "Uzun cevap. " * 300                                       # ~3600: bol tek parça bırakır
    son = tg.gonder(metin, dugmeler=[[("Tamam", "x:ok")]], gecmise=False)
    assert son is not None and tg._kuyruk_oku() == []
    giden = [v for y, v in cagri if y == "sendMessage" and len(v["text"]) <= 1800]
    assert len(giden) >= 2 and all("parse_mode" not in v for v in giden)   # düz metin
    assert "".join(v["text"] for v in giden).replace(" ", "") == metin.replace(" ", "")
    assert "reply_markup" in giden[-1] and all("reply_markup" not in v for v in giden[:-1])


def test_too_long_kuyrugu_tikamaz(monkeypatch):
    def hata(yontem, veri):
        if len(veri.get("text", "")) > 1000:
            return tg.TelegramHatasi(400, "Bad Request: message is too long")
        return None
    tg._kuyruk_yaz([{"zaman": dt.datetime.now().timestamp() - 600, "parcalar": ["eski uzun mesaj " * 200],
                     "dugmeler": None, "sessiz": False, "bicimli": True},
                    {"zaman": dt.datetime.now().timestamp() - 300, "parcalar": ["arkadaki kısa mesaj"],
                     "dugmeler": None, "sessiz": False, "bicimli": True}])
    cagri = _kaydet(monkeypatch, hata)
    assert tg.kuyrugu_bosalt() == 0 and tg._kuyruk_oku() == []
    assert any("arkadaki kısa mesaj" in v["text"] for y, v in cagri)


def _alt_parca_ag_hatasi(durum):
    """1800'den uzun metin 'too long'; 'ag' açıkken düz alt parçalardan 'dusen'incisi ve sonrası ağ hatasıyla düşer."""
    def hata(yontem, veri):
        t = veri.get("text", "")
        if len(t) > 1800:
            return tg.TelegramHatasi(400, "Bad Request: message is too long")
        durum["n"] += 1
        if durum["ag"] and durum["n"] >= durum["dusen"]:
            return tg.TelegramHatasi(0, "ağ koptu")
        durum.setdefault("gitti", []).append(t)                       # gerçekten giden düz alt parçalar
        return None
    return hata


def test_too_long_alt_parca_dusunce_yalniz_gitmeyenler_kuyruga_girer(monkeypatch):
    durum = {"n": 0, "ag": True, "dusen": 3}                           # 1. ve 2. alt parça gider, 3.'de ağ kopar
    cagri = _kaydet(monkeypatch, _alt_parca_ag_hatasi(durum))
    monkeypatch.setattr(tg, "SOHBET_JETONU", "test")
    metin = "Uzun cevap. " * 300
    assert tg.gonder(metin, dugmeler=[[("Tamam", "x:ok")]], gecmise=False) is None
    gitmis = list(durum["gitti"])
    assert len(gitmis) == 2
    kuyruk = tg._kuyruk_oku()
    assert len(kuyruk) == 1                                            # tek kayıt: yalnızca gitmeyen alt parçalar
    k = kuyruk[0]
    assert k["bicimli"] is False and k["isaretli"] is True and k["dugmeler"] == [[["Tamam", "x:ok"]]]
    assert (" ".join(gitmis) + " " + " ".join(k["parcalar"])).split() == metin.split()   # eksik de fazla da yok
    # ağ düzelince kuyruk yalnızca kalanları gönderir; gitmiş parçalar tekrarlanmaz, düğmeler son parçada
    durum["ag"] = False
    onceki = len(cagri)
    assert tg.kuyrugu_bosalt() == 0 and tg._kuyruk_oku() == []
    yeni = [v for y, v in cagri[onceki:] if y == "sendMessage"]
    assert [v["text"] for v in yeni] == k["parcalar"]                  # "(gecikmeli ...)" öneki devam parçasına eklenmez
    assert "reply_markup" in yeni[-1] and all("reply_markup" not in v for v in yeni[:-1])


def test_too_long_alt_parca_dusunce_sonraki_parcalar_ayri_kayitta_dugmeler_sonda(monkeypatch):
    durum = {"n": 0, "ag": True, "dusen": 2}                           # ilk parçanın 2. alt parçasında ağ kopar
    cagri = _kaydet(monkeypatch, _alt_parca_ag_hatasi(durum))
    monkeypatch.setattr(tg, "SOHBET_JETONU", "test")
    metin = "Uzun cevap. " * 500                                       # 6000 karakter: iki ham parça
    parcalar = tg.bol(metin)
    assert len(parcalar) == 2
    assert tg.gonder(metin, dugmeler=[[("Tamam", "x:ok")]], gecmise=False) is None
    k1, k2 = tg._kuyruk_oku()
    assert k1["bicimli"] is False and k1["dugmeler"] is None and k1["isaretli"] is True      # gitmeyen alt parçalar
    assert k2["parcalar"] == [parcalar[1]] and k2["bicimli"] is True                         # sonraki ham parça
    assert k2["dugmeler"] == [[["Tamam", "x:ok"]]]
    assert k2["devami"] is True and "isaretli" not in k2              # gecikmeli giderse "(devamı, gecikmeli ...)" alır
    gitmis = list(durum["gitti"])
    assert len(gitmis) == 1
    assert (" ".join(gitmis) + " " + " ".join(k1["parcalar"])).split() == parcalar[0].split()


def test_too_long_hic_alt_parca_gitmediyse_eski_davranis_tum_parca_kuyrukta(monkeypatch):
    durum = {"n": 0, "ag": True, "dusen": 1}                           # ilk alt parça bile gitmez
    _kaydet(monkeypatch, _alt_parca_ag_hatasi(durum))
    monkeypatch.setattr(tg, "SOHBET_JETONU", "")
    metin = "Uzun cevap. " * 300
    assert tg.gonder(metin, gecmise=False) is None
    (k,) = tg._kuyruk_oku()
    assert k["parcalar"] == tg.bol(metin) and k["bicimli"] is True and "isaretli" not in k


# ------------------------------------------------------------------ kuyruktan boşaltırken kısmi ilerleme
def _kuyruga_koy(parcalar, dugmeler=None, bicimli=True, yas=600):
    tg._kuyruk_yaz([{"zaman": dt.datetime.now().timestamp() - yas, "parcalar": parcalar, "dugmeler": dugmeler,
                     "sessiz": False, "bicimli": bicimli}])


def _kelimeler(metinler):
    return " ".join(metinler).split()


def test_kuyruktan_bosaltirken_alt_parca_dusunce_kopya_ve_kayip_yok(monkeypatch):
    durum = {"n": 0, "ag": True, "dusen": 2}                           # 1. alt parça gider, 2.'de ağ kopar
    cagri = _kaydet(monkeypatch, _alt_parca_ag_hatasi(durum))
    monkeypatch.setattr(tg, "SOHBET_JETONU", "test")                   # düğmeler yalnızca sohbet botunda
    metin = "satır sonu. " * 300                                       # ~3600: tek ham parça, Telegram 'too long' der
    _kuyruga_koy([metin], dugmeler=[[["Tamam", "x:ok"]]])
    assert tg.kuyrugu_bosalt() == 1
    (k,) = tg._kuyruk_oku()                                            # kayıt yalnızca gitmeyen alt parçalara indi
    assert k["bicimli"] is False and k["isaretli"] is True and k["dugmeler"] == [[["Tamam", "x:ok"]]]
    assert len(durum["gitti"]) == 1 and durum["gitti"][0].startswith("(gecikmeli, ")
    durum["ag"] = False
    onceki = len(cagri)
    assert tg.kuyrugu_bosalt() == 0 and tg._kuyruk_oku() == []
    yeni = [v for y, v in cagri[onceki:] if y == "sendMessage"]
    assert [v["text"] for v in yeni] == k["parcalar"]                  # tekrar gönderilen yalnızca kalanlar
    assert not any(v["text"].startswith("(gecikmeli") for v in yeni)   # önceki mesajın devamı: öneksiz
    assert "reply_markup" in yeni[-1] and all("reply_markup" not in v for v in yeni[:-1])
    gonderilen = durum["gitti"]                                       # iki turda gerçekten giden her şey
    assert _kelimeler(gonderilen)[3:] == metin.split()                 # önek (3 sözcük) dışında kopya da kayıp da yok


def test_kuyruktan_bosaltirken_kismi_ilerleme_sonraki_parcalar_ayri_kayit(monkeypatch):
    durum = {"n": 0, "ag": True, "dusen": 2}
    cagri = _kaydet(monkeypatch, _alt_parca_ag_hatasi(durum))
    monkeypatch.setattr(tg, "SOHBET_JETONU", "test")
    parcalar = ["Uzun cevap. " * 158, "Son parça. " * 20]                # ilki önekle 1800'ü aşar: 'too long' → alt parça
    _kuyruga_koy(parcalar, dugmeler=[[["Tamam", "x:ok"]]])
    assert tg.kuyrugu_bosalt() == 2
    k1, k2 = tg._kuyruk_oku()                                          # sıra: önce gitmeyen alt parçalar
    assert k1["bicimli"] is False and k1["isaretli"] is True and k1["dugmeler"] is None
    assert k2["parcalar"] == [parcalar[1]] and k2["bicimli"] is True and k2["devami"] is True
    assert k2["dugmeler"] == [[["Tamam", "x:ok"]]] and "isaretli" not in k2
    durum["ag"] = False
    onceki = len(cagri)
    assert tg.kuyrugu_bosalt() == 0 and tg._kuyruk_oku() == []
    yeni = [v for y, v in cagri[onceki:] if y == "sendMessage"]
    assert [v["text"] for v in yeni[:-1]] == k1["parcalar"]            # kalan: öneksiz
    assert yeni[-1]["text"].startswith("(devamı, gecikmeli ") and yeni[-1]["text"].rstrip().endswith(parcalar[1].rstrip())
    assert "reply_markup" in yeni[-1] and all("reply_markup" not in v for v in yeni[:-1])
    gonderilen = durum["gitti"]                                       # iki turda gerçekten giden her şey
    kelimeler = _kelimeler(gonderilen)
    assert kelimeler.count("Uzun") == 158 and kelimeler.count("Son") == 20   # her sözcük tam bir kez


def test_esp_yedek_bot_alt_parcalari_kismen_gonderip_dusunce_kopya_olmaz(monkeypatch):
    esp = {"n": 0, "gitti": []}
    cagri = []

    def api(yontem, veri=None, jeton=None, **k):
        cagri.append((yontem, jeton))
        t = (veri or {}).get("text", "")
        if len(t) > 1800:
            raise tg.TelegramHatasi(400, "Bad Request: message is too long")
        if jeton == "test":                                            # sohbet botu hiç ulaşamıyor
            raise tg.TelegramHatasi(0, "ağ koptu")
        esp["n"] += 1
        if esp["n"] >= 2:                                              # ESP ilk alt parçayı gönderir, ikincisinde düşer
            raise tg.TelegramHatasi(0, "ağ koptu")
        esp["gitti"].append(t)
        return {"message_id": esp["n"]}
    monkeypatch.setattr(tg, "api", api)
    monkeypatch.setattr(tg, "SOHBET_JETONU", "test")
    metin = "Uzun cevap. " * 300
    assert tg.gonder(metin, dugmeler=[[("Tamam", "x:ok")]], gecmise=False) is None
    assert len(esp["gitti"]) == 1
    (k,) = tg._kuyruk_oku()
    assert k["bicimli"] is False and k["isaretli"] is True and k["dugmeler"] == [[["Tamam", "x:ok"]]]
    assert _kelimeler(esp["gitti"] + k["parcalar"]) == metin.split()   # ilk alt parça tekrar kuyruğa girmedi


def test_gecikme_oneki_bolme_olcumune_dahil_sinira_yakin_parca_too_long_almaz(monkeypatch):
    uzun = {"hata": 0}

    def hata(yontem, veri):
        if len(veri.get("text", "")) > tg.TELEGRAM_SINIR:
            uzun["hata"] += 1
            return tg.TelegramHatasi(400, "Bad Request: message is too long")
        return None
    cagri = _kaydet(monkeypatch, hata)
    monkeypatch.setattr(tg, "SOHBET_JETONU", "test")
    parca = "abcd " * 815                                              # 4075 karakter: tek başına sığar, önekle sığmaz
    assert len(parca) > 4060 and len(tg.html_bicim(parca)) <= tg.TELEGRAM_SINIR
    _kuyruga_koy([parca.strip()], dugmeler=[[["Tamam", "x:ok"]]])
    assert tg.kuyrugu_bosalt() == 0
    giden = [v for y, v in cagri if y == "sendMessage"]
    assert uzun["hata"] == 0 and all(len(v["text"]) <= tg.TELEGRAM_SINIR for v in giden)
    assert len(giden) == 2 and giden[0]["text"].startswith("(gecikmeli, ")   # önek ilk parçada, ikinci parçada yok
    assert "(gecikmeli" not in giden[1]["text"]
    assert _kelimeler([v["text"] for v in giden])[3:] == parca.split()        # kayıp ve kopya yok
    assert "reply_markup" in giden[-1] and "reply_markup" not in giden[0]


def test_parse_hatasinda_duz_metne_dusme_korunur(monkeypatch):
    def hata(yontem, veri):
        if veri.get("parse_mode"):
            return tg.TelegramHatasi(400, "Bad Request: can't parse entities")
        return None
    cagri = _kaydet(monkeypatch, hata)
    assert tg.gonder("**kalın** metin", gecmise=False) is not None
    assert cagri[-1][1]["text"] == "**kalın** metin" and "parse_mode" not in cagri[-1][1]


def test_gecikmeli_onek_tarih_metniyle(monkeypatch):
    zaman = dt.datetime.now().replace(second=0, microsecond=0) - dt.timedelta(minutes=30)
    tg._kuyruk_yaz([{"zaman": zaman.timestamp(), "parcalar": ["kütüphane"], "dugmeler": None, "sessiz": False,
                     "bicimli": True}])
    cagri = _kaydet(monkeypatch)
    assert tg.kuyrugu_bosalt() == 0
    assert cagri[-1][1]["text"].startswith(f"(gecikmeli, {eylemler.tarih_metni(zaman)})\n")
    assert "bugün " in cagri[-1][1]["text"] or "dün " in cagri[-1][1]["text"]


# ------------------------------------------------------------------ önizleme ve akış
def test_link_preview_options_gonderilir(monkeypatch):
    cagri = _kaydet(monkeypatch)
    monkeypatch.setattr(tg, "SOHBET_JETONU", "test")
    tg.gonder("bağlantı: https://ornek.example/x", gecmise=False)
    a = tg.Akis()
    a.guncelle("Uzunca bir taslak metni burada yazılıyor ki akış başlasın, evet: https://ornek.example")
    a.bitir("Uzunca bir taslak metni burada yazılıyor ki akış başlasın, evet: https://ornek.example — bitti.")
    gonderimler = [v for y, v in cagri if y in ("sendMessage", "editMessageText")]
    assert len(gonderimler) == 3
    for v in gonderimler:
        assert v["link_preview_options"] == {"is_disabled": True} and "disable_web_page_preview" not in v


def test_akis_taslaginda_ham_isaret_yok_imlec_uc_nokta(monkeypatch):
    cagri = _kaydet(monkeypatch)
    monkeypatch.setattr(tg, "SOHBET_JETONU", "test")
    monkeypatch.setattr(tg.Akis, "ARALIK", 0)
    a = tg.Akis()
    a.guncelle("## Plan\nYarın **08:00** için `hatırlatma` kurdum, her sabah tekrar edecek.")
    a.guncelle("## Plan\nYarın **08:00** için `hatırlatma` kurdum, her sabah tekrar edecek.\n# Not\n**Önemli:** su")
    taslaklar = [v["text"] for y, v in cagri]
    assert len(taslaklar) == 2
    for t in taslaklar:
        assert t.endswith(" …") and "▍" not in t
        assert "**" not in t and "`" not in t and not any(s.lstrip().startswith("#") for s in t.split("\n"))
    assert taslaklar[0].startswith("Plan\nYarın 08:00 için hatırlatma kurdum")
    a.bitir("## Plan\nYarın **08:00** için kurdum.")
    son = cagri[-1][1]
    assert son["parse_mode"] == "HTML" and "<b>08:00</b>" in son["text"] and "…" not in son["text"]


def test_akis_araligi_iki_buçuk_saniye():
    assert tg.Akis.ARALIK == 2.5 and tg.Akis.IMLEC == " …"


def test_dugme_cevapla_yeni_metin_parametresi_yok(monkeypatch):
    import inspect
    assert "yeni_metin" not in inspect.signature(tg.dugme_cevapla).parameters
    cagri = _kaydet(monkeypatch)
    tg.dugme_cevapla("q1", "Tamam", 5)
    assert [y for y, _ in cagri] == ["answerCallbackQuery", "editMessageReplyMarkup"]
    tg.dugme_cevapla("q2", "Tamam", 5, dugmeleri_kaldir=False)
    assert [y for y, _ in cagri][2:] == ["answerCallbackQuery"]
