# Bot de Telegram uu.life — plan y estrategia

> Canal de Telegram que postea info de péptidos **2x/semana**: atractivo, educativo, persuasivo,
> ordenado y **corto**. Parte del funnel de Telegram (llega gente que compró o quiere comprar).
> Reusa el motor de contenido del newsletter (mismos ángulos/temas/compliance), adaptado a Telegram.

## 0. Estado
- ✅ Bot `@uulife1_bot` creado, admin del canal "Uu life research" (`@uuliferesearch`), test end-to-end OK.
- ✅ Poster + workflow + config + lote curado de 14 días (EN) listos. Primer post (reta) ya publicado.
- ⏳ Falta: cargar los 2 secrets en GitHub para que el cron vuele solo (ver [README](./README.md)).

## 1. ⚠️ Aprendizaje crítico — Telegram banea este vertical (igual que el email)
El **primer bot (`@uulife_bot`) fue baneado** por Telegram: *"illegal goods - drugs", a partir de
reportes de usuarios confirmados por moderadores*. **No es un problema de copy: es estructural**, el
mismo muro que los ESP de email (ver [SOP newsletter §2](../newsletter/SOP-NEWSLETTER.md)). Un bot
**público** que **vende péptidos con botones de compra** es whack-a-mole: lo reportan y cae.

**Mitigaciones que aguantan (orden de impacto):**
1. **Canal privado / invite-only** alimentado solo por el funnel → casi nadie random para reportar.
   *(Recomendado, pendiente de decisión del cliente — hoy el canal es público.)*
2. **Educación-first, no vidriera.** Contenido = research/mecanismo/longevidad/lifestyle. CTAs
   **suaves** (`See the research →`, `View the COAs →`), **no** "Comprá X". La venta vive en el
   **sitio + email flows**, no en Telegram.
3. **Perfil del bot limpio** (sin emojis/imágenes que griten "droga").
4. Si recae: apelar (`/appealbot<id>`), bot nuevo **solo** junto al reposicionamiento (idéntico = recae).

## 2. Arquitectura (elegida: GitHub Actions cron)
```
.github/workflows/telegram-bot.yml   cron 2x/día (UTC) → corre el poster
automatizaciones/telegram-bot/
  config.json            canal, chat_id, base_url del store, footer, UTMs, horarios
  contenido/calendario.json   lote curado (días × {morning, evening})
  poster/post.mjs        lee el calendario, elige post por fecha, manda por Bot API
/.secrets/telegram-bot.token   token (gitignored)
```
- **Estado-less:** el post se deriva de la fecha (`día = hoy − start_date`), no hay puntero que
  commitear. Cero infra, gratis, versionado en el repo. Cuando salga el dominio, se cambia
  `store_base_url` en un solo lugar.
- **Por qué no n8n / SaaS:** n8n necesita instancia viva; el engine del SaaS es más potente pero
  pesado. GitHub Actions = lo más simple y robusto para arrancar. Migrable después.

## 3. Formato Telegram (≠ email: corto y escaneable)
Molde por post: hook de 1 línea (emoji) → 2-4 frases, una idea, lenguaje cotidiano → 1 dato/prueba
(COA/research) → botón suave. **Ritmo:** ☀️ mañana = valor/educación · 🌙 tarde (opcional) = liviano
(tip, poll, calidad/COA, social). Engagement: polls, mini-series, botones a la PDP. Disclaimer RUO
fijado (pinned) + footer corto en posts con link.

## 4. Motor de contenido (100% automático, fundamentado en la página)
- **Generador** (`generador/generar.mjs`): por fecha elige tema (producto real) + ángulo →
  **scrapea la página** (`generador/scraper.mjs`, lee JSON-LD: nombre, precio €, categoría, stock,
  descripción) → le pide a **Claude (`claude-opus-4-8`)** un post en el tono aprobado, fundamentado
  en esos datos → **filtro de compliance** (bloquea weight loss / cure / dosis y regenera) → postea.
- **Tono aprobado por Juanma:** menos técnico, benefit-first ("qué hace / para qué sirve / en qué
  cambia"), construye la marca uu.life, sin footer "research only". Ver [[telegram-bot-uulife]] (memoria).
- **Ojo:** la web usa lenguaje prohibido ("major weight reduction", categoría "Weight Management").
  El generador toma los **hechos** pero los **reencuadra** al marco compliant; el filtro es la red de seguridad.
- **Híbrido:** el lote curado (`contenido/calendario.json`) queda como **fallback** del cron si la
  generación falla (sin key / API caída) → nunca un slot vacío.
- **Tracking:** cada link estampa `utm_source=telegram&utm_medium=channel&utm_campaign={topic}_{slot}&utm_content={angle}`
  → se mide qué tema/ángulo convierte, mismo cerebro que el email.

## 5. Compliance (innegociable — ver [brief](../../copy/00-brief-marca.md))
Sin claims médicos · sin dosis · framing research · **GLP-1 = regulación metabólica/apetito**, nunca
"weight loss"/"Ozempic" · footer RUO. El lote curado ya cumple; el engine debe respetarlo sí o sí.

## 6. Roadmap
1. ✅ MVP: poster + workflow + lote curado + test end-to-end (post de reta vivo).
2. ✅ Motor 100% automático: scraper de la página + generador con Claude + filtro de compliance.
3. ⏳ **Cargar `ANTHROPIC_API_KEY`** (+ `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) en GitHub → cron solo.
4. ⏳ **Decidir canal privado** (recomendado) + pinear disclaimer.
5. Reporte de qué tema/ángulo convierte (reusa el tracking del newsletter).
6. Lead magnet en el funnel que empuje al canal privado (no scrapeable).
