"""Okul postası "erişim kesildi" tespiti (MFA geçişi). Telegram'a ve Claude'a çıkmaz, ağa da çıkmaz."""
import imaplib
import json
import socket
import ssl

import pytest

from test_cevrimdisi import giden, yalitim  # noqa: F401  (ortak fikstürler: Telegram'a hiçbir şey gitmesin)

from telefon import eylemler
from telefon.gozculer import okul_posta as op

DK = 60


def takip_kur(tmp_path, internet=True, once_basarili=True):
    gelen = []
    t = op.ErisimTakibi(str(tmp_path / "erisim.json"), gelen.append, once_basarili=once_basarili,
                        internet=lambda: internet)
    return t, gelen


def test_hata_turu_siniflandirmasi():
    assert op.hata_turu(imaplib.IMAP4.error("b'[AUTHENTICATIONFAILED] LOGIN failed'")) == "red"
    assert op.hata_turu(imaplib.IMAP4.error("invalid credentials")) == "red"
    assert op.hata_turu(imaplib.IMAP4.error("INBOX açılamadı")) == "sunucu"
    assert op.hata_turu(socket.gaierror(11001, "getaddrinfo failed")) == "ag"     # DNS
    assert op.hata_turu(TimeoutError("timed out")) == "ag"
    assert op.hata_turu(socket.timeout("timed out")) == "ag"
    assert op.hata_turu(ConnectionRefusedError()) == "ag"
    assert op.hata_turu(OSError(10051, "network unreachable")) == "ag"
    assert op.hata_turu(ssl.SSLError("bad handshake")) == "sunucu"                 # sunucuya ulaşıldı
    assert op.hata_turu(ValueError("x")) == "sunucu"


def test_giris_reddi_30_dk_sonra_tek_uyari_6_saatte_bir_tekrar(tmp_path):
    t, gelen = takip_kur(tmp_path)
    t0 = 1_800_000_000.0
    assert not t.hata("red", t0) and gelen == []                                   # ilk başarısızlık: sessiz
    assert not t.hata("red", t0 + 10 * DK) and gelen == []                         # art arda ama 30 dk dolmadı
    assert t.hata("red", t0 + 31 * DK) and len(gelen) == 1                         # 30 dk geçti: uyarı
    m = gelen[0]
    assert "Okul postası okunamıyor" in m and "MFA" in m and "webmail" in m.lower() and "uygulama şifresi" in m
    assert "BT" in m and "reddediyor" in m
    for dk in (90, 150, 6 * 60 + 30):                                              # aynı kesinti: 6 saat dolmadan yok
        assert not t.hata("red", t0 + dk * DK)
    assert len(gelen) == 1
    assert t.hata("red", t0 + (31 + 6 * 60) * DK) and len(gelen) == 2              # 6 saat sonra yeniden


def test_tek_basarisizlik_uyarmaz(tmp_path):
    t, gelen = takip_kur(tmp_path)
    assert not t.hata("red", 1_800_000_000.0)
    t.basari()                                                                     # uyarı yoktu: "yeniden" mesajı da yok
    assert not t.hata("red", 1_800_000_000.0 + 40 * DK)                            # sayaç sıfırlandı: yeni ilk hata
    assert gelen == []


def test_giris_duzelince_bir_kez_bilgi_ve_sayaclar_sifirlanir(tmp_path):
    t, gelen = takip_kur(tmp_path)
    t0 = 1_800_000_000.0
    t.hata("red", t0)
    t.hata("red", t0 + 31 * DK)
    t.basari()
    assert len(gelen) == 2 and "yeniden okunuyor" in gelen[1]
    t.basari()
    assert len(gelen) == 2                                                         # ikinci başarı sessiz
    d = json.load(open(tmp_path / "erisim.json", encoding="utf-8"))
    assert d["ilk_hata"] is None and d["son_uyari"] is None and d["hata_sayisi"] == 0
    assert not t.hata("red", t0 + 60 * DK) and not t.hata("red", t0 + 80 * DK)     # yeni kesinti baştan sayılır
    assert t.hata("red", t0 + 91 * DK) and len(gelen) == 3


def test_internet_yokken_ag_hatasi_sayilmaz(tmp_path):
    t, gelen = takip_kur(tmp_path, internet=False)
    t0 = 1_800_000_000.0
    for dk in (0, 31, 120, 600):
        assert not t.hata("ag", t0 + dk * DK)
    assert gelen == [] and t.durum["ilk_hata"] is None


def test_internet_varken_sunucuya_ulasilamazsa_uyarir(tmp_path):
    t, gelen = takip_kur(tmp_path, internet=True)
    t0 = 1_800_000_000.0
    t.hata("ag", t0)
    assert t.hata("ag", t0 + 35 * DK)
    assert "ulaşılamıyor" in gelen[0] and "internet var" in gelen[0] and "webmail" in gelen[0].lower()


