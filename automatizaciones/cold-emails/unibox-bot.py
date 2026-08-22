#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bot de Unibox — contesta las respuestas de las campañas cold (EU) por API.

Corre cada 20 min en GitHub Actions (.github/workflows/unibox-bot.yml).

Flujo por cada respuesta nueva (ue_type=2) en las campañas cold:
  1. STOP/baja/enojo → blocklist en Instantly + NO se responde. (stop_on_reply ya frenó la secuencia.)
  2. Pregunta SEGURA (precio, envío, certificados/COA, cupón, cómo comprar) → responde solo,
     corto, voz uu.life, en el idioma del mensaje (EN/DE), desde la misma casilla.
  3. Dosis/uso/temas médicos → respuesta fija de compliance (research use only) — NUNCA aconseja.
  4. Pagos, reclamos, pedidos existentes, o cualquier cosa dudosa → NO responde; queda en el
     Unibox para humano y se registra en unibox-escalaciones.md (commiteado por el workflow).

Estado en unibox-bot-estado.json (ids de mensajes ya procesados — sin PII).
Guardarraíles duros en el prompt: cero claims médicos, cero dosis, GLP nunca weight-loss,
si no está 100% seguro → ESCALATE. El texto generado pasa un filtro de palabras prohibidas.
"""
import json, os, re, sys, urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = Path(__file__).resolve().parent
ESTADO = HERE / "unibox-bot-estado.json"
ESCALADAS = HERE / "unibox-escalaciones.md"


def escalar(ts, email, cid, casilla, cuerpo):
    """Registra una respuesta que el bot no supo contestar.

    Va a la BASE, no a un archivo: el .md guardaba email, nombre y el texto del
    mensaje del lead — PII pura — y el bot lo commiteaba al repo. Con el repo de
    crons siendo PUBLICO eso seria una filtracion (y con leads alemanes, GDPR).
    Sin DATABASE_URL (uso local) cae al archivo de siempre, que esta gitignoreado.
    """
    if os.environ.get("DATABASE_URL"):
        import estado
        cx = estado.conectar()
        estado.init_escalaciones(cx)
        estado.registrar_escalacion(cx, email, cid, casilla, cuerpo)
        cx.close()
        print(f"  escalada guardada en la base (sin tocar git): {email}")
        return
    with open(ESCALADAS, "a", encoding="utf-8") as f:
        f.write(f"\n## {ts} — {email} (campaña {cid[:8]}, casilla {casilla})\n> "
                + (cuerpo or "")[:400].replace("\n", "\n> ")
                + "\n\n**→ SIN RESPONDER — revisar en el Unibox.**\n")

KEY = os.environ.get("INSTANTLY_API_KEY") or (HERE.parents[1] / ".secrets" / "instantly-api.key").read_text().strip()
OPENAI = os.environ.get("OPENAI_API_KEY", "")

CAMPANAS = json.loads((HERE / "campanas.json").read_text())
COLD_IDS = [v for k, v in CAMPANAS.items() if k in ("p1eu", "p2euuk", "p2eude") and v]

# Base de conocimiento — SOLO lo confirmado. Lo que no está acá, se escala.
KB = """
- uu.life is an EU online store for research peptides. Every vial ships with a published,
  lot-matched certificate of analysis (COA): https://uu.life/lab-testing-coas
- Retatrutide vial: EUR 89. Store: https://uu.life
- Shipping: dispatched within 24h from the EU store.
- Discount codes: HELLO10 (10% first order) for welcome contacts; TEXT15 only if they mention it.
- All products are for research use only.
"""

PROHIBIDAS = re.compile(r"\b(dose|dosage|dosing|inject|mcg|iu\b|cure|treat|prevent|weight.?loss|ozempic|prescription)\b", re.I)


def api(method, url, body=None):
    req = urllib.request.Request(url, method=method,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json",
                 "User-Agent": "uulife-unibox-bot/1.0"},
        data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read()) if r.length != 0 else {}


def llm(system, user):
    req = urllib.request.Request("https://api.openai.com/v1/chat/completions", method="POST",
        headers={"Authorization": f"Bearer {OPENAI}", "Content-Type": "application/json",
                 "User-Agent": "uulife-unibox-bot/1.0"},
        data=json.dumps({"model": "gpt-4o-mini", "temperature": 0.3,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}).encode())
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"].strip()


CLASIFICADOR = """You classify replies to a cold email from uu.life (EU research peptide store).
Reply ONLY with valid JSON: {"tipo": "...", "idioma": "en|de"}
tipo must be one of:
- "stop": wants to unsubscribe/stop/angry/hostile (any language)
- "segura": a question the store can answer factually: price, shipping time, certificates/COA,
  discount code, how/where to buy, what the store sells, link request
