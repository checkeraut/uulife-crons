#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Base de estado de los leads cold — la memoria que Instantly NO puede guardar.

POR QUE EXISTE (2026-08-18)
El plan de Instantly topea los leads ALMACENADOS (25.000), no los contactados
historicamente. La unica forma de no soltar nunca a un lead SIN pagar el plan de
100k es rotar: subir -> mandarle la secuencia -> borrarlo (libera cupo) -> volver a
subirlo mas adelante con otro contenido. El problema es que **al borrar el lead de
Instantly se pierde todo su historial** (aperturas, clicks, si respondio). Sin ese
historial, en el segundo ciclo no sabriamos a quien tratar como interesado.

DONDE VIVE
  - Sin `DATABASE_URL`  -> SQLite en `listas/leads.db` (gitignored: es PII).
  - Con `DATABASE_URL`  -> Postgres. Es lo que usa GitHub Actions, porque la PC de
    Juanma se apaga y una tarea programada de Windows no correria. El Action no
    tiene los CSV (son PII y estan gitignored), por eso la base guarda tambien los
    datos del lead en `datos` (JSON) y el ciclo arma el payload desde ahi.

LA REGLA SAGRADA
Instantly **no expone la blocklist por API** (probados 9 nombres de endpoint el
2026-08-18, ninguno existe). Por lo tanto, cuando borramos un lead, la proteccion
del desuscripto queda ENTERAMENTE de nuestro lado. Si el ciclo re-sube por error a
alguien que se dio de baja, le volvemos a escribir: eso no es un bug de entregas,
es un problema legal — y para un merchant de categoria restringida se paga con el
procesador de pagos, no con una queja.

