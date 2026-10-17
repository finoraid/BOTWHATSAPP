const crypto = require('crypto');
const { waitUntil } = require('@vercel/functions');

const GRAPH_VERSION = process.env.GRAPH_API_VERSION || 'v23.0';
const GRAPH_BASE = `https://graph.facebook.com/${GRAPH_VERSION}`;

function json(res, status, body) {
  res.statusCode = status;
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  res.end(JSON.stringify(body));
}

function safeEqual(a, b) {
  const aa = Buffer.from(String(a || ''));
  const bb = Buffer.from(String(b || ''));
  return aa.length === bb.length && crypto.timingSafeEqual(aa, bb);
}

async function readBody(req) {
  if (req.body && typeof req.body === 'object') return req.body;
  let raw = '';
  for await (const chunk of req) raw += chunk;
  try { return JSON.parse(raw || '{}'); } catch { return {}; }
}

async function waSend(to, payload) {
  const phoneId = process.env.WHATSAPP_PHONE_NUMBER_ID;
  const token = process.env.WHATSAPP_ACCESS_TOKEN;
  if (!phoneId || !token) throw new Error('Konfigurasi WhatsApp belum lengkap.');
  const r = await fetch(`${GRAPH_BASE}/${phoneId}/messages`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ messaging_product: 'whatsapp', recipient_type: 'individual', to, ...payload })
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data?.error?.message || `WhatsApp API error ${r.status}`);
  return data;
}

async function sendText(to, body) {
  return waSend(to, { type: 'text', text: { preview_url: false, body: String(body).slice(0, 4000) } });
}

async function gemini(parts) {
  const key = process.env.GEMINI_API_KEY;
  if (!key) throw new Error('GEMINI_API_KEY belum diatur.');
  const model = process.env.GEMINI_MODEL || 'gemini-2.5-flash';
  const r = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${encodeURIComponent(key)}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ contents: [{ role: 'user', parts }], generationConfig: { temperature: 0.6, maxOutputTokens: 900 } })
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data?.error?.message || `Gemini API error ${r.status}`);
  return (data.candidates?.[0]?.content?.parts || []).map(p => p.text || '').join('').trim() || 'Maaf, AI belum bisa menjawab saat ini.';
}

async function getWaMedia(mediaId) {
  const token = process.env.WHATSAPP_ACCESS_TOKEN;
  const r = await fetch(`${GRAPH_BASE}/${mediaId}`, { headers: { Authorization: `Bearer ${token}` } });
  const meta = await r.json().catch(() => ({}));
  if (!r.ok || !meta.url) throw new Error('Tidak bisa mengambil media WhatsApp.');
  const file = await fetch(meta.url, { headers: { Authorization: `Bearer ${token}` } });
  if (!file.ok) throw new Error('Gagal mengunduh gambar dari WhatsApp.');
  const bytes = Buffer.from(await file.arrayBuffer());
  if (bytes.length > 8 * 1024 * 1024) throw new Error('Gambar terlalu besar (maksimal 8 MB).');
  return { bytes, mime: meta.mime_type || 'image/jpeg' };
}

async function saveHistory(phone, userText, answer) {
  const url = process.env.SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!url || !key) return;
  const headers = { apikey: key, Authorization: `Bearer ${key}`, 'Content-Type': 'application/json', Prefer: 'resolution=merge-duplicates,return=minimal' };
  try {
    const get = await fetch(`${url}/rest/v1/chat_memory?phone=eq.${encodeURIComponent(phone)}&select=history`, { headers });
    const rows = await get.json();
    const old = Array.isArray(rows?.[0]?.history) ? rows[0].history : [];
    const history = [...old, { role: 'user', text: userText }, { role: 'assistant', text: answer }].slice(-12);
    await fetch(`${url}/rest/v1/chat_memory?on_conflict=phone`, { method: 'POST', headers, body: JSON.stringify({ phone, history, updated_at: new Date().toISOString() }) });
  } catch (e) { console.error('History save failed:', e.message); }
}

async function getHistory(phone) {
  const url = process.env.SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!url || !key) return [];
  try {
    const r = await fetch(`${url}/rest/v1/chat_memory?phone=eq.${encodeURIComponent(phone)}&select=history`, { headers: { apikey: key, Authorization: `Bearer ${key}` } });
    const rows = await r.json();
    return Array.isArray(rows?.[0]?.history) ? rows[0].history : [];
  } catch { return []; }
}

function isUrl(s) { try { const u = new URL(s); return ['http:', 'https:'].includes(u.protocol); } catch { return false; } }
function calc(text) {
  const expr = text.trim().replace(/,/g, '.');
  if (!expr || !/^[\d\s()+\-*/%.^]+$/.test(expr)) return null;
  const js = expr.replace(/\^/g, '**');
  try {
    const result = Function(`"use strict"; return (${js});`)();
    return typeof result === 'number' && Number.isFinite(result) ? String(result) : null;
  } catch { return null; }
}

