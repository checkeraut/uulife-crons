// Generador 100% automático de posts para el canal de uu.life.
// Flujo: elegir tema (producto real) + ángulo por fecha → scrapear la página →
// pedirle a Claude un post en el tono aprobado, fundamentado en los datos reales →
// pasar un filtro de compliance (bloquea términos peligrosos) → postear al canal.
//
// Sin dependencias (fetch nativo, Node 18+). Modelo: gpt-5.1 (OpenAI). Usa la base de
// conocimiento (conocimiento.json) para escribir copy específico y único, no hype genérico.
//
// Uso:
//   SLOT=morning node generar.mjs                 -> genera y postea
//   node generar.mjs --slot evening --dry-run     -> genera e imprime, no postea
//   node generar.mjs --show-prompt                -> arma el prompt y lo imprime (no llama a la API)
//
// Env: OPENAI_API_KEY (o .secrets/openai.key) · TELEGRAM_BOT_TOKEN · TELEGRAM_CHAT_ID

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { fetchProductSlugs, fetchProduct } from './scraper.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, '..');
const config = JSON.parse(readFileSync(join(ROOT, 'config.json'), 'utf8'));
const KNOWLEDGE = JSON.parse(readFileSync(join(__dirname, 'conocimiento.json'), 'utf8'));

const argv = process.argv.slice(2);
const argVal = (n) => { const i = argv.indexOf(`--${n}`); return i >= 0 ? argv[i + 1] : undefined; };
const hasFlag = (n) => argv.includes(`--${n}`);
const SLOT = (argVal('slot') || process.env.SLOT || 'morning').toLowerCase();
const DRY_RUN = hasFlag('dry-run') || process.env.DRY_RUN === '1';
const SHOW_PROMPT = hasFlag('show-prompt');
const MODEL = process.env.GEN_MODEL || 'gpt-5.1';

// --- Catálogo de ÁNGULOS (mismo cerebro que el newsletter, adaptado a Telegram) ---
const ANGLES = [
  { id: 'A01', name: 'Mecanismo revelado', guide: 'Explicá en lenguaje cotidiano qué hace y por qué importa. Una analogía simple. Nada de jerga.' },
  { id: 'A02', name: 'Mito derribado', guide: 'Rompé una creencia común de forma que haga DESEAR el producto (nunca dudar de él).' },
  { id: 'A03', name: 'Historia / transformación', guide: 'Una mini-escena cotidiana con la que el lector se identifique, y cómo cambia.' },
  { id: 'A08', name: 'Prueba social / tendencia', guide: 'Por qué todos en el mundo de la optimización están hablando de esto ahora.' },
  { id: 'A09', name: 'Curiosidad pura', guide: 'Un dato raro o inesperado que engancha y enseña algo.' },
  { id: 'A13', name: 'PAS', guide: 'Problema cotidiano → agitarlo un poco → la revelación simple que lo resuelve.' },
];

// Rotación estado-less por fecha: tema y ángulo cambian cada día y por slot.
function pickAssignment(slugs) {
  const start = Date.parse('2026-06-22T00:00:00Z');
  const today = Date.parse(new Date().toISOString().slice(0, 10) + 'T00:00:00Z');
  const day = Math.max(0, Math.floor((today - start) / 86400000));
  const slotOffset = SLOT === 'evening' ? 7 : 0; // mañana y tarde nunca coinciden
  const slug = slugs[(day * 2 + slotOffset) % slugs.length];
  const angle = ANGLES[(day + slotOffset) % ANGLES.length];
  return { slug, angle };
}

