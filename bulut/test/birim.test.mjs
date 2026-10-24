// node --test bulut/test — Workers ortamı gerektirmeyen parçalar. Tekrar örnekleri servisle ortak.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { kod, sonraki } from "../src/tekrar.js";
import { duzMetin, hatirlatmaMetni, tarihMetni, yerelDakika, yereldenOfsetli } from "../src/zaman.js";

const ornekler = JSON.parse(readFileSync(new URL("../../test/tekrar_ornekleri.json", import.meta.url), "utf8"));

test("sıradaki tekrar servisle aynı (ortak örnekler)", () => {
  for (const o of ornekler) assert.equal(sonraki(o.zaman, o.tekrar, o.simdi), o.sonraki, JSON.stringify(o));
});

test("tekrar adları", () => {
  assert.equal(kod("Hafta içi"), "haftaici");
  assert.equal(kod("Yok"), null);
  assert.equal(kod(undefined), null);
  assert.equal(kod("Yılda bir"), null); // tanınmayan: tek seferlik (servis de öyle sayar)
});

test("Notion tarihi yerel dakikaya (servisteki _yerel ile aynı)", () => {
  assert.equal(yerelDakika("2026-10-16T17:40:00.000+03:00", 180), "2026-10-16T17:40");
  assert.equal(yerelDakika("2026-10-16T14:40:00.000Z", 180), "2026-10-16T17:40");
  assert.equal(yerelDakika("2026-10-16T22:30:00.000Z", 180), "2026-10-17T01:30");
  assert.equal(yerelDakika("2026-10-16T17:40:00.000", 180), "2026-10-16T17:40"); // ofsetsiz: zaten yerel
  assert.equal(yerelDakika("2026-10-17", 180), "2026-10-17T00:00");
  assert.equal(yerelDakika(null, 180), null);
  assert.equal(yereldenOfsetli("2026-10-17T08:00", 180), "2026-10-17T08:00:00+03:00");
});

