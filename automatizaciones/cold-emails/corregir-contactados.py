#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Libera los leads que la migracion marco como contactados sin que lo estuvieran.

    python corregir-contactados.py --dry-run
    python corregir-contactados.py

EL ERROR QUE ARREGLA (2026-08-24)
La migracion inicial marco como "contactado" a todo el que figurara en el ledger
`subidos-<seg>.txt`. Pero ese ledger registra a quien se SUBIO a la campana, no a
quien recibio un email. La mayoria estaba cargada esperando turno y se borro en el
drenaje sin recibir nada. Resultado medido:

    segmento   la base decia   Instantly contacto de verdad
    p1eu               6.902                          1.271
    p2euuk             9.237                          2.381
    p2eude            11.447                            640

~23.000 leads quedaron bloqueados por la cadencia sin haber recibido jamas un
email — y por eso P1-EU "se apagaba": no le quedaba nadie elegible, cuando en
realidad tenia 6.291 personas sin contactar.

COMO SE DISTINGUEN
Las fechas que vienen de Instantly (`timestamp_last_contact`) terminan en `Z`.
Las que puso la migracion son isoformat con `+00:00`. Un lead con fecha de la
migracion y CERO actividad (0 aperturas, 0 clicks, 0 respuestas) nunca recibio nada.

EL RESGUARDO
De los ~4.300 realmente contactados, solo ~1.200 conservan su fecha de Instantly:
al resto se le perdio en el drenaje inicial, asi que caen en el mismo filtro. Por
eso no se los deja completamente libres: se les pone `ultimo_contacto` a hace
PISO_DIAS. Si alguno si habia recibido algo, el peor caso es que le vuelva a llegar
con la distancia minima que el sistema considera segura — nunca menos.
"""
import argparse, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import estado

SEGMENTOS = ["p1eu", "p2euuk", "p2eude"]

# El patron del LIKE va como parametro, no embebido: psycopg toma un '%+' literal
# como placeholder y revienta con "only '%s', '%b', '%t' are allowed".
PATRON_MIGRACION = "%+00:00"

CONDICION = """segmento = ? AND ciclo > 0 AND suprimido = 0
               AND ultimo_contacto LIKE ?
               AND aperturas = 0 AND clicks = 0 AND respuestas = 0"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cx = estado.conectar(); estado.init(cx)
    print(f"base: {cx.donde()}\n")
    fecha = (datetime.now(timezone.utc) - timedelta(days=estado.PISO_DIAS)).isoformat(timespec="seconds")

    total = 0
    for seg in SEGMENTOS:
        n = cx.valor(f"SELECT COUNT(*) FROM {{T}} WHERE {CONDICION}", (seg, PATRON_MIGRACION))
        antes = len(estado.candidatos(cx, seg, 200000))
        if not args.dry_run and n:
            cx.ejecutar(f"""UPDATE {{T}} SET ciclo = 0, ultimo_contacto = ?, actualizado = ?
                            WHERE {CONDICION}""", (fecha, estado.ahora(), seg, PATRON_MIGRACION))
            cx.commit()
        despues = len(estado.candidatos(cx, seg, 200000))
        total += n
        print(f"  {seg:8} libera {n:7,}  |  candidatos: {antes:,} -> {despues:,}")

    print(f"\n  {'[dry-run] ' if args.dry_run else ''}total liberado: {total:,}")
    r = estado.resumen(cx)
    print(f"  base: {r['total']:,} leads | sin contactar {r['sin_contactar']:,} | "
          f"contactados {r['contactados']:,} | suprimidos {r['suprimidos']}")
    cx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