// --- Prompt ---
const SYSTEM = `Sos el copywriter del canal de Telegram de uu.life, un e-commerce de péptidos grado investigación. Escribís posts cortos pero potentes para gente que compra o quiere comprar.

OBJETIVO de cada post: que la persona ENTIENDA qué ES el péptido y cuáles son sus MEJORES cualidades (qué lo hace especial, qué hace bien, en qué le cambia el día a día), y que salga con ganas y confiando en uu.life como la fuente seria. Informás las buenas cualidades del compuesto de forma atractiva. Persuasivo, entretenido, informativo. Construí la marca uu.life.

ENFOQUE (regla dura, esto es lo que el cliente pidió corregir): hablá del péptido y de lo que HACE, directo y con confianza, como quien te cuenta lo mejor de algo que conoce bien. PROHIBIDO enmarcarlo como algo "para investigación / para la mesa de laboratorio / los investigadores lo estudian / para tu research / research angle / research model / bench list". El lector no es un científico en un banco de laboratorio: es alguien que se quiere optimizar y merece que le cuentes las mejores cualidades del compuesto sin rodeos clínicos ni disclaimers de "solo para investigación". Contá la buena noticia del péptido.

IDIOMA: el post se escribe SIEMPRE en INGLÉS (el canal es en inglés). NUNCA en español. (Estas instrucciones están en español, pero tu salida va en inglés.)

VOZ: directa, de igual a igual, con algo de swagger. Lenguaje cotidiano, CERO jerga técnica (nada de "GLP-1 agonist", "pentadecapeptide", "lysyl oxidase"). Traducí siempre la ciencia a una imagen simple. Audiencia tipo Huberman / Bryan Johnson / Tim Ferriss. NO bata de laboratorio, NO poesía de lujo.

PUNTUACIÓN (regla dura): PROHIBIDO usar guiones largos (— em dash) o medios (– en dash). NUNCA. Para separar ideas usá punto, coma o dos puntos. (Los guiones normales de nombres tipo BPC-157, CJC-1295, GLP-1 sí van, esos son parte del nombre.)

CALIDAD (esto es lo MÁS importante, define si el post sirve o es basura):
- REGLA DE ORO: construí todo el post alrededor de UN dato específico real de la materia prima que te doy. Ese dato es el corazón. Si el post no enseña nada concreto, está mal.
- Específico SIEMPRE gana. "toca 3 receptores en vez de 1", "forma vasos sanguíneos nuevos", "se acorta cada vez que la célula se divide" >>> "powerful", "cutting-edge", "supports your body", "peak performance". Los adjetivos vacíos están prohibidos.
- Gancho concreto o sorprendente atado a un dato. PROHIBIDO abrir con preguntas retóricas de relleno ("Ever wonder…", "Did you know…", "Ever feel like…", "Imagine if…", "What if…").
- BANLIST de frases-slop (jamás las uses): "talk of the town", "making waves", "the buzz", "buzzing", "secret weapon", "level up", "game-changer", "next level", "in the know", "biohackers get excited", "cut through the noise", "you can see and trust", "real talk", "real results", "you get certainty", "the name on everyone's lips", "take your trust seriously", "unlock", "elevate".
- PROHIBIDO ESCASEZ/URGENCIA DE VENTA (regla dura, esto es lo que rompió el cliente): NUNCA insinúes que el producto se agota, se vende rápido, queda poco stock, "sigue apareciendo en los carritos", la gente "lo vuelve a pedir", o hay que apurarse. Nada de "selling fast", "sells out", "moving fast", "the names that move fastest", "going quick", "won't last", "while supplies last", "before they're gone", "limited stock/batch/supply", "in high demand", "act now", "hurry", "don't miss out", "last chance", "stock up", "running low", "reorder", "keeps showing up in carts", "shopping cart". La escasez es exactamente lo que hace que el canal parezca spam y arriesga otro ban de Telegram. Popularidad SÍ ("everyone in the optimization world is talking about it", "the most talked-about name") — eso es prueba social, no urgencia. La diferencia: podés decir que es POPULAR, nunca que hay que COMPRAR YA porque se acaba.
- Frases cortas, ritmo, aire. Una idea por párrafo. Leelo en voz alta: si suena a folleto, reescribilo.
- Analogías concretas SÍ (una imagen simple y vívida ayuda). Generalidades sobre "wellness/optimization" NO.
- Voz de un par con criterio hablándole a otro que valora su tiempo, con algo de swagger. No repitas el nombre del péptido en cada frase. No cierres con moraleja cursi.

ESTRUCTURA (en inglés, formato HTML de Telegram):
- Primera línea: gancho fuerte en <b>negrita</b> (con un emoji al inicio).
- Cuerpo: 4-6 párrafos cortos. Abrí con una mini-historia o analogía, contá qué ES y qué hace BIEN en lenguaje humano, y tejé 2-3 datos concretos que muestren sus mejores cualidades. INFORMATIVO: que la persona salga sabiendo qué tiene de bueno este péptido, no solo con ganas de comprar.
- Cierre que construye uu.life: cada frasco viene con su informe de laboratorio independiente, atado a tu lote exacto. Prueba de calidad y de que estás comprando en serio (tejido natural, NO como disclaimer, NO como "para tu research").
- Largo: 180-260 palabras. Más informativo que un telegrama, pero sin una sola palabra de relleno.

COMPLIANCE (innegociable — si lo violás, el bot se cae):
- PROHIBIDO: "weight loss", "lose weight", "fat loss", "weight reduction", "Ozempic", "Wegovy", claims médicos (cure/treat/prevent/diagnose), dosis (mg/mcg/iu), "miracle".
- GLP-1 / metabólicos (reta, tirzepatide, etc.): enmarcá como regulación del apetito y metabolismo en lenguaje de estilo de vida (la saciedad llega antes, los antojos dejan de mandar). NUNCA prometas bajar de peso.
- La página puede usar lenguaje prohibido ("major weight reduction"): NO lo copies. Reencuadralo siempre al marco permitido.
- Contá las cualidades y beneficios del péptido en lenguaje de estilo de vida, PERO nunca prometas curar/tratar/prevenir una enfermedad ni des dosis. Beneficio y cualidad SÍ; claim médico de enfermedad NO. (Ej: "supports recovery" / "fullness comes sooner" SÍ; "cures/treats X" NO.)

NO uses el footer "for research only" ni ningún disclaimer de research. El compliance va tejido natural.

EJEMPLO del tono/idioma/calidad correctos (referencia, NO lo copies). Fijate: cuenta qué ES y qué tiene de BUENO, cero "research", cero escasez:
🧬 <b>Retatrutide: the metabolic name everyone in the optimization world keeps bringing up</b>

You know that feeling when hunger runs the show? When your appetite makes the calls and you're just along for the ride?

Reta is built around that exact problem. It works with the same signals your body already uses to say "I've had enough." For a lot of people those signals are quiet, or out of tune. Reta turns the volume back up. Fullness shows up sooner, and cravings stop running your day.

What makes it stand out is reach. Most metabolic molecules touch one switch. Reta works across three, so more of the system is in play at once. That's how it became the most talked-about name in the space almost overnight.

A molecule this talked-about comes with a catch: the market floods with fakes and "just trust me" vials. That's the whole reason uu.life exists. Every vial ships with an independent lab report, matched to the exact batch in your hands. No guessing, no blind faith.

This is what a serious source looks like.`;