test("mesaj metni kaçışlı, gecikme ve tekrar notuyla", () => {
  const m = hatirlatmaMetni({ zaman: "2026-10-16T08:00", konu: "<b>ilaç</b> & su", not: "", tur: "Hatırlatma",
                              tekrar: "gunluk" }, "2026-10-16T08:12:00");
  assert.match(m, /^<b>Hatırlatma<\/b> · bugün 08:00 içindi, 12 dk geç\n/);
  assert.match(m, /&lt;b&gt;ilaç&lt;\/b&gt; &amp; su/);
  assert.match(m, /\nher gün tekrarlanıyor\n/);
  assert.match(m, /<i>\(bilgisayar kapalıyken buluttan gönderildi; düğmeler ve mesajlarınız/);
  assert.doesNotMatch(m, /[⏰🔁☁️✅]/u);
  assert.match(hatirlatmaMetni({ zaman: "2026-10-16T08:00", konu: "x" }, "2026-10-16T09:17:00"),
               /^<b>Hatırlatma<\/b> · bugün 08:00 içindi, 1 saat 17 dk geç\n/);
  assert.match(hatirlatmaMetni({ zaman: "2026-10-16T08:00", konu: "x" }, "2026-10-16T08:03:00"),
               /^<b>Hatırlatma<\/b> · bugün 08:00\n/);                // GEC_DK içinde: "içindi" yok
  const dun = hatirlatmaMetni({ zaman: "2026-10-15T23:50", konu: "x", tur: "Acil" }, "2026-10-16T01:00:00");
  assert.match(dun, /^🚨 <b>Hatırlatma<\/b> · dün 23:50 içindi, bilgisayar kapalıydı\n/);
  const eski = hatirlatmaMetni({ zaman: "2026-10-14T20:00", konu: "x" }, "2026-10-16T07:00:00");
  assert.match(eski, /^<b>Hatırlatma<\/b> · 14 Eki Çar 20:00 içindi, bilgisayar kapalıydı\n/);
});

test("gece yarısı kuralı: gün değişse bile en çok 60 dk geç kalan 'N dk geç' yazar", () => {
  const gec = hatirlatmaMetni({ zaman: "2026-10-17T23:58", konu: "x" }, "2026-10-18T00:04:00");
  assert.match(gec, /^<b>Hatırlatma<\/b> · dün 23:58 içindi, 6 dk geç\n/);
  assert.match(hatirlatmaMetni({ zaman: "2026-10-17T23:50", konu: "x" }, "2026-10-18T00:50:00"),
               /· dün 23:50 içindi, 1 saat geç\n/);                                // tam 60 dk: hâlâ gecikme
  assert.match(hatirlatmaMetni({ zaman: "2026-10-17T23:50", konu: "x" }, "2026-10-18T00:51:00"),
               /· dün 23:50 içindi, bilgisayar kapalıydı\n/);                       // 61 dk
  assert.match(hatirlatmaMetni({ zaman: "2026-10-18T08:00", konu: "x" }, "2026-10-18T11:00:00"),
               /· bugün 08:00 içindi, 3 saat geç\n/);                               // aynı gün kuralı değişmedi
});

test("duzMetin servisteki telegram.duz_metin ile aynı", () => {
  assert.equal(duzMetin("[tıkla](https://kotu.example/x) **acil** `kod`"), "tıkla (https://kotu.example/x) acil kod");
  assert.equal(duzMetin("# Başlık\n  ## iki\nsatır [x]"), "Başlık\n  iki\nsatır x");
  assert.equal(duzMetin(undefined), "");
});

test("çok satırlı not her satır ayrı italik; konu ve not düz metin", () => {
  const m = hatirlatmaMetni({ zaman: "2026-10-17T20:00", konu: "[a](https://kotu.example/x) *b*",
                              not: "ilk\n\n  ikinci <i>  \r\nüçüncü" }, "2026-10-17T20:00:00");
  assert.match(m, /\na \(https:\/\/kotu\.example\/x\) b\n<i>ilk<\/i>\n<i>ikinci &lt;i&gt;<\/i>\n<i>üçüncü<\/i>\n/);
  assert.doesNotMatch(m, /<a |\[|\]/);
  assert.doesNotMatch(hatirlatmaMetni({ zaman: "2026-10-17T20:00", konu: "x", not: " \n " }, "2026-10-17T20:00:00"),
                      /<i>\n/);                                                    // boş satırlar atılır
});

test("servisle ortak örnekler: aynı html (test/hatirlatma_ornekleri.json)", () => {
  const altbilgi = "\n\n<i>(bilgisayar kapalıyken buluttan gönderildi; düğmeler ve mesajlarınız bilgisayar açılınca işlenir)</i>";
  const ornekler = JSON.parse(readFileSync(new URL("../../test/hatirlatma_ornekleri.json", import.meta.url), "utf8"));
  assert.ok(ornekler.length >= 10);
  for (const o of ornekler) {
    const kayit = { ...o.kayit, tur: o.kayit.tur === "acil" ? "Acil" : "Hatırlatma" };   // bulutta Notion'daki ad
    const js = hatirlatmaMetni(kayit, o.simdi);
    assert.ok(js.endsWith(altbilgi), o.ad);
    assert.equal(js.slice(0, -altbilgi.length), o.html, o.ad);
  }
});

test("tarihMetni servisteki eylemler.tarih_metni ile aynı", () => {
  const simdi = "2026-10-17T10:41:00";                                  // Cumartesi
  assert.equal(tarihMetni("2026-10-17T20:00", simdi), "bugün 20:00");
  assert.equal(tarihMetni("2026-10-18T23:59", simdi), "yarın 23:59");
  assert.equal(tarihMetni("2026-10-16T14:00", simdi), "dün 14:00");
  assert.equal(tarihMetni("2026-10-21T23:59", simdi), "21 Eki Çar 23:59");
  assert.equal(tarihMetni("2026-10-05T08:30", simdi), "5 Eki Pzt 08:30");
  assert.equal(tarihMetni("2027-01-04T09:00", simdi), "4 Oca 2027 Pzt 09:00");
  assert.equal(tarihMetni("2026-10-21T23:59", simdi, false), "21 Eki Çar");
  assert.equal(tarihMetni("2026-10-18T09:00", simdi, false), "yarın");
  assert.equal(tarihMetni("2027-01-01T00:00", "2026-12-31T23:30"), "yarın 00:00");   // yıl dönümünde de gün farkı
  assert.equal(tarihMetni("2026-11-01T10:00", "2026-10-31T10:00"), "yarın 10:00");   // ay dönümü
});

test("Hakem kuralları: sahiplenme, bırakma, teslim, onarım", async () => {
  const { sahiplen, birak, teslim, onarim, kim, ONARIM_MS } = await import("../src/hakem.js");
  const m = new Map();
  const depo = { get: async (k) => m.get(k), put: async (k, v) => void m.set(k, v), delete: async (k) => m.delete(k) };
  const A = "a".repeat(32) + "@2026-10-16T09:00";
  const t0 = 1_000_000;
  assert.equal(await sahiplen(depo, A, "servis", t0), true);
  assert.equal(await sahiplen(depo, A, "servis", t0), true);   // servis kendi sahiplendiğini yeniden alır
  assert.equal(await sahiplen(depo, A, "bulut", t0), false);
  assert.equal(await onarim(depo, A, t0 + 60_000), "bekle");   // erken
  // servis 5 dk'dır teslim edemedi (kapandı): bulut devralır ve gönderir, "Gönderildi" diye kapatmaz
  assert.equal(await onarim(depo, A, t0 + ONARIM_MS + 1), "gonder");
  assert.equal((await kim(depo, A)).sahip, "bulut");
  assert.equal(await sahiplen(depo, A, "servis", t0), false);  // artık bulutun
  assert.equal(await birak(depo, A, "servis"), false);         // başkasınınkini bırakamaz
  assert.equal(await teslim(depo, A, "bulut"), true);
  assert.equal(await birak(depo, A, "bulut"), false);          // teslim edilmiş bırakılmaz
  const B = "b".repeat(32) + "@2026-10-16T09:00";
  await sahiplen(depo, B, "servis", t0);
  await teslim(depo, B, "servis");
  assert.equal(await sahiplen(depo, B, "servis", t0), false);  // teslim edildiyse servis de yeniden alamaz
  assert.equal(await onarim(depo, B, t0 + ONARIM_MS + 1), "kapat"); // teslim edilmiş: yalnızca Notion kapanır
  const C = "c".repeat(32) + "@2026-10-16T09:00";
  await sahiplen(depo, C, "servis", t0);
  assert.equal(await birak(depo, C, "servis"), true);
  assert.equal(await sahiplen(depo, C, "bulut", t0), true);    // bırakılanı bulut alır
});

test("nabız kuralları: sustu bir kez, temiz kapanışta yok, açılınca bir kez", async () => {
  const { nabizAl, kapaniyor, nabizKarar, nabizBildirildi } = await import("../src/hakem.js");
  const m = new Map();
  const depo = { get: async (k) => m.get(k), put: async (k, v) => void m.set(k, v), delete: async (k) => m.delete(k) };
  const ESIK = 15 * 60_000;
  const t0 = 10_000_000;
  assert.equal(await nabizKarar(depo, ESIK, t0 + 99 * ESIK), null);          // hiç nabız yok: uyarı yok
  await kapaniyor(depo, t0);
  assert.equal(await nabizKarar(depo, ESIK, t0 + 99 * ESIK), null);          // nabızsız kapanış da uyarmaz
  assert.equal(await nabizAl(depo, t0), false);
  assert.equal(await nabizKarar(depo, ESIK, t0 + ESIK), null);               // eşikte değil, eşikten eski
  const k = await nabizKarar(depo, ESIK, t0 + ESIK + 1);
  assert.deepEqual(k, { tur: "sustu", ms: t0 });
  await nabizBildirildi(depo, k);
  assert.equal(await nabizKarar(depo, ESIK, t0 + 3 * ESIK), null);           // ikinci kez uyarmaz
  assert.equal(await nabizAl(depo, t0 + 3 * ESIK), true);                    // uyarıdan sonra ilk nabız
  const a = await nabizKarar(depo, ESIK, t0 + 3 * ESIK + 1);
  assert.deepEqual(a, { tur: "acildi", ms: t0 + 3 * ESIK, sessiz_ms: t0 });
  await nabizBildirildi(depo, a);
  assert.equal(await nabizKarar(depo, ESIK, t0 + 3 * ESIK + 2), null);       // "açık" bir kez
  // temiz kapanış: bir sonraki nabıza kadar uyarı yok
  const t1 = t0 + 4 * ESIK;
  await nabizAl(depo, t1);
  await kapaniyor(depo, t1 + 1000);
  assert.equal(await nabizKarar(depo, ESIK, t1 + 10 * ESIK), null);
  await nabizAl(depo, t1 + 10 * ESIK);                                       // açıldı: uyarı gitmediği için "açık" da yok
  assert.equal(await nabizKarar(depo, ESIK, t1 + 10 * ESIK + 1), null);
  assert.equal((await nabizKarar(depo, ESIK, t1 + 12 * ESIK)).tur, "sustu"); // kapanıştan sonraki nabız yeniden izlenir
});

test("nabız: karar ile gönderim arasında nabız gelirse 'açık' da yazılır", async () => {
  const { nabizAl, nabizKarar, nabizBildirildi } = await import("../src/hakem.js");
  const m = new Map();
  const depo = { get: async (k) => m.get(k), put: async (k, v) => void m.set(k, v), delete: async (k) => m.delete(k) };
  await nabizAl(depo, 1000);
  const k = await nabizKarar(depo, 1000, 5000);
  assert.equal(await nabizAl(depo, 5500), false);                            // uyarı henüz işaretlenmemişti
  await nabizBildirildi(depo, k);                                             // ama Telegram'a gitti
  assert.deepEqual(await nabizKarar(depo, 1000, 5600), { tur: "acildi", ms: 5500, sessiz_ms: 1000 });
});

test("nabız mesajları ve bulut mesajı düğmeleri", async () => {
  const { sustuMetni, acildiMetni, hatirlatmaDugmeleri } = await import("../src/zaman.js");
  const son = Date.parse("2026-10-16T11:05:00Z");                             // 14:05 Türkiye
  assert.equal(sustuMetni(son, son + 15 * 60_000 + 30_000, 180),
               "⚠️ Masaüstü 15 dakikadır yanıt vermiyor (son nabız bugün 14:05). Elektrik, çökme, donma ya da uyku "
               + "olabilir; bilgisayar kapalıyken hatırlatmaları bulut gönderiyor.");
  assert.match(sustuMetni(Date.parse("2026-10-15T20:50:00Z"), son, 180), /son nabız dün 23:50\)/);
  assert.match(sustuMetni(Date.parse("2026-10-13T20:50:00Z"), son, 180), /son nabız 13 Eki Sal 23:50\)/);
  assert.equal(acildiMetni(son, son + 3_600_000, 180),
               "Masaüstü yeniden açık (son nabız bugün 14:05 idi). Hatırlatmaları yine bilgisayar gönderiyor.");
  assert.match(acildiMetni(null, son, 180), /^Masaüstü yeniden açık\./);
  const id = "0123456789abcdef".repeat(2);
  const d = hatirlatmaDugmeleri(id);
  assert.deepEqual(d.inline_keyboard[0].map((x) => x.callback_data), [`hb:ok:${id}`, `hb:60:${id}`, `hb:y:${id}`]);
  assert.deepEqual(d.inline_keyboard[0].map((x) => x.text), ["Tamam", "1 saat ertele", "Yarına ertele"]);
  for (const x of d.inline_keyboard[0]) assert.ok(new TextEncoder().encode(x.callback_data).length <= 64);
});

