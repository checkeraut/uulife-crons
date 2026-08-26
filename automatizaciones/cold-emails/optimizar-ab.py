#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Optimizador perpetuo del A/B de asuntos de TODAS las campañas cold.

Corre a diario en GitHub Actions. Para cada campaña:
  1. Lee las métricas por variante del paso 1 (Instantly analytics/steps).
  2. Compara los DELTAS desde la última rotación (baseline por campaña).
  3. Si ambas variantes juntaron >= MIN_ENVIOS nuevos y la perdedora abre menos del
     UMBRAL de la ganadora -> reemplaza el asunto perdedor por el próximo retador.
  4. Cola vacía -> pide retadores nuevos a OpenAI con el ángulo y el idioma de ESA
     campaña (compliance dura en el prompt); si falla, avisa y sigue sin rotar.
  5. Escribe reporte-ab.md con las tres campañas, acción e historial.

POR QUE TRES Y NO UNA (2026-08-24)
Antes solo optimizaba P1-EU. Las otras dos nunca testearon nada — y P2-EU-UK es
justamente la de peor rendimiento (1,43% de clicks contra 4,33% de P1-EU) y la que
mas volumen se lleva por tamaño de lista. Testear solo donde ya funciona bien es
donde menos sirve.

CADA CAMPAÑA TIENE SU PROPIO ANGULO. P1-EU le escribe a compradores previos sobre
su reposición; las P2 le escriben a gente sin historial, con un cupón de bienvenida.
Un retador generado para una no sirve para la otra, así que el prompt cambia por
campaña — y la DE se genera en alemán.

