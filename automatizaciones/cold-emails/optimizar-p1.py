#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Optimizador perpetuo del A/B de asuntos de la campaña COLD P1 ("siempre testeando").

Corre a diario en GitHub Actions (.github/workflows/optimizar-p1.yml):
  1. Lee las métricas por variante del paso 1 (Instantly analytics/steps).
  2. Compara los DELTAS desde la última rotación (baseline en optimizador-estado.json).
  3. Si ambas variantes juntaron ≥40 envíos nuevos y la perdedora abre <60% que la
     ganadora → reemplaza el asunto perdedor por el próximo retador de la cola.
  4. Cola vacía → le pide 5 retadores nuevos a la API de OpenAI (reglas de compliance
     duras en el prompt); si falla, avisa y sigue sin rotar.
  5. Escribe reporte-p1.md (commiteado por el workflow) con métricas, acción e historial.

Alertas en el reporte (NO frena nada solo): rebote >3% o bajas >1%.
Local: python optimizar-p1.py  (lee la key de ../../.secrets/instantly-api.key)
"""
import json, os, sys, urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = Path(__file__).resolve().parent
# Campaña objetivo: la EU si existe en campanas.json (2026-08-04, pivote a UK/DE), si no la P1 original.
_c = json.loads((HERE / "campanas.json").read_text())
P1 = _c.get("p1eu") or _c["p1"]
ESTADO = HERE / "optimizador-estado.json"
REPORTE = HERE / "reporte-p1.md"
MIN_ENVIOS = 40        # por variante, desde el último baseline
UMBRAL = 0.6           # perdedora abre < 60% de la ganadora → rotar

KEY = os.environ.get("INSTANTLY_API_KEY") or (HERE.parents[1] / ".secrets" / "instantly-api.key").read_text().strip()
OPENAI = os.environ.get("OPENAI_API_KEY", "")


def api(method, url, body=None):
    req = urllib.request.Request(url, method=method,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json",
                 "User-Agent": "uulife-optimizador/1.0"},
        data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def generar_retadores(actuales, historial):
    """5 asuntos nuevos via OpenAI. Compliance dura de uu.life en el prompt."""
    if not OPENAI:
        return []
    usados = [h.get("puso") for h in historial] + [h.get("saco") for h in historial] + actuales
    prompt = (
        "Generate 5 cold email subject lines for e-commerce restock emails to past customers "
        "of a research peptide store. Style: lowercase, 3-6 words, curiosity-driven, human "
        "(like a colleague wrote it), NO emoji, NO exclamation marks.\n"
        "HARD RULES (regulated merchant): never mention drug/peptide/compound names, never "
        "medical claims (cure/treat/prevent), no doses, no 'weight loss', no urgency/scarcity "
        "(no 'last chance', 'act now', 'running out'), no spam words (free, discount, deal).\n"
        "Angles that work: their timeline/next order, lab certificates (COAs), quality standards.\n"
        f"Already used (avoid similar): {usados[:20]}\n"
        'Return ONLY a JSON array of 5 strings.'
    )
    try:
        req = urllib.request.Request("https://api.openai.com/v1/chat/completions", method="POST",
            headers={"Authorization": f"Bearer {OPENAI}", "Content-Type": "application/json",
                     "User-Agent": "uulife-optimizador/1.0"},
            data=json.dumps({"model": "gpt-4o-mini", "temperature": 0.9,
                "messages": [{"role": "user", "content": prompt}]}).encode())
        with urllib.request.urlopen(req, timeout=60) as r:
            txt = json.loads(r.read())["choices"][0]["message"]["content"].strip()
        txt = txt[txt.find("["): txt.rfind("]") + 1]
        nuevos = [s.strip() for s in json.loads(txt) if isinstance(s, str) and 2 < len(s) < 60]
        return nuevos[:5]
    except Exception as e:
        print("generador fallo:", e)
        return []


def main():
    ahora = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    estado = json.loads(ESTADO.read_text(encoding="utf-8")) if ESTADO.exists() else {
        "baseline": None, "cola": [
            "worth 60 seconds before you reorder",
            "the certificate question",
            "read this before you restock",
            "one thing to check before reordering",
        ], "historial": []}

    camp = api("GET", f"https://api.instantly.ai/api/v2/campaigns/{P1}")
    variants = camp["sequences"][0]["steps"][0]["variants"]
    subjects = [v.get("subject", "") for v in variants]

    steps = api("GET", f"https://api.instantly.ai/api/v2/campaigns/analytics/steps?campaign_id={P1}")
    acum = {}
    for s in steps:
        if s.get("step") == "0":
            acum[int(s["variant"])] = {"sent": s.get("sent", 0), "opens": s.get("unique_opened", 0)}
    for i in range(len(variants)):
        acum.setdefault(i, {"sent": 0, "opens": 0})

    glob = api("GET", f"https://api.instantly.ai/api/v2/campaigns/analytics?id={P1}")
    glob = glob[0] if isinstance(glob, list) and glob else {}
    sent_g = glob.get("emails_sent_count", 0) or 1

    accion = "sin cambios"
    alertas = []
    br = glob.get("bounced_count", 0) * 100 / sent_g
    ur = glob.get("unsubscribed_count", 0) * 100 / sent_g
    if br > 3: alertas.append(f"⚠ REBOTE {br:.1f}% (>3%) — revisar lista/entrega")
    if ur > 1: alertas.append(f"⚠ BAJAS {ur:.1f}% (>1%) — revisar copy/frecuencia")

    if estado["baseline"] is None:
        estado["baseline"] = {"ts": ahora, "datos": acum, "subjects": subjects}
        accion = "baseline inicial establecido"
    else:
        base = estado["baseline"]["datos"]
        delta = {i: {"sent": acum[i]["sent"] - base.get(str(i), base.get(i, {})).get("sent", 0)
                        if isinstance(base.get(str(i), base.get(i)), dict) else acum[i]["sent"],
                     "opens": acum[i]["opens"] - base.get(str(i), base.get(i, {})).get("opens", 0)
                        if isinstance(base.get(str(i), base.get(i)), dict) else acum[i]["opens"]}
                 for i in acum}
        if len(delta) >= 2 and all(d["sent"] >= MIN_ENVIOS for d in delta.values()):
            tasas = {i: (d["opens"] / d["sent"]) if d["sent"] else 0 for i, d in delta.items()}
            ganadora = max(tasas, key=tasas.get)
            perdedora = min(tasas, key=tasas.get)
            if ganadora != perdedora and tasas[ganadora] > 0 and tasas[perdedora] < UMBRAL * tasas[ganadora]:
                if not estado["cola"]:
                    estado["cola"] = generar_retadores(subjects, estado["historial"])
                    if estado["cola"]:
                        accion = "cola regenerada por IA + "
                if estado["cola"]:
                    nuevo = estado["cola"].pop(0)
                    viejo = subjects[perdedora]
                    camp["sequences"][0]["steps"][0]["variants"][perdedora]["subject"] = nuevo
                    api("PATCH", f"https://api.instantly.ai/api/v2/campaigns/{P1}",
                        {"sequences": camp["sequences"]})
                    estado["historial"].append({"fecha": ahora, "saco": viejo, "puso": nuevo,
                        "datos": {str(i): {"sent": delta[i]["sent"], "opens": delta[i]["opens"],
                                           "tasa": round(tasas[i] * 100, 1)} for i in delta}})
                    estado["baseline"] = {"ts": ahora, "datos": acum, "subjects": subjects}
                    # el baseline debe reflejar el acumulado AL momento de la rotación
                    accion = (accion if accion != "sin cambios" else "") + \
                        f"ROTADO: '{viejo}' ({tasas[perdedora]*100:.0f}%) → '{nuevo}' | ganadora: '{subjects[ganadora]}' ({tasas[ganadora]*100:.0f}%)"
                else:
                    accion = "rotacion pendiente: cola vacia y generador fallo"
            else:
                accion = f"A/B sigue juntando datos (tasas: {', '.join(f'{tasas[i]*100:.0f}%' for i in sorted(tasas))})"
        else:
            faltan = {i: max(0, MIN_ENVIOS - d["sent"]) for i, d in delta.items()}
            accion = f"juntando datos (faltan envios por variante: {faltan})"

    ESTADO.write_text(json.dumps(estado, indent=1, ensure_ascii=False), encoding="utf-8")

    hist = "\n".join(f"- {h['fecha']}: sacó \"{h['saco']}\" → puso \"{h['puso']}\"" for h in estado["historial"][-10:]) or "- (sin rotaciones aún)"
    REPORTE.write_text(f"""# Reporte automático — COLD P1

