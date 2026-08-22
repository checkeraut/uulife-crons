#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
⚠️ REEMPLAZADO POR `ciclo.py` (2026-08-18). NO correr los dos.

Este script rota leads pero con una regla que ya no sirve: su ledger
`listas/subidos-<seg>.txt` es una lista negra permanente ("a quien ya le escribí,
nunca más"). El pedido de Juanma es el contrario — que a nadie se le deje de
escribir nunca, salvo que se desuscriba. `ciclo.py` hace la rotación con una base
de estado (`estado.py`) que sí permite volver a contactar pasada la cadencia, y
conserva el historial de aperturas/clicks que al borrar el lead se pierde.

Se deja acá porque `ciclo.py` reutiliza sus funciones (`api`, `payload_lead`,
`LISTAS`, `CAMPANAS`) y porque documenta gotchas de la API que costaron caro.

─────────────────────────────────────────────────────────────────────────────
Goteo de leads cold — rotación dentro del tope del plan Growth de Instantly (1.000 leads).

El plan topea los leads ALMACENADOS, pero borrar libera cupo (verificado 2026-07-11).
Este script mantiene la campaña llena: borra los que ya no van a recibir nada más
(secuencia terminada / rebotados / desuscriptos) y rellena con leads frescos de la lista.

    python goteo.py p1              # rotación normal
    python goteo.py p1 --dry-run    # muestra qué haría sin tocar nada

Reglas duras:
  - NUNCA borra leads que respondieron (email_reply_count > 0): son los valiosos.
  - NUNCA re-sube un email que ya pasó por la campaña: registro append-only en
    listas/subidos-<seg>.txt (gitignored, PII). Si borrás ese archivo, podés
    mandarle de nuevo a gente que ya recibió/rebotó/se desuscribió. NO borrarlo.
  - Correrlo 1 vez por día (o al menos 2-3x/semana) mientras la campaña esté activa.

Con 230/día (2 × 115 casillas) y 3 emails por lead, arrancan ~76 leads nuevos por día
hábil; en régimen hay ~700 leads "en vuelo", así que la ventana de 1.000 alcanza para
sostener el ritmo sin que el goteo se quede corto.
"""
import argparse, csv, json, os, re, subprocess, sys, time
from pathlib import Path
from urllib.parse import quote

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
# En GitHub Actions no existe `.secrets/` (esta gitignored): la key viene por env,
# igual que en optimizar-p1.py y unibox-bot.py.
KEY = os.environ.get("INSTANTLY_API_KEY") or (HERE.parents[1] / ".secrets" / "instantly-api.key").read_text().strip()
CAMPANAS = json.loads((HERE / "campanas.json").read_text())
LISTAS = {
    "p1": "cold-p1-glp1-restock.csv", "p2": "cold-p2-glp1-winback.csv",
    "p3": "cold-p3-regen-crossell.csv", "p4": "cold-p4-vip.csv",
    # 2026-08-05: campañas EU (el tope de 25k del plan hace obligatoria la rotación
    # para la cola UK de ~38k; DE ya subió completa). Los CSV p2eu no tienen columnas
    # de producto — payload_lead los maneja igual (custom_variables mínimas).
    "p1eu": "cold-p1-eu.csv",
    "p2euuk": "cold-p2-eu-uk.csv",
    "p2eude": "cold-p2-eu-de.csv",
}
BATCH = 50
# status de lead en Instantly: 1=activo, 3=terminó la secuencia, -1=rebotó, -2=se desuscribió
STATUS_BORRABLES = {3, -1, -2}


def api(method, url, body=None):
    cmd = ["curl", "-s", "--max-time", "60", "-X", method, url,
           "-H", f"Authorization: Bearer {KEY}"]
    # OJO: el Content-Type SOLO va cuando hay body. Instantly rechaza un DELETE sin
    # cuerpo con "FST_ERR_CTP_EMPTY_JSON_BODY" si igual mandas el header, y como el
    # borrado devolvia 400 en silencio la rotacion de leads NUNCA borro nada.
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body, ensure_ascii=True)]
    for intento in range(5):
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        try:
            d = json.loads(r.stdout) if r.stdout.strip() else {}
        except json.JSONDecodeError:
            d = {"_raw": (r.stdout or "")[:300], "statusCode": 599}
        if isinstance(d, dict) and d.get("statusCode") == 429:  # rate limit → backoff
            time.sleep(2 ** (intento + 1)); continue
        return d
    return d


def listar_leads(cid):
    leads, cursor = [], None
    while True:
        body = {"campaign": cid, "limit": 100}
        if cursor:
            body["starting_after"] = cursor
        d = api("POST", "https://api.instantly.ai/api/v2/leads/list", body)
        items = d.get("items", [])
        leads += items
        cursor = d.get("next_starting_after")
        if not cursor or not items:
            return leads


def payload_lead(r, seg):
    utm = f"utm_source=email-cold&utm_medium=email&utm_campaign={seg}"
    def bridge(url):
        path = (url or "https://uu.life").replace("https://uu.life", "") or "/"
        return f"https://uu.life/checkout/?uu_go={quote(path, safe='')}&{utm}"
    cv = {"compound": r.get("compound", "")}
    if r.get("product"):
        cv.update(product=r["product"], productUrl=bridge(r.get("product_url")), price=r.get("price", ""))
    if r.get("companion"):
        cv.update(companion=r["companion"], companionUrl=bridge(r.get("companion_url")),
                  companionPrice=r.get("companion_price", ""))
    return {"email": r["email"].strip().lower(), "first_name": r.get("first_name", ""),
            "custom_variables": cv}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("segmento", choices=list(LISTAS))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    seg = args.segmento
    cid = CAMPANAS[seg]
    ledger_path = HERE / "listas" / f"subidos-{seg}.txt"
    ledger = set(ledger_path.read_text(encoding="utf-8").splitlines()) if ledger_path.exists() else set()

    # 1. Estado actual de la campaña
    leads = listar_leads(cid)
    borrables = [l for l in leads if l.get("status") in STATUS_BORRABLES
                 and not l.get("email_reply_count")]
    print(f"[{seg}] en campaña: {len(leads)} | terminados/rebotados/unsub borrables: {len(borrables)}")

    # 2. Candidatos frescos (en la lista segmentada, nunca subidos)
    with open(HERE / "listas" / LISTAS[seg], newline="", encoding="utf-8") as f:
        frescos = [r for r in csv.DictReader(f) if r["email"].strip().lower() not in ledger]
    print(f"[{seg}] frescos disponibles en la lista: {len(frescos)}")

    if args.dry_run:
        print(f"[dry-run] borraría {len(borrables)} y subiría hasta "
              f"{len(borrables) if borrables else BATCH} frescos (si hay cupo). Fin.")
        return

    # 3. Borrar los que ya no reciben nada más (libera cupo del plan)
    borrados = 0
    for l in borrables:
        d = api("DELETE", f"https://api.instantly.ai/api/v2/leads/{l['id']}")
        if d.get("id") == l["id"]:
            borrados += 1
        time.sleep(0.3)
    print(f"[{seg}] borrados: {borrados}/{len(borrables)}")

    # 4. Rellenar el cupo liberado. Si no se borró nada (p.ej. campaña recién activada),
    #    intenta igual una tanda chica por si quedó lugar libre en el plan.
    # GUARDA DE CUPO (2026-08-07): el tope del plan lo comparten las cold con los flows
    # hot. Nunca rellenar más allá de lo que deja la reserva de los hot — si no, un
    # cliente que intenta pagar se queda sin su email de rescate.
    from cupo import cupo_disponible_para_cold, RESERVA_HOT
    disponible = cupo_disponible_para_cold()
    objetivo = min( borrados if borrados else BATCH, disponible )
    if objetivo <= 0:
        print(f"[{seg}] CUPO: sin lugar sin invadir la reserva de {RESERVA_HOT} de los hot. "
              f"No se sube nada (los borrados igual liberaron cupo).")
        return
    tanda = frescos[:objetivo]
    subidos = []
    for i in range(0, len(tanda), BATCH):
        batch = tanda[i:i + BATCH]
        d = api("POST", "https://api.instantly.ai/api/v2/leads/add",
                {"campaign_id": cid, "skip_if_in_campaign": True,
                 "leads": [payload_lead(r, seg) for r in batch]})
        if d.get("error") or d.get("statusCode", 200) >= 400:
            print(f"[{seg}] tope del plan alcanzado ({json.dumps(d)[:160]}) — lo subido quedó registrado.")
            break
        subidos += [r["email"].strip().lower() for r in batch]
        time.sleep(0.5)

    if subidos:
        with open(ledger_path, "a", encoding="utf-8") as f:
            f.write("".join(e + "\n" for e in subidos))
    print(f"[{seg}] subidos nuevos: {len(subidos)} | registro: {ledger_path.name} "
          f"({len(ledger) + len(subidos)} emails acumulados)")
    print(f"[{seg}] quedan {len(frescos) - len(subidos)} frescos en cola.")


if __name__ == "__main__":
    main()