- "medica": asks about usage, dosing, effects on the body, medical advice, comparisons to medicines
- "escalar": ANYTHING else — payments, existing orders, complaints, bulk/wholesale, unclear, out-of-office, bounce-like
When in doubt: "escalar"."""

RESPONDEDOR = """You write a SHORT reply (2-4 sentences max) from uu.life to a potential customer.
Voice: helpful peer, plain language, no hype, no emojis. Sign off exactly as: "— uu.life".
Answer ONLY from this knowledge base (if the answer is not here, you must not invent it):
{kb}
HARD RULES (regulated merchant — breaking any = failure):
- NEVER medical claims (cure/treat/prevent), NEVER dosing/usage advice, NEVER "weight loss",
  never compare to medicines. Products are for research use only.
- No urgency, no pressure. No invented facts, prices, or policies.
- Write in the same language as the customer ({idioma}).
Return ONLY the reply text, no subject."""

COMPLIANCE_EN = ("Thanks for reaching out. Our products are supplied strictly for research use, "
                 "so we can't advise on usage. What we can vouch for: every vial ships with an "
                 "independent lab certificate you can read before buying: https://uu.life/lab-testing-coas — uu.life")
COMPLIANCE_DE = ("Danke für deine Nachricht. Unsere Produkte sind ausschließlich für Forschungszwecke, "
                 "daher können wir nicht zur Verwendung beraten. Wofür wir geradestehen: jedes Vial kommt "
                 "mit unabhängigem Laborzertifikat, einsehbar vor dem Kauf: https://uu.life/lab-testing-coas — uu.life")


def main():
    estado = json.loads(ESTADO.read_text()) if ESTADO.exists() else {"procesados": []}
    procesados = set(estado["procesados"])
    ahora = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    resumen = {"respondidas": 0, "stops": 0, "compliance": 0, "escaladas": 0}

    for cid in COLD_IDS:
        try:
            data = api("GET", f"https://api.instantly.ai/api/v2/emails?campaign_id={cid}&limit=50")
        except Exception as e:
            print(f"campaña {cid}: error listando ({e})"); continue
        for m in data.get("items", []):
            if m.get("ue_type") != 2:  # solo respuestas entrantes
                continue
            mid = m.get("id")
            if not mid or mid in procesados:
                continue
            de = m.get("from_address_email", "")
            casilla = m.get("eaccount") or m.get("to_address_email_list", "")
            cuerpo = ((m.get("body") or {}).get("text") or "")[:1500]
            if not cuerpo.strip():
                procesados.add(mid); continue

            try:
                cls = json.loads(re.search(r"\{.*\}", llm(CLASIFICADOR, cuerpo), re.S).group(0))
            except Exception as e:
                print(f"  clasificador fallo ({e}) → escalar"); cls = {"tipo": "escalar", "idioma": "en"}
            tipo, idioma = cls.get("tipo", "escalar"), cls.get("idioma", "en")

            if tipo == "stop":
                try:
                    api("POST", "https://api.instantly.ai/api/v2/block-lists-entries", {"bl_value": de})
                    resumen["stops"] += 1
                    print(f"  STOP → blocklist: {de}")
                except Exception as e:
                    print(f"  blocklist fallo: {e}")
                procesados.add(mid); continue

            if tipo == "medica":
                texto = COMPLIANCE_DE if idioma == "de" else COMPLIANCE_EN
            elif tipo == "segura":
                try:
                    texto = llm(RESPONDEDOR.format(kb=KB, idioma=idioma), cuerpo)
                except Exception as e:
                    print(f"  respondedor fallo ({e}) → escalar"); tipo = "escalar"; texto = None
                if texto and PROHIBIDAS.search(texto):
                    print("  ⚠ texto generado con palabra prohibida → escalar"); tipo = "escalar"; texto = None
            else:
                texto = None

            if tipo == "escalar" or texto is None:
                resumen["escaladas"] += 1
                escalar(ahora, de, cid, casilla, cuerpo)
                procesados.add(mid); continue

            try:
                api("POST", "https://api.instantly.ai/api/v2/emails/reply", {
                    "reply_to_uuid": mid,
                    "eaccount": casilla,
                    "subject": m.get("subject") or "re:",
                    "body": {"text": texto},
                })
                resumen["respondidas" if tipo == "segura" else "compliance"] += 1
                print(f"  respondido ({tipo}) a {de}")
            except Exception as e:
                resumen["escaladas"] += 1
                print(f"  envio fallo ({e}) → queda para humano")
            procesados.add(mid)

    estado["procesados"] = list(procesados)[-2000:]
    ESTADO.write_text(json.dumps(estado, indent=1))
    print(f"[{ahora}] respondidas: {resumen['respondidas']} | compliance: {resumen['compliance']} | "
          f"stops→blocklist: {resumen['stops']} | escaladas a humano: {resumen['escaladas']}")


if __name__ == "__main__":
    main()