_Última corrida: {ahora} (optimizador diario en GitHub Actions)_

## Global (tasas POR EMAIL enviado — definición Juanma)
| emails enviados | emails abiertos | tasa apertura | clicks | respuestas | rebotes | bajas |
|---|---|---|---|---|---|---|
| {glob.get('emails_sent_count',0)} | {glob.get('open_count_unique_by_step', glob.get('open_count_unique',0))} | {glob.get('open_count_unique_by_step', glob.get('open_count_unique',0))*100//max(1,glob.get('emails_sent_count',1))}% | {glob.get('link_click_count',0)} | {glob.get('reply_count',0)} | {glob.get('bounced_count',0)} | {glob.get('unsubscribed_count',0)} |

## A/B de asuntos (paso 1, acumulado)
| variante | asunto | enviados | aperturas únicas |
|---|---|---|---|
""" + "\n".join(f"| {chr(65+i)} | {subjects[i]} | {acum[i]['sent']} | {acum[i]['opens']}" for i in sorted(acum)) + f"""

## Acción de hoy
{accion}

{chr(10).join(alertas) if alertas else "Sin alertas."}

## Historial de rotaciones
{hist}

## Cola de retadores
{chr(10).join('- ' + s for s in estado['cola']) or '- (vacía — se regenera por IA en la próxima rotación)'}
""", encoding="utf-8")
    print(accion)
    for a in alertas: print(a)


if __name__ == "__main__":
    main()
