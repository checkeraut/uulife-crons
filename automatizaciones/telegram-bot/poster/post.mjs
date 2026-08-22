#!/usr/bin/env node
// Poster del bot de Telegram de uu.life.
// Lee el calendario curado y postea el slot correspondiente (morning|evening) al canal.
// Sin dependencias: usa fetch nativo (Node 18+). Estado-less: el post se deriva de la fecha,
// así no hay que commitear un puntero ni mantener estado entre corridas.
//
// Uso:
//   SLOT=morning node post.mjs              -> postea el slot de la mañana de hoy
//   node post.mjs --slot evening            -> idem, slot tarde
//   node post.mjs --dry-run                 -> imprime el payload sin enviar
//   node post.mjs --slot morning --dry-run  -> combinable
//
// Env:
//   TELEGRAM_BOT_TOKEN  (o .secrets/telegram-bot.token en local)
//   TELEGRAM_CHAT_ID    (o config.channel.chat_id)
//   SLOT, DRY_RUN

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, '..');

const argv = process.argv.slice(2);
function argVal(name) {
  const i = argv.indexOf(`--${name}`);
  return i >= 0 ? argv[i + 1] : undefined;
}
const hasFlag = (name) => argv.includes(`--${name}`);

const config = JSON.parse(readFileSync(join(ROOT, 'config.json'), 'utf8'));
const calendar = JSON.parse(readFileSync(join(ROOT, 'contenido', 'calendario.json'), 'utf8'));

const SLOT = (argVal('slot') || process.env.SLOT || 'morning').toLowerCase();
const DRY_RUN = hasFlag('dry-run') || process.env.DRY_RUN === '1';

function readToken() {
  if (process.env.TELEGRAM_BOT_TOKEN) return process.env.TELEGRAM_BOT_TOKEN.trim();
  try {
    return readFileSync(join(ROOT, '..', '..', '.secrets', 'telegram-bot.token'), 'utf8').trim();
  } catch {
    return null;
  }
}
const TOKEN = readToken();
const CHAT_ID = (process.env.TELEGRAM_CHAT_ID || config.channel.chat_id || '').toString().trim();

// --- Selección del post de hoy (estado-less, derivado de la fecha) ---
const days = calendar.days || [];
if (days.length === 0) {
  console.error('Calendario vacío. Nada que postear.');
  process.exit(0);
}
const startMs = Date.parse(`${calendar.start_date}T00:00:00Z`);
const todayMs = Date.parse(new Date().toISOString().slice(0, 10) + 'T00:00:00Z');
let dayIndex = Math.floor((todayMs - startMs) / 86400000);
if (dayIndex < 0) dayIndex = 0;
if (dayIndex >= days.length) {
  if (calendar.loop_when_exhausted) dayIndex = dayIndex % days.length;
  else {
    console.log(`Calendario agotado (día ${dayIndex} de ${days.length}). Regenerá o activá loop_when_exhausted.`);
    process.exit(0);
  }
}
const day = days[dayIndex];
const post = day && day[SLOT];

if (!post) {
  console.log(`Día ${dayIndex + 1}, slot "${SLOT}": sin post programado. Saliendo limpio.`);
  process.exit(0);
}

// --- Helpers de armado ---
function resolveUrl(button) {
  if (!button) return null;
  let url = button.url
    || (button.product && `${config.store_base_url}/products/${button.product}`)
    || (button.path && `${config.store_base_url}${button.path}`);
  if (!url) return null;
  // UTMs para tracking (todo minúscula, como el resto del sistema)
  const u = new URL(url);
  u.searchParams.set('utm_source', config.utm.source);
  u.searchParams.set('utm_medium', config.utm.medium);
  const campaign = `${post.topic_id || 'general'}_${SLOT}`.toLowerCase();
  u.searchParams.set('utm_campaign', campaign);
  if (post.angle_id) u.searchParams.set('utm_content', post.angle_id.toLowerCase());
  return u.toString();
}

// Elimina guiones largos/medios (—, –). Los guiones de nombres (BPC-157) no llevan espacios y quedan intactos.
function noDashes(s) {
  if (!s) return s;
  return s
    .replace(/\s*[—–]\s*/g, ', ')
    .replace(/\s+,/g, ',')
    .replace(/,\s*,/g, ',')
    .replace(/,\s*([.!?:;])/g, '$1');
}

function buildText() {
  let text = noDashes(post.text || '');
  // Footer de compliance solo en posts con link comercial (mantiene los educativos limpios).
  if (post.button && post.footer !== false && config.compliance_footer) {
    text += `\n\n${config.compliance_footer}`;
  }
  return text;
}

function buildKeyboard() {
  if (!post.button) return undefined;
  const url = resolveUrl(post.button);
  if (!url) return undefined;
  return { inline_keyboard: [[{ text: noDashes(post.button.text), url }]] };
}

// --- Envío ---
async function tg(method, payload) {
  const res = await fetch(`https://api.telegram.org/bot${TOKEN}/${method}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!data.ok) throw new Error(`${method} falló: ${data.error_code} ${data.description}`);
  return data.result;
}

function buildPayload() {
  const base = { chat_id: CHAT_ID, parse_mode: config.parse_mode, disable_web_page_preview: config.disable_web_page_preview };
  if (post.type === 'poll' || post.poll) {
    return {
      method: 'sendPoll',
      payload: {
        chat_id: CHAT_ID,
        question: post.poll.question,
        options: post.poll.options,
        is_anonymous: post.poll.is_anonymous !== false,
        allows_multiple_answers: !!post.poll.allows_multiple_answers,
      },
    };
  }
  if (post.image) {
    return {
      method: 'sendPhoto',
      payload: { ...base, photo: post.image, caption: buildText(), reply_markup: buildKeyboard() },
    };
  }
  return {
    method: 'sendMessage',
    payload: { ...base, text: buildText(), reply_markup: buildKeyboard() },
  };
}

async function main() {
  const { method, payload } = buildPayload();
  const label = `Día ${dayIndex + 1}/${days.length} · ${SLOT} · ${post.topic_id || post.type || 'post'} · ${post.angle_id || ''}`;

  if (DRY_RUN) {
    console.log(`[DRY-RUN] ${label}`);
    console.log(`[DRY-RUN] ${method}:`);
    console.log(JSON.stringify(payload, null, 2));
    return;
  }

  if (!TOKEN) { console.error('Falta TELEGRAM_BOT_TOKEN.'); process.exit(1); }
  if (!CHAT_ID || CHAT_ID === 'PENDIENTE_CHAT_ID') { console.error('Falta TELEGRAM_CHAT_ID (o config.channel.chat_id).'); process.exit(1); }

  const result = await tg(method, payload);
  console.log(`✅ Posteado: ${label} (message_id ${result.message_id})`);
}

main().catch((err) => { console.error('❌', err.message); process.exit(1); });
