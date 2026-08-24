#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ciclo de vida del cold — que a NINGUN lead se le deje de escribir nunca,
salvo que se desuscriba.

    python ciclo.py --migrar     # UNA sola vez: carga las listas y los ledgers viejos
    python ciclo.py --dry-run    # muestra exactamente que haria, sin tocar nada
    python ciclo.py              # corrida diaria real

LA IDEA (2026-08-18, pedido de Juanma)
El plan de Instantly topea los leads ALMACENADOS (25.000), no los contactados
historicamente. Entonces en vez de pagar el plan de 100k (~$358/mes) rotamos:
se sube un lead, recibe su secuencia, se lo borra (libera cupo) y mas adelante
vuelve a entrar. Nadie se pierde y el cupo nunca se llena.

EL DESPERDICIO QUE ARREGLA
Medido el 2026-08-18: habia 24.450 leads cold cargados y solo 4.131 contactados.
O sea ~20.300 leads ocupando cupo **haciendo cola sin recibir nada**, mientras el
plan marcaba 99% lleno y no dejaba subir a nadie mas. Un lead solo necesita estar
cargado los ~7 dias que dura su secuencia; el resto del tiempo vive en la base
local (`estado.py`). A 1.000 emails/dia alcanza con ~2.300 leads cargados a la vez.

ORDEN DE CADA CORRIDA
  1. Lee el estado real de las campanas en Instantly.
  2. Vuelca aperturas/clicks/respuestas a la base local — SIEMPRE antes de borrar,
     porque al borrar el lead ese historial se pierde para siempre.
  3. Suprime permanente a rebotados y desuscriptos (ver la regla sagrada en estado.py).
  4. Borra de Instantly: los que terminaron la secuencia + el exceso que hace cola.
  5. Repone hasta el objetivo, respetando la cadencia de cada segmento y sin
     invadir nunca la reserva de cupo de los flows hot.

