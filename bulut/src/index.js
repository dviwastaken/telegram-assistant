/*
 * claude-hatirlatici — bilgisayar kapalıyken hatırlatmaları Telegram'a yazan Cloudflare Worker
 * =============================================================================================
 * Her dakika (cron) Notion "Hatırlatma" veritabanında durumu "Bekliyor" olan ve zamanı GECIKME_DK'dan fazla geçmiş
 * kayıtları bulur, sohbet botuyla kullanıcıya yazar. Bilgisayar açıkken hatırlatmayı zaten servis gönderir
 * (zamanı gelince, 20 sn içinde); Worker yalnızca servisin göndermediklerini yakalar.
 *
 * Çift gönderim olmasın diye her gönderimden önce "sahiplenme" yapılır: Hakem adlı Durable Object, aynı
 * "kayıt@zaman" anahtarını yalnızca bir kez verir. Servis de göndermeden önce POST /sahiplen ile sorar; Worker
 * önce davrandıysa servis o hatırlatmayı göndermez, yalnızca yerelde gönderildi sayar.
 *
 * Gönderimden sonra Notion güncellenir: tek seferlik kayıt "Gönderildi", tekrarlayan kayıt sıradaki zamanına ilerler
 * (src/tekrar.js, servisteki telefon/tekrar.py ile aynı kural). Brifingler atlanır: onları servisteki beyin yazar.
 *
 * Nabız: servis 5 dk'da bir POST /nabiz gönderir, temiz kapanırken POST /kapaniyor. Son nabız NABIZ_ESIK_DK'dan
 * eskiyse ve arada temiz kapanış yoksa cron bir kez "Masaüstü yanıt vermiyor" yazar; sonra nabız gelince bir kez
 * "yeniden açık" (kurallar src/hakem.js). Gece sessiz saatlerinde (SESSIZ_SAAT) ikisi de bildirimsiz gider.
 *
 * Bulutun hatırlatma mesajının altında hb:ok / hb:60 / hb:y düğmeleri var (veri: Notion kimliği, 32 hex). Düğmeleri
 * bilgisayardaki servis işler: bilgisayar kapalıyken basılan düğme, açılınca işlenir.
 *
 * Sırlar (wrangler secret put): NOTION_KEY, TELEGRAM_JETON (sohbet botu), TELEGRAM_SOHBET_ID, SERVIS_JETON.
 * Ayarlar (wrangler.jsonc vars): NOTION_DS, TZ_DAKIKA (180 = Türkiye), GECIKME_DK, EN_ESKI_SAAT, NABIZ_ESIK_DK,
 * NOTION_API, TELEGRAM_API (testte sahte sunucuya yönlendirmek için), SESSIZ_SAAT (yoksa "23:30-08:00").
 */

import { DurableObject } from "cloudflare:workers";
import { kod, sonraki } from "./tekrar.js";
import { acildiMetni, hatirlatmaDugmeleri, hatirlatmaMetni, sessizMi, simdiYerel, sustuMetni, yerelDakika,
         yereldenOfsetli } from "./zaman.js";
import * as kural from "./hakem.js";

const SAHIP_OMRU_MS = 3 * 86400 * 1000; // sahiplenme kaydı bu kadar tutulur (aynı zaman tekrar gelmez)
const TUR_SINIRI = 10;                  // bir taramada en çok bu kadar kayıt işlenir (ücretsiz planda çağrı başına
                                        // 50 alt istek var; her kayıt 3-4 istek), kalanı bir sonraki dakika
const ANAHTAR = /^[0-9a-f]{32}@\d{4}-\d\d-\d\dT\d\d:\d\d$/;

// ---------------------------------------------------------------- yardımcılar

function esit(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  const ea = new TextEncoder().encode(a);
  const eb = new TextEncoder().encode(b);
  if (ea.length !== eb.length) return false;
  let fark = 0;
  for (let i = 0; i < ea.length; i++) fark |= ea[i] ^ eb[i];
  return fark === 0;
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });
}

function metin(prop) {
  if (!prop) return "";
  const parca = prop[prop.type] || [];
  return Array.isArray(parca) ? parca.map((x) => x.plain_text || "").join("") : "";
}