Alertas en el reporte (NO frena nada solo): rebote >3% o bajas >1%.
Local: python optimizar-ab.py  (lee la key de ../../.secrets/instantly-api.key)
"""
import json, os, sys, urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = Path(__file__).resolve().parent
CAMPANAS = json.loads((HERE / "campanas.json").read_text())
ESTADO = HERE / "optimizador-estado.json"
REPORTE = HERE / "reporte-ab.md"

MIN_ENVIOS = 40        # por variante, desde el ultimo baseline
UMBRAL = 0.6           # perdedora abre < 60% de la ganadora -> rotar

KEY = os.environ.get("INSTANTLY_API_KEY") or (HERE.parents[1] / ".secrets" / "instantly-api.key").read_text().strip()
OPENAI = os.environ.get("OPENAI_API_KEY", "")

# Cada campaña con su angulo, su idioma y su cola inicial de retadores.
OBJETIVOS = {
    "p1eu": {
        "nombre": "P1-EU — GLP Restock (UK/DE)",
        "idioma": "English",
        "angulo": ("restock emails to PAST CUSTOMERS of a research peptide store. They "
                   "already bought once. Angles that work: their own timeline, their next "
                   "order, lab certificates (COAs), quality standards."),
        "cola": ["worth 60 seconds before you reorder", "the certificate question",
                 "read this before you restock", "one thing to check before reordering"],
    },
    "p2euuk": {
        "nombre": "P2-EU-UK — Welcome 10",
        "idioma": "English",
        "angulo": ("first-contact emails to UK shoppers who have NEVER bought from us. "
                   "There is a 10% welcome code. They do not know the brand yet, so the "
                   "subject has to earn the open on its own. Angles that work: independent "
                   "lab reports nobody else shows, what to ask a vendor before buying, "
                   "the welcome code."),
        "cola": ["the question most vendors dodge", "before you buy from anyone",
                 "your 10% is still open", "what the lab report actually says"],
    },
    "p2eude": {
        "nombre": "P2-EU-DE — Welcome 10",
        "idioma": "German",
        "angulo": ("first-contact emails to GERMAN shoppers who have never bought from us. "
                   "There is a 10% welcome code. Write the subject lines IN GERMAN, natural "
                   "and direct — do not translate English idioms literally. Angles that work: "
                   "independent lab reports, what to check before buying, the welcome code."),
        "cola": ["die Frage, die kaum ein Anbieter beantwortet", "bevor du irgendwo bestellst",
                 "dein 10% Code ist noch offen", "was im Laborbericht wirklich steht"],
    },
}


def api(method, url, body=None):
    req = urllib.request.Request(url, method=method,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json",
                 "User-Agent": "uulife-optimizador/2.0"},
        data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or "{}")


def generar_retadores(cfg, actuales, historial):
    """Retadores nuevos via OpenAI, con el angulo y el idioma de ESTA campaña."""
    if not OPENAI:
        return []
    usados = [h.get("puso") for h in historial] + [h.get("saco") for h in historial] + actuales
    prompt = (
        f"Generate 5 cold email subject lines in {cfg['idioma']} for {cfg['angulo']}\n"
        "Style: lowercase, 3-6 words, curiosity-driven, human (like a colleague wrote it), "
        "NO emoji, NO exclamation marks.\n"
        "HARD RULES (regulated merchant): never mention drug/peptide/compound names, never "
        "medical claims (cure/treat/prevent), no doses, no 'weight loss' or comparisons to "
        "prescription drugs, no urgency/scarcity ('last chance', 'act now', 'running out'), "
        "no spam words (free, deal, discount).\n"
        f"Already used (avoid similar): {usados[:20]}\n"
        'Return ONLY a JSON array of 5 strings.'
    )
    try:
        req = urllib.request.Request("https://api.openai.com/v1/chat/completions", method="POST",
            headers={"Authorization": f"Bearer {OPENAI}", "Content-Type": "application/json",
                     "User-Agent": "uulife-optimizador/2.0"},
            data=json.dumps({"model": "gpt-4o-mini", "temperature": 0.9,
                "messages": [{"role": "user", "content": prompt}]}).encode())
        with urllib.request.urlopen(req, timeout=60) as r:
            txt = json.loads(r.read())["choices"][0]["message"]["content"].strip()
        txt = txt[txt.find("["): txt.rfind("]") + 1]
        return [s.strip() for s in json.loads(txt) if isinstance(s, str) and 2 < len(s) < 70][:5]
    except Exception as e:
        print(f"  generador fallo: {e}")
        return []


def _delta(acum, base, i):
    """Envios/aperturas desde el baseline. El estado viejo guardaba las claves como
    string y el nuevo como int, asi que se aceptan las dos."""
    b = base.get(str(i), base.get(i)) if isinstance(base, dict) else None
    b = b if isinstance(b, dict) else {}
    return {"sent": acum[i]["sent"] - b.get("sent", 0),
            "opens": acum[i]["opens"] - b.get("opens", 0)}


def optimizar(seg, cfg, est, ahora):
    """Corre el A/B de una campaña. Devuelve (accion, alertas, datos para el reporte)."""
    cid = CAMPANAS.get(seg)
    if not cid:
        return "sin campaña en campanas.json", [], None
    camp = api("GET", f"https://api.instantly.ai/api/v2/campaigns/{cid}")
    variants = camp["sequences"][0]["steps"][0]["variants"]
    subjects = [v.get("subject", "") for v in variants]

    steps = api("GET", f"https://api.instantly.ai/api/v2/campaigns/analytics/steps?campaign_id={cid}")
    acum = {}
    for s in steps:
        if s.get("step") == "0":
            acum[int(s["variant"])] = {"sent": s.get("sent", 0), "opens": s.get("unique_opened", 0)}
    for i in range(len(variants)):
        acum.setdefault(i, {"sent": 0, "opens": 0})

    glob = api("GET", f"https://api.instantly.ai/api/v2/campaigns/analytics?id={cid}")
    glob = glob[0] if isinstance(glob, list) and glob else {}
    enviados = glob.get("emails_sent_count", 0) or 1

    alertas = []
    br = glob.get("bounced_count", 0) * 100 / enviados
    ur = glob.get("unsubscribed_count", 0) * 100 / enviados
    if br > 3: alertas.append(f"⚠ {cfg['nombre']}: REBOTE {br:.1f}% (>3%) — revisar lista/entrega")
    if ur > 1: alertas.append(f"⚠ {cfg['nombre']}: BAJAS {ur:.1f}% (>1%) — revisar copy/frecuencia")

    if len(variants) < 2:
        return (f"una sola variante — no hay A/B que correr. Agregar una segunda "
                f"variante al paso 1 para que empiece a testear."), alertas, (subjects, acum, glob)

    if est.get("baseline") is None:
        est["baseline"] = {"ts": ahora, "datos": acum, "subjects": subjects}
        return "baseline inicial establecido", alertas, (subjects, acum, glob)

    base = est["baseline"]["datos"]
    delta = {i: _delta(acum, base, i) for i in acum}
    if not all(d["sent"] >= MIN_ENVIOS for d in delta.values()):
        faltan = {chr(65 + i): max(0, MIN_ENVIOS - d["sent"]) for i, d in delta.items()}
        return f"juntando datos (faltan envios por variante: {faltan})", alertas, (subjects, acum, glob)

    tasas = {i: (d["opens"] / d["sent"]) if d["sent"] else 0 for i, d in delta.items()}
    gana, pierde = max(tasas, key=tasas.get), min(tasas, key=tasas.get)
    if gana == pierde or not tasas[gana] or tasas[pierde] >= UMBRAL * tasas[gana]:
        return (f"A/B sigue juntando datos (tasas: "
                f"{', '.join(f'{chr(65+i)}={tasas[i]*100:.0f}%' for i in sorted(tasas))})"), alertas, (subjects, acum, glob)

    prefijo = ""
    if not est.get("cola"):
        est["cola"] = generar_retadores(cfg, subjects, est.get("historial", []))
        if est["cola"]:
            prefijo = "cola regenerada por IA + "
    if not est.get("cola"):
        return "rotacion pendiente: cola vacia y generador fallo", alertas, (subjects, acum, glob)

    nuevo = est["cola"].pop(0)
    viejo = subjects[pierde]
    camp["sequences"][0]["steps"][0]["variants"][pierde]["subject"] = nuevo
    api("PATCH", f"https://api.instantly.ai/api/v2/campaigns/{cid}", {"sequences": camp["sequences"]})
    est.setdefault("historial", []).append({"fecha": ahora, "saco": viejo, "puso": nuevo,
        "datos": {str(i): {"sent": delta[i]["sent"], "opens": delta[i]["opens"],
                           "tasa": round(tasas[i] * 100, 1)} for i in delta}})
    # el baseline nuevo es el acumulado AL momento de rotar
    est["baseline"] = {"ts": ahora, "datos": acum, "subjects": subjects}
    return (f"{prefijo}ROTADO: '{viejo}' ({tasas[pierde]*100:.0f}%) -> '{nuevo}' | "
            f"gana: '{subjects[gana]}' ({tasas[gana]*100:.0f}%)"), alertas, (subjects, acum, glob)


def main():
    ahora = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    estado = json.loads(ESTADO.read_text(encoding="utf-8")) if ESTADO.exists() else {}
    # el estado viejo era de una sola campaña (P1-EU): se migra a su clave
    if "baseline" in estado or "cola" in estado:
        estado = {"p1eu": estado}

    bloques, alertas = [], []
    for seg, cfg in OBJETIVOS.items():
        est = estado.setdefault(seg, {"baseline": None, "cola": list(cfg["cola"]), "historial": []})
        print(f"[{seg}] {cfg['nombre']}")
        try:
            accion, al, datos = optimizar(seg, cfg, est, ahora)
        except Exception as e:
            accion, al, datos = f"ERROR: {str(e)[:120]}", [], None
        print(f"  {accion}")
        alertas += al
        if datos:
            subjects, acum, glob = datos
            enviados = glob.get("emails_sent_count", 0)
            abiertos = glob.get("open_count_unique_by_step", glob.get("open_count_unique", 0))
            tabla = "\n".join(f"| {chr(65+i)} | {subjects[i]} | {acum[i]['sent']} | {acum[i]['opens']} |"
                              for i in sorted(acum))
            hist = "\n".join(f"- {h['fecha']}: sacó \"{h['saco']}\" → puso \"{h['puso']}\""
                             for h in est.get("historial", [])[-5:]) or "- (sin rotaciones aún)"
            bloques.append(f"""### {cfg['nombre']}

