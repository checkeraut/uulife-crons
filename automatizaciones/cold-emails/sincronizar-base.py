#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Vuelca la base local (SQLite) a la base de la nube (Postgres). One-shot.

    DATABASE_URL=postgres://... COLD_TABLA=uulife_cold_leads python sincronizar-base.py
    ... --dry-run

POR QUE HACE FALTA
`ciclo.py --migrar` carga las listas y los ledgers, pero NO el historial: aperturas,
clicks y sobre todo las **supresiones**. Ese historial solo existe en la SQLite local
porque se rescato de Instantly antes de borrar cada lead — y de los 18.469 leads ya
borrados, Instantly no lo tiene mas. Si la base de la nube arranca sin las
supresiones, el ciclo le volveria a escribir a gente que se dio de baja. Eso no es un
bug de entregas: es legal. Ver la regla sagrada en estado.py.

Copia, para cada lead: ciclo, ultimo_contacto, aperturas, clicks, respuestas,
suprimido y motivo. Nunca baja un `suprimido` de 1 a 0: si en cualquiera de las dos
bases el lead esta suprimido, queda suprimido.
"""
import argparse, json, os, sqlite3, sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import estado

LOTE = 1000

SQL_VOLCAR = """
INSERT INTO {T} (email, segmento, ciclo, ultimo_contacto, aperturas, clicks,
                 respuestas, suprimido, motivo, datos, actualizado)
VALUES (?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT (email) DO UPDATE SET
    ciclo           = MAX({T}.ciclo,      EXCLUDED.ciclo),
    ultimo_contacto = COALESCE(EXCLUDED.ultimo_contacto, {T}.ultimo_contacto),
    aperturas       = MAX({T}.aperturas,  EXCLUDED.aperturas),
    clicks          = MAX({T}.clicks,     EXCLUDED.clicks),
    respuestas      = MAX({T}.respuestas, EXCLUDED.respuestas),
    suprimido       = MAX({T}.suprimido,  EXCLUDED.suprimido),
    motivo          = COALESCE({T}.motivo, EXCLUDED.motivo),
    datos           = COALESCE(EXCLUDED.datos, {T}.datos),
    actualizado     = EXCLUDED.actualizado
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not os.environ.get("DATABASE_URL"):
        print("Falta DATABASE_URL: no hay base de destino a la que volcar.")
        return 1
    origen = HERE / "listas" / "leads.db"
    if not origen.exists():
        print(f"No existe la base local {origen}")
        return 1

    sq = sqlite3.connect(origen)
    sq.row_factory = sqlite3.Row
    filas = sq.execute("""SELECT email, segmento, ciclo, ultimo_contacto, aperturas,
                                 clicks, respuestas, suprimido, motivo, datos, actualizado
                          FROM leads""").fetchall()
    sup = sum(1 for f in filas if f["suprimido"])
    act = sum(1 for f in filas if f["aperturas"] or f["clicks"])
    print(f"base local : {len(filas):,} leads | {sup} suprimidos | {act:,} con actividad")

    cx = estado.conectar(); estado.init(cx)
    print(f"destino    : {cx.donde()}")
    antes = estado.resumen(cx)
    print(f"destino antes: {antes['total']:,} leads | {antes['suprimidos']} suprimidos | warm {antes['warm']:,}")

    if args.dry_run:
        print("[dry-run] no se volco nada.")
        return 0

    datos = [(f["email"], f["segmento"], f["ciclo"] or 0, f["ultimo_contacto"],
              f["aperturas"] or 0, f["clicks"] or 0, f["respuestas"] or 0,
              f["suprimido"] or 0, f["motivo"], f["datos"], f["actualizado"])
             for f in filas]
    for i in range(0, len(datos), LOTE):
        cx.ejecutar_muchos(SQL_VOLCAR, datos[i:i + LOTE])
        if (i // LOTE) % 10 == 0:
            print(f"  volcados {min(i + LOTE, len(datos)):,}/{len(datos):,}")
    cx.commit()

    desp = estado.resumen(cx)
    print(f"\ndestino despues: {desp['total']:,} leads | {desp['suprimidos']} suprimidos | "
          f"warm {desp['warm']:,} | con datos {desp['con_datos']:,}")

    # control duro: las supresiones son lo que no se puede perder
    faltan = [f["email"] for f in filas if f["suprimido"]
              and not estado.esta_suprimido(cx, f["email"])]
    print(f"supresiones verificadas: {sup - len(faltan)}/{sup}"
          + ("  -> OK" if not faltan else f"  -> FALTAN: {faltan}"))
    cx.close(); sq.close()
    return 0 if sup and not faltan or not sup else 1


if __name__ == "__main__":
    sys.exit(main())
