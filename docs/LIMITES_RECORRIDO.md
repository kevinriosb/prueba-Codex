# Recorridos con varias páginas y recuperación del avance

El aviso del VPS «Se alcanzó el límite de peticiones del recorrido» identificó
otro problema: las 60 peticiones se compartían entre todas las páginas y sus
recursos. Tres páginas normales podían agotar ese presupuesto antes del formulario.

## Presupuesto

El recorrido público autorizado admite 60 peticiones por página y 240 en total.
La página nueva renueva su contador; el total sigue acumulándose. Los intentos
auxiliares denegados también cuentan. Una página con reintentos no puede eludir
su límite, y muchos cambios de página tampoco eluden el total. La API con permisos
antiguos por dominio conserva su límite total anterior de 60.

El recorrido público admite 4 MiB por respuesta, 8 MiB por página y 24 MiB de
red por sesión, medidos tras descomprimir. Los scripts/estilos reutilizables se
guardan hasta 180 segundos en una caché privada de 8 MiB; no se cachean documentos,
XHR, respuestas privadas ni respuestas con cookies. Una reutilización sigue
contando como petición y bytes de página, sin volver a cargar el total de red.
La API con permisos antiguos conserva sus 6 MiB totales.

Siguen vigentes los ocho pasos, 180 segundos por sesión, 12 orígenes,
DNS público/IP fijada/TLS, aislamiento y denegación de envíos. Las descargas se
cortan durante la lectura al superar el presupuesto; cada GET tiene además un
plazo total de ocho segundos. Las cuotas de IA y documentos no cambian.

## Retomar

El backend conserva cifrada durante 30 minutos la última dirección pública
estable observada del ticket. Se vincula al propietario, ticket, perfil y origen
exacto de la autorización. Solo conserva parámetros públicos admitidos de marca
o idioma; no guarda referencias de compra, sesiones, tokens ni cookies.

Si la revisión agota peticiones o bytes de página/recorrido, Telegram ofrece
**Retomar desde esta página**.
Ese botón solicita otra ejecución de IA, sujeta a la cuota diaria. Solo la primera
observación de ese trabajo puede cerrar la sesión agotada y abrir la dirección
guardada. El modelo no puede reiniciar el presupuesto dentro del mismo trabajo.
Un navegador reiniciado o una revisión vencida también pueden recuperar esa
dirección, si el permiso del mismo ticket sigue vigente.

Si venció el permiso, se vuelve a pedir. Si cambió el ticket/origen o venció el
avance, se usa la entrada original y se descarta la marca de la sesión anterior.
El avance no sustituye la liga impresa ni enseña un portal a otros tickets. Guardar
un portal para futuros tickets sigue requiriendo **Guardar portal**.

## Validación

La revisión posterior de recursos, cookies, DOM, transporte TLS y diagnóstico
está documentada en [auditoría de navegación](AUDITORIA_NAVEGACION.md). La
verificación siguiente corresponde a la entrega anterior del presupuesto de
peticiones; no demuestra la navegación de producción de esta entrega.

Las pruebas usan Chromium y el SDK reales con sitios/modelo simulados. Incluyen:
tres páginas con 81 GET, siete páginas que exceden el total, una página con más
de 60 intentos, recuperación desde otra dirección observada, revisión vencida,
aislamiento, caducidad, cambio de origen y cuotas de IA. No usan OpenAI, Gmail o
SAT reales. La llegada al formulario del comercio real se debe verificar en el VPS.

Verificación local: suite completa de 575 pruebas, nueve casos focalizados de
recuperación tras el ajuste final, Ruff y compilación. Las dos imágenes finales
pasaron su arranque y el stack Compose confirmó salud, autenticación interna y
rechazo de archivos locales. Se retiraron sus contenedores y volúmenes de prueba.

## Actualizar desde Termius

```bash
(
  set -e
  cd /opt/lilyapp-agent
  git pull --ff-only
  docker compose -f compose.hostinger.yaml -f compose.browser.yaml build agent browser
  docker compose -f compose.hostinger.yaml -f compose.browser.yaml up -d --wait --wait-timeout 180
  docker compose -f compose.hostinger.yaml -f compose.browser.yaml exec -T browser python -c "from backend.browser_worker.policy import PAGE_REQUEST_LIMIT, ROUTE_REQUEST_LIMIT; print('Peticiones por página:', PAGE_REQUEST_LIMIT, '| Por recorrido:', ROUTE_REQUEST_LIMIT)"
  curl --fail --silent --show-error --max-time 20 https://agente.lilyapp.mx/health
)
```

La comprobación debe mostrar `60` por página, `240` por recorrido y salud `ok`.
En Telegram: `/estado` y **Revisar portal con IA**. Si se ofrece **Retomar desde
esta página**, usar ese botón. No es necesario volver a subir CSF ni ticket.