// ---------------------------------------------------------------- Notion ve Telegram

async function notion(env, yontem, yol, govde) {
  const r = await fetch((env.NOTION_API || "https://api.notion.com/v1") + yol, {
    method: yontem,
    headers: {
      authorization: `Bearer ${env.NOTION_KEY}`,
      "notion-version": "2025-09-03",
      "content-type": "application/json",
    },
    body: govde ? JSON.stringify(govde) : undefined,
  });
  if (!r.ok) throw new Error(`Notion ${r.status}: ${(await r.text()).slice(0, 200)}`);
  return r.json();
}

async function bekleyenler(env) {
  const satirlar = [];
  let imlec;
  do {
    const d = await notion(env, "POST", `/data_sources/${env.NOTION_DS}/query`, {
      page_size: 100,
      filter: { property: "Durum", select: { equals: "Bekliyor" } },
      ...(imlec ? { start_cursor: imlec } : {}),
    });
    satirlar.push(...d.results);
    imlec = d.has_more ? d.next_cursor : null;
  } while (imlec);
  return satirlar;
}

async function telegram(env, yazi, dugmeler, sessiz = false) {
  const r = await fetch(`${env.TELEGRAM_API || "https://api.telegram.org"}/bot${env.TELEGRAM_JETON}/sendMessage`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      chat_id: env.TELEGRAM_SOHBET_ID, text: yazi, parse_mode: "HTML", link_preview_options: { is_disabled: true },
      ...(dugmeler ? { reply_markup: dugmeler } : {}),
      ...(sessiz ? { disable_notification: true } : {}),
    }),
  });
  if (!r.ok) throw new Error(`Telegram ${r.status}: ${(await r.text()).slice(0, 200)}`);
  return (await r.json()).result?.message_id;
}

// ---------------------------------------------------------------- cron

// Masaüstü sustu mu, yeniden açıldı mı: dakikada en çok bir mesaj. Gitmezse işaretlenmez, sonraki dakika yeniden.
// Gece (SESSIZ_SAAT, yerel saat) ikisi de sessiz gider: kapanış kaçarsa uyarı kullanıcıyı uyandırmasın.
async function nabizKontrol(env, hakem, ms, tz) {
  const esik = parseFloat(env.NABIZ_ESIK_DK || "15") * 60000;
  const karar = await hakem.nabizKarar(esik, ms);
  if (!karar) return null;
  await telegram(env, karar.tur === "sustu" ? sustuMetni(karar.ms, ms, tz) : acildiMetni(karar.sessiz_ms, ms, tz),
                 null, sessizMi(ms, tz, env.SESSIZ_SAAT));
  await hakem.nabizBildirildi(karar);
  return karar.tur;
}

