# Revisión del portal sin bloquearse por el chat

La captura del propietario mostraba una página desactualizada seguida del aviso
de límite o interrupción del chat. Se reprodujeron dos problemas del flujo:
la navegación compartía el historial del chat general y podía reutilizar
referencias antiguas del navegador; además, un chat lleno, bloqueado o con una
confirmación privada pendiente impedía revisar el portal.

## Cambio

- El mismo agente/modelo usa un contexto de revisión pública independiente,
  limpio en cada ejecución y limitado a observar y navegar. No tiene acceso a
  herramientas de perfiles, correo o cambios privados dentro de esa revisión.
- El ticket queda vinculado en el servidor al elegido en Telegram. Un
  identificador incorrecto o distinto que proponga el modelo no sustituye esa
  selección ni provoca una consulta a otro ticket.
- El chat general conserva su historial, estado y confirmaciones. El contexto
  técnico no ocupa un lugar en la lista de chats del usuario y se reutiliza para
  mantener acotado su almacenamiento.
- La navegación del modelo solo acepta referencias observadas en esa ejecución.
  Si recibe una referencia anterior, lee de nuevo la página y devuelve los
  controles actuales, sin ejecutar el clic anterior.
- Un botón nativo desactualizado o una revisión vencida se refresca sin llamar a
  IA. Si venció también el permiso, vuelve a mostrar la autorización necesaria.
- Si el navegador rechaza un clic por una página o sesión desactualizada, se hace
  una lectura nueva, sin repetir la acción. El clic fallido no se repite; el recorrido
  nuevo puede seguir varias opciones inequívocas dentro del límite de pasos.
- Se conservan los límites diarios de ejecuciones/tokens, tiempos de espera,
  dominios autorizados y límites del navegador. No se reintentan envíos ni
  acciones privadas. Un límite diario real sigue mostrándose.
- Si una página supera el presupuesto de contexto, se pausa su revisión y se
  muestran los controles actuales; no se atribuye ese límite al chat general.
- Si una instrucción del modelo es inválida antes de abrir el portal, se muestra
  el fallo actual. No se reutiliza como explicación un error anterior guardado
  en el ticket. Los fallos internos no exponen sus detalles privados.

La base agrega automáticamente la columna `purpose` a las conversaciones; las
existentes quedan como `chat`. La migración conserva sus mensajes y estados.
No cambia la clave de cifrado, perfiles, documentos ni tickets. Solo se limpia
el historial técnico del contexto público antes de otra revisión.

## Comprobación

Se incorporaron casos de regresión con el SDK real y servicios simulados: chat general
lleno/bloqueado/largo o con autorización pendiente; referencia antigua del modelo;
botón nativo antiguo; cuota diaria agotada; herramientas públicas restringidas;
detención al encontrar el formulario; caducidad de página y permiso; migración de una
base existente, recuperación tras rechazo de una referencia por el navegador y
pausa de una página extensa sin pedir `/nuevo`, identificadores incorrectos o de
otro ticket y errores nuevos que no reutilizan un aviso de página antiguo.

Las pruebas no usan OpenAI, Gmail ni SAT reales. La comprobación del recorrido
real en el VPS queda pendiente de aplicar la actualización y probar en Telegram.

La suite completa pasó con **542 pruebas** (154,81 segundos). También pasó el
arranque del stack Docker/Compose, su autenticación interna y el rechazo de
archivos locales, con el agente nuevo y el navegador existente. La comprobación
del código dentro de la imagen devolvió `Ticket vinculado: True`.

## Actualizar desde Termius

La actualización del recorrido público necesita reconstruir `agent` y `browser`.
Conserva las variables, claves, perfil fiscal y volúmenes existentes. Véase
[recorrido público](RECORRIDO_PUBLICO.md).

```bash
(
  set -e
  cd /opt/lilyapp-agent
  git pull --ff-only
  docker compose -f compose.hostinger.yaml -f compose.browser.yaml build agent browser
  docker compose -f compose.hostinger.yaml -f compose.browser.yaml up -d --wait --wait-timeout 180
  docker compose -f compose.hostinger.yaml -f compose.browser.yaml exec -T agent python -c "from pathlib import Path; print('Ticket vinculado:', 'browser_selection' in Path('/app/backend/app/billing/tools.py').read_text())"
  curl --fail --silent --show-error --max-time 20 https://agente.lilyapp.mx/health
)
```

Debe aparecer `Ticket vinculado: True` y `"status":"ok"`.
Después, en Telegram: `/estado` y **Permitir** o **Revisar portal con IA**, según
el botón que aparezca. No hace falta volver a subir la CSF ni el ticket.
