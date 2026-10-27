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
- El chat general conserva su historial, estado y confirmaciones. El contexto
  técnico no ocupa un lugar en la lista de chats del usuario y se reutiliza para
  mantener acotado su almacenamiento.
- La navegación del modelo solo acepta referencias observadas en esa ejecución.
  Si recibe una referencia anterior, lee de nuevo la página y devuelve los
  controles actuales, sin ejecutar el clic anterior.
- Un botón nativo desactualizado o una revisión vencida se refresca sin llamar a
  IA. Si venció también el permiso, vuelve a mostrar la autorización necesaria.
- Si el navegador rechaza un clic por una página o sesión desactualizada, se hace
  una lectura nueva, sin repetir la acción. Sigue permitiéndose un solo intento
  de navegación por ejecución.
- Se conservan los límites diarios de ejecuciones/tokens, tiempos de espera,
  dominios autorizados y límites del navegador. No se reintentan envíos ni
  acciones privadas. Un límite diario real sigue mostrándose.
- Si una página supera el presupuesto de contexto, se pausa su revisión y se
  muestran los controles actuales; no se atribuye ese límite al chat general.

La base agrega automáticamente la columna `purpose` a las conversaciones; las
existentes quedan como `chat`. La migración conserva sus mensajes y estados.
No cambia la clave de cifrado, perfiles, documentos ni tickets. Solo se limpia
el historial técnico del contexto público antes de otra revisión.

## Comprobación

Se incorporaron 15 casos de regresión con el SDK real y servicios simulados: chat general
lleno/bloqueado/largo o con autorización pendiente; referencia antigua del modelo;
botón nativo antiguo; cuota diaria agotada; herramientas públicas restringidas;
un único intento de navegación; caducidad de página y permiso; migración de una
base existente, recuperación tras rechazo de una referencia por el navegador y
pausa de una página extensa sin pedir `/nuevo`.

Las pruebas no usan OpenAI, Gmail ni SAT reales. La comprobación del recorrido
real en el VPS queda pendiente de aplicar la actualización y probar en Telegram.

## Actualizar desde Termius

Este cambio necesita recrear solamente `agent`; conserva el navegador que ya
está funcionando y sus variables/volúmenes existentes.

```bash
(
  set -e
  cd /opt/lilyapp-agent
  git pull --ff-only
  docker compose -f compose.hostinger.yaml -f compose.browser.yaml build agent
  docker compose -f compose.hostinger.yaml -f compose.browser.yaml up -d --no-deps --wait --wait-timeout 180 agent
  curl --fail --silent --show-error --max-time 20 https://agente.lilyapp.mx/health
)
```

Después, en Telegram: `/estado` y **Permitir** o **Revisar portal con IA**, según
el botón que aparezca. No hace falta volver a subir la CSF ni el ticket.