| enviados | abiertos | apertura | clicks | respuestas | rebotes | bajas |
|---|---|---|---|---|---|---|
| {enviados} | {abiertos} | {abiertos*100//max(1,enviados)}% | {glob.get('link_click_count',0)} | {glob.get('reply_count',0)} | {glob.get('bounced_count',0)} | {glob.get('unsubscribed_count',0)} |

| variante | asunto | enviados | aperturas únicas |
|---|---|---|---|
{tabla}

**Acción:** {accion}

**Últimas rotaciones:**
{hist}

**Retadores en cola:** {len(est.get('cola', []))}
""")
        else:
            bloques.append(f"### {cfg['nombre']}\n\n**Acción:** {accion}\n")

    ESTADO.write_text(json.dumps(estado, indent=1, ensure_ascii=False), encoding="utf-8")
    REPORTE.write_text(
        f"# Reporte automático — A/B de asuntos (cold)\n\n"
        f"_Última corrida: {ahora} (optimizador diario en GitHub Actions)_\n\n"
        + ("\n".join(alertas) + "\n\n" if alertas else "Sin alertas.\n\n")
        + "\n".join(bloques)
        + f"\n---\n\nRota cuando ambas variantes juntan {MIN_ENVIOS} envíos nuevos y la "
          f"perdedora abre menos del {int(UMBRAL*100)}% de la ganadora.\n",
        encoding="utf-8")
    print(f"\nreporte: {REPORTE.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