NUNCA se borra a alguien que respondio: esos los atiende una persona.
"""
import argparse, csv, json, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from math import ceil
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import estado
from cupo import cupo_disponible_para_cold, RESERVA_HOT
from goteo import api, payload_lead, LISTAS, CAMPANAS, BATCH

# Forma de la secuencia (paso 1 -> +3d -> +4d). Sirve para calcular cuantos leads
# hacen falta cargados para sostener el ritmo diario sin desperdiciar cupo.
PASOS = 3
DIAS_SECUENCIA = 7
MARGEN = 2.0          # colchon: los delays reales no son exactos

SEGMENTOS = ["p1eu", "p2euuk", "p2eude"]

# status de Instantly
ACTIVO, TERMINADO, REBOTE, UNSUB = 1, 3, -1, -2

HILOS = 8             # el borrado es de a uno (no hay bulk en la API v2)
MAX_BORRAR = 6000     # tope por corrida, para que una corrida no se eternice
LOTE_DB = 1000        # filas por INSERT en lote (Postgres remoto: de a una no termina)


# Filtros del lado del servidor (verificados 2026-08-18). Sin esto habria que leer
# las ~245 paginas de cada campana en cada corrida (el `limit` topea en 100, probado):
# eran ~5 minutos de listado antes de tocar un solo lead. Con filtro se leen decenas.
F_CONTACTADOS   = "FILTER_VAL_CONTACTED"
F_SIN_CONTACTAR = "FILTER_VAL_NOT_CONTACTED"
F_REBOTADOS     = "FILTER_VAL_BOUNCED"
F_UNSUB         = "FILTER_VAL_UNSUBSCRIBED"


def objetivo_en_vuelo(cid):
    """Cuantos leads conviene tener cargados en esta campana."""
    c = api("GET", f"https://api.instantly.ai/api/v2/campaigns/{cid}")
    limite = c.get("daily_limit") or 0
    return ceil(limite / PASOS * DIAS_SECUENCIA * MARGEN), limite


def listar_filtro(cid, filtro=None, tope=None):
    """Lista leads de una campana aplicando el filtro del servidor."""
    out, cursor = [], None
    while True:
        body = {"campaign": cid, "limit": 100}
        if filtro:
            body["filter"] = filtro
        if cursor:
            body["starting_after"] = cursor
        d = api("POST", "https://api.instantly.ai/api/v2/leads/list", body)
        items = d.get("items", [])
        out += items
        cursor = d.get("next_starting_after")
        if not cursor or not items or (tope and len(out) >= tope):
            return out[:tope] if tope else out


def cargados_en_campana(cid):
    """Total de leads almacenados en la campana, segun analytics."""
    for c in api("GET", "https://api.instantly.ai/api/v2/campaigns/analytics") or []:
        if c.get("campaign_id") == cid:
            return c.get("leads_count", 0)
    return 0


def borrar_muchos(leads, dry):
    """Borra en paralelo. Devuelve cuantos se borraron de verdad."""
    if dry or not leads:
        return 0
    def uno(l):
        d = api("DELETE", f"https://api.instantly.ai/api/v2/leads/{l['id']}")
        return 1 if d.get("id") == l["id"] else 0
    ok = 0
    with ThreadPoolExecutor(max_workers=HILOS) as ex:
        for r in ex.map(uno, leads):
            ok += r
    return ok


def filas_csv(seg):
    """Datos completos del lead desde el CSV. Solo se usa en la migracion inicial:
    en la corrida diaria los datos salen de la base (columna `datos`), porque el
    workflow de GitHub Actions no tiene los CSV — son PII y estan gitignored."""
    ruta = HERE / "listas" / LISTAS[seg]
    with open(ruta, newline="", encoding="utf-8") as f:
        return {r["email"].strip().lower(): r for r in csv.DictReader(f)}


def datos_de(cand):
    """Reconstruye la fila del lead guardada en la base."""
    d = cand.get("datos")
    if not d:
        return None
    fila = json.loads(d) if isinstance(d, str) else d
    fila.setdefault("email", cand["email"])
    return fila


# ─────────────────────────────── migracion ───────────────────────────────

def migrar(cx, dry):
    """Carga en la base las listas completas y los ledgers de lo ya enviado.

    Los `subidos-<seg>.txt` son append-only y registran a quien YA se le escribio.
    Se importan como ciclo 1 para que la cadencia arranque bien y no se le
    reescriba manana a alguien que recibio ayer.
    """
    print("MIGRACION — cargando listas y ledgers en la base local\n")
    for seg in SEGMENTOS:
        filas = filas_csv(seg)
        ledger_p = HERE / "listas" / f"subidos-{seg}.txt"
        ledger = set()
        if ledger_p.exists():
            ledger = {l.strip().lower() for l in ledger_p.read_text(encoding="utf-8").splitlines() if l.strip()}
        nuevos = ya = 0
        # IDEMPOTENTE: correr --migrar dos veces no debe volver a sumar ciclo ni
        # resetear la fecha de contacto (eso retrasaria la cadencia de todos).
        yacontactados = {f["email"] for f in cx.filas(
            "SELECT email FROM {T} WHERE segmento=? AND ciclo>0", (seg,))}
        refrescar, marcar = [], []
        for email, fila in filas.items():
            if email in ledger:
                (refrescar if email in yacontactados else marcar).append((email, fila))
                ya += 1
            else:
                refrescar.append((email, fila))
                nuevos += 1
        # emails del ledger que ya no estan en el CSV (listas viejas): igual se registran
        huerfanos = 0
        for email in ledger - set(filas):
            (refrescar if email in yacontactados else marcar).append((email, None))
            huerfanos += 1
        if not dry:
            # en lotes: de a uno contra Postgres remoto la migracion no termina nunca
            for i in range(0, len(refrescar), LOTE_DB):
                estado.altas_lote(cx, refrescar[i:i + LOTE_DB], seg)
            for i in range(0, len(marcar), LOTE_DB):
                estado.contactados_lote(cx, marcar[i:i + LOTE_DB], seg)
            cx.commit()
        print(f"  {seg:8} lista={len(filas):6,}  ya contactados={ya:6,}  sin contactar={nuevos:6,}  "
              f"fuera de lista={huerfanos:5,}")
    print()


# ──────────────────────────────── corrida ────────────────────────────────

# ─────────────────────────── escalado automatico ────────────────────────────────
# POR QUE ESTA ACA (2026-08-24): se acordo un plan de escalones (400 -> 600 -> 800
# -> 1.000/dia) y quedo clavado en 400 seis dias, porque dependia de que alguien se
# acordara de subirlo a mano. Ahora lo hace el ciclo, todos los dias, y solo si los
# numeros lo permiten. Un escalon por dia: los saltos grandes de golpe son lo que
# quema dominios, no el volumen alto en si.
OBJETIVO_POR_CASILLA = 50     # techo al que queremos llegar
PASO_POR_CASILLA     = 5      # cuanto sube por dia como maximo
APERTURA_MINIMA      = 30.0   # % — por debajo, no sube (estamos cayendo en spam)
REBOTES_MAXIMO       = 2.0    # % — por encima, no sube
CONTACTADOS_MINIMOS  = 300    # con menos datos que esto, las tasas no son confiables


def salud_del_canal():
    """(apertura %, rebotes %, personas contactadas) acumulado de las cold activas."""
    d = api("GET", "https://api.instantly.ai/api/v2/campaigns/analytics") or []
    cont = op = reb = 0
    for c in d:
        if not str(c.get("campaign_name", "")).startswith("[COLD]") or c.get("campaign_status") != 1:
            continue
        cont += c.get("contacted_count", 0) or 0
        op   += c.get("open_count_unique", 0) or 0
        reb  += c.get("bounced_count", 0) or 0
    if not cont:
        return 0.0, 0.0, 0
    return 100.0 * op / cont, 100.0 * reb / cont, cont


def escalar_volumen(dry):
    """Sube un escalon el limite diario por casilla, si la salud lo permite."""
    cuentas = [a for a in listar_casillas_cold() if a.get("status") == 1]
    if not cuentas:
        print("escalado: no hay casillas sanas.")
        return
    actual = min(a.get("daily_limit") or 0 for a in cuentas)
    if actual >= OBJETIVO_POR_CASILLA:
        print(f"escalado: ya en el objetivo ({actual}/casilla × {len(cuentas)} = "
              f"{actual * len(cuentas)}/dia). Sin cambios.")
        return
    ap, reb, cont = salud_del_canal()
    if cont < CONTACTADOS_MINIMOS:
        print(f"escalado: solo {cont} contactados, muy poca data para decidir. No sube.")
        return
    if ap < APERTURA_MINIMA or reb > REBOTES_MAXIMO:
        print(f"escalado: FRENADO — apertura {ap:.1f}% (min {APERTURA_MINIMA}) | "
              f"rebotes {reb:.2f}% (max {REBOTES_MAXIMO}). Se sostiene en {actual}/casilla.")
        return
    nuevo = min(actual + PASO_POR_CASILLA, OBJETIVO_POR_CASILLA)
    print(f"escalado: salud OK (apertura {ap:.1f}%, rebotes {reb:.2f}%) -> "
          f"{actual} a {nuevo} por casilla = {nuevo * len(cuentas)}/dia")
    if dry:
        return
    for a in cuentas:
        api("PATCH", f"https://api.instantly.ai/api/v2/accounts/{a['email']}", {"daily_limit": nuevo})
    # los topes de campana se reparten proporcional a como estan hoy
    topes = {}
    for seg in SEGMENTOS:
        c = api("GET", f"https://api.instantly.ai/api/v2/campaigns/{CAMPANAS[seg]}")
        topes[seg] = c.get("daily_limit") or 0
    suma = sum(topes.values()) or 1
    objetivo_total = nuevo * len(cuentas)
    for seg, v in topes.items():
        nv = max(1, round(objetivo_total * v / suma))
        api("PATCH", f"https://api.instantly.ai/api/v2/campaigns/{CAMPANAS[seg]}", {"daily_limit": nv})
        print(f"    {seg}: {v} -> {nv}/dia")


def listar_casillas_cold():
    """Las casillas que usan las campanas cold (las lee de la campana, no hardcodeadas)."""
    emails, out = set(), []
    for seg in SEGMENTOS:
        c = api("GET", f"https://api.instantly.ai/api/v2/campaigns/{CAMPANAS[seg]}")
        emails |= set(c.get("email_list") or [])
    for e in sorted(emails):
        a = api("GET", f"https://api.instantly.ai/api/v2/accounts/{e}")
        if a.get("email"):
            out.append(a)
    return out


def dkim_publicado(dominio):
    """True si el dominio tiene el selector1 de DKIM resolviendo en DNS publico."""
    try:
        out = subprocess.run(
            ["curl", "-s", "--max-time", "15", "-H", "accept: application/dns-json",
             f"https://cloudflare-dns.com/dns-query?name=selector1._domainkey.{dominio}&type=CNAME"],
            capture_output=True, text=True, encoding="utf-8").stdout
        return bool(json.loads(out or "{}").get("Answer"))
    except Exception:
        return True    # ante la duda no alarmamos: el chequeo no debe frenar el ciclo


def alertar_dkim_faltante():
    """Grita si alguna casilla EN CAMPANA manda desde un dominio sin DKIM.

    Sin DKIM, Gmail y Outlook desconfian del remitente y los emails caen en spam:
    una casilla asi quema su dominio en dias. Paso dos veces (tandas 2 y 3): se
    configura MX, SPF y DMARC, y el DKIM queda afuera porque Microsoft no lo genera
    solo — hay que crear la config en Exchange Y publicar los CNAME que devuelve.
    Crear la config sin publicar los CNAME NO firma nada, aunque el panel liste el
    dominio como si estuviera. Por eso el chequeo es contra el DNS, no contra el panel.
    """
    dominios = set()
    for seg in SEGMENTOS:
        c = api("GET", f"https://api.instantly.ai/api/v2/campaigns/{CAMPANAS[seg]}")
        for e in (c.get("email_list") or []):
            if "@" in e:
                dominios.add(e.split("@")[1].lower())
    faltan = [d for d in sorted(dominios) if not dkim_publicado(d)]
    if faltan:
        print("*** ALERTA DKIM: estos dominios estan EN CAMPANA y no firman ***")
        for d in faltan:
            print(f"      {d}")
        print("      Sus emails van a spam. Arreglar con:")
        print("      pwsh -File preparar-dkim.ps1 && python publicar-dkim.py && pwsh -File habilitar-dkim.ps1")
    else:
        print(f"DKIM: los {len(dominios)} dominios en campana firman OK.")


def correr(cx, dry):
    alertar_dkim_faltante()       # que no vuelva a pasar lo de las tandas 2 y 3
    escalar_volumen(dry)          # primero el escalon del dia, despues la rotacion
    total_borrados = total_subidos = 0
    # El presupuesto se reparte por segmento: si no, el primero se lo come entero y
    # los demas no drenan nunca. Los terminados/rebotes/unsub NO gastan presupuesto:
    # son basura que ocupa cupo y no va a recibir nada, se borran siempre.
    presupuesto = MAX_BORRAR // len(SEGMENTOS)

    for seg in SEGMENTOS:
        cid = CAMPANAS[seg]
        objetivo, limite = objetivo_en_vuelo(cid)
        cargados = cargados_en_campana(cid)

        # Solo se leen los CONTACTADOS (miles): son los unicos con historial que
        # rescatar. La cola se lee aparte y nada mas que lo que se va a borrar.
        contactados = listar_filtro(cid, F_CONTACTADOS)
        rebotes = listar_filtro(cid, F_REBOTADOS)
        unsub   = listar_filtro(cid, F_UNSUB)
        aparte  = {l["id"] for l in rebotes} | {l["id"] for l in unsub}

        respondieron = [l for l in contactados if l.get("email_reply_count") and l["id"] not in aparte]
        vivos        = [l for l in contactados if not l.get("email_reply_count") and l["id"] not in aparte]
        terminados   = [l for l in vivos if l.get("status") == TERMINADO]
        en_vuelo     = [l for l in vivos if l.get("status") == ACTIVO]
        en_cola_n    = max(0, cargados - len(contactados) - len(rebotes) - len(unsub))

        print(f"\n=== {seg}  (campana manda {limite}/dia -> conviene tener ~{objetivo} cargados) ===")
        print(f"  en Instantly: {cargados:6,}  | en vuelo {len(en_vuelo):5,} "
              f"| en cola {en_cola_n:6,} | terminados {len(terminados):5,} "
              f"| rebotes {len(rebotes)} | unsub {len(unsub)} | respondieron {len(respondieron)}")

        # 1. Volcar actividad a la base ANTES de borrar nada: al borrar el lead en
        #    Instantly, aperturas y clicks se pierden para siempre.
        if not dry:
            for l in contactados + rebotes + unsub:
                estado.guardar_actividad(cx, l["email"], seg, l.get("email_open_count"),
                                         l.get("email_click_count"), l.get("email_reply_count"),
                                         l.get("timestamp_last_contact"))
            cx.commit()

        # 2. Supresiones permanentes.
        for l in rebotes:
            if not dry: estado.suprimir(cx, l["email"], estado.REBOTE, seg)
        for l in unsub:
            if not dry: estado.suprimir(cx, l["email"], estado.UNSUB, seg)
        for l in respondieron:
            if not dry: estado.suprimir(cx, l["email"], estado.RESPUESTA, seg)
        if not dry: cx.commit()
        supr = len(rebotes) + len(unsub) + len(respondieron)
        if supr:
            print(f"  suprimidos permanentes: {supr}  (rebote/unsub/respondio — no vuelven a entrar)")

        # 3. Los terminados ya cumplieron su ciclo: sellar fecha y sacarlos.
        for l in terminados:
            if not dry: estado.marcar_contactado(cx, l["email"], seg)
        if not dry: cx.commit()

        # 4. Borrar: terminados + rebotes + unsub (siempre) + el exceso que hace cola
        #    (con presupuesto). El exceso vuelve a la base como candidato: no se
        #    pierde nadie, solo deja de ocupar cupo mientras espera su turno.
        basura = terminados + rebotes + unsub
        sobra_cola = max(0, en_cola_n - max(0, objetivo - len(en_vuelo)))
        de_cola = listar_filtro(cid, F_SIN_CONTACTAR, tope=min(sobra_cola, presupuesto)) \
                  if min(sobra_cola, presupuesto) > 0 else []
        a_borrar = basura + de_cola
        if a_borrar:
            hechos = borrar_muchos(a_borrar, dry)
            total_borrados += hechos
            print(f"  borrar de Instantly: {len(a_borrar):5,} "
                  f"({len(terminados)} terminados + {len(rebotes) + len(unsub)} rebote/unsub "
                  f"+ {len(de_cola):,} de la cola, quedan {sobra_cola - len(de_cola):,} por drenar) "
                  f"-> {'[dry-run]' if dry else f'{hechos} borrados'}")

        # 5. Reponer hasta el objetivo.
        # lo que queda cargado tras el borrado real (no el ideal): si el presupuesto
        # corto el drenaje, la cola sigue alta y no hay que reponer nada.
        faltan = max(0, objetivo - len(en_vuelo) - (en_cola_n - len(de_cola)))
        if faltan <= 0:
            print("  reponer: nada, la campana ya tiene su cuota cargada.")
            continue
        disponible = cupo_disponible_para_cold()
        cupo_real = min(faltan, max(0, disponible))
        if cupo_real <= 0:
            print(f"  reponer: SIN CUPO (reserva hot={RESERVA_HOT} intocable). "
                  f"El borrado de arriba libera lugar para la proxima corrida.")
            continue
        cands = estado.candidatos(cx, seg, cupo_real)
        if not cands:
            print(f"  reponer: no hay candidatos que cumplan la cadencia todavia "
                  f"(warm {estado.DIAS_WARM}d / frio {estado.DIAS_FRIO}d).")
            continue
        bloqueados = estado.suprimidos(cx)          # cinturon Y tirantes
        tanda = [f for f in (datos_de(c) for c in cands
                             if c["email"] not in bloqueados) if f]
        sin_datos = len(cands) - len(tanda)
        if sin_datos:
            print(f"    ojo: {sin_datos} candidatos sin datos guardados (correr --migrar)")
        print(f"  reponer: {len(tanda)} leads (faltan {faltan}, cupo del plan permite {cupo_real})")
        if dry or not tanda:
            continue
        subidos = []
        for i in range(0, len(tanda), BATCH):
            lote = tanda[i:i + BATCH]
            d = api("POST", "https://api.instantly.ai/api/v2/leads/add",
                    {"campaign_id": cid, "skip_if_in_campaign": True,
                     "leads": [payload_lead(r, seg) for r in lote]})
            if d.get("error") or d.get("statusCode", 200) >= 400:
                print(f"    corte: {json.dumps(d)[:140]}")
                break
            subidos += [r["email"].strip().lower() for r in lote]
            time.sleep(0.4)
        for e in subidos:
            estado.marcar_contactado(cx, e, seg)
        cx.commit()
        total_subidos += len(subidos)
        print(f"    subidos: {len(subidos)}")

    print(f"\n{'[DRY-RUN] ' if dry else ''}TOTAL — borrados de Instantly: {total_borrados:,} | subidos: {total_subidos:,}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="no toca nada, solo muestra")
    ap.add_argument("--migrar", action="store_true", help="carga inicial de listas y ledgers")
    args = ap.parse_args()

    cx = estado.conectar(); estado.init(cx)
    if args.migrar:
        migrar(cx, args.dry_run)
    else:
        correr(cx, args.dry_run)
    print()
    r = estado.resumen(cx)
    print(f"BASE LOCAL: {r['total']:,} leads | sin contactar {r['sin_contactar']:,} | "
          f"contactados {r['contactados']:,} (warm {r['warm']:,}) | suprimidos {r['suprimidos']:,}")
    cx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
