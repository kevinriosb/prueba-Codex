# Selector de marcas: POST auxiliar y enlaces de pie

> Sesión 3A: el recorrido actual exige confirmar cada nuevo dominio y puede interpretar controles y etiquetas por su significado. Véase [portal correcto](PORTAL_CORRECTO.md). Los conteos y despliegues descritos más abajo son históricos.

La actualización posterior de presupuestos por página y recuperación del avance
está descrita en [límites del recorrido](LIMITES_RECORRIDO.md).

El diagnóstico ejecutado por el propietario en el VPS mostró 27 peticiones,
1.530.670 bytes y tres dominios: no había alcanzado los límites. Se detectaban
los enlaces de las marcas (incluido Domino’s), pero un POST XHR de la página
activaba `network_blocked` e impedía cualquier clic posterior. El enlace genérico
«Facturación Electrónica» del pie también tenía prioridad sobre el selector.

## Corrección general

- En un recorrido público autorizado, un POST auxiliar XHR/fetch/beacon sigue
  denegado antes de salir a la red. No invalida enlaces GET compatibles ya
  observados. Su cuerpo nunca se reenvía y el contador de peticiones lo incluye
  para que una página con reintentos no eluda los límites.
- El navegador identifica la indicación visible de seleccionar una marca. Si hay
  varias opciones y una coincidencia única con el ticket, sigue esa opción antes
  que los enlaces genéricos del pie. Ante una marca ambigua pide una elección;
  no vuelve al inicio ni recorre todas las marcas por ensayo.
- La observación informa de la causa concreta de un bloqueo: método no admitido,
  peticiones, tamaño, dominios, permisos o carga. Telegram deja de mezclar todos
  los casos como «bloqueo o límite».
- Una navegación principal POST sigue detenida. Si Chromium muestra su página de
  error, se conserva la última dirección pública y la causa del bloqueo, sin
  mostrar campos de un formulario inexistente.

No hay una dirección, marca o receta de comercio en estas reglas. No se amplían
los permisos, los límites ni la capacidad de enviar datos o solicitar facturas.
La CSF, los tickets, las claves y sus autorizaciones se conservan.

## Validación

Las regresiones usan Chromium y el SDK reales con páginas y modelo simulados:
selector con marcas, enlace de pie, POST auxiliar denegado y formulario de destino.
Se comprueban varias indicaciones de selección, ambigüedad, una tormenta de
reintentos y el rechazo de un envío principal. No se llama a OpenAI, Gmail o SAT.

La suite completa pasó con **567 pruebas** (170,12 segundos). También pasaron
Ruff, la compilación y las pruebas de arranque de las imágenes finales `agent`
y `browser`, incluida la autenticación interna del stack Compose y el rechazo
de archivos locales. Los contenedores y volúmenes ficticios se retiraron.

La lectura directa de Interfactura desde desarrollo sigue bloqueada por el proxy
de ese entorno. La evidencia de la página real procede del diagnóstico del VPS;
el formulario final requiere comprobarse allí.

## Actualización

```bash
(
  set -e
  cd /opt/lilyapp-agent
  git pull --ff-only
  docker compose -f compose.hostinger.yaml -f compose.browser.yaml build agent browser
  docker compose -f compose.hostinger.yaml -f compose.browser.yaml up -d --wait --wait-timeout 180
  curl --fail --silent --show-error --max-time 20 https://agente.lilyapp.mx/health
)
```

En Telegram: `/estado` y **Revisar portal con IA**. Solo se vuelve a autorizar el
recorrido si su permiso venció. No hay que volver a subir la CSF ni el ticket.