export async function tara(env, hakem, ms = Date.now()) {
  const tz = parseInt(env.TZ_DAKIKA || "180", 10);
  const gecikme = parseInt(env.GECIKME_DK || "2", 10);
  const enEski = parseFloat(env.EN_ESKI_SAAT || "12");
  const simdi = simdiYerel(tz, ms);
  const ust = simdiYerel(tz, ms - gecikme * 60000).slice(0, 16);   // bundan eski olanlar servise bırakılmaz
  const alt = simdiYerel(tz, ms - enEski * 3600000).slice(0, 16);  // bundan eskiler bilgisayar açılınca toplu gelir
  const ozet = { zaman: new Date(ms).toISOString(), bekleyen: 0, vadesi: 0, gonderilen: 0, atlanan: 0, onarilan: 0,
                 hatalar: [] };
  let islenen = 0;
  try {
    ozet.nabiz = await nabizKontrol(env, hakem, ms, tz);  // Notion hatası nabız uyarısını engellemesin: önce bu
  } catch (e) {
    ozet.hatalar.push("nabız: " + String(e.message || e));
  }

  // Gönderilmiş sayılan kaydı Notion'da kapat: tek seferlik "Gönderildi", tekrarlayan sıradaki zamanına. Aynı
  // girdiyle hep aynı sonucu yazar (servis de aynı değeri hesaplar), iki kez çalışması zararsız.
  async function kapat(s, h) {
    const props = h.tekrar
      ? { Zaman: { date: { start: yereldenOfsetli(sonraki(h.zaman, h.tekrar, simdi), tz) } } }
      : { Durum: { select: { name: "Gönderildi" } } };
    await notion(env, "PATCH", `/pages/${s.id}`, { properties: props });
  }

  for (const s of await bekleyenler(env)) {
    ozet.bekleyen++;
    const p = s.properties || {};
    const h = {
      id: s.id.replace(/-/g, ""),
      zaman: yerelDakika(p.Zaman?.date?.start, tz),
      konu: metin(p.Konu),
      not: metin(p.Not),
      tur: p["Tür"]?.select?.name || "Hatırlatma",
      tekrar: kod(p.Tekrar?.select?.name),
    };
    if (!h.zaman || h.zaman > ust || h.tur === "Brifing") continue;
    if (islenen >= TUR_SINIRI) break;
    if (h.zaman < alt) {
      // çok eski: göndermeyiz (bilgisayar açılınca toplu gelir). Tekrarlayan seri ise burada takılıp kalmasın,
      // göndermeden sıradaki zamanına ilerlesin (bilgisayar kapalı kaldıkça sonraki tekrarlar gitsin).
      if (h.tekrar) {
        islenen++;
        try {
          await kapat(s, h);
          ozet.onarilan++;
        } catch (e) {
          ozet.hatalar.push("eski tekrar ilerletilemedi: " + String(e.message || e));
        }
      }
      continue;
    }
    ozet.vadesi++;
    islenen++;
    const anahtar = `${h.id}@${h.zaman}`;
    if (!(await hakem.sahiplen(anahtar, "bulut"))) {
      // Başkası sahiplenmiş ama kayıt hâlâ "Bekliyor". Teslim edildiği doğrulanmışsa (gönderen Notion'u kapatamadan
      // kapandı ya da Notion hata verdi) kaydı kapatırız; servis sahiplenip 5 dk'dır teslim edemediyse (çöktü,
      // kapandı, Telegram'a gidemedi) gönderimi devralırız. Teslim edilmemiş bir hatırlatma "Gönderildi" yapılmaz.
      const karar = await hakem.onarim(anahtar);
      if (karar === "kapat") {
        try {
          await kapat(s, h);
          ozet.onarilan++;
        } catch (e) {
          ozet.hatalar.push("onarım: " + String(e.message || e));
        }
      }
      if (karar !== "gonder") {
        ozet.atlanan++;
        continue;
      }
    }
    try {
      await telegram(env, hatirlatmaMetni(h, simdi), hatirlatmaDugmeleri(h.id));
    } catch (e) {
      await hakem.birak(anahtar, "bulut");  // gitmedi: bir sonraki dakika yeniden denensin
      ozet.hatalar.push(String(e.message || e));
      continue;
    }
    await hakem.teslim(anahtar, "bulut");
    ozet.gonderilen++;
    try {
      await kapat(s, h);
    } catch (e) {
      // gönderildi ama Notion yazılamadı: sahiplenme kaydı duruyor, aynı zaman ikinci kez gitmez; birkaç dakika
      // sonra yukarıdaki onarım kaydı kapatır
      ozet.hatalar.push("notion güncellenemedi: " + String(e.message || e));
    }
  }
  await hakem.kaydet(ozet);
  return ozet;
}

// ---------------------------------------------------------------- Durable Object

export class Hakem extends DurableObject {
  // Kurallar src/hakem.js'te (node testiyle sınanır). Durable Object içinde get ve put arasında başka istek
  // araya giremez (giriş kapısı), bu yüzden sahiplenme atomik.
  async sahiplen(anahtar, sahip) {
    const kazandi = await kural.sahiplen(this.ctx.storage, anahtar, sahip);
    if (kazandi && Math.random() < 0.05) await this.temizle();
    return kazandi;
  }

  async birak(anahtar, sahip) {
    return kural.birak(this.ctx.storage, anahtar, sahip);
  }

  async teslim(anahtar, sahip) {
    return kural.teslim(this.ctx.storage, anahtar, sahip);
  }

  async onarim(anahtar) {
    return kural.onarim(this.ctx.storage, anahtar);
  }