test("nabız uyarıları gece sessiz (23:30-08:00, Türkiye saati)", async () => {
  const { sessizMi, SESSIZ_SAAT } = await import("../src/zaman.js");
  const tr = (saat) => Date.parse(`2026-10-16T${saat}:00+03:00`);
  assert.equal(SESSIZ_SAAT, "23:30-08:00");
  for (const s of ["23:30", "23:59", "00:00", "03:15", "07:59"]) assert.equal(sessizMi(tr(s), 180), true, s);
  for (const s of ["08:00", "12:00", "17:40", "23:29"]) assert.equal(sessizMi(tr(s), 180), false, s);
  // UTC 21:00 = TR 00:00: saat dilimi TZ_DAKIKA'dan
  assert.equal(sessizMi(Date.parse("2026-10-16T21:00:00Z"), 180), true);
  assert.equal(sessizMi(Date.parse("2026-10-16T21:00:00Z"), 0), false);
  // ayarlanabilir; gece yarısını aşmayan aralık; bozuk değer varsayılana döner
  assert.equal(sessizMi(tr("13:00"), 180, "12:00-14:00"), true);
  assert.equal(sessizMi(tr("14:00"), 180, "12:00-14:00"), false);
  assert.equal(sessizMi(tr("02:00"), 180, "saçma"), true);
  assert.equal(sessizMi(tr("12:00"), 180, undefined), false);
});
