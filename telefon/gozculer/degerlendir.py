"""Yeni mailleri Haiku ile değerlendirir: önem derecesi, tek cümlelik özet, varsa teslim/sınav tarihleri."""
import json
import re
import shutil
import subprocess

from .. import limit
from .. import eylemler
from ..ayarlar import KOK, log

CLAUDE = shutil.which("claude")

ISTEM = """Sen kullanıcının asistanısın. Kullanıcı üniversitede okuyan bir öğrenci ve yazılım geliştirici.
Aşağıdaki yeni e-postaları değerlendir. Bunlar onun okul ve kişisel posta kutularına geldi.

Şu an: {simdi}
kullanıcının takviminden (tekrar eklememek için):
{takvim}

Her mail için önem derecesi seç:
- "acil": kullanıcının BUGÜN ya da 24 saat içinde bir şey yapması gerekiyor. Örnekler: 24 saatten kısa süre kalmış bir teslim, öne alınmış ya da değişmiş bir sınav/teslim, bugünkü dersin iptali ya da yer değişikliği, hocanın hemen cevap beklediği bir mail, hesabıyla ilgili gerçek bir güvenlik uyarısı.
- "onemli": yeni ödev ya da duyuru, not açıklaması, hocadan kişisel mail, birkaç gün içindeki bir sınav.
- "bilgi": bilmesi iyi olur ama aciliyeti yok.
- "onemsiz": reklam, bülten, otomatik bildirim kalabalığı.

Mailde bir teslim tarihi, sınav ya da ders etkinliği varsa "teslimler" listesine ekle. Zamanı Türkiye saatiyle
"YYYY-MM-DDTHH:MM" biçiminde yaz. Saat belirtilmemişse teslimler için 23:59 kullan. Tarih net değilse ekleme.
Takvimde zaten aynı ders ve başlıkla olan bir kaydın tarihi değişmişse yeni tarihi yaz ve "degisti": true koy.
Mailde bir ödev için ek süre, geç gönderim hakkı, süre uzatma ya da "late submission" açılmışsa o ödevi yeni
(ek) son tarihle teslimler'e ekle ve "ek_sure": true koy. Böyle bir mail genelde en az "onemli" sayılır.
"ders" alanına ders kodunu yaz (ör. "BIL 207"); "baslik" alanına ders kodunu tekrar yazma.

Yalnızca şu JSON'u döndür, başka hiçbir şey yazma:
{{"mailler": [{{"id": "...", "onem": "acil|onemli|bilgi|onemsiz", "ozet": "Telegram'da bildirim olarak gidecek tek kısa cümle; 'bugün/yarın' yerine tarihi '21 Eki Çar 23:59' biçiminde yaz (ISO yazma); emoji, köşeli parantez, yıldız ve bağlantı biçimi kullanma",
  "teslimler": [{{"ders": "...", "baslik": "...", "zaman": "YYYY-MM-DDTHH:MM", "tur": "Teslim|Sınav|Etkinlik", "degisti": false, "ek_sure": false}}]}}]}}

E-postalar:
{mailler}"""


def degerlendir(mailler):
    """mailler: [{id, kaynak, kimden, konu, tarih, metin}] -> {id: {onem, ozet, teslimler}}"""
    if not mailler or limit.dolu_mu():
        return {}
    import datetime as dt
    metin = "\n\n".join(
        f"--- id: {m['id']} | kutu: {m['kaynak']} | kimden: {m['kimden']} | tarih: {m['tarih']}\n"
        f"konu: {m['konu']}\n{m['metin'][:3000]}" for m in mailler)
    istem = ISTEM.format(simdi=dt.datetime.now().strftime("%Y-%m-%d %H:%M %A"),
                         takvim=eylemler.baglam_ozeti(gun=30), mailler=metin)
    args = [CLAUDE, "-p", "--model", "haiku", "--output-format", "json", "--tools", "", "--strict-mcp-config",
            "--no-session-persistence", "--system-prompt", "Yalnızca istenen JSON'u üret."]
    try:
        p = subprocess.run(args, input=istem, capture_output=True, text=True, encoding="utf-8", cwd=KOK,
                           timeout=240, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        sonuc = json.loads(p.stdout).get("result", "")
        if limit.kontrol(sonuc, "mail değerlendirme"):
            return {}  # mailler görülmedi sayılır, limit açılınca yeniden değerlendirilir
        m = re.search(r"\{.*\}", sonuc, re.S)
        veri = json.loads(m.group(0)) if m else {}
    except Exception as e:
        log("mail değerlendirme hatası:", repr(e))
        return {}
    cikti = {}
    for x in veri.get("mailler", []):
        if x.get("id") is None:
            continue
        onem = x.get("onem") if x.get("onem") in ("acil", "onemli", "bilgi", "onemsiz") else "bilgi"
        cikti[str(x["id"])] = {"onem": onem, "ozet": str(x.get("ozet", ""))[:300],
                               "teslimler": [t for t in x.get("teslimler", []) if isinstance(t, dict)]}
    return cikti
