# Bot de Telegram uu.life — operación

Bot que postea **2x/semana** al canal de Telegram (educación-first, CTAs suaves al sitio).
Parte del funnel de Telegram: la venta vive en el sitio + email flows; Telegram = confianza / top-of-mind.

- **Bot:** `@uulife1_bot` · **Canal:** "Uu life research" (`@uuliferesearch`) · `chat_id` en [`config.json`](./config.json)
- **Token:** en `/.secrets/telegram-bot.token` (gitignored) — **nunca al repo**.
- **Motor:** GitHub Actions cron ([`.github/workflows/telegram-bot.yml`](../../.github/workflows/telegram-bot.yml)), 2x/día.

## Cómo funciona
1. El cron dispara 2 veces/semana (martes y viernes, ver horarios en `config.json`).
2. **Modo automático (default):** [`generador/generar.mjs`](./generador/generar.mjs) elige tema (producto real) + ángulo del día, **scrapea la página** ([`generador/scraper.mjs`](./generador/scraper.mjs)) para fundamentar el post en datos reales (qué es, precio, categoría, stock), le pide a **Claude** un post en el tono aprobado, lo pasa por un **filtro de compliance** (bloquea weight loss / cure / dosis…) y lo postea.
3. **Fallback:** si la generación falla (sin key, API caída), el cron cae a [`poster/post.mjs`](./poster/post.mjs), que postea del lote curado [`contenido/calendario.json`](./contenido/calendario.json) — así nunca queda un slot vacío.

## Generador automático
```bash
cd automatizaciones/telegram-bot/generador
node scraper.mjs                       # lista productos del sitio
node scraper.mjs retatrutide           # datos de un producto
node generar.mjs --show-prompt         # arma el prompt (no llama a la API)
SLOT=morning node generar.mjs --dry-run  # genera con la IA e imprime, no postea
SLOT=morning node generar.mjs          # genera y postea (necesita ANTHROPIC_API_KEY)
```
La key de OpenAI va en `/.secrets/openai.key` (local) o `OPENAI_API_KEY` (env / GitHub secret).

## Probar sin enviar (dry-run)
```bash
cd automatizaciones/telegram-bot/poster
SLOT=morning node post.mjs --dry-run    # imprime el payload, no envía
SLOT=evening node post.mjs --dry-run
```

## Postear a mano
```bash
cd automatizaciones/telegram-bot/poster
SLOT=morning node post.mjs              # usa token de .secrets + chat_id de config
```

## Activar el cron (lo que falta para que vuele solo)
En GitHub → repo `checkeraut/uu.life` → **Settings → Secrets and variables → Actions → New repository secret**:
- `TELEGRAM_BOT_TOKEN` = el token de `@uulife1_bot`
- `TELEGRAM_CHAT_ID` = `-1004331437254`
- `OPENAI_API_KEY` = tu key de OpenAI (para la generación automática con gpt-5.1; sin esto, el cron usa el lote curado)

Con eso + el workflow pusheado, el cron postea solo. Para disparar manual: pestaña **Actions → uu.life Telegram bot → Run workflow** (elegís slot y dry-run).

## Editar / agregar contenido
- Cada día en `calendario.json` tiene `morning` y (opcional) `evening`.
- Tipos de post: mensaje con `text` + `button` · `poll` · `image` (foto con caption).
- Botones: `{"text": "...", "product": "bpc-157"}` (→ `/products/bpc-157`) o `{"text":"...","path":"/store"}` o `{"text":"...","url":"..."}`. Las UTMs se agregan solas.
- Texto en **HTML de Telegram** (`<b>`, `<i>`, `<a>`).
- `footer: false` en un post saca el disclaimer RUO (por default se agrega en los que tienen botón).

## Compliance (igual que el resto de uu.life)
- Sin claims médicos (trata/cura/previene), sin dosis.
- GLP-1 (reta/tirzepatide) = **regulación metabólica/apetito en research**, NUNCA "weight loss"/"Ozempic".
- Framing research ("studied for", "research looks at"), footer RUO.
- Detalle: [brief de marca](../../copy/00-brief-marca.md) · [PLAN.md](./PLAN.md).