  async kim(anahtar) {
    return kural.kim(this.ctx.storage, anahtar);
  }

  async nabiz() {
    return kural.nabizAl(this.ctx.storage);
  }

  async kapaniyor() {
    return kural.kapaniyor(this.ctx.storage);
  }

  async nabizKarar(esikMs, ms) {
    return kural.nabizKarar(this.ctx.storage, esikMs, ms);
  }

  async nabizBildirildi(karar) {
    return kural.nabizBildirildi(this.ctx.storage, karar);
  }

  async temizle() {
    const sinir = Date.now() - SAHIP_OMRU_MS;
    const eski = [];
    for (const [k, v] of await this.ctx.storage.list({ prefix: "s:" })) if (v.ms < sinir) eski.push(k);
    for (let i = 0; i < eski.length; i += 128) await this.ctx.storage.delete(eski.slice(i, i + 128));
  }

  async kaydet(ozet) {
    await this.ctx.storage.put("son_tarama", ozet);
    if (ozet.gonderilen || ozet.hatalar.length || ozet.nabiz) await this.ctx.storage.put("son_olay", ozet);
  }

  async durum() {
    return {
      son_tarama: (await this.ctx.storage.get("son_tarama")) || null,
      son_olay: (await this.ctx.storage.get("son_olay")) || null,
      nabiz: await kural.nabizDurum(this.ctx.storage),
    };
  }
}

// ---------------------------------------------------------------- Worker

function hakemi(env) {
  return env.HAKEM.get(env.HAKEM.idFromName("tek"));
}

export default {
  async scheduled(olay, env, ctx) {
    ctx.waitUntil(tara(env, hakemi(env)).catch((e) => console.error("tarama hatası:", e.message || e)));
  },

  async fetch(istek, env) {
    const url = new URL(istek.url);
    if (url.pathname === "/") return new Response("claude-hatirlatici ok\n");
    if (!env.SERVIS_JETON) return json({ ok: false, hata: "yapilandirilmamis" }, 500);
    const m = /^Bearer\s+(.+)$/i.exec((istek.headers.get("authorization") || "").trim());
    if (!m || !esit(m[1], env.SERVIS_JETON)) return json({ ok: false, hata: "yetkisiz" }, 401);
    const hakem = hakemi(env);

    if (["/sahiplen", "/birak", "/teslim"].includes(url.pathname) && istek.method === "POST") {
      const ham = await istek.text();
      if (ham.length > 512) return json({ ok: false, hata: "govde-cok-buyuk" }, 400);
      let anahtar;
      try {
        anahtar = JSON.parse(ham).anahtar;
      } catch {
        return json({ ok: false, hata: "gecersiz-json" }, 400);
      }
      if (typeof anahtar !== "string" || !ANAHTAR.test(anahtar)) {
        return json({ ok: false, hata: "gecersiz-anahtar" }, 400);
      }
      if (url.pathname === "/teslim") {
        // servis hatırlatmayı Telegram'a gönderdi: bulut bu kaydı devralmasın, gerekirse yalnızca Notion'u kapatsın
        return json({ ok: true, teslim: await hakem.teslim(anahtar, "servis") });
      }
      if (url.pathname === "/birak") {
        // servis sahiplendiği hatırlatmayı Telegram'a gönderemedi: bilgisayar kapanırsa bulut göndersin
        return json({ ok: true, birakildi: await hakem.birak(anahtar, "servis") });
      }
      const kazandi = await hakem.sahiplen(anahtar, "servis");
      return json({ ok: true, kazandi, sahip: kazandi ? "servis" : (await hakem.kim(anahtar))?.sahip });
    }
    if (url.pathname === "/nabiz" && istek.method === "POST") {
      return json({ ok: true, acildi: await hakem.nabiz() });
    }
    if (url.pathname === "/kapaniyor" && istek.method === "POST") {
      await hakem.kapaniyor();  // bir sonraki nabıza kadar "sustu" uyarısı yok
      return json({ ok: true });
    }
    if (url.pathname === "/durum") return json({ ok: true, ...(await hakem.durum()) });
    return json({ ok: false, hata: "bulunamadi" }, 404);
  },
};