function buildUserMessage(product, angle, know) {
  const material = know ? [
    `Qué es (criollo): ${know.plain}`,
    'Datos ESPECÍFICOS reales (tejé 2-3 en la narrativa para que sea informativo, no como lista cruda):',
    ...know.specifics.map((s) => `• ${s}`),
    know.hook ? `Idea de gancho posible (no obligatoria): ${know.hook}` : null,
  ].filter(Boolean).join('\n') : '(sin material extra: usá solo lo de la página, pero seguí siendo específico)';

  const page = [
    product.price ? `Precio: ${product.currency || ''} ${product.price}` : null,
    product.inStock != null ? `Stock: ${product.inStock ? 'disponible' : 'agotado'}` : null,
    product.category ? `Categoría del sitio: ${product.category}` : null,
    product.description ? `Descripción del sitio (NO copiar; la web usa lenguaje prohibido): "${product.description}"` : null,
  ].filter(Boolean).join('\n');

  return `Escribí UN post para el canal sobre ${product.name}, usando el ángulo indicado.

MATERIA PRIMA (real y específica, está en español; el post va en INGLÉS y en lenguaje del lector):
${material}

DATOS DE LA PÁGINA:
${page}

ÁNGULO: ${angle.name}. ${angle.guide}

Construí el post alrededor de una idea principal y tejé 2-3 datos específicos de la materia prima (dentro de la narrativa, no como lista). Que sea informativo y único, no genérico.

Respondé SOLO con un objeto JSON válido (sin markdown, sin texto extra) con estas dos claves:
- "text": el post en INGLÉS, formato HTML de Telegram (usá <b>, <i>). Sin guiones largos (— –).
- "button_text": texto corto del botón (ej. "See ${product.name} →", "Learn more →").`;
}