Por eso `suprimido` es de una sola via: se pone en 1 y NUNCA vuelve a 0. No hay
funcion para desuprimir, a proposito.
"""
import json, os, sqlite3, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
DB_SQLITE = HERE / "listas" / "leads.db"
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
# Nombre de tabla configurable: si la base Postgres es compartida con otro proyecto,
# esto evita chocar con una tabla `leads` ajena.
TABLA = os.environ.get("COLD_TABLA", "leads")

# Cadencia por segmento de comportamiento: cada cuantos dias se le puede volver a
# escribir a alguien que ya termino una secuencia.
#   - warm = abrio o clickeo alguna vez -> mostro interes, se le habla seguido
#   - frio = recibio la secuencia entera y no abrio nada -> bajar frecuencia y
#            cambiar el angulo; insistir semanalmente con esto es lo que dispara
#            las quejas de spam y quema los dominios (y ahi se pierden TODOS).
DIAS_WARM = 14
DIAS_FRIO = 42

UNSUB     = "desuscripto"
REBOTE    = "reboto"
RESPUESTA = "respondio"   # sale del automatico: lo atiende una persona


# ───────────────────────── capa de base (SQLite / Postgres) ─────────────────────

class Conexion:
    """API chica y uniforme sobre sqlite3 o psycopg.

    El SQL se escribe UNA vez con placeholders `?` y `MAX(a,b)`; para Postgres se
    traduce a `%s` y `GREATEST(a,b)`. `ON CONFLICT ... DO UPDATE` y `EXCLUDED`
    funcionan igual en los dos motores, asi que no hay dos versiones del SQL.
    """

    def __init__(self):
        self.pg = bool(DATABASE_URL)
        if self.pg:
            import psycopg
            self.cx = psycopg.connect(DATABASE_URL, connect_timeout=30)
        else:
            DB_SQLITE.parent.mkdir(parents=True, exist_ok=True)
            self.cx = sqlite3.connect(DB_SQLITE)
            self.cx.row_factory = sqlite3.Row

    def _sql(self, s):
        s = s.replace("{T}", TABLA)
        if not self.pg:
            return s
        return s.replace("?", "%s").replace("MAX(", "GREATEST(")

    def ejecutar(self, sql, args=()):
        cur = self.cx.cursor()
        cur.execute(self._sql(sql), args)
        return cur

    def ejecutar_muchos(self, sql, seq):
        """Inserta en lote. Imprescindible con Postgres remoto: la migracion de
        63.573 leads de a uno son 63.573 viajes de red a Neon (no termina nunca);
        en lote baja a minutos."""
        cur = self.cx.cursor()
        cur.executemany(self._sql(sql), list(seq))
        return cur

    def filas(self, sql, args=()):
        cur = self.ejecutar(sql, args)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def valor(self, sql, args=()):
        return self.ejecutar(sql, args).fetchone()[0]

    def commit(self):
        self.cx.commit()

    def close(self):
        self.cx.close()

    def donde(self):
        return (f"Postgres (tabla {TABLA})" if self.pg else f"SQLite {DB_SQLITE}")


def conectar():
    return Conexion()


def init(cx):
    # OJO: sin f-string. `{T}` lo resuelve el traductor de `Conexion._sql`, no Python.
    cx.ejecutar("""
    CREATE TABLE IF NOT EXISTS {T} (
        email            TEXT PRIMARY KEY,
        segmento         TEXT NOT NULL,
        ciclo            INTEGER NOT NULL DEFAULT 0,
        ultimo_contacto  TEXT,
        aperturas        INTEGER NOT NULL DEFAULT 0,
        clicks           INTEGER NOT NULL DEFAULT 0,
        respuestas       INTEGER NOT NULL DEFAULT 0,
        suprimido        INTEGER NOT NULL DEFAULT 0,
        motivo           TEXT,
        datos            TEXT,
        actualizado      TEXT
    )""")
    cx.ejecutar("CREATE INDEX IF NOT EXISTS ix_{T}_seg ON {T}(segmento, suprimido)")
    cx.ejecutar("CREATE INDEX IF NOT EXISTS ix_{T}_cont ON {T}(ultimo_contacto)")
    # Este commit va ANTES del ALTER a proposito: en Postgres un DDL que falla aborta
    # la transaccion entera, y el rollback del `except` se llevaba puesto el CREATE
    # TABLE de arriba — la tabla nunca llegaba a existir. En SQLite no pasaba, por eso
    # el bug solo aparecio al probar contra Neon.
    cx.commit()
    # `datos` se agrego despues de la primera version: las bases creadas antes no la
    # tienen, y CREATE TABLE IF NOT EXISTS no agrega columnas.
    try:
        cx.ejecutar("ALTER TABLE {T} ADD COLUMN datos TEXT")
        cx.commit()
    except Exception:
        if cx.pg:
            cx.cx.rollback()


def ahora():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ─────────────────────────── escalaciones del unibox ────────────────────────────
# El unibox-bot escribia las respuestas que no supo contestar a un .md que despues
# commiteaba al repo. Eso incluye email, nombre y el texto del mensaje del lead: PII
# pura. Con el repo de crons siendo PUBLICO eso seria una filtracion (y con leads
# alemanes, GDPR). Van a la base, que es privada.

def init_escalaciones(cx):
    cx.ejecutar("""
    CREATE TABLE IF NOT EXISTS {T}_escalaciones (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        ts           TEXT,
        email        TEXT,
        campana      TEXT,
        casilla      TEXT,
        cuerpo       TEXT,
        atendida     INTEGER NOT NULL DEFAULT 0
    )""" if not cx.pg else """
    CREATE TABLE IF NOT EXISTS {T}_escalaciones (
        id           BIGSERIAL PRIMARY KEY,
        ts           TEXT,
        email        TEXT,
        campana      TEXT,
        casilla      TEXT,
        cuerpo       TEXT,
        atendida     INTEGER NOT NULL DEFAULT 0
    )""")
    cx.commit()


def registrar_escalacion(cx, email, campana, casilla, cuerpo):
    cx.ejecutar("""INSERT INTO {T}_escalaciones (ts, email, campana, casilla, cuerpo)
                   VALUES (?,?,?,?,?)""",
                (ahora(), email, campana, casilla, (cuerpo or "")[:2000]))
    cx.commit()


def escalaciones_pendientes(cx, limite=50):
    return cx.filas("""SELECT id, ts, email, campana, casilla, cuerpo
                       FROM {T}_escalaciones WHERE atendida=0
                       ORDER BY ts DESC LIMIT ?""", (limite,))


# ──────────────────────────────── operaciones ───────────────────────────────────

def alta(cx, email, segmento, datos=None):
    """Registra un lead que todavia no recibio nada (ciclo 0)."""
    cx.ejecutar("""INSERT INTO {T} (email, segmento, datos, actualizado) VALUES (?,?,?,?)
                   ON CONFLICT (email) DO UPDATE SET
                       datos = COALESCE(EXCLUDED.datos, {T}.datos),
                       actualizado = EXCLUDED.actualizado""",
                (email.strip().lower(), segmento,
                 json.dumps(datos, ensure_ascii=False) if datos else None, ahora()))


def marcar_contactado(cx, email, segmento, datos=None):
    """Suma un ciclo y sella la fecha. Se llama al SUBIR el lead a la campana."""
    e = email.strip().lower()
    cx.ejecutar("""INSERT INTO {T} (email, segmento, ciclo, ultimo_contacto, datos, actualizado)
                   VALUES (?,?,1,?,?,?)
                   ON CONFLICT (email) DO UPDATE SET
                       ciclo = {T}.ciclo + 1,
                       ultimo_contacto = EXCLUDED.ultimo_contacto,
                       datos = COALESCE(EXCLUDED.datos, {T}.datos),
                       actualizado = EXCLUDED.actualizado""",
                (e, segmento, ahora(),
                 json.dumps(datos, ensure_ascii=False) if datos else None, ahora()))


SQL_ALTA = """INSERT INTO {T} (email, segmento, datos, actualizado) VALUES (?,?,?,?)
              ON CONFLICT (email) DO UPDATE SET
                  datos = COALESCE(EXCLUDED.datos, {T}.datos),
                  actualizado = EXCLUDED.actualizado"""

SQL_CONTACTADO = """INSERT INTO {T} (email, segmento, ciclo, ultimo_contacto, datos, actualizado)
                    VALUES (?,?,1,?,?,?)
                    ON CONFLICT (email) DO UPDATE SET
                        ciclo = {T}.ciclo + 1,
                        ultimo_contacto = EXCLUDED.ultimo_contacto,
                        datos = COALESCE(EXCLUDED.datos, {T}.datos),
                        actualizado = EXCLUDED.actualizado"""


def altas_lote(cx, items, segmento):
    """items: [(email, datos_dict_o_None)]. Solo registra/refresca, no toca el ciclo."""
    t = ahora()
    cx.ejecutar_muchos(SQL_ALTA, [
        (e.strip().lower(), segmento,
         json.dumps(d, ensure_ascii=False) if d else None, t)
        for e, d in items])


def contactados_lote(cx, items, segmento):
    """items: [(email, datos_dict_o_None)]. Suma ciclo y sella la fecha."""
    t = ahora()
    cx.ejecutar_muchos(SQL_CONTACTADO, [
        (e.strip().lower(), segmento, t,
         json.dumps(d, ensure_ascii=False) if d else None, t)
        for e, d in items])


def guardar_actividad(cx, email, segmento, aperturas, clicks, respuestas, ultimo_contacto=None):
    """Vuelca las metricas de Instantly ANTES de borrar el lead alla.

    Se queda con el maximo: si el lead ya paso por otros ciclos, la actividad
    acumulada no se pisa con la del ciclo actual.

    `ultimo_contacto` es el `timestamp_last_contact` real de Instantly. Importa para
    la cadencia: la migracion inicial no tenia la fecha verdadera y sello a todos con
    la de hoy, lo que retrasaria semanas a quien en realidad recibio hace diez dias.
    """
    cx.ejecutar("""INSERT INTO {T} (email, segmento, aperturas, clicks, respuestas,
                                      ultimo_contacto, actualizado)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT (email) DO UPDATE SET
                       aperturas  = MAX({T}.aperturas,  EXCLUDED.aperturas),
                       clicks     = MAX({T}.clicks,     EXCLUDED.clicks),
                       respuestas = MAX({T}.respuestas, EXCLUDED.respuestas),
                       ultimo_contacto = COALESCE(EXCLUDED.ultimo_contacto, {T}.ultimo_contacto),
                       actualizado = EXCLUDED.actualizado""",
                (email.strip().lower(), segmento, aperturas or 0, clicks or 0,
                 respuestas or 0, ultimo_contacto, ahora()))


def suprimir(cx, email, motivo, segmento="?"):
    """PERMANENTE. Nunca mas se le escribe. No existe la operacion inversa."""
    cx.ejecutar("""INSERT INTO {T} (email, segmento, suprimido, motivo, actualizado)
                   VALUES (?,?,1,?,?)
                   ON CONFLICT (email) DO UPDATE SET
                       suprimido = 1,
                       motivo = COALESCE({T}.motivo, EXCLUDED.motivo),
                       actualizado = EXCLUDED.actualizado""",
                (email.strip().lower(), segmento, motivo, ahora()))


def esta_suprimido(cx, email):
    f = cx.filas("SELECT suprimido FROM {T} WHERE email=?", (email.strip().lower(),))
    return bool(f and f[0]["suprimido"])


def suprimidos(cx):
    """Set con TODOS los suprimidos. Se consulta antes de cualquier subida."""
    return {f["email"] for f in cx.filas("SELECT email FROM {T} WHERE suprimido=1")}


def candidatos(cx, segmento, limite):
    """A quien le toca recibir, en orden de prioridad.

    1) Los que nunca recibieron nada (ciclo 0) — primero agotar la lista fresca.
    2) Los que ya cumplieron su cadencia, el que hace mas tiempo que no recibe.

    Nunca devuelve suprimidos: el filtro esta en el WHERE, no en el codigo que
    llama, para que no se pueda olvidar.
    """
    hoy = datetime.now(timezone.utc)
    corte_warm = (hoy - timedelta(days=DIAS_WARM)).isoformat(timespec="seconds")
    corte_frio = (hoy - timedelta(days=DIAS_FRIO)).isoformat(timespec="seconds")
    return cx.filas("""
        SELECT email, ciclo, aperturas, clicks, ultimo_contacto, datos FROM {T}
        WHERE segmento = ? AND suprimido = 0
          AND ( ciclo = 0
             OR (ultimo_contacto IS NOT NULL AND (
                    ((aperturas > 0 OR clicks > 0) AND ultimo_contacto < ?)
                 OR ((aperturas = 0 AND clicks = 0) AND ultimo_contacto < ?) )) )
        ORDER BY ciclo ASC, COALESCE(ultimo_contacto, '0') ASC
        LIMIT ?""", (segmento, corte_warm, corte_frio, limite))


def resumen(cx):
    v = cx.valor
    return {
        "total":         v("SELECT COUNT(*) FROM {T}"),
        "suprimidos":    v("SELECT COUNT(*) FROM {T} WHERE suprimido=1"),
        "sin_contactar": v("SELECT COUNT(*) FROM {T} WHERE ciclo=0 AND suprimido=0"),
        "contactados":   v("SELECT COUNT(*) FROM {T} WHERE ciclo>0 AND suprimido=0"),
        "warm":          v("SELECT COUNT(*) FROM {T} WHERE suprimido=0 AND (aperturas>0 OR clicks>0)"),
        "frio":          v("SELECT COUNT(*) FROM {T} WHERE suprimido=0 AND ciclo>0 AND aperturas=0 AND clicks=0"),
        "con_datos":     v("SELECT COUNT(*) FROM {T} WHERE datos IS NOT NULL"),
    }


def por_segmento(cx):
    return cx.filas("""
        SELECT segmento,
               COUNT(*) AS total,
               SUM(CASE WHEN suprimido=1 THEN 1 ELSE 0 END) AS suprimidos,
               SUM(CASE WHEN ciclo=0 AND suprimido=0 THEN 1 ELSE 0 END) AS sin_contactar,
               SUM(CASE WHEN ciclo>0 AND suprimido=0 THEN 1 ELSE 0 END) AS contactados,
               SUM(CASE WHEN suprimido=0 AND (aperturas>0 OR clicks>0) THEN 1 ELSE 0 END) AS warm
        FROM {T} GROUP BY segmento ORDER BY segmento""")


def main():
    cx = conectar(); init(cx)
    r = resumen(cx)
    print(f"base: {cx.donde()}")
    print(f"  leads totales    : {r['total']:>7,}")
    print(f"  sin contactar    : {r['sin_contactar']:>7,}")
    print(f"  ya contactados   : {r['contactados']:>7,}")
    print(f"    - warm (abrio) : {r['warm']:>7,}")
    print(f"    - frio         : {r['frio']:>7,}")
    print(f"  SUPRIMIDOS       : {r['suprimidos']:>7,}  (nunca mas reciben)")
    print(f"  con datos p/subir: {r['con_datos']:>7,}")
    if r["total"]:
        print()
        print(f"  {'segmento':10} {'total':>8} {'sin cont.':>10} {'contact.':>9} {'warm':>7} {'suprim.':>8}")
        for s in por_segmento(cx):
            print(f"  {s['segmento']:10} {s['total']:>8,} {s['sin_contactar']:>10,} "
                  f"{s['contactados']:>9,} {s['warm']:>7,} {s['suprimidos']:>8,}")
    cx.close()


if __name__ == "__main__":
    main()