async function sendToDownloader(path, body) {
  const base = process.env.DOWNLOADER_URL;
  const secret = process.env.DOWNLOADER_SHARED_SECRET;
  if (!base || !secret) throw new Error('DOWNLOADER_URL atau DOWNLOADER_SHARED_SECRET belum diatur.');
  const r = await fetch(`${base.replace(/\/$/, '')}${path}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Shared-Secret': secret },
    body: JSON.stringify(body), signal: AbortSignal.timeout(8000)
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || data.error || `Downloader error ${r.status}`);
  return data;
}

async function handleMessage(msg) {
  const from = msg.from;
  const text = (msg.text?.body || msg.image?.caption || '').trim();
  const lower = text.toLowerCase();

  if (msg.type === 'image') {
    const mediaId = msg.image?.id;
    if (!mediaId) return sendText(from, 'Gambar tidak terbaca. Coba kirim ulang sebagai foto.');
    if (lower === 'stiker' || lower === 'sticker') {
      const { bytes, mime } = await getWaMedia(mediaId);
      await sendText(from, '🎨 Sedang membuat stiker...');
      const result = await sendToDownloader('/sticker', { to: from, image_base64: bytes.toString('base64'), mime_type: mime });
      return sendText(from, result.message || 'Stiker selesai diproses.');
    }
    const { bytes, mime } = await getWaMedia(mediaId);
    const caption = msg.image?.caption || 'Baca gambar ini. Jika berisi soal matematika, jelaskan langkah dan jawabannya. Jika bukan, jelaskan isi gambar secara singkat.';
    const answer = await gemini([{ text: caption }, { inlineData: { mimeType: mime, data: bytes.toString('base64') } }]);
    return sendText(from, answer);
  }

  if (!text) return;
  if (/^(menu|help|bantuan|\/start)$/i.test(text)) {
    return sendText(from, '🤖 *BOT AI + DOWNLOADER*\n\nKirim pesan biasa untuk chat AI.\n\nPerintah:\n• menu — daftar fitur\n• translate id | hello world — terjemahkan\n• calc 12*(3+2) — kalkulator\n• tts Halo semuanya — ubah teks jadi suara\n• stiker — kirim bersama foto untuk membuat stiker\n• mp3 https://... — unduh audio\n• kirim tautan video untuk mengunduh\n• kirim foto soal matematika untuk dibaca AI\n\nCatatan: downloader bergantung dukungan situs dan batas ukuran WhatsApp.');
  }

  if (lower.startsWith('translate ')) {
    const raw = text.slice(9).trim();
    const split = raw.split('|');
    if (split.length < 2) return sendText(from, 'Format: translate bahasa tujuan | teks\nContoh: translate Inggris | selamat pagi');
    const answer = await gemini([{ text: `Terjemahkan teks berikut ke ${split[0].trim()}. Berikan hanya hasil terjemahan.\n\n${split.slice(1).join('|').trim()}` }]);
    return sendText(from, answer);
  }

  if (lower.startsWith('calc ')) {
    const result = calc(text.slice(5));
    if (result !== null) return sendText(from, `🧮 Hasil: ${result}`);
    return sendText(from, 'Format kalkulator tidak valid. Contoh: calc (12+8)*3');
  }

  if (lower.startsWith('tts ')) {
    const spoken = text.slice(4).trim();
    if (!spoken) return sendText(from, 'Contoh: tts Halo, apa kabar?');
    await sendText(from, '🔊 Sedang membuat audio...');
    const result = await sendToDownloader('/tts', { to: from, text: spoken.slice(0, 1000), language: 'id' });
    return sendText(from, result.message || 'Permintaan TTS dikirim.');
  }

  const mp3 = lower.startsWith('mp3 ');
  const url = mp3 ? text.slice(4).trim() : text;
  if (isUrl(url)) {
    await sendText(from, '📥 Permintaan diterima. Downloader gratis bisa perlu waktu untuk aktif. Tunggu hasilnya ya.');
    try {
      const result = await sendToDownloader('/jobs', { to: from, url, format: mp3 ? 'audio' : 'video' });
      if (result.message) return sendText(from, result.message);
      return;
    } catch (e) {
      return sendText(from, `Downloader belum merespons. Coba lagi sebentar ya. (${e.message})`);
    }
  }

  const history = await getHistory(from);
  const prompt = [
    { text: 'Kamu adalah asisten WhatsApp berbahasa Indonesia yang ramah, ringkas, dan akurat. Gunakan konteks percakapan berikut bila relevan. Jangan mengaku memiliki kemampuan yang tidak tersedia.\nRiwayat:\n' + history.map(x => `${x.role}: ${x.text}`).join('\n') + '\nPesan terbaru: ' + text }
  ];
  const answer = await gemini(prompt);
  await saveHistory(from, text, answer);
  return sendText(from, answer);
}

module.exports = async (req, res) => {
  if (req.method === 'GET') {
    const q = req.query || {};
    const verifyToken = process.env.WHATSAPP_VERIFY_TOKEN;
    if (q['hub.mode'] === 'subscribe' && safeEqual(q['hub.verify_token'], verifyToken)) {
      res.statusCode = 200; res.setHeader('Content-Type', 'text/plain'); return res.end(q['hub.challenge'] || '');
    }
    return json(res, 403, { error: 'Webhook verification failed' });
  }
  if (req.method !== 'POST') return json(res, 405, { error: 'Method not allowed' });

  const body = await readBody(req);
  // Respond quickly to Meta, then keep processing alive using Vercel waitUntil.
  json(res, 200, { received: true });
  waitUntil((async () => {
    try {
      const entries = body.entry || [];
      for (const entry of entries) {
        for (const change of entry.changes || []) {
          const value = change.value || {};
          for (const msg of value.messages || []) {
            try { await handleMessage(msg); }
            catch (e) {
              console.error('Message handler error:', e.message);
              try { await sendText(msg.from, `Maaf, fitur gagal diproses. ${e.message}`); } catch {}
            }
          }
        }
      }
    } catch (e) { console.error('Webhook error:', e.message); }
  })());
};