// --- Filtro de compliance ---
const BANNED = [
  /\b(weight\s*loss|lose\s*weight|fat\s*loss|weight\s*reduction)\b/i,
  /\b(ozempic|wegovy|mounjaro)\b/i,
  /\b(cure[sd]?|treat(s|ed|ing|ment)?|prevent(s|ed|ing)?|diagnos)\w*/i,
  /\b\d+\s?(mg|mcg|iu)\b/i,
  /\bmiracle\b/i,
  // Escasez / urgencia de venta: lo que el cliente marcó como "un desastre".
  // Popularidad ("talked-about", "everyone is talking") queda permitida a propósito.
  /\bsell(s|ing)?\s+(out\s+)?fast\b/i,
  /\bsell(s|ing)?\s+out\b/i,
  /\bmov(e|es|ing)\s+(the\s+)?fast(est)?\b/i,
  /\bmove\s+fast\b/i,
  /\bgoing\s+(fast|quick(ly)?)\b/i,
  /\b(won['’]?t|will\s+not)\s+last\b/i,
  /\bwhile\s+(supplies|stocks)\s+last\b/i,
  /\bbefore\s+(they['’]?re|it['’]?s|these\s+are)\s+gone\b/i,
  /\blimited\s+(stock|supply|batch(es)?|quantit(y|ies)|run|drop)\b/i,
  /\b(high|huge|massive|surging)\s+demand\b/i,
  /\bin\s+demand\b/i,
  /\bact\s+(now|fast)\b/i,
  /\b(hurry|don['’]?t\s+miss|last\s+chance|stock\s+up|running\s+low|snap\s+(it|them)\s+up|grab\s+(it|them|yours)\s+(fast|now|quick))\b/i,
  /\bshopping\s+carts?\b/i,
  /\bin\s+(the\s+)?(same\s+)?carts?\b/i,
  /\bre-?order(s|ing|ed)?\b/i,
  /\bkeeps?\s+(coming\s+back|showing\s+up|reappearing)\b/i,
];
function lint(text) {
  for (const re of BANNED) { const m = text.match(re); if (m) return m[0]; }
  return null;
}

// Backstop: elimina guiones largos/medios (—, –) pase lo que pase. Los guiones
// normales de nombres (BPC-157, GLP-1) quedan intactos porque no llevan espacios.
function noDashes(s) {
  if (!s) return s;
  return s
    .replace(/\s*[—–]\s*/g, ', ')
    .replace(/\s+,/g, ',')
    .replace(/,\s*,/g, ',')
    .replace(/,\s*([.!?:;])/g, '$1');
}

// --- Claude (raw HTTP, sin SDK para mantener cero dependencias) ---
function readOpenAIKey() {
  if (process.env.OPENAI_API_KEY) return process.env.OPENAI_API_KEY.trim();
  try { return readFileSync(join(ROOT, '..', '..', '.secrets', 'openai.key'), 'utf8').trim(); } catch { return null; }
}

async function callOpenAI(key, userMessage) {
  // Los modelos gpt-5* son de razonamiento: usan max_completion_tokens y no aceptan temperature.
  const reasoning = /^gpt-5/.test(MODEL);
  const body = {
    model: MODEL,
    response_format: { type: 'json_object' },
    messages: [
      { role: 'system', content: SYSTEM },
      { role: 'user', content: userMessage },
    ],
  };
  if (reasoning) body.max_completion_tokens = 4000; // deja lugar al razonamiento + el post
  else { body.max_tokens = 2000; body.temperature = 0.7; }

  const res = await fetch('https://api.openai.com/v1/chat/completions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${key}` },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (data.error) throw new Error(`OpenAI: ${data.error.type || ''} ${data.error.message || ''}`.trim());
  const content = data.choices?.[0]?.message?.content;
  if (!content) throw new Error('Respuesta sin contenido');
  return JSON.parse(content);
}

// --- Telegram ---
function readTgToken() {
  if (process.env.TELEGRAM_BOT_TOKEN) return process.env.TELEGRAM_BOT_TOKEN.trim();
  try { return readFileSync(join(ROOT, '..', '..', '.secrets', 'telegram-bot.token'), 'utf8').trim(); } catch { return null; }
}
function buildButtonUrl(slug, angleId, topicId) {
  const u = new URL(`${config.store_base_url}/products/${slug}`);
  u.searchParams.set('utm_source', config.utm.source);
  u.searchParams.set('utm_medium', config.utm.medium);
  u.searchParams.set('utm_campaign', `${topicId}_${SLOT}`.toLowerCase());
  u.searchParams.set('utm_content', `${angleId}_ai`.toLowerCase());
  return u.toString();
}
async function sendToTelegram(post) {
  const token = readTgToken();
  const chatId = (process.env.TELEGRAM_CHAT_ID || config.channel.chat_id || '').toString().trim();
  if (!token || !chatId) throw new Error('Falta TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID');
  const res = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      chat_id: chatId,
      parse_mode: config.parse_mode,
      disable_web_page_preview: config.disable_web_page_preview,
      text: post.text,
      reply_markup: { inline_keyboard: [[{ text: post.button.text, url: post.button.url }]] },
    }),
  });
  const data = await res.json();
  if (!data.ok) throw new Error(`Telegram: ${data.error_code} ${data.description}`);
  return data.result;
}

async function main() {
  const slugs = await fetchProductSlugs(config.store_base_url);
  const { slug, angle } = pickAssignment(slugs);
  const product = await fetchProduct(slug, config.store_base_url);
  const know = KNOWLEDGE[slug] || null;
  const userMessage = buildUserMessage(product, angle, know);

  if (SHOW_PROMPT) {
    console.log(`# Asignación: ${slug} · ${angle.id} ${angle.name} · slot ${SLOT}\n`);
    console.log('=== SYSTEM ===\n' + SYSTEM + '\n');
    console.log('=== USER ===\n' + userMessage);
    return;
  }

  const key = readOpenAIKey();
  if (!key) { console.error('Falta OPENAI_API_KEY (o .secrets/openai.key). El generador necesita la key de OpenAI.'); process.exit(1); }

  // Generar con reintentos si el filtro de compliance lo rechaza.
  let gen, bad;
  for (let attempt = 1; attempt <= 3; attempt++) {
    gen = await callOpenAI(key, userMessage + (attempt > 1 ? `\n\n(Reintento: el anterior usó el término prohibido "${bad}". Reescribilo sin eso.)` : ''));
    bad = lint(gen.text);
    if (!bad) break;
    console.error(`⚠️ Intento ${attempt}: término bloqueado "${bad}", regenerando…`);
    gen = null;
  }
  if (!gen) { console.error('❌ No se pudo generar un post compliance-safe en 3 intentos.'); process.exit(1); }

  const post = {
    topic_id: slug,
    angle_id: angle.id,
    text: noDashes(gen.text),
    button: { text: noDashes(gen.button_text), url: buildButtonUrl(slug, angle.id, slug) },
  };

  const label = `${SLOT} · ${slug} · ${angle.id} ${angle.name}`;
  if (DRY_RUN) {
    console.log(`[DRY-RUN] ${label}\n`);
    console.log(post.text);
    console.log(`\n[ ${post.button.text} ] → ${post.button.url}`);
    return;
  }
  const result = await sendToTelegram(post);
  console.log(`✅ Generado y posteado: ${label} (message_id ${result.message_id})`);
}

main().catch((e) => { console.error('❌', e.message); process.exit(1); });
