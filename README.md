# uulife-crons

Automatizaciones programadas de **uu.life** que corren en GitHub Actions.

## Por qué este repo existe (y por qué es público)

El repo principal de uu.life es privado, y en repos privados GitHub Actions consume
una cuota mensual de minutos. En **agosto de 2026 esa cuota se agotó** y *todas* las
automatizaciones se detuvieron durante días — incluido el bot que responde a la gente
que contesta los emails. El consumo no venía de acá: uu.life usaba el 12% de la cuota
y otro repo se llevaba el 90%.

**En repos públicos GitHub Actions es gratis e ilimitado.** Así que estos crons viven
acá: no vuelven a frenarse por cuota, y no dependen de que una PC esté encendida.

## Qué corre acá

| workflow | cuándo | qué hace |
|---|---|---|
| `ciclo-cold.yml` | 06:00 UTC diario | Rota los leads cold para que a nadie se le deje de escribir nunca (salvo que se desuscriba), sin pasarse del tope de leads del plan de Instantly. |
| `unibox-bot.yml` | cada 20 min, lun-sáb | Responde a los leads que contestan los cold emails; escala a una persona lo que no sabe contestar. |
| `optimizar-p1.yml` | diario | Prueba variantes de asunto en la campaña P1 y reporta cuál gana. |

## Qué NO vive acá

**Ningún dato de personas.** Los emails, nombres y mensajes de los leads viven en una
base Postgres privada; las credenciales, en los Secrets del repo. Este repo tiene
código y nada más.

El [`.gitignore`](.gitignore) bloquea las categorías peligrosas (listas, `.csv`, `.db`,
escalaciones, `.env`, claves). **Antes de agregar un archivo nuevo, preguntate si
contiene el email, el nombre o el mensaje de alguien.** Si es que sí o que quizás, va
en la base, no en git.

## Secrets que necesita

| secret | para qué |
|---|---|
| `INSTANTLY_API_KEY` | leer y escribir campañas en Instantly |
| `COLD_DATABASE_URL` | Postgres: estado de los leads y escalaciones |
| `OPENAI_API_KEY` | redactar respuestas del unibox y variantes de asunto |

## Correrlo a mano

Desde el repo privado de uu.life, donde están las listas:

```bash
python automatizaciones/cold-emails/ciclo.py --dry-run   # muestra qué haría
python automatizaciones/cold-emails/ciclo.py             # corrida real
```

Sin `DATABASE_URL` usa una base SQLite local; con `DATABASE_URL`, la de la nube.
La explicación completa de cada decisión está en la cabecera de cada script.
