# Sanity check: portal, foto y preparación del formulario

## Evidencia del VPS

La salida compartida por el propietario actualiza `65f549c` a `3323ade`, deja ambos
contenedores saludables y termina con «Resultado: formulario observado». El primer
paso identifica once marcas; el segundo abre el formulario correcto y observa RFC,
Ticket, Tienda y Fecha. No hay bloqueo, CAPTCHA, login ni errores de recursos.
La prueba pública no analiza la foto, consulta la compra ni solicita una factura.

Ese resultado permite conservar la navegación comprobada y revisar la siguiente
parte del flujo. Se ejecutó primero la suite original: 689 pruebas pasadas. También
se verificaron las versiones instaladas frente a las fijadas, sintaxis de Python y
JavaScript y errores de nombres de Python.

## Debug por etapas

| Etapa | Comprobación | Resultado y corrección |
|---|---|---|
| Acceso y datos propios | Login, CSRF, invitaciones, propietario y perfil persistente | Se conservan los mecanismos existentes y sus regresiones. |
| Portal y marca | Diagnóstico real del VPS; rutas públicas genéricas en Chromium | El diagnóstico llegó al formulario. El motor del navegador y sus límites se conservan. |
| Campos solicitados | Cuenta, Referencia, Código, Web ID, Tienda y fechas con formatos diferentes | El identificador se extrae por su concepto; la fecha respeta el orden y separador explícitos del control. |
| Alternativas del portal | Web ID ausente con la opción pública «No tengo Web ID» | El flujo anterior se detenía en el primer formulario. Ahora sigue una alternativa inequívoca observada y vuelve a leer sus campos. |
| Datos ya disponibles | La alternativa pide folio y fecha ya leídos | Reutiliza los valores; no cobra otra lectura por conocerlos. |
| Ambigüedad | Dos alternativas públicas compatibles | Presenta los controles en Telegram; no elige por ensayo. Una selección humana usa las mismas comprobaciones que una automática. |
| Foto reenviada | Botón de un mensaje anterior, con todos los campos ya guardados | Antes podía proponer llenar con valores de la foto anterior. Ahora verifica el nuevo adjunto antes del llenado. Bytes idénticos reutilizan la lectura comprobada. |
| Correcciones simultáneas | Una corrección humana mientras se abre la página, incluso con fallo de navegación | Al guardar la observación se recupera el ticket actual; la copia anterior no reemplaza los datos nuevos. |
| Rechazo del llenado | El control rechaza una opción antes de escribir | Se distingue un rechazo comprobado de una respuesta perdida; no se anuncia una operación incierta. |
| Respuesta perdida | El llenado ocurre, pero no llega su respuesta | Consulta el estado sin repetir el llenado y actualiza tanto la revisión como el ticket. |
| Caducidad | Vence la sesión o la revisión del llenado | Conserva un estado propio recuperable; un botón anterior no termina simplemente en «registro no encontrado». |

Siete regresiones fallaron antes de corregir los casos de foto, rechazo, respuesta
perdida, formatos de fecha y alternativa. Otras dos demostraron los fallos de
caducidad. Se añadieron controles de compatibilidad para los permisos anteriores,
reutilización de datos, otros identificadores, elección humana y correcciones.

## Cómo se elige una alternativa

La alternativa procede exclusivamente de los controles públicos observados.
Debe referirse explícitamente a la falta de un dato reconocido del ticket y ser
un enlace público o control de despliegue admitido. La selección queda vinculada al documento,
perfil, alcance y estructura observada. Solo se excluye el identificador anterior
cuando la vista nueva ofrece otros campos de compra. Una estructura distinta
invalida esa selección. Los intentos se acotan y no se repite el mismo despliegue
en la misma sesión por un reintento del mensaje.

No hay direcciones o secuencias por restaurante en esta lógica. Las pruebas usan
varias etiquetas, formatos e identificadores, incluidos un Web ID existente que
mantiene su método original y una Referencia ausente que usa otra alternativa.
Si un control no ofrece una alternativa inequívoca o usa un mecanismo no admitido,
el flujo queda para revisión. No se promete compatibilidad con cualquier sitio.

## Consentimiento, costes y estado

El alcance nuevo `portal-fields-v2` describe lecturas según los campos necesarios;
cada llamada sigue contando contra la cuota existente. No se amplían los permisos
antiguos `portal-fields-v1`, que anunciaban dos lecturas. Una alternativa que pide
otros datos requiere otro botón en ese caso. Las autorizaciones por formulario
están ligadas a sus campos y documento. Una reapertura no repite la lectura del
mismo archivo y los mismos campos.

El llenado sigue siendo una preparación con la red cerrada. No se pulsa buscar,
registrar, siguiente o facturar ni se emite un CFDI. Una sesión vencida o una
respuesta perdida nunca se convierten en una factura completada.

## Alcance del cambio y despliegue

El cambio funcional se limita a cuatro archivos del módulo de facturación del
backend. Se conservan motor Playwright, frontend, contratos HTTP del navegador,
dependencias, imágenes base, Compose, TLS, claves y esquema de base de datos.
Los campos nuevos son metadatos cifrados del ticket/documento; no requieren una
migración ni borrar registros. El perfil no se reinicia por cambiar de chat,
revisión o fotografía.

Para aplicar esta entrega, actualizar y reconstruir únicamente `agent`, usando
los dos Compose. `--no-deps` conserva el navegador ya activo:

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

Después, `/estado` y **Revisar portal con IA**. Los permisos vigentes se reutilizan;
los vencidos se renuevan. Si la foto temporal ya venció y faltan datos, se reenvía
solo el ticket mediante su botón. No se recuperan bytes ya eliminados por caducidad.

Las pruebas de lectura y formularios usan SDK/Chromium reales con respuestas,
documentos y páginas ficticias. Las pruebas web comprueban login, cálculo,
historial, CSF, perfil, revisión y correo simulado, además de controles de iPad.
No son una evaluación de OCR sobre una foto fiscal real ni de todos los portales
de Internet. No se realizaron llamadas de IA de pago ni envíos reales de correo.

## Validación de esta entrega

- Suite completa tras las primeras correcciones: **703 pruebas pasadas** (335,28 s).
- Después de los últimos casos de alternativa por enlace y edición simultánea:
  **79 regresiones pasadas** (99,59 s), incluidas las 21 de esta revisión.
- Pasaron las dos pruebas web con Chromium: conversación y facturación, incluida
  la vista de iPad. El correo es simulado.
- Se construyó la imagen final del agente y pasó la prueba de persistencia
  cifrada, salud y login al recrear su contenedor.
- La repetición adicional de las pruebas Docker de navegador y stack Compose
  quedó bloqueada por `no space left on device` en el entorno de desarrollo,
  antes de ejecutar sus comprobaciones funcionales. No corresponde al VPS ni se
  considera una prueba pasada. Sus recursos temporales se retiraron.

La prueba Compose incluye ahora una comprobación de recrear solo el agente,
conservar el identificador de browser y repetir la autenticación privada. Esa
comprobación adicional queda pendiente de un entorno Docker con espacio.
