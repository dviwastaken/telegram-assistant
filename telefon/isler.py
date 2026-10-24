"""Arka plan işleri: sohbette başlatılan uzun işler (plan, araştırma, özet).

Ayrı bir `claude -p` süreci (sonnet) işi yapar. Sonucun tamamı Notion'da Claude sayfasının altına yeni bir sayfa
olarak kaydedilir. Kısa özet Telegram sohbetine yazılır.

İki mod var, ikisi bilerek birleştirilmez:
- web (varsayılan): WebSearch/WebFetch, dosya aracı yok.
- dosya: Read/Glob/Grep, yalnızca ortak klasör (ORTAK_KLASOR) ve ders notları klasörü; web yok. Web içeriğindeki bir talimatın
  işi şifre dosyalarını okuyup dışarı göndermeye yönlendirmesi böylece mümkün olmaz.
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time

from . import limit
from . import notion
from .ayarlar import KAYITLAR, KOK, ayar, log

CLAUDE = shutil.which("claude")
# Çıktı sayfalarının altına ekleneceği Notion sayfasının kimliği (.env: NOTION_ANA_SAYFA)
CLAUDE_SAYFA = ayar("NOTION_ANA_SAYFA", "")
ORTAK = ayar("ORTAK_KLASOR", os.path.join(KOK, "ortak"))
PANOPTO = ayar("PANOPTO_KLASORU", os.path.join(KOK, "panopto"))
WEB_ARACLARI = ["WebSearch", "WebFetch"]
KOK_IS = os.path.join(KAYITLAR, "is-klasoru")  # web modunun çalışma klasörü: içinde şifre dosyası yok
os.makedirs(KOK_IS, exist_ok=True)
DOSYA_ARACLARI = ["Read", "Glob", "Grep"]
DOSYA_NOTU = (f"Dosyaları okuyabilirsin (yalnızca okuma): ödev klasörleri {ORTAK}\\odevler\\<ders>\\<ödev>, Learn ders "
              f"içerikleri {ORTAK}\\dersler\\<ders>\\İçerik.md (PDF/PPTX/DOCX dosyalarının metni yanlarındaki "
              f"'<dosya adı>.metin.txt'), ders notları {PANOPTO}\\<ders>\\<tarih> - Ders Notu.md. "
              f"Ders klasörlerinin adı '2026-1 <KOD> <Ders adı> 1' biçimindedir (ör. {PANOPTO}\\2026-1 PSYC 114 Sosyal "
              "Psikoloji 1); Glob orada dosya bulamayabilir, Read ile tam yolu dene. Web erişimin yok.")
ISTEM = """kullanıcı için arka planda bir iş yapıyorsun. Kullanıcı üniversitede okuyan bir öğrenci ve yazılım geliştirici. İşi baştan sona kendin bitir, soru sorma. {araclar}

Kullanıcının takviminden bağlam:
{baglam}

İş: {gorev}

Çıktı biçimi:
- İlk satır "BAŞLIK:" ile başlasın: Notion sayfası için en fazla 6 kelimelik başlık.
- İkinci satır "ÖZET:" ile başlasın: Telegram'da gönderilecek 1-3 kısa cümle.
- Sonra boş bir satır bırak ve sonucun tamamını yaz. Bu bölüm Notion'a kaydedilecek, sade markdown olabilir."""


def _notion_sayfasi(baslik, metin):
    """Claude sayfasının altına sonuç sayfası (REST). Dönüş: URL ya da None."""
    if not notion.JETON:
        return None
    bloklar = []
    for paragraf in [p for p in re.split(r"\n\s*\n", metin) if p.strip()][:95]:
        p = paragraf.strip()
        tur, p = ("heading_3", p.lstrip("#").strip()) if p.startswith("#") else \
                 ("bulleted_list_item", p[2:]) if p.startswith(("- ", "* ")) and "\n" not in p else ("paragraph", p)
        bloklar.append({"object": "block", "type": tur,
                        tur: {"rich_text": [{"type": "text", "text": {"content": p[:1900]}}]}})
    try:
        s = notion._rest("POST", "/pages", {
            "parent": {"type": "page_id", "page_id": CLAUDE_SAYFA}, "icon": {"type": "emoji", "emoji": "🧾"},
            "properties": {"title": {"title": [{"text": {"content": baslik[:180]}}]}}, "children": bloklar})
        return s.get("url")
    except Exception as e:
        log("iş sonucu Notion'a yazılamadı:", repr(e))
        return None


def komut(dosyalar=False):
    araclar = DOSYA_ARACLARI if dosyalar else WEB_ARACLARI
    args = [CLAUDE, "-p", "--model", "sonnet", "--output-format", "json", "--tools", ",".join(araclar),
            "--permission-mode", "dontAsk", "--no-session-persistence", "--strict-mcp-config"]
    if dosyalar:
        # Okuma araçlarına genel izin VERİLMEZ: o zaman her yeri okuyabiliyordu (2026-10-16 denendi). İzinsiz hâlde
        # yalnızca çalışma klasörü (ORTAK_KLASOR) ve --add-dir okunur, dışı izin ister ve dontAsk onu reddeder.
        args += ["--add-dir", PANOPTO]
    else:
        args += ["--allowedTools", *araclar]
    return args, (ORTAK if dosyalar else KOK_IS)


def baslat(gorev, baglam, bitince, dosyalar=False):
    """İşi arka planda başlatır. bitince(ozet, url, hata) çağrılır."""
    def is_():
        t0 = time.time()
        log(f"iş başladı ({'dosya' if dosyalar else 'web'}): {gorev}")
        args, klasor = komut(dosyalar)
        araclar = DOSYA_NOTU if dosyalar else "Gerekiyorsa web araması yapabilirsin; bilgisayardaki dosyalara erişimin yok."
        try:
            p = subprocess.run(args, input=ISTEM.format(gorev=gorev, baglam=baglam, araclar=araclar),
                               capture_output=True, text=True, encoding="utf-8", cwd=klasor, timeout=900,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            sonuc = str(json.loads(p.stdout).get("result", "")).strip()
            limit.kontrol(sonuc, "arka plan işi")
            if not sonuc or "session limit" in sonuc or "Failed to authenticate" in sonuc:
                raise RuntimeError(sonuc[:200] or "boş sonuç")
        except Exception as e:
            log(f"iş başarısız: {gorev}: {e!r}")
            bitince("", None, str(e)[:200])
            return
        b = re.match(r"\s*BAŞLIK:\s*(.+?)\n", sonuc)
        baslik = b.group(1).strip() if b else gorev[:60]
        if b:
            sonuc = sonuc[b.end():]
        m = re.match(r"\s*ÖZET:\s*(.+?)(?:\n\s*\n|$)(.*)", sonuc, re.S)
        ozet, ayrinti = (m.group(1).strip(), m.group(2).strip()) if m else (sonuc[:300], sonuc)
        url = _notion_sayfasi(f"{baslik} ({time.strftime('%d.%m')})", ayrinti or sonuc)
        log(f"iş bitti ({time.time() - t0:.0f} sn): {gorev} -> {url}")
        bitince(ozet, url, None)
    threading.Thread(target=is_, daemon=True, name="arka-plan-isi").start()
