# Cerrar la revisión cuando ya existe un formulario

La captura del VPS llegó al formulario de Domino’s con RFC, Ticket, Tienda y
Fecha, pero mostró «Revisión pausada por el límite de ejecución» y ofreció
«Continuar revisión con IA». La navegación había conseguido su objetivo; el
Runner todavía esperaba un turno del modelo para terminar. Un límite en ese
turno podía convertir un resultado útil en un aviso de pausa.

## Resultado de la corrección

Una revisión pública termina cuando el servidor guarda un formulario reconocido
sin bloqueo, CAPTCHA, login ni indicadores de una operación privada. Un callback
del SDK devuelve el resultado final directamente; no solicita un turno de IA para
redactar el cierre. El indicador se crea en la herramienta del servidor y no se
acepta del texto del modelo o de la página. El chat general y sus aprobaciones
conservan el funcionamiento anterior.

Telegram muestra «Formulario encontrado», los datos de los campos observados y
los datos obligatorios que faltan. Retira «Continuar revisión con IA» cuando ya
hay formulario. Un botón de continuar de una vista anterior reutiliza el resultado
vigente y no reserva otra ejecución. Si un límite llega después de guardar el
formulario, muestra ese resultado en lugar del aviso de pausa. Los límites reales
sin formulario utilizable siguen informándose.

Guardar el enlace sigue siendo una elección opcional. Encontrar o guardar un
formulario no valida la compra ni solicita una factura. La captura y el envío en
el portal siguen siendo manuales en esta fase pública.

## Tienda y archivos temporales

`store_number` es un campo opcional de compra, distinto del folio, Cuenta y
nombre de sucursal (`branch`). Se reconoce cuando el formulario pide Tienda,
Store number o un número de sucursal; no se confunde con el nombre o dirección
del establecimiento. La extracción copia un código impreso como texto, conservando
ceros iniciales. Nunca lo deduce de la marca, sucursal, dirección o folio.

Si ese dato falta en un campo observado, Telegram ofrece **Completar Tienda** y
guarda la entrada humana mediante `/dato_ticket`. No se vuelve a analizar el
archivo ni se pide otra CSF. Los datos de compra ya analizados sobreviven al
adjunto temporal; pueden completarse aun cuando hayan vencido tanto la foto como
la revisión del navegador. `/estado` conserva y muestra Tienda una vez completada.

La edición sigue comprobando propietario, dato pendiente y análisis previo. No
permite modificar tickets que ya tienen una solicitud enviada. Los tickets no
analizados no adquieren permiso de edición al vencer su archivo.

## Pruebas y despliegue

Las regresiones cubren el cierre con un único turno del modelo, límites posteriores
al guardado, botones viejos sin nuevas llamadas de IA, bloqueos/CAPTCHA/login que
no se ocultan, Tienda separada de otros datos, ceros iniciales, entrada desde
Telegram con foto y revisión vencidas, propiedad y tickets ya enviados. Usan el
SDK real con modelo simulado; no llaman a OpenAI ni envían solicitudes fiscales.

Validación local: suite completa de 648 pruebas y 17 regresiones específicas
aprobadas, Ruff (F821/F822/F823/E9), compilación y comprobación del diff.
Ambas imágenes se construyeron y pasaron las comprobaciones del contenedor y del
stack Compose: salud, autenticación interna y rechazo de destinos locales. Sus
contenedores y volúmenes ficticios se retiraron al terminar. La captura del VPS
confirma la llegada al formulario real; el cierre corregido requiere desplegar
esta entrega para comprobarse allí.

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

Después: `/estado`, **Revisar portal con IA** si hace falta renovar la observación,
y **Completar Tienda** cuando aparezca. Escribir el número impreso en el ticket;
no usar el folio como sustituto. No es necesario volver a subir CSF ni ticket.
