#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cupo de leads del plan de Instantly — el recurso compartido que nadie estaba mirando.

El plan topea los leads ALMACENADOS (Hypergrowth = 25.000) y ese cupo lo comparten
las campanas COLD con los flows HOT (payment rescue, abandoned checkout, welcome...).
El 2026-08-07 las cold llegaron a 24.952/25.000 y durante 12 horas todo `add_lead`
devolvio 403 "Lead limit reached": clientes que intentaron pagar no recibieron nada.

Regla que impone este modulo: **las cold nunca pueden bajar la reserva de los hot.**

    python cupo.py            # estado actual

Se usa como libreria desde goteo.py / subir-leads.py:

    from cupo import cupo_disponible_para_cold
    n = cupo_disponible_para_cold()   # cuantos leads cold PUEDO subir ahora
"""
import json, os, subprocess, sys, time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
# En GitHub Actions no existe `.secrets/` (esta gitignored): la key viene por env,
# igual que en optimizar-p1.py y unibox-bot.py.
KEY = os.environ.get("INSTANTLY_API_KEY") or (HERE.parents[1] / ".secrets" / "instantly-api.key").read_text().strip()

# Tope del plan (pid_hg_v1 = Hypergrowth). Si se cambia de plan, actualizar aca.
TOPE_PLAN = 25_000
# Colchon intocable para los flows hot. Un cliente que intenta pagar SIEMPRE tiene
# que entrar, aunque la prospeccion fria se quede sin lugar.
RESERVA_HOT = 1_500

PREFIJOS_COLD = ("[COLD]",)


def _api(method, url, body=None):
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
            d = {"statusCode": 599}
        if isinstance(d, dict) and d.get("statusCode") == 429:
            time.sleep(2 ** (intento + 1)); continue
        return d
    return d


def estado():
    """Devuelve (total, cold, hot, libre) leyendo el conteo real por campana."""
    d = _api("GET", "https://api.instantly.ai/api/v2/campaigns/analytics")
    if not isinstance(d, list):
        raise RuntimeError(f"no pude leer analytics: {str(d)[:200]}")
    cold = sum(c.get("leads_count", 0) for c in d
               if str(c.get("campaign_name", "")).startswith(PREFIJOS_COLD))
    total = sum(c.get("leads_count", 0) for c in d)
    return total, cold, total - cold, max(0, TOPE_PLAN - total)


def cupo_disponible_para_cold():
    """Cuantos leads cold se pueden subir sin invadir la reserva de los hot."""
    total, _cold, _hot, _libre = estado()
    return max(0, TOPE_PLAN - RESERVA_HOT - total)


def main():
    total, cold, hot, libre = estado()
    print(f"  tope del plan        : {TOPE_PLAN:>7,}")
    print(f"  leads almacenados    : {total:>7,}  ({100*total/TOPE_PLAN:.1f}%)")
    print(f"    - campanas cold    : {cold:>7,}")
    print(f"    - flows hot        : {hot:>7,}")
    print(f"  libre                : {libre:>7,}")
    print(f"  reserva para hot     : {RESERVA_HOT:>7,}")
    print(f"  subible por cold AHORA: {cupo_disponible_para_cold():>6,}")
    if libre == 0:
        print("\n  *** CUPO AGOTADO: los flows hot NO pueden contactar clientes. ***")
        print("  Fix: python liberar-cupo.py p2eude 3000")
        return 1
    if libre < RESERVA_HOT:
        print(f"\n  *** AVISO: quedan {libre} lugares, por debajo de la reserva. ***")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