def test_internet_kesintisi_suren_red_kesintisini_sifirlamaz(tmp_path):
    t0 = 1_800_000_000.0
    durum = {"internet": True}
    gelen = []
    t = op.ErisimTakibi(str(tmp_path / "e.json"), gelen.append, once_basarili=True, internet=lambda: durum["internet"])
    t.hata("red", t0)
    durum["internet"] = False
    t.hata("ag", t0 + 20 * DK)                                                     # yok sayılır, ilk hata korunur
    durum["internet"] = True
    assert t.hata("red", t0 + 32 * DK) and len(gelen) == 1


def test_hic_okunmamis_hesapta_uyari_yok(tmp_path):
    t, gelen = takip_kur(tmp_path, once_basarili=False)
    t0 = 1_800_000_000.0
    for dk in (0, 31, 400):
        assert not t.hata("red", t0 + dk * DK)
    assert gelen == []
    t.basari()                                                                     # bir kez okununca takip başlar
    assert t.once_basarili


def test_durum_yeniden_baslatmada_korunur(tmp_path):
    yol = str(tmp_path / "erisim.json")
    gelen = []
    t0 = 1_800_000_000.0
    op.ErisimTakibi(yol, gelen.append, once_basarili=True, internet=lambda: True).hata("red", t0)
    t = op.ErisimTakibi(yol, gelen.append, internet=lambda: True)                  # yeniden başlatma
    assert t.once_basarili and t.durum["ilk_hata"] == t0
    assert t.hata("red", t0 + 31 * DK) and len(gelen) == 1                         # ilk hata unutulmadı
    t = op.ErisimTakibi(yol, gelen.append, internet=lambda: True)
    assert t.durum["son_uyari"] == t0 + 31 * DK
    assert not t.hata("red", t0 + 3 * 60 * DK) and len(gelen) == 1                 # son uyarı da unutulmadı
    (tmp_path / "bozuk.json").write_text("{yarım", encoding="utf-8")
    assert op.ErisimTakibi(str(tmp_path / "bozuk.json"), gelen.append).durum["ilk_hata"] is None


class Dur(BaseException):  # gözcünün "except Exception"ına takılmasın
    pass


def test_gozcu_dongusu_uyari_notion_notu_ve_geri_donus(giden, tmp_path, monkeypatch):  # noqa: F811
    """Gerçek _dongu: giriş reddi → ~30 dk sonraki denemede Telegram + Notion notu; giriş düzelince bilgi."""
    from telefon.gozculer import uyari as uy
    saat = {"t": 1_800_000_000.0}
    monkeypatch.setattr(op.time, "time", lambda: saat["t"])
    uykular = []

    def uyu(s):
        uykular.append(s)
        saat["t"] += s
        if len(uykular) >= sinir["n"]:
            raise Dur()
    monkeypatch.setattr(op.time, "sleep", uyu)
    monkeypatch.setattr(op, "internet_var", lambda *a, **k: True)
    sonuc = {"red": True}

    class SahteImap:
        def __init__(self, *a, **k):
            pass

        def login(self, k, s):
            if sonuc["red"]:
                raise imaplib.IMAP4.error("b'[AUTHENTICATIONFAILED] LOGIN failed'")

    monkeypatch.setattr(op.imaplib, "IMAP4_SSL", SahteImap)
    monkeypatch.setattr(op.threading, "Thread", lambda *a, **k: type("T", (), {"start": lambda self: None})())
    monkeypatch.setattr(uy, "sessiz_mi", lambda: False)
    (tmp_path / "durum.json").write_text(json.dumps({"uidvalidity": "1", "son_uid": 5}), encoding="utf-8")

    class Gozcu(op.OkulPostaGozcusu):
        KULLANICI, SIFRE = "kullanici", "x"
        DURUM, ERISIM = str(tmp_path / "durum.json"), str(tmp_path / "erisim.json")

    g = Gozcu(uy.Uyari(baslat=False))
    g.erisim.internet = lambda: True
    sinir = {"n": 2}
    with pytest.raises(Dur):
        g._dongu()
    assert uykular[0] == 1800                                                      # hesabı kilitletmemek için 30 dk
    assert len(giden) == 1 and giden[0][0].startswith("**Önemli:** Okul postası okunamıyor") and "MFA" in giden[0][0]
    assert any("Okul postası okunamıyor" in n["metin"] for n in eylemler.oku(eylemler.NOTLAR, []))
    assert not g.bagli and "AUTHENTICATIONFAILED" in g.son_hata

    sonuc["red"], sinir["n"] = False, len(uykular) + 1                             # giriş düzeldi, _yokla'da dur
    monkeypatch.setattr(Gozcu, "_yokla", lambda self, imap: (_ for _ in ()).throw(Dur()))
    with pytest.raises(Dur):
        g._dongu()
    assert len(giden) == 2 and "yeniden okunuyor" in giden[1][0]
    assert json.load(open(tmp_path / "erisim.json", encoding="utf-8"))["ilk_hata"] is None


def test_gmailde_erisim_takibi_yok_eski_mesaj_kalir():
    assert op.GmailImapGozcusu.ERISIM is None and op.OkulPostaGozcusu.ERISIM.endswith("okul_posta.erisim.json")
